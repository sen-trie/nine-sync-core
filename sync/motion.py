import hashlib
import struct
import types

from ..pmm import OUTSIDE_FREE, REST_EPSILON, bone_track_moves, encode_name, outside_parent_keys


BONE_KEY = struct.Struct("<i3d4d")
MORPH_KEY = struct.Struct("<id")
VISIBILITY_KEY = struct.Struct("<iB")
CAMERA_KEY = struct.Struct("<id3d3diiii")
LIGHT_KEY = struct.Struct("<i3d3d")
VISIBILITY_TRACK = "visibility"
# Name the IK switches go under in a motion record's per-track digests.
IK_TRACK = "IK switches"
BONE_MORPH_TRACK = "Bone morphs"
OUTSIDE_TRACK = "Outside parents"


# Hash of canonical parsed tracks, not raw file bytes: re-saving a PMM or changing an
# unrelated section leaves it alone, and every field that reaches the action is in it.
def digest_tracks(names, tracks, pack_key):
    sha = hashlib.sha256()
    sha.update(struct.pack("<I", len(tracks)))
    for name, keys in zip(names, tracks):
        sha.update(name.encode("utf-8", "replace") + b"\0")
        sha.update(struct.pack("<I", len(keys)))
        for key in keys:
            sha.update(pack_key(key))
    return sha.hexdigest()[:32]


def pack_bone_key(key):
    raw = bytes(key.interpolation or b"")
    return BONE_KEY.pack(key.frame, *key.location, *key.rotation) + raw


def pack_morph_key(key):
    return MORPH_KEY.pack(key.frame, key.weight)


def pack_visibility_key(key):
    return VISIBILITY_KEY.pack(key.frame, 1 if key.visible else 0)


# Every camera field that reaches the rig or the follow constraints.
def pack_camera_key(key):
    head = CAMERA_KEY.pack(
        key.frame,
        key.distance,
        *key.look_at,
        *key.angle,
        key.parent_model,
        key.parent_bone,
        key.perspective,
        key.fov,
    )
    return head + bytes(key.interpolation or b"")


def pack_light_key(key):
    return LIGHT_KEY.pack(key.frame, *key.color, *key.direction)


# One unnamed track, for camera and light.
def keys_fingerprint(keys, pack_key):
    return digest_tracks([""], [keys], pack_key)


# digest_tracks plus per-track hashes, saved with a sync so the next preview can name
# what changed, in one pass that packs each key once. Eight hex digits only have to tell
# one revision of a track from the next.
def fingerprint_and_digests(names, tracks, pack_key):
    whole = hashlib.sha256()
    whole.update(struct.pack("<I", len(tracks)))
    table = {}
    for name, keys in zip(names, tracks):
        packed = b"".join(map(pack_key, keys))
        whole.update(name.encode("utf-8", "replace") + b"\0")
        whole.update(struct.pack("<I", len(keys)))
        whole.update(packed)
        table[name] = hashlib.sha256(packed).hexdigest()[:8]
    return whole.hexdigest()[:32], table


# Per IK constraint index, the (frame, on) points where its switch changes, first key
# included, for the IKs the PMM switches off at least once. A track that keeps every
# IK on is what a rig does anyway, so it yields nothing.
def ik_switches(model):
    found = {}
    for index in range(len(model.constraints)):
        points = []
        for key in model.ik_keys:
            state = key.states[index] if index < len(key.states) else 1
            if not points or points[-1][1] != state:
                points.append((key.frame, state))
        if any(not state for _frame, state in points):
            found[index] = points
    return found


# Hash of the IK switches for the motion fingerprint, or "" when no IK is ever switched
# off, which leaves fingerprints of PMMs without such keys as they were.
def ik_fingerprint(model):
    switches = ik_switches(model)
    if not switches:
        return ""
    names = [model.bone_names[model.constraints[i]] if 0 <= model.constraints[i] < len(model.bone_names) else str(i) for i in sorted(switches)]
    return digest_values(names, [switches[i] for i in sorted(switches)])


# The bones of a model that MMD binds to another model's bone at some frame, with the
# bindings over time. Empty when no slot is ever bound.
def outside_bindings(model):
    found = {}
    for bone in model.outside_parents:
        keys = outside_parent_keys(model, bone)
        if any(parent is not None for _frame, parent in keys):
            found[bone] = keys
    return found


