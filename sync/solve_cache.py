import hashlib
from collections import OrderedDict

import bpy
import numpy as np

from ..common.animation import iter_fcurves

# Session-only memory of outside-parent solves. A forced resync reimports the same VMD and
# would evaluate every bound frame again; a solve whose inputs hash the same is taken from
# here instead, after a few of its frames are evaluated and agree (see VERIFY_FRAMES).
# Nothing is written to disk or to the .blend, so no model data outlives the session.
SOLVE_VERSION = 1
CACHE_LIMIT = 8
VERIFY_FRAMES = 6
VERIFY_TOLERANCE = 1e-6
_entries = OrderedDict()
stats = {"hits": 0, "misses": 0, "rejected": 0, "stored": 0}


def clear():
    _entries.clear()
    for key in stats:
        stats[key] = 0


def lookup(signature):
    entry = _entries.get(signature)
    if entry is not None:
        _entries.move_to_end(signature)
    return entry


def store(signature, solved):
    _entries[signature] = solved
    _entries.move_to_end(signature)
    stats["stored"] += 1
    while len(_entries) > CACHE_LIMIT:
        _entries.popitem(last=False)


# A plain-value fingerprint of an RNA struct (a constraint, a modifier, a driver
# variable): every property but evaluated output, pointers by ID name. Unknown values
# are kept as their repr, which at worst makes a key miss.
SKIPPED = {"rna_type", "error_location", "error_rotation", "is_valid", "is_override_data", "active", "bones", "bone_collections", "bone_groups", "show_expanded", "is_active"}


def rna_values(struct, depth=0):
    found = []
    for prop in struct.bl_rna.properties:
        name = prop.identifier
        if name in SKIPPED:
            continue
        try:
            value = getattr(struct, name)
        except AttributeError:
            continue
        if prop.type == "POINTER":
            if isinstance(value, bpy.types.ID):
                found.append((name, value.name, type(value).__name__))
            elif value is not None and depth < 2:
                found.append((name, rna_values(value, depth + 1)))
        elif prop.type == "COLLECTION":
            if depth < 2:
                found.append((name, [rna_values(item, depth + 1) for item in value]))
        elif getattr(prop, "is_array", False) or (prop.type in ("FLOAT", "INT", "BOOLEAN") and getattr(prop, "array_length", 0)):
            found.append((name, tuple(value)))
        else:
            found.append((name, value if not hasattr(value, "__iter__") or isinstance(value, str) else repr(value)))
    return found


def curve_bytes(fcurve):
    points = fcurve.keyframe_points
    count = len(points)
    parts = [("%s|%d|%d|%s|%d" % (fcurve.data_path, fcurve.array_index, count, fcurve.extrapolation, len(fcurve.modifiers))).encode("utf-8", "replace")]
    for prop, size, kind in (("co", 2, np.float32), ("handle_left", 2, np.float32), ("handle_right", 2, np.float32), ("interpolation", 1, np.int32), ("handle_left_type", 1, np.int32), ("handle_right_type", 1, np.int32), ("easing", 1, np.int32)):
        buffer = np.empty(size * count, dtype=kind)
        if count:
            points.foreach_get(prop, buffer)
        parts.append(buffer.tobytes())
    for modifier in fcurve.modifiers:
        parts.append(repr(rna_values(modifier)).encode("utf-8", "replace"))
    return b"".join(parts)


def action_bytes(action):
    if action is None:
        return b"-"
    # The name is left out: a resync may bring the same keys back under a ".001" name.
    curves = sorted(iter_fcurves(action), key=lambda c: (c.data_path, c.array_index))
    return b"".join(curve_bytes(c) for c in curves)


def animated_paths(id_data):
    anim = getattr(id_data, "animation_data", None)
    found = set()
    if anim is None:
        return found
    actions = [anim.action] + [s.action for t in anim.nla_tracks for s in t.strips]
    for action in actions:
        if action is not None:
            found |= {(c.data_path, c.array_index) for c in iter_fcurves(action)}
    found |= {(c.data_path, c.array_index) for c in anim.drivers}
    return found


def animation_bytes(id_data, digest):
    anim = getattr(id_data, "animation_data", None)
    if anim is None:
        digest.update(b"no-anim")
        return
    digest.update(repr((anim.action_slot.identifier if anim.action_slot else None, anim.use_nla, anim.use_tweak_mode, anim.action_blend_type, anim.action_extrapolation, anim.action_influence)).encode())
    digest.update(action_bytes(anim.action))
    for track in anim.nla_tracks:
        digest.update(repr((track.mute, track.is_solo)).encode())
        for strip in track.strips:
            values = [v for v in rna_values(strip, 2) if v[0] not in ("name", "action")]
            digest.update(repr(values).encode("utf-8", "replace"))
            digest.update(action_bytes(strip.action))
    for fcurve in anim.drivers:
        digest.update(curve_bytes(fcurve))
        digest.update(repr(rna_values(fcurve.driver, 0)).encode("utf-8", "replace"))


