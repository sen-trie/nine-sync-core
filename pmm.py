import struct
from typing import NamedTuple, Tuple

from .common import (
    AUDIO_EXTENSIONS,
    AUDIO_PATH_LENGTH,
    AUDIO_STATE_SIZE,
    log,
)


SIGNATURE = b"Polygon Movie maker "


PATH_LENGTH = 256


BONE_INITIAL = struct.Struct("<iii16s3f4fBB")


BONE_ADDITIONAL = struct.Struct("<iiii16s3f4fBB")


MORPH_INITIAL = struct.Struct("<iiifB")


MORPH_ADDITIONAL = struct.Struct("<iiiifB")


CAMERA_INITIAL = struct.Struct("<iiif3f3fii24sBiB")


CAMERA_ADDITIONAL = struct.Struct("<iiiif3f3fii24sBiB")


LIGHT_INITIAL = struct.Struct("<iii3f3fB")


LIGHT_ADDITIONAL = struct.Struct("<iiii3f3fB")


CAMERA_STATE_SIZE = 37


BONE_STATE_SIZE = 31


MORPH_STATE_SIZE = 4


OUTSIDE_PARENT_STATE_SIZE = 16


LIGHT_STATE_SIZE = 24


ACCESSORY_NAME_LENGTH = 100


ACCESSORY_PATH_LENGTH = 256


ACCESSORY_KEY = struct.Struct("<iiiBii3f3ffBB")


ACCESSORY_ADDITIONAL = struct.Struct("<iiiiBii3f3ffBB")


ACCESSORY_STATE_SIZE = 39


class PmmError(Exception):
    pass


class BoneKey(NamedTuple):
    frame: int
    location: Tuple[float, float, float]
    rotation: Tuple[float, float, float, float]
    interpolation: bytes


class MorphKey(NamedTuple):
    frame: int
    weight: float


class CameraKey(NamedTuple):
    frame: int
    distance: float
    look_at: Tuple[float, float, float]
    angle: Tuple[float, float, float]
    parent_model: int
    parent_bone: int
    interpolation: bytes
    perspective: int
    fov: int


class LightKey(NamedTuple):
    frame: int
    color: Tuple[float, float, float]
    direction: Tuple[float, float, float]


class PmmModel:
    __slots__ = (
        "index",
        "name",
        "name_en",
        "path",
        "bone_names",
        "morph_names",
        "bones",
        "morphs",
        "last_frame",
    )

    def __init__(self):
        self.index = 0
        self.name = ""
        self.name_en = ""
        self.path = ""
        self.bone_names = []
        self.morph_names = []
        self.bones = []
        self.morphs = []
        self.last_frame = 0


class AccessoryKey(NamedTuple):
    frame: int
    visible: int
    parent_model: int
    parent_bone: int
    position: Tuple[float, float, float]
    rotation: Tuple[float, float, float]
    scale: float
    shadow: int
    opacity: float = 1.0


class PmmAccessory:
    __slots__ = ("index", "name", "path", "draw_order", "keys")

    def __init__(self):
        self.index = 0
        self.name = ""
        self.path = ""
        self.draw_order = 0
        self.keys = []


class PmmProject:
    __slots__ = (
        "version",
        "models",
        "selected_model",
        "camera_keys",
        "light_keys",
        "scene_error",
        "accessories",
        "accessory_error",
        "audio_enabled",
        "audio_path",
        "audio_error",
    )

    def __init__(self):
        self.audio_enabled = False
        self.audio_path = ""
        self.audio_error = ""
        self.version = 0
        self.models = []
        self.selected_model = 0
        self.camera_keys = []
        self.light_keys = []
        self.scene_error = ""
        self.accessories = []
        self.accessory_error = ""


class Reader:
    __slots__ = ("data", "pos")

    def __init__(self, data):
        self.data = data
        self.pos = 0

    def need(self, count):
        if count < 0 or self.pos + count > len(self.data):
            raise PmmError("unexpected end of file at offset %d" % self.pos)

    def skip(self, count):
        self.need(count)
        self.pos += count

    def u8(self):
        self.need(1)
        value = self.data[self.pos]
        self.pos += 1
        return value

    def i32(self):
        self.need(4)
        (value,) = struct.unpack_from("<i", self.data, self.pos)
        self.pos += 4
        return value

    def count(self):
        value = self.i32()
        if value < 0:
            raise PmmError("negative count at offset %d" % (self.pos - 4))
        return value

    def unpack(self, layout):
        self.need(layout.size)
        values = layout.unpack_from(self.data, self.pos)
        self.pos += layout.size
        return values

    def raw(self, length):
        self.need(length)
        chunk = self.data[self.pos : self.pos + length]
        self.pos += length
        return chunk

    def var_string(self):
        return decode_text(self.raw(self.u8()))

    def fixed_string(self, length):
        return decode_text(self.raw(length))


