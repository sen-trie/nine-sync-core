import struct

from .pmm import (
    BONE_ADDITIONAL,
    BONE_INITIAL,
    BoneKey,
    CAMERA_ADDITIONAL,
    CameraKey,
    MorphKey,
    CAMERA_INITIAL,
    MORPH_ADDITIONAL,
    MORPH_INITIAL,
    PATH_LENGTH,
    PmmError,
    parse_pmm,
)


SELECTED_MODEL_OFFSET = 30 + 12 + 4 + 7


# MMD's own default curve (20, 20, 107, 107) for each of X, Y, Z and rotation.
DEFAULT_BONE_INTERPOLATION = bytes((20, 20, 107, 107) * 4)


DEFAULT_CAMERA_INTERPOLATION = bytes((20, 20, 107, 107) * 6)


def var_string(text):
    raw = text.encode("cp932", errors="replace")[:255]
    return bytes((len(raw),)) + raw


def fixed_string(text, length):
    raw = text.encode("cp932", errors="replace")
    if len(raw) >= length:
        raise PmmError("path is too long for a PMM (%d bytes max): %s" % (length - 1, text))
    return raw.ljust(length, b"\x00")


def bone_state(key):
    return struct.pack("<3f4f3B", *key.location, *key.rotation, 0, 0, 0)


# Every bone and morph needs a first key at frame 0, which MMD treats as the
# model's initial state.
def with_initial(keys, default):
    keys = sorted(keys, key=lambda k: k.frame)
    if not keys or keys[0].frame != 0:
        keys = [default] + keys
    return keys


# Key indices share one sequence per track, as MMD itself saves them: a bone's (or
# morph's) first key has its own position as index, and the additional keys are
# numbered from the bone (or morph) count upwards across all of them. The first
# additional key's 'prev' is therefore the owner's position, not 0.
def chain_links(owner, count, counter):
    indexes = [owner]
    for _ in range(count - 1):
        counter[0] += 1
        indexes.append(counter[0])
    return indexes


def write_bone_track(bone_keys, default):
    counter = [len(bone_keys) - 1]
    initials = []
    additional = []
    for owner, keys in enumerate(bone_keys):
        keys = with_initial(keys, default)
        indexes = chain_links(owner, len(keys), counter)
        first = keys[0]
        initials.append(
            BONE_INITIAL.pack(
                first.frame,
                0,
                indexes[1] if len(keys) > 1 else 0,
                first.interpolation,
                *first.location,
                *first.rotation,
                0,
                0,
            )
        )
        for position in range(1, len(keys)):
            key = keys[position]
            additional.append(
                BONE_ADDITIONAL.pack(
                    indexes[position],
                    key.frame,
                    indexes[position - 1],
                    indexes[position + 1] if position + 1 < len(keys) else 0,
                    key.interpolation,
                    *key.location,
                    *key.rotation,
                    0,
                    0,
                )
            )
    return b"".join(initials) + struct.pack("<i", len(additional)) + b"".join(additional)


def write_morph_track(morph_keys, default):
    counter = [len(morph_keys) - 1]
    initials = []
    additional = []
    for owner, keys in enumerate(morph_keys):
        keys = with_initial(keys, default)
        indexes = chain_links(owner, len(keys), counter)
        first = keys[0]
        initials.append(
            MORPH_INITIAL.pack(
                first.frame, 0, indexes[1] if len(keys) > 1 else 0, first.weight, 0
            )
        )
        for position in range(1, len(keys)):
            key = keys[position]
            additional.append(
                MORPH_ADDITIONAL.pack(
                    indexes[position],
                    key.frame,
                    indexes[position - 1],
                    indexes[position + 1] if position + 1 < len(keys) else 0,
                    key.weight,
                    0,
                )
            )
    return b"".join(initials) + struct.pack("<i", len(additional)) + b"".join(additional)


def write_camera_track(keys):
    keys = sorted(keys, key=lambda k: k.frame)
    counter = [0]
    indexes = chain_links(0, len(keys), counter)
    first = keys[0]

    def payload(key):
        return (
            key.distance,
            *key.look_at,
            *key.angle,
            key.parent_model,
            key.parent_bone,
            key.interpolation,
            key.perspective,
            key.fov,
            0,
        )

    out = [
        CAMERA_INITIAL.pack(
            first.frame, 0, indexes[1] if len(keys) > 1 else 0, *payload(first)
        ),
        struct.pack("<i", len(keys) - 1),
    ]
    for position in range(1, len(keys)):
        out.append(
            CAMERA_ADDITIONAL.pack(
                indexes[position],
                keys[position].frame,
                indexes[position - 1],
                indexes[position + 1] if position + 1 < len(keys) else 0,
                *payload(keys[position]),
            )
        )
    return b"".join(out)