# Values of the transform channels nothing animates: they hold at every frame, and a
# changed one moves the solve.
def still_values(owner, paths, animated):
    found = []
    for path, size in paths:
        value = getattr(owner, path.rsplit(".", 1)[-1])
        base = owner.path_from_id(path.rsplit(".", 1)[-1]) if hasattr(owner, "path_from_id") and not isinstance(owner, bpy.types.ID) else path
        for index in range(size):
            if (base, index) not in animated:
                found.append((base, index, round(float(value[index]), 7)))
    return found


TRANSFORMS = (("location", 3), ("rotation_quaternion", 4), ("rotation_euler", 3), ("rotation_axis_angle", 4), ("scale", 3))


def object_bytes(obj, digest):
    animated = animated_paths(obj)
    digest.update(
        repr(
            (
                obj.name,
                obj.type,
                obj.parent.name if obj.parent else None,
                obj.parent_type,
                obj.parent_bone,
                tuple(tuple(row) for row in obj.matrix_parent_inverse),
                obj.rotation_mode,
                still_values(obj, TRANSFORMS, animated),
                [rna_values(c) for c in obj.constraints],
                [rna_values(m) for m in obj.modifiers],
                sorted((k, repr(obj[k])) for k in obj.keys()),
            )
        ).encode("utf-8", "replace")
    )
    animation_bytes(obj, digest)
    if obj.type == "ARMATURE":
        arm = obj.data
        animation_bytes(arm, digest)
        rest = [
            (
                b.name,
                b.parent.name if b.parent else None,
                tuple(tuple(row) for row in b.matrix_local),
                b.use_connect,
                b.use_inherit_rotation,
                b.inherit_scale,
                b.use_local_location,
                b.use_relative_parent,
            )
            for b in arm.bones
        ]
        digest.update(repr(rest).encode("utf-8", "replace"))
        pose = []
        for pb in obj.pose.bones:
            pose.append(
                (
                    pb.name,
                    pb.rotation_mode,
                    still_values(pb, TRANSFORMS, animated),
                    [rna_values(c) for c in pb.constraints],
                    (pb.lock_ik_x, pb.lock_ik_y, pb.lock_ik_z, pb.ik_stretch, pb.use_ik_limit_x, pb.use_ik_limit_y, pb.use_ik_limit_z, pb.ik_min_x, pb.ik_max_x, pb.ik_min_y, pb.ik_max_y, pb.ik_min_z, pb.ik_max_z),
                    sorted((k, repr(pb[k])) for k in pb.keys()),
                )
            )
        digest.update(repr(pose).encode("utf-8", "replace"))
        digest.update(repr(rna_values(obj.pose, 1)).encode("utf-8", "replace"))
    elif obj.type == "MESH":
        mesh = obj.data
        co = np.empty(3 * len(mesh.vertices), dtype=np.float32)
        mesh.vertices.foreach_get("co", co)
        digest.update(co.tobytes())
        digest.update(repr([g.name for g in obj.vertex_groups]).encode("utf-8", "replace"))
        if mesh.shape_keys is not None:
            animation_bytes(mesh.shape_keys, digest)


# The key of one solve: the algorithm and Blender version, the scene's time settings, the
# jobs and frames, and every object of the closure as it is now (curves, NLA layout,
# drivers, constraints, modifiers, rest poses, unanimated transforms). None when there is
# no complete closure to describe.
def signature(scene, armature, jobs, closure):
    if closure is None:
        return None
    digest = hashlib.blake2b(digest_size=20)
    render = scene.render
    digest.update(repr((SOLVE_VERSION, bpy.app.version, render.fps, render.fps_base, render.frame_map_old, render.frame_map_new, armature.name)).encode())
    for own, parent_armature, parent, frames in jobs:
        digest.update(repr((own.name, parent_armature.name if parent_armature else None, parent.name if parent else None, sorted(frames))).encode("utf-8", "replace"))
    for obj in sorted(closure, key=lambda o: o.name):
        object_bytes(obj, digest)
    return digest.hexdigest()


# The frames a cached solve is checked on: both ends, every place a job starts or stops,
# and a few in between, at most VERIFY_FRAMES of them.
def verify_frames(jobs, frames):
    edges = set()
    for job in jobs:
        ordered = sorted(job[3])
        if ordered:
            edges |= {ordered[0], ordered[-1]}
    picks = sorted(edges | {frames[0], frames[-1]})
    step = max(1, len(frames) // (VERIFY_FRAMES + 1))
    inner = [frames[i] for i in range(step, len(frames) - 1, step)]
    for frame in inner:
        if len(picks) >= VERIFY_FRAMES:
            break
        if frame not in picks:
            picks.append(frame)
    return sorted(picks[: max(VERIFY_FRAMES, 2)])


def agrees(cached, fresh):
    for name, frames in fresh.items():
        known = cached.get(name, {})
        for frame, (location, rotation) in frames.items():
            if frame not in known:
                return False
            old_location, old_rotation = known[frame]
            if max(abs(a - b) for a, b in zip(location, old_location)) > VERIFY_TOLERANCE:
                return False
            # The cached value carries the sign chosen next to its previous frame.
            if min(
                max(abs(a - b) for a, b in zip(rotation, old_rotation)),
                max(abs(a + b) for a, b in zip(rotation, old_rotation)),
            ) > VERIFY_TOLERANCE:
                return False
    return True