def decode_text(raw):
    end = raw.find(b"\x00")
    if end != -1:
        raw = raw[:end]
    return raw.decode("cp932", errors="replace")


def read_version(data):
    if not data.startswith(SIGNATURE):
        raise PmmError("not a PMM file")
    tail = data[len(SIGNATURE) : 32]
    end = tail.find(b"\x00")
    if end != -1:
        tail = tail[:end]
    try:
        return int(tail.decode("ascii").strip())
    except ValueError:
        raise PmmError("unreadable PMM version")


def normalize_keys(keys):
    latest = {}
    for key in keys:
        if key.frame >= 0:
            latest[key.frame] = key
    return [latest[frame] for frame in sorted(latest)]


def collect_bone_chain(initial, additional):
    frame, interp, loc, rot, next_index = initial
    keys = [BoneKey(frame, loc, rot, interp)]
    seen = set()
    cursor = next_index
    while cursor != 0 and cursor not in seen:
        entry = additional.get(cursor)
        if entry is None:
            break
        seen.add(cursor)
        frame, interp, loc, rot, next_index = entry
        keys.append(BoneKey(frame, loc, rot, interp))
        cursor = next_index
    return normalize_keys(keys)


def collect_morph_chain(initial, additional):
    frame, weight, next_index = initial
    keys = [MorphKey(frame, weight)]
    seen = set()
    cursor = next_index
    while cursor != 0 and cursor not in seen:
        entry = additional.get(cursor)
        if entry is None:
            break
        seen.add(cursor)
        frame, weight, next_index = entry
        keys.append(MorphKey(frame, weight))
        cursor = next_index
    return normalize_keys(keys)


def build_camera_payload(rest):
    return (
        rest[0],
        tuple(rest[1:4]),
        tuple(rest[4:7]),
        rest[7],
        rest[8],
        rest[9],
        rest[10],
        rest[11],
    )


def build_light_payload(rest):
    return (tuple(rest[0:3]), tuple(rest[3:6]))


def read_scene_track(reader, initial_layout, additional_layout, build_payload, factory):
    v = reader.unpack(initial_layout)
    initial = (v[0], v[2], build_payload(v[3:]))
    additional = {}
    for _ in range(reader.count()):
        v = reader.unpack(additional_layout)
        additional[v[0]] = (v[1], v[3], build_payload(v[4:]))
    frame, next_index, payload = initial
    keys = [factory(frame, *payload)]
    seen = set()
    cursor = next_index
    while cursor != 0 and cursor not in seen:
        entry = additional.get(cursor)
        if entry is None:
            break
        seen.add(cursor)
        frame, next_index, payload = entry
        keys.append(factory(frame, *payload))
        cursor = next_index
    return normalize_keys(keys)


def read_bone_track(reader, bone_count):
    initials = []
    for _ in range(bone_count):
        v = reader.unpack(BONE_INITIAL)
        initials.append((v[0], v[3], tuple(v[4:7]), tuple(v[7:11]), v[2]))
    additional = {}
    for _ in range(reader.count()):
        v = reader.unpack(BONE_ADDITIONAL)
        additional[v[0]] = (v[1], v[4], tuple(v[5:8]), tuple(v[8:12]), v[3])
    return [collect_bone_chain(initials[i], additional) for i in range(bone_count)]


def read_morph_track(reader, morph_count):
    initials = []
    for _ in range(morph_count):
        v = reader.unpack(MORPH_INITIAL)
        initials.append((v[0], v[3], v[2]))
    additional = {}
    for _ in range(reader.count()):
        v = reader.unpack(MORPH_ADDITIONAL)
        additional[v[0]] = (v[1], v[4], v[3])
    return [collect_morph_chain(initials[i], additional) for i in range(morph_count)]