# Hash of those bindings for the motion fingerprint, or "" when there are none, which
# leaves the fingerprints of PMMs without them as they were.
def outside_fingerprint(model):
    found = outside_bindings(model)
    if not found:
        return ""
    return digest_values(
        [(model.bone_names[b] if 0 <= b < len(model.bone_names) else b, found[b]) for b in sorted(found)]
    )


# The indices of the other models a model's bones are bound to at some frame. live, when
# given, names the bones whose binding becomes a live constraint (live(model, bone)); they
# are left out, since a constraint follows its parent whenever the parent syncs.
def outside_parent_models(model, live=None):
    found = set()
    for bone, keys in outside_bindings(model).items():
        if live is not None and live(model, bone):
            continue
        for _frame, parent in keys:
            if parent is not None and parent[0] not in (model.index, OUTSIDE_FREE):
                found.add(parent[0])
    return found


# The bone fingerprint and per-track digests of a model, computed once per plan when memo
# (keyed by model index) is given: a parent model's motion part and the models bound to
# it read the same value. Callers copy the digest table before changing it.
def bone_fingerprint(model, memo=None):
    if memo is not None and model.index in memo:
        return memo[model.index]
    value = fingerprint_and_digests(model.bone_names, model.bones, pack_bone_key)
    if memo is not None:
        memo[model.index] = value
    return value


# Hash of the PMM motion of those other models (bone and IK tracks), "" when there are
# none. Their pose decides where the bound bones are solved, so an edit to a parent
# model's motion has to resync the bound model too.
def outside_parent_motion(model, models, memo=None):
    others = sorted(outside_parent_models(model))
    if not others:
        return ""
    by_index = {m.index: m for m in models}
    parts = []
    for index in others:
        parent = by_index.get(index)
        parts.append((index, None if parent is None else (bone_fingerprint(parent, memo)[0], ik_fingerprint(parent))))
    return digest_values(parts)


# Model indices in sync order: a model after the other models its solved bones are bound
# to, so the solve reads their new motion, otherwise in PMM order. Bindings that become
# live constraints (see live above) add no order, so a prop on a hand that the body in turn
# follows (a two-handed weapon) still syncs before the body, whatever the PMM order; MMD
# plays such a pair the same in either order. A cycle of solved bindings cannot be honoured
# in full: it is broken at its model first in PMM order, which syncs last.
def outside_parent_order(models, live=None):
    present = {m.index for m in models}
    needs = {m.index: sorted(outside_parent_models(m, live) & present) for m in models}
    order = []
    state = {}

    def visit(index):
        if state.get(index):
            return
        state[index] = "open"
        for other in needs[index]:
            visit(other)
        state[index] = "done"
        order.append(index)

    for model in models:
        visit(model.index)
    return order


# A short stable hash of any repr-able values, for settings and rig signatures.
def digest_values(*values):
    return hashlib.sha256(repr(values).encode("utf-8", "replace")).hexdigest()[:24]


# Same rule as report_morphs: a morph counts once a key has a non-zero weight.
def morph_track_moves(keys):
    return any(abs(k.weight) > REST_EPSILON for k in keys)


# A display track only matters once it hides the model; PMM always stores a shown key at
# frame 0, which a Blender model already is.
def visibility_hides(keys):
    return any(not key.visible for key in keys)


# (low, high) over the keys of some tracks, or None when there are none.
def track_range(tracks):
    frames = [key.frame for keys in tracks for key in keys]
    return (min(frames), max(frames)) if frames else None


# Source-side numbers for one model. Nothing here looks at Blender.
def summarize_model(model):
    animated_bones = [k for k in model.bones if bone_track_moves(k)]
    animated_morphs = [k for k in model.morphs if morph_track_moves(k)]
    return types.SimpleNamespace(
        key=None,
        index=model.index,
        name=model.name or model.name_en or "model",
        name_en=model.name_en,
        bone_tracks=len(model.bones),
        animated_bones=len(animated_bones),
        bone_keys=sum(len(k) for k in model.bones),
        bone_range=track_range(model.bones),
        morph_tracks=len(model.morphs),
        animated_morphs=len(animated_morphs),
        morph_keys=sum(len(k) for k in model.morphs),
        morph_range=track_range(model.morphs),
        visibility_keys=len(model.visibility),
        hidden_keys=sum(1 for k in model.visibility if not k.visible),
    )


# The name mmd_tools will look up: a VMD holds 15 bytes of a bone name, so a longer
# PMM name reaches the importer cut.
def vmd_bone_name(name):
    return encode_name(name).rstrip(b"\x00").decode("cp932", "replace")
