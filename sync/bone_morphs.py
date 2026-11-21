import bisect
import math

from mathutils import Quaternion, Vector

from ..common.animation import create_fcurve, find_fcurve, rewrite_fcurve
from ..common.logger import log
from .leg_ik import keep_continuous, slerp_free
from .motion import digest_values


# MMD's bone morphs (a wing flap, a finger curl) move bones by an offset times the
# morph's weight, on top of the keyed pose. mmd_tools only plays vertex, material and UV
# morphs from a VMD; a bone morph needs an assembled model, so the importer skips it and
# the bones stay at their keys. These functions bake the offsets into the bone keys.


# Morph weights of one PMM track at a frame: linear between keys and held beyond the
# first and last, the way MMD plays them.
class WeightTrack:
    def __init__(self, keys):
        self.keys = sorted(keys, key=lambda k: k.frame)
        self.frames = [k.frame for k in self.keys]

    def at(self, frame):
        keys = self.keys
        index = bisect.bisect_right(self.frames, frame) - 1
        if index < 0:
            return keys[0].weight
        if index + 1 >= len(keys):
            return keys[-1].weight
        prev, nxt = keys[index], keys[index + 1]
        span = nxt.frame - prev.frame
        if span <= 0:
            return nxt.weight
        return prev.weight + (nxt.weight - prev.weight) * ((frame - prev.frame) / span)


# The bone morphs of the target that the PMM animates with a non-zero weight:
# [(offsets, WeightTrack)], where offsets are (bone name, location, rotation) in the
# bone basis mmd_tools stored them in, the same one its VMD import writes keys in.
def animated_bone_morphs(root, model):
    if root is None or not model.morph_names:
        return []
    found = []
    tracks = dict(zip(model.morph_names, model.morphs))
    for morph in root.mmd_root.bone_morphs:
        keys = tracks.get(morph.name)
        if not keys or not any(abs(k.weight) > 1e-6 for k in keys):
            continue
        offsets = [
            (data.bone, Vector(data.location), Quaternion(data.rotation))
            for data in morph.data
            if data.bone
        ]
        if offsets:
            found.append((morph.name, offsets, WeightTrack(keys)))
    return found


# Hash of what the bone morphs add to the motion, so a changed weight or offset re-runs
# the motion part. Empty when there is nothing to bake, which keeps older records valid.
def bone_morph_fingerprint(root, model):
    morphs = animated_bone_morphs(root, model)
    if not morphs:
        return ""
    parts = []
    for name, offsets, track in morphs:
        parts.append(
            (
                name,
                [(b, tuple(round(v, 6) for v in loc), tuple(round(v, 6) for v in rot)) for b, loc, rot in offsets],
                [(k.frame, round(k.weight, 6)) for k in track.keys],
            )
        )
    return digest_values(parts)


def basis_curves(armature, bone, kind, size):
    path = 'pose.bones["%s"].%s' % (bone.name, kind)
    curves = []
    for index in range(size):
        curve = find_fcurve(armature, path, index)
        if curve is None:
            curve = create_fcurve(armature, path, index, bone.name, 0)
        if curve is None:
            return None
        curves.append(curve)
    return curves


# Bakes the animated bone morphs into per-frame location and rotation keys of the bones
# they move, on the armature's active action. Run after the importer and the leg IK, so
# it adds to whatever those left. Returns how many bones were written.
def apply_bone_morphs(armature, root, model):
    morphs = animated_bone_morphs(root, model)
    if not morphs:
        return 0
    per_bone = {}
    for _name, offsets, track in morphs:
        for bone_name, location, rotation in offsets:
            per_bone.setdefault(bone_name, []).append((location, rotation, track))
    morph_frames = [f for _n, _o, track in morphs for f in track.frames]
    written = 0
    for bone_name, effects in per_bone.items():
        bone = armature.pose.bones.get(bone_name)
        if bone is None:
            continue
        rot_curves = basis_curves(armature, bone, "rotation_quaternion", 4)
        loc_curves = basis_curves(armature, bone, "location", 3)
        if rot_curves is None or loc_curves is None:
            log.warning("bone morph: could not reach the curves of '%s'", bone_name)
            continue
        ends = [max(f for f in (p.co[0] for p in c.keyframe_points)) for c in rot_curves + loc_curves if len(c.keyframe_points)]
        first = 0
        last = int(math.ceil(max(ends + morph_frames))) if ends or morph_frames else 0
        frames = list(range(first, last + 1))
        quats = []
        locs = []
        for frame in frames:
            quat = Quaternion([c.evaluate(frame) for c in rot_curves])
            loc = Vector([c.evaluate(frame) for c in loc_curves])
            for location, rotation, track in effects:
                weight = track.at(frame)
                if weight == 0.0:
                    continue
                loc = loc + location * weight
                quat = quat @ slerp_free(Quaternion(), rotation, weight)
            quats.append(quat)
            locs.append(loc)
        quats = keep_continuous(quats)
        for component, curve in enumerate(rot_curves):
            rewrite_fcurve(curve, frames, [q[component] for q in quats], "BREAKDOWN")
        for component, curve in enumerate(loc_curves):
            rewrite_fcurve(curve, frames, [v[component] for v in locs], "BREAKDOWN")
        written += 1
    log.info("bone morphs: %d morph(s) baked into %d bone(s)", len(morphs), written)
    return written