def skip_model_keyframe(reader, constraint_count, outside_count, with_index):
    reader.skip(
        (4 if with_index else 0) + 12 + 1 + constraint_count + outside_count * 8 + 1
    )


def read_model(reader):
    model = PmmModel()
    model.index = reader.u8()
    model.name = reader.var_string()
    model.name_en = reader.var_string()
    model.path = reader.fixed_string(PATH_LENGTH)
    reader.u8()
    bone_count = reader.count()
    model.bone_names = [reader.var_string() for _ in range(bone_count)]
    morph_count = reader.count()
    model.morph_names = [reader.var_string() for _ in range(morph_count)]
    constraint_count = reader.count()
    reader.skip(constraint_count * 4)
    outside_count = reader.count()
    reader.skip(outside_count * 4)
    reader.u8()
    reader.u8()
    reader.skip(4 + 16)
    reader.skip(reader.u8())
    reader.skip(4)
    model.last_frame = reader.i32()
    model.bones = read_bone_track(reader, bone_count)
    model.morphs = read_morph_track(reader, morph_count)
    skip_model_keyframe(reader, constraint_count, outside_count, False)
    for _ in range(reader.count()):
        skip_model_keyframe(reader, constraint_count, outside_count, True)
    reader.skip(bone_count * BONE_STATE_SIZE)
    reader.skip(morph_count * MORPH_STATE_SIZE)
    reader.skip(constraint_count)
    reader.skip(outside_count * OUTSIDE_PARENT_STATE_SIZE)
    reader.skip(1 + 4 + 1 + 1)
    return model


def accessory_key_from(values):
    raw = values[3]
    return AccessoryKey(
        values[0],
        raw & 1,
        values[4],
        values[5],
        tuple(values[6:9]),
        tuple(values[9:12]),
        values[12],
        values[13],
        min(1.0, max(0.0, (100 - (raw >> 1)) / 100.0)),
    )


def read_accessory_keys(reader):
    first = reader.unpack(ACCESSORY_KEY)
    additional = {}
    for _ in range(reader.count()):
        v = reader.unpack(ACCESSORY_ADDITIONAL)
        additional[v[0]] = (v[2], v[4], v[1:])
    keys = [accessory_key_from(first)]
    seen = set()
    cursor = first[2]
    while cursor != 0 and cursor not in seen:
        entry = additional.get(cursor)
        if entry is None:
            break
        seen.add(cursor)
        values = entry[2]
        keys.append(accessory_key_from(values))
        cursor = values[2]
    return normalize_keys(keys)


def read_accessories(reader):
    reader.u8()
    reader.i32()
    count = reader.u8()
    names = [reader.fixed_string(ACCESSORY_NAME_LENGTH) for _ in range(count)]
    found = []
    seen = set()
    for position in range(count):
        acc = PmmAccessory()
        acc.index = reader.u8()
        acc.name = reader.fixed_string(ACCESSORY_NAME_LENGTH)
        if acc.index in seen or acc.name not in names:
            raise PmmError(
                "accessory record %d out of sync (index %d, name %r)"
                % (position, acc.index, acc.name)
            )
        seen.add(acc.index)
        acc.path = reader.fixed_string(ACCESSORY_PATH_LENGTH)
        acc.draw_order = reader.u8()
        acc.keys = read_accessory_keys(reader)
        reader.skip(ACCESSORY_STATE_SIZE)
        found.append(acc)
    return found


def read_audio(reader):
    reader.skip(AUDIO_STATE_SIZE)
    enabled = reader.u8() != 0
    path = reader.fixed_string(AUDIO_PATH_LENGTH)
    if path and not path.lower().endswith(AUDIO_EXTENSIONS):
        return False, ""
    return enabled, path


def parse_pmm(data):
    project = PmmProject()
    project.version = read_version(data)
    if project.version != 2:
        raise PmmError(
            "unsupported PMM version %d (only PMM v2 is supported)" % project.version
        )
    reader = Reader(data)
    reader.skip(30 + 12 + 4 + 7)
    project.selected_model = reader.u8()
    model_count = reader.u8()
    for _ in range(model_count):
        project.models.append(read_model(reader))
    try:
        project.camera_keys = read_scene_track(
            reader, CAMERA_INITIAL, CAMERA_ADDITIONAL, build_camera_payload, CameraKey
        )
        reader.skip(CAMERA_STATE_SIZE)
        project.light_keys = read_scene_track(
            reader, LIGHT_INITIAL, LIGHT_ADDITIONAL, build_light_payload, LightKey
        )
    except PmmError as error:
        project.scene_error = str(error)
    else:
        try:
            reader.skip(LIGHT_STATE_SIZE)
            project.accessories = read_accessories(reader)
        except PmmError as error:
            project.accessory_error = str(error)
            project.accessories = []
        else:
            try:
                project.audio_enabled, project.audio_path = read_audio(reader)
            except PmmError as error:
                project.audio_error = str(error)
    return project