# model.opaque is the template model's editor state, reused for every written model
# because the writer has no data of its own for those bytes.
# The per-bone and per-morph state MMD shows on load is written from the key at
# frame 0, with no selection flag; copying a template bone's state would pose
# everything at rest and mark every bone selected.
def write_model(model, opaque, bone_default, morph_default):
    constraint_count = len(model.constraints)
    last_frame = 0
    for keys in list(model.bones) + list(model.morphs):
        if keys:
            last_frame = max(last_frame, keys[-1].frame)
    out = [
        bytes((model.index,)),
        var_string(model.name),
        var_string(model.name_en),
        fixed_string(model.path, PATH_LENGTH),
        opaque["after_path"],
        struct.pack("<i", len(model.bone_names)),
        b"".join(var_string(n) for n in model.bone_names),
        struct.pack("<i", len(model.morph_names)),
        b"".join(var_string(n) for n in model.morph_names),
        struct.pack("<i", constraint_count),
        b"".join(struct.pack("<i", i) for i in model.constraints),
        struct.pack("<i", 0),
        opaque["middle"],
        struct.pack("<i", last_frame),
        write_bone_track(model.bones, bone_default),
        write_morph_track(model.morphs, morph_default),
        struct.pack("<iii", 0, 0, 0),
        b"\x01",
        b"\x01" * constraint_count,
        b"\x00",
        struct.pack("<i", 0),
        b"".join(
            bone_state(with_initial(keys, bone_default)[0]) for keys in model.bones
        ),
        b"".join(
            struct.pack("<f", with_initial(keys, morph_default)[0].weight)
            for keys in model.morphs
        ),
        opaque["constraint_state"] * constraint_count,
        opaque["tail"],
    ]
    return b"".join(out)


# Offset of the 28-byte playback state from the end of the camera track: camera
# state 37, light track 41, light state 24, accessory header 6. Layout decoded from
# MMD saves with known ranges: current frame at +12, flag bytes at +16..+19 (repeat,
# stop frame on, from frame on), play start at +20 and play end at +24. The first
# 12 bytes are not understood and are left as the template has them.
PLAYBACK_OFFSET = 37 + 41 + 24 + 6


def set_play_range(tail, start, end):
    state = bytearray(tail[PLAYBACK_OFFSET : PLAYBACK_OFFSET + 28])
    struct.pack_into("<i", state, 12, 0)
    state[18] = 1
    state[19] = 1
    struct.pack_into("<ii", state, 20, max(0, start), max(0, end))
    return tail[:PLAYBACK_OFFSET] + bytes(state) + tail[PLAYBACK_OFFSET + 28 :]


# Builds a PMM from a template saved by real MMD: header, light, accessories, audio
# and the other global state are copied untouched; only the model and camera
# sections are replaced. The result is parsed back as a self-check.
def build_pmm(template, models, camera_keys, play_range=None):
    base = parse_pmm(template)
    if not base.models or base.spans is None:
        raise PmmError("the PMM template needs at least one model and a readable camera section")
    if not models:
        raise PmmError("no models to write")
    if len(models) > 255:
        raise PmmError("a PMM holds at most 255 models")
    opaque = dict(base.models[0].opaque)
    opaque["constraint_state"] = opaque["constraint_state"] or bytes((1,))
    models_start, _, camera_end = base.spans
    bone_default = BoneKey(0, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), DEFAULT_BONE_INTERPOLATION)
    morph_default = MorphKey(0, 0.0)
    camera = camera_keys or [
        CameraKey(0, -45.0, (0.0, 10.0, 0.0), (0.0, 0.0, 0.0), -1, -1, DEFAULT_CAMERA_INTERPOLATION, 0, 30)
    ]
    head = bytearray(template[: models_start - 1])
    head[SELECTED_MODEL_OFFSET] = 0
    out = b"".join(
        [
            bytes(head),
            bytes((len(models),)),
            b"".join(write_model(m, opaque, bone_default, morph_default) for m in models),
            write_camera_track(camera),
            set_play_range(template[camera_end:], *play_range) if play_range else template[camera_end:],
        ]
    )
    check = parse_pmm(out)
    if len(check.models) != len(models) or len(check.camera_keys) != len(camera):
        raise PmmError("the written PMM does not read back correctly")
    for wrote, read in zip(models, check.models):
        if read.bone_names != wrote.bone_names or read.morph_names != wrote.morph_names:
            raise PmmError("model '%s' did not read back correctly" % wrote.name)
    return out
