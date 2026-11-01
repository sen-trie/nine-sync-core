import hashlib
import struct
import types

from ..pmm import encode_name


REST_EPSILON = 1e-6
BONE_KEY = struct.Struct("<i3d4d")
MORPH_KEY = struct.Struct("<id")


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


def motion_fingerprint(model):
    return digest_tracks(model.bone_names, model.bones, pack_bone_key)


def shape_fingerprint(model):
    return digest_tracks(model.morph_names, model.morphs, pack_morph_key)


# Per-track hashes, saved with a sync so the next preview can name what changed. Eight
# hex digits only have to tell one revision of a track from the next.
def track_digests(names, tracks, pack_key):
    table = {}
    for name, keys in zip(names, tracks):
        sha = hashlib.sha256()
        for key in keys:
            sha.update(pack_key(key))
        table[name] = sha.hexdigest()[:8]
    return table


# A short stable hash of any repr-able values, for settings and rig signatures.
def digest_values(*values):
    return hashlib.sha256(repr(values).encode("utf-8", "replace")).hexdigest()[:24]


# True when a key poses the bone away from rest. PMM stores one initial key per bone
# whether or not it was touched, so a lone rest key is not animation.
def bone_key_moves(key):
    if any(abs(v) > REST_EPSILON for v in key.location):
        return True
    return abs(abs(key.rotation[3]) - 1.0) > REST_EPSILON


def bone_track_moves(keys):
    return len(keys) > 1 or any(bone_key_moves(k) for k in keys)


# Same rule as report_morphs: a morph counts once a key has a non-zero weight.
def morph_track_moves(keys):
    return any(abs(k.weight) > REST_EPSILON for k in keys)


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
    )


# The name mmd_tools will look up: a VMD holds 15 bytes of a bone name, so a longer
# PMM name reaches the importer cut.
def vmd_bone_name(name):
    return encode_name(name).rstrip(b"\x00").decode("cp932", "replace")