def parse_pmm_file(path):
    with open(path, "rb") as handle:
        data = handle.read()
    log.debug("read %d bytes from %s", len(data), path)
    return parse_pmm(data)


VMD_SIGNATURE = b"Vocaloid Motion Data 0002"


NAME_LIMIT = 15


BONE_FRAME = struct.Struct("<15sI3f4f64s")


MORPH_FRAME = struct.Struct("<15sIf")


def encode_name(name, limit=NAME_LIMIT):
    out = b""
    for char in name:
        piece = char.encode("cp932", errors="replace")
        if len(out) + len(piece) > limit:
            break
        out += piece
    return out.ljust(limit, b"\x00")


def interpolation_to_vmd(source, layout="CURVE"):
    row = bytearray(16)
    if layout == "VMD_ROW":
        for k in range(16):
            row[k] = min(int(source[k]), 127)
    else:
        for curve in range(4):
            x1, y1, x2, y2 = (
                min(int(v), 127) for v in source[curve * 4 : curve * 4 + 4]
            )
            row[curve] = x1
            row[4 + curve] = y1
            row[8 + curve] = x2
            row[12 + curve] = y2
    out = bytearray(64)
    for shift in range(4):
        for k in range(16 - shift):
            out[shift * 16 + k] = row[k + shift]
    return bytes(out)


def build_vmd(model, layout="CURVE", morph_map=None):
    chunks = [struct.pack("<30s20s", VMD_SIGNATURE, encode_name(model.name, 20))]
    bone_frames = []
    for name, keys in zip(model.bone_names, model.bones):
        packed_name = encode_name(name)
        for key in keys:
            bone_frames.append(
                BONE_FRAME.pack(
                    packed_name,
                    key.frame,
                    *key.location,
                    *key.rotation,
                    interpolation_to_vmd(key.interpolation, layout),
                )
            )
    chunks.append(struct.pack("<I", len(bone_frames)))
    chunks.extend(bone_frames)
    morph_frames = []
    for name, keys in zip(model.morph_names, model.morphs):
        packed_name = encode_name(morph_map.get(name, name) if morph_map else name)
        for key in keys:
            morph_frames.append(MORPH_FRAME.pack(packed_name, key.frame, key.weight))
    chunks.append(struct.pack("<I", len(morph_frames)))
    chunks.extend(morph_frames)
    chunks.append(struct.pack("<IIII", 0, 0, 0, 0))
    log.debug(
        "built VMD for '%s': %d bone frames, %d morph frames",
        model.name,
        len(bone_frames),
        len(morph_frames),
    )
    return b"".join(chunks)


CAMERA_FRAME = struct.Struct("<If3f3f24sIB")


def camera_interpolation_to_vmd(source):
    out = bytearray(24)
    for param in range(6):
        x1, y1, x2, y2 = source[param * 4 : param * 4 + 4]
        out[param * 4 : param * 4 + 4] = bytes((x1, x2, y1, y2))
    return bytes(out)


def build_scene_vmd(camera_keys):
    chunks = [
        struct.pack("<30s20s", VMD_SIGNATURE, encode_name("scene", 20)),
        struct.pack("<II", 0, 0),
        struct.pack("<I", len(camera_keys)),
    ]
    for key in camera_keys:
        chunks.append(
            CAMERA_FRAME.pack(
                key.frame,
                key.distance,
                *key.look_at,
                *key.angle,
                camera_interpolation_to_vmd(key.interpolation),
                max(0, key.fov),
                1 if key.perspective else 0,
            )
        )
    chunks.append(struct.pack("<IIII", 0, 0, 0, 0))
    log.debug("built scene VMD: %d camera frames", len(camera_keys))
    return b"".join(chunks)
