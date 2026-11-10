import hashlib
import struct
import types

from ..pmm import REST_EPSILON, bone_key_moves, bone_track_moves, encode_name


BONE_KEY = struct.Struct("<i3d4d")
MORPH_KEY = struct.Struct("<id")
VISIBILITY_KEY = struct.Struct("<iB")
CAMERA_KEY = struct.Struct("<id3d3diiii")
LIGHT_KEY = struct.Struct("<i3d3d")
VISIBILITY_TRACK = "visibility"
# Name the IK switches go under in a motion record's per-track digests.
IK_TRACK = "IK switches"


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


def motion_fingerprint(model):
    return digest_tracks(model.bone_names, model.bones, pack_bone_key)


def shape_fingerprint(model):
    return digest_tracks(model.morph_names, model.morphs, pack_morph_key)


def visibility_fingerprint(model):
    return digest_tracks([VISIBILITY_TRACK], [model.visibility], pack_visibility_key)


# Per-track hashes, saved with a sync so the next preview can name what changed. Eight
# hex digits only have to tell one revision of a track from the next.
def track_digests(names, tracks, pack_key):
    return fingerprint_and_digests(names, tracks, pack_key)[1]


# digest_tracks and track_digests in one pass, packing each key once. The hashes are
# the same as computing the two apart.
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