THIN_TOLERANCE = 1e-5
# Longest stretch of dropped rows that is not flat. Each new row re-checks the whole
# stretch, so this caps the cost at a key every few seconds; a flat stretch (a bone held
# still) is dropped whole without the check.
THIN_MAX_RUN = 120


# Indices of the rows to keep so that every dropped row lies on the straight line between
# the kept rows either side of it, within tol on every value. The first and last stay.
def thin_linear(frames, rows, tol=THIN_TOLERANCE):
    if len(frames) <= 2:
        return list(range(len(frames)))
    kept = [0]
    flat = True
    for index in range(1, len(frames) - 1):
        anchor = kept[-1]
        span = frames[index + 1] - frames[anchor]
        same = lambda row: all(abs(a - b) <= tol for a, b in zip(row, rows[anchor]))
        flat = flat and same(rows[index])
        if flat and same(rows[index + 1]):
            continue
        ok = span > 0 and index - anchor < THIN_MAX_RUN
        if ok:
            for between in range(anchor + 1, index + 1):
                t = (frames[between] - frames[anchor]) / span
                low, high, mid = rows[anchor], rows[index + 1], rows[between]
                if any(abs(m - (a + (b - a) * t)) > tol for a, b, m in zip(low, high, mid)):
                    ok = False
                    break
        if not ok:
            kept.append(index)
            flat = True
    kept.append(len(frames) - 1)
    return kept


# The inverse of apply_bone_morphs, for an export: takes the morph offsets back out of
# the bones the bake moved, on the armature's active action (the export's temp action),
# so MMD can add them again from the morph track. MMD plays bone key + morph, which then
# gives back the pose Blender shows, also on frames the user edited. The per-frame keys
# the bake left are thinned where they fall on a straight line. model must carry the
# weights the sync baked with (retimed the same way). Returns how many bones changed.
def unbake_bone_morphs(armature, root, model):
    morphs = animated_bone_morphs(root, model)
    if not morphs:
        return 0
    per_bone = {}
    for _name, offsets, track in morphs:
        for bone_name, location, rotation in offsets:
            per_bone.setdefault(bone_name, []).append((location, rotation, track))
    written = 0
    for bone_name, effects in per_bone.items():
        bone = armature.pose.bones.get(bone_name)
        if bone is None or bone.rotation_mode != "QUATERNION":
            continue
        rot_path = 'pose.bones["%s"].rotation_quaternion' % bone_name
        loc_path = 'pose.bones["%s"].location' % bone_name
        rot_curves = [find_fcurve(armature, rot_path, i) for i in range(4)]
        loc_curves = [find_fcurve(armature, loc_path, i) for i in range(3)]
        if any(c is None for c in rot_curves + loc_curves):
            continue
        frames = sorted({int(round(p.co[0])) for c in rot_curves + loc_curves for p in c.keyframe_points})
        if not frames:
            continue
        rows = []
        quats = []
        for frame in frames:
            quat = Quaternion([c.evaluate(frame) for c in rot_curves])
            loc = Vector([c.evaluate(frame) for c in loc_curves])
            added = Quaternion()
            for location, rotation, track in effects:
                weight = track.at(frame)
                if weight == 0.0:
                    continue
                loc = loc - location * weight
                added = added @ slerp_free(Quaternion(), rotation, weight)
            quats.append(quat @ added.inverted())
            rows.append(loc)
        quats = keep_continuous(quats)
        rows = [tuple(q) + tuple(v) for q, v in zip(quats, rows)]
        kept = thin_linear(frames, rows)
        kept_frames = [frames[i] for i in kept]
        for component, curve in enumerate(rot_curves):
            rewrite_fcurve(curve, kept_frames, [rows[i][component] for i in kept])
        for component, curve in enumerate(loc_curves):
            rewrite_fcurve(curve, kept_frames, [rows[i][4 + component] for i in kept])
        written += 1
    log.info("export: bone morphs taken out of %d bone(s)", written)
    return written
