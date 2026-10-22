bl_info = {
    "name": "MMD PMM Bridge",
    "author": "",
    "version": (0, 1, 0),
    "blender": (5, 2, 0),
    "location": "View3D > Sidebar > MMD Bridge",
    "description": "Live-sync MikuMikuDance .pmm keyframes into Blender through mmd_tools",
    "category": "Import-Export",
}

import hashlib
import importlib
import inspect
import logging
import os
import re
import struct
import sys
import math
import tempfile
import textwrap
import time
import traceback
from typing import NamedTuple, Tuple

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup
from mathutils import Matrix, Vector

LOG_TEXT_NAME = "MMDBridgeLog"
LOG_TEXT_MAX_LINES = 3000

log = logging.getLogger("MMDBridge")


class TextBlockHandler(logging.Handler):
    def emit(self, record):
        try:
            text = bpy.data.texts.get(LOG_TEXT_NAME) or bpy.data.texts.new(
                LOG_TEXT_NAME
            )
            if len(text.lines) > LOG_TEXT_MAX_LINES:
                text.clear()
            text.write(self.format(record) + "\n")
        except Exception:
            pass


def setup_logging():
    for handler in list(log.handlers):
        log.removeHandler(handler)
    formatter = logging.Formatter(
        "[MMDBridge %(asctime)s %(levelname)s] %(message)s", "%H:%M:%S"
    )
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    block = TextBlockHandler()
    block.setFormatter(formatter)
    log.addHandler(stream)
    log.addHandler(block)
    log.setLevel(logging.DEBUG)
    log.propagate = False


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
    )

    def __init__(self):
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
    return AccessoryKey(
        values[0],
        values[3],
        values[4],
        values[5],
        tuple(values[6:9]),
        tuple(values[9:12]),
        values[12],
        values[13],
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
    reader.skip(count * ACCESSORY_NAME_LENGTH)
    found = []
    for position in range(count):
        acc = PmmAccessory()
        acc.index = reader.u8()
        if acc.index != position:
            raise PmmError(
                "accessory record %d out of sync (index %d)" % (position, acc.index)
            )
        acc.name = reader.fixed_string(ACCESSORY_NAME_LENGTH)
        acc.path = reader.fixed_string(ACCESSORY_PATH_LENGTH)
        acc.draw_order = reader.u8()
        acc.keys = read_accessory_keys(reader)
        reader.skip(ACCESSORY_STATE_SIZE)
        found.append(acc)
    return found


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


X_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[{}]|[;,]|[^\s{};,"]+')


class XBlock:
    __slots__ = ("kind", "name", "data", "children")

    def __init__(self, kind, name):
        self.kind = kind
        self.name = name
        self.data = []
        self.children = []


class XMaterial(NamedTuple):
    name: str
    diffuse: Tuple[float, float, float, float]
    power: float
    specular: Tuple[float, float, float]
    emissive: Tuple[float, float, float]
    texture: str


class XMesh:
    __slots__ = ("vertices", "faces", "uvs", "face_materials", "materials", "colors")

    def __init__(self):
        self.vertices = []
        self.faces = []
        self.uvs = []
        self.face_materials = []
        self.materials = []
        self.colors = {}


class XScene:
    __slots__ = ("meshes", "warnings", "mode")

    def __init__(self):
        self.meshes = []
        self.warnings = []
        self.mode = "txt"


class XError(Exception):
    pass


UNSUPPORTED_BLOCKS = (
    "SkinWeights",
    "XSkinMeshHeader",
    "AnimationSet",
    "Animation",
    "AnimTicksPerSecond",
)


def x_header_mode(raw):
    if len(raw) < 16 or raw[:4] != b"xof ":
        raise XError("not a DirectX .x file")
    return raw[8:12].decode("ascii", errors="replace").strip()


def x_tokens(text):
    text = re.sub(r"//[^\n]*|#[^\n]*", "", text)
    return X_TOKEN.findall(text)


def x_number(token):
    try:
        return float(token)
    except ValueError:
        return None


def x_skip_block(tokens, i):
    depth = 0
    while i < len(tokens):
        if tokens[i] == "{":
            depth += 1
        elif tokens[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise XError("unbalanced braces")


def x_parse_body(tokens, i, block):
    while i < len(tokens):
        token = tokens[i]
        if token == "}":
            return i + 1
        if token in (";", ","):
            i += 1
            continue
        if token == "{":
            j = i + 1
            ref = []
            while j < len(tokens) and tokens[j] != "}":
                if tokens[j] not in (";", ","):
                    ref.append(tokens[j])
                j += 1
            child = XBlock("__ref__", ref[0] if ref else "")
            block.children.append(child)
            i = j + 1
            continue
        if token.startswith('"'):
            block.data.append(token[1:-1])
            i += 1
            continue
        number = x_number(token)
        if number is not None:
            block.data.append(number)
            i += 1
            continue
        kind = token
        i += 1
        name = ""
        if i < len(tokens) and tokens[i] != "{":
            name = tokens[i]
            i += 1
        if i >= len(tokens) or tokens[i] != "{":
            raise XError("expected '{' after %s" % kind)
        if kind == "template":
            i = x_skip_block(tokens, i)
            continue
        i += 1
        child = XBlock(kind, name)
        if i < len(tokens) and tokens[i].startswith("<") and tokens[i].endswith(">"):
            i += 1
        i = x_parse_body(tokens, i, child)
        block.children.append(child)
    return i


def x_parse_tree(text):
    root = XBlock("root", "")
    tokens = x_tokens(text)
    x_parse_body(tokens, 0, root)
    return root


def x_mat_mul(a, b):
    return [
        sum(a[r * 4 + k] * b[k * 4 + c] for k in range(4))
        for r in range(4)
        for c in range(4)
    ]


X_IDENTITY = [
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
]


def x_collect_materials(root):
    found = {}

    def walk(block):
        for child in block.children:
            if child.kind == "Material" and child.name:
                found[child.name] = child
            walk(child)

    walk(root)
    return found


def x_read_material(block, name):
    d = block.data
    if len(d) < 11:
        raise XError("short Material block")
    texture = ""
    for child in block.children:
        if child.kind == "TextureFilename" and child.data:
            texture = str(child.data[0])
    return XMaterial(
        name or "Material",
        (d[0], d[1], d[2], d[3]),
        d[4],
        (d[5], d[6], d[7]),
        (d[8], d[9], d[10]),
        texture,
    )


def x_read_mesh(block, matrix, named_materials):
    d = block.data
    mesh = XMesh()
    pos = 0
    count = int(d[pos])
    pos += 1
    raw = d[pos : pos + count * 3]
    pos += count * 3
    for k in range(count):
        x, y, z = raw[k * 3 : k * 3 + 3]
        m = matrix
        mesh.vertices.append(
            (
                x * m[0] + y * m[4] + z * m[8] + m[12],
                x * m[1] + y * m[5] + z * m[9] + m[13],
                x * m[2] + y * m[6] + z * m[10] + m[14],
            )
        )
    face_count = int(d[pos])
    pos += 1
    for _ in range(face_count):
        n = int(d[pos])
        pos += 1
        mesh.faces.append(tuple(int(v) for v in d[pos : pos + n]))
        pos += n
    for child in block.children:
        cd = child.data
        if child.kind == "MeshTextureCoords" and cd:
            n = int(cd[0])
            mesh.uvs = [(cd[1 + k * 2], cd[2 + k * 2]) for k in range(n)]
        elif child.kind == "MeshMaterialList" and len(cd) >= 2:
            n_mat = int(cd[0])
            n_idx = int(cd[1])
            indexes = [int(v) for v in cd[2 : 2 + n_idx]]
            if n_idx == 1 and face_count > 1:
                indexes = indexes * face_count
            mesh.face_materials = indexes
            for sub in child.children:
                if sub.kind == "Material":
                    mesh.materials.append(x_read_material(sub, sub.name))
                elif sub.kind == "__ref__":
                    target = named_materials.get(sub.name)
                    if target is not None:
                        mesh.materials.append(x_read_material(target, target.name))
            if n_mat and len(mesh.materials) < n_mat:
                while len(mesh.materials) < n_mat:
                    mesh.materials.append(
                        XMaterial(
                            "Material",
                            (0.8, 0.8, 0.8, 1.0),
                            0.0,
                            (0.0, 0.0, 0.0),
                            (0.0, 0.0, 0.0),
                            "",
                        )
                    )
        elif child.kind == "MeshVertexColors" and cd:
            n = int(cd[0])
            for k in range(n):
                base = 1 + k * 5
                mesh.colors[int(cd[base])] = (
                    cd[base + 1],
                    cd[base + 2],
                    cd[base + 3],
                    cd[base + 4],
                )
    return mesh


def x_walk(block, matrix, scene, named_materials):
    for child in block.children:
        if child.kind == "Frame":
            local = list(X_IDENTITY)
            for sub in child.children:
                if sub.kind == "FrameTransformMatrix" and len(sub.data) >= 16:
                    local = [float(v) for v in sub.data[:16]]
            x_walk(child, x_mat_mul(local, matrix), scene, named_materials)
        elif child.kind == "Mesh":
            scene.meshes.append(x_read_mesh(child, matrix, named_materials))
            for sub in child.children:
                if sub.kind in UNSUPPORTED_BLOCKS:
                    note = (
                        "%s ignored (skinned/animated .x is not supported)" % sub.kind
                    )
                    if note not in scene.warnings:
                        scene.warnings.append(note)
        elif child.kind in UNSUPPORTED_BLOCKS:
            note = "%s ignored (skinned/animated .x is not supported)" % child.kind
            if note not in scene.warnings:
                scene.warnings.append(note)


def x_decode(raw):
    raw = raw.replace(b"\x00", b"")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp932", errors="replace")


def parse_x_bytes(raw):
    scene = XScene()
    scene.mode = x_header_mode(raw)
    if scene.mode != "txt":
        return scene
    body = x_decode(raw[16:])
    root = x_parse_tree(body)
    named = x_collect_materials(root)
    x_walk(root, list(X_IDENTITY), scene, named)
    for block in root.children:
        if block.kind in UNSUPPORTED_BLOCKS:
            note = "%s ignored (skinned/animated .x is not supported)" % block.kind
            if note not in scene.warnings:
                scene.warnings.append(note)
    return scene


ACTION_PREFIX = "MMDBridge_"
CAMERA_COLLECTION = "MMD_Camera"
LIGHT_COLLECTION = "MMD_Sun"
FOLLOW_PREFIX = "MMDBridgeFollow"
MANUAL_FOLLOW = (-2, -2)
UNIT_SCALE = 1.0
TEMP_PREFIX = "mmd_bridge_"
TEMP_DIR_NAME = "mmd_bridge"
STALE_TEMP_SECONDS = 300
FRAME_EPSILON = 1e-4
CLIP_TRACK_NAME = "MMDBridge"
UV_KEY_PREFIX = "MMDUV_"
UV_NODE_PREFIX = "MMDBridgeUV"
UV_ATTRIBUTE_PREFIX = "MMDBridgeUV_"
UV_SLIDER_LIMIT = 10.0
ACCESSORY_COLLECTION = "MMD_Accessories"
ACCESSORY_PREFIX = "MMDAcc_"
ACCESSORY_FOLLOW_PREFIX = "MMDBridgeAcc"
ACCESSORY_UNIT = 10.0
X_IMPORTER_MODULES = ("io_directx_x", "io_scene_x")
X_IMPORTER_URL = "https://extensions.blender.org/add-ons/io-directx-x/"
MMD_TOOLS_URL = "https://extensions.blender.org/add-ons/mmd-tools/"

_follow_items = [("NONE", "None", "")]
_last_sync = {"digest": None}


def mmd_tools_available():
    return hasattr(bpy.types, "MMD_TOOLS_OT_import_vmd")


def addon_loaded(modules):
    try:
        names = list(bpy.context.preferences.addons.keys())
    except Exception:
        return False
    return any(name.rsplit(".", 1)[-1] in modules for name in names)


def x_importer_available():
    return addon_loaded(X_IMPORTER_MODULES)


def missing_dependencies():
    missing = []
    if not mmd_tools_available():
        missing.append(
            ("mmd_tools", "mmd_tools is not installed or enabled", MMD_TOOLS_URL)
        )
    if not x_importer_available():
        missing.append(
            (
                "DirectX X Importer",
                "DirectX X Importer is not installed (only needed for binary or compressed .x accessories)",
                X_IMPORTER_URL,
            )
        )
    return missing


def target_usable(obj):
    return (
        obj is not None
        and len(obj.users_collection) > 0
        and find_armature(obj) is not None
    )


def current_scale(settings):
    if settings.auto_scale:
        for item in settings.targets:
            if item.enabled and target_usable(item.target):
                size = getattr(item.target, "empty_display_size", 0.0)
                if size > 0.0:
                    return size * 0.2
    return settings.scale


def scale_warning_active(settings):
    if not settings.sync_camera:
        return False
    scale = current_scale(settings)
    if abs(scale - UNIT_SCALE) < 1e-6:
        return False
    return abs(scale - settings.scale_warning_dismissed_for) > 1e-9


def model_key(model):
    return "%d:%s" % (model.index, model.name)


def is_mmd_root(self, obj):
    return getattr(obj, "mmd_type", "") == "ROOT"


def clip_poll(self, action):
    return not (
        action.name.startswith(ACTION_PREFIX + "Scene")
        or action.name.startswith(ACTION_PREFIX + "Light")
    )


def redraw_all():
    wm = bpy.context.window_manager
    if wm is None:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            area.tag_redraw()


def on_target_changed(self, context):
    name = self.target.name if self.target else None
    log.info("target for PMM model '%s' (%s) set to %s", self.pmm_name, self.key, name)
    if self.clip is not None:
        self.clip = None
    if self.shape_clip is not None:
        self.shape_clip = None


def follow_model_items(self, context):
    global _follow_items
    items = [("NONE", "None", "")]
    for target in context.scene.mmd_bridge.targets:
        if target.target is not None:
            items.append((target.key, target.pmm_name, ""))
    _follow_items = items
    return _follow_items


def find_root_for(model):
    roots = [
        o
        for o in bpy.data.objects
        if getattr(o, "mmd_type", "") == "ROOT" and target_usable(o)
    ]
    log.debug(
        "auto-match for PMM model '%s' / '%s' among roots: %s",
        model.name,
        model.name_en,
        [(o.name, o.mmd_root.name, o.mmd_root.name_e) for o in roots],
    )
    for obj in roots:
        if model.name and obj.mmd_root.name == model.name:
            return obj
    for obj in roots:
        if model.name_en and obj.mmd_root.name_e == model.name_en:
            return obj
    for obj in roots:
        if obj.name in (model.name, model.name_en):
            return obj
    for obj in roots:
        for name in (obj.mmd_root.name, obj.mmd_root.name_e, obj.name):
            if name and model.name and (name in model.name or model.name in name):
                return obj
    return None


def refresh_targets(settings, project):
    wanted = [model_key(m) for m in project.models]
    log.info("PMM contains %d model(s): %s", len(wanted), wanted)
    for index in reversed(range(len(settings.targets))):
        if settings.targets[index].key not in wanted:
            log.info("removing stale target %s", settings.targets[index].key)
            settings.targets.remove(index)
    present = {t.key: t for t in settings.targets}
    for model in project.models:
        key = model_key(model)
        item = present.get(key)
        if item is None:
            item = settings.targets.add()
            item.key = key
            item.pmm_name = model.name or model.name_en or key
            item.enabled = True
            present[key] = item
            log.info("added target slot %s", key)
        if item.target is not None and not target_usable(item.target):
            log.warning(
                "target '%s' for '%s' is stale (removed from the scene or has no armature); re-matching",
                item.target.name,
                item.pmm_name,
            )
            item.target = None
        if item.target is None:
            found = find_root_for(model)
            if found is not None:
                log.info("auto-matched '%s' -> %s", item.pmm_name, found.name)
                item.target = found
            else:
                log.warning(
                    "no mmd_tools root matched PMM model '%s'; assign one manually",
                    item.pmm_name,
                )


def find_armature(root):
    for child in root.children_recursive:
        if child.type == "ARMATURE":
            return child
    return None


def diagnose_bones(root, model):
    armature = find_armature(root)
    if armature is None:
        log.error(
            "root '%s' has no armature child; bone animation cannot be applied",
            root.name,
        )
        return
    names = set()
    for pose_bone in armature.pose.bones:
        mmd_bone = getattr(pose_bone, "mmd_bone", None)
        if mmd_bone is not None and mmd_bone.name_j:
            names.add(mmd_bone.name_j)
    matched = [n for n in model.bone_names if n in names]
    unmatched = [n for n in model.bone_names if n not in names]
    log.info(
        "armature '%s': %d/%d PMM bones match mmd_bone.name_j",
        armature.name,
        len(matched),
        len(model.bone_names),
    )
    if unmatched:
        log.warning("unmatched PMM bones (first 15): %s", unmatched[:15])
    animated = [n for n, keys in zip(model.bone_names, model.bones) if len(keys) > 1]
    log.info("bones with more than one keyframe: %d (%s)", len(animated), animated[:10])


def describe_action(label, obj):
    data = obj.animation_data if obj else None
    action = data.action if data else None
    if action is None:
        log.warning(
            "%s: '%s' has no action after import", label, obj.name if obj else None
        )
        return
    fcurves = iter_fcurves(action)
    log.info(
        "%s: '%s' action '%s' fcurves=%d frame_range=%s",
        label,
        obj.name,
        action.name,
        len(fcurves),
        tuple(action.frame_range),
    )


class CollectHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.summary = []
        self.warnings = []

    def emit(self, record):
        message = record.getMessage()
        if record.levelno >= logging.WARNING:
            self.warnings.append(message)
        elif message.startswith("----"):
            self.summary.append(message)


def mmd_tools_base():
    return bpy.types.MMD_TOOLS_OT_import_vmd.__module__.rsplit(".operators", 1)[0]


def mmd_module(name):
    return importlib.import_module(mmd_tools_base() + "." + name)


def mmd_tools_modules():
    return (
        mmd_module("core.vmd.importer"),
        mmd_module("core.model"),
        mmd_module("utils"),
    )


def is_bridge_action(action):
    return action is not None and action.name.startswith(ACTION_PREFIX)


def purge_actions(prefix):
    removed = 0
    for action in list(bpy.data.actions):
        if action.name.startswith(prefix) and action.users == 0:
            bpy.data.actions.remove(action)
            removed += 1
    if removed:
        log.info("purged %d unused action(s) with prefix '%s'", removed, prefix)


def clear_bridge_nla(owners):
    for owner in owners:
        data = owner.animation_data
        if data is None:
            continue
        for track in list(data.nla_tracks):
            stale = [strip for strip in track.strips if is_bridge_action(strip.action)]
            for strip in stale:
                track.strips.remove(strip)
            if stale and len(track.strips) == 0:
                data.nla_tracks.remove(track)
                log.info(
                    "removed empty NLA track '%s' from '%s'", track.name, owner.name
                )


def tag_actions(owners, base):
    for owner in owners:
        data = owner.animation_data
        action = data.action if data else None
        if action is not None and not action.name.startswith(ACTION_PREFIX):
            action.name = ACTION_PREFIX + base + "_" + owner.name


def iter_fcurves(action):
    fcurves = getattr(action, "fcurves", None)
    if fcurves is not None:
        return list(fcurves)
    found = []
    for layer in action.layers:
        for strip in layer.strips:
            for bag in getattr(strip, "channelbags", []):
                found.extend(bag.fcurves)
    return found


def animated_ids(obj):
    ids = [obj]
    data = getattr(obj, "data", None)
    if obj.type in {"CAMERA", "LIGHT"} and data is not None:
        ids.append(data)
    shape_keys = getattr(data, "shape_keys", None)
    if shape_keys is not None:
        ids.append(shape_keys)
    return ids


def collect_owners(objects):
    owners = []
    for obj in objects:
        for item in animated_ids(obj):
            if item not in owners:
                owners.append(item)
    return owners


def frame_span(frames):
    frames = list(frames)
    if not frames:
        return math.inf, -math.inf
    return min(frames), max(frames)


def model_frames(model):
    for keys in model.bones:
        for key in keys:
            yield key.frame
    for keys in model.morphs:
        for key in keys:
            yield key.frame


def snapshot_outside(owners, low, high, sources=None):
    saved = []
    for owner in owners:
        key = owner.as_pointer()
        if sources is not None and key in sources:
            action = sources[key]
        else:
            data = owner.animation_data
            action = data.action if data else None
        if action is None:
            continue
        for fcurve in iter_fcurves(action):
            if fcurve.data_path.startswith("constraints["):
                continue
            points = []
            for point in fcurve.keyframe_points:
                frame = point.co[0]
                if frame < low - FRAME_EPSILON or frame > high + FRAME_EPSILON:
                    points.append(
                        (
                            frame,
                            point.co[1],
                            point.interpolation,
                            point.easing,
                            tuple(point.handle_left),
                            tuple(point.handle_right),
                            point.handle_left_type,
                            point.handle_right_type,
                            point.type,
                        )
                    )
            if points:
                group = fcurve.group.name if fcurve.group else ""
                saved.append(
                    (owner, fcurve.data_path, fcurve.array_index, group, points)
                )
    return saved


def find_fcurve(owner, data_path, index):
    data = owner.animation_data
    action = data.action if data else None
    if action is None:
        return None
    for fcurve in iter_fcurves(action):
        if fcurve.data_path == data_path and fcurve.array_index == index:
            return fcurve
    return None


def create_fcurve(owner, data_path, index, group, frame):
    try:
        if group:
            owner.keyframe_insert(
                data_path=data_path, index=index, frame=frame, group=group
            )
        else:
            owner.keyframe_insert(data_path=data_path, index=index, frame=frame)
    except (TypeError, RuntimeError, ValueError, KeyError):
        return None
    return find_fcurve(owner, data_path, index)


def restore_points(saved):
    restored = 0
    for owner, data_path, index, group, points in saved:
        fcurve = find_fcurve(owner, data_path, index)
        if fcurve is None:
            fcurve = create_fcurve(owner, data_path, index, group, points[0][0])
        if fcurve is None:
            log.warning(
                "could not restore %d key(s) on '%s' %s[%d]",
                len(points),
                owner.name,
                data_path,
                index,
            )
            continue
        for (
            frame,
            value,
            interpolation,
            easing,
            left,
            right,
            left_type,
            right_type,
            key_type,
        ) in points:
            point = fcurve.keyframe_points.insert(
                frame, value, options={"FAST"}, keyframe_type=key_type
            )
            point.interpolation = interpolation
            point.easing = easing
            point.handle_left_type = left_type
            point.handle_right_type = right_type
            point.handle_left = left
            point.handle_right = right
        fcurve.update()
        restored += len(points)
    return restored


def reset_animation(targets, keep_keys=False):
    for obj in targets:
        if obj.animation_data:
            obj.animation_data.action = None
        data = getattr(obj, "data", None)
        if obj.type in {"CAMERA", "LIGHT"} and data is not None and data.animation_data:
            data.animation_data.action = None
        shape_keys = getattr(data, "shape_keys", None)
        if shape_keys is not None and not keep_keys:
            if shape_keys.animation_data:
                shape_keys.animation_data.action = None
            for block in shape_keys.key_blocks:
                if block.name != "Basis":
                    block.value = 0.0
        if obj.type == "ARMATURE":
            for bone in obj.pose.bones:
                bone.location = (0.0, 0.0, 0.0)
                bone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
                bone.rotation_euler = (0.0, 0.0, 0.0)
                bone.scale = (1.0, 1.0, 1.0)


def run_importer(tmp_path, settings, targets, action_name, detect_changes):
    importer_mod, _model_mod, utils_mod = mmd_tools_modules()
    wanted = dict(
        filepath=tmp_path,
        scale=current_scale(settings),
        bone_mapper=utils_mod.makePmxBoneMap,
        use_pose_mode=False,
        frame_margin=0,
        use_mirror=False,
        use_nla=False,
        detect_camera_changes=detect_changes,
        detect_light_changes=detect_changes,
    )
    accepted = inspect.signature(importer_mod.VMDImporter.__init__).parameters
    scene = bpy.context.scene
    original_frame = scene.frame_current
    root_logger = logging.getLogger()
    previous_level = root_logger.level
    previous_handlers = list(root_logger.handlers)
    collector = CollectHandler()
    for handler in previous_handlers:
        root_logger.removeHandler(handler)
    root_logger.addHandler(collector)
    root_logger.setLevel(logging.INFO)
    try:
        scene.frame_current = 0
        importer = importer_mod.VMDImporter(
            **{k: v for k, v in wanted.items() if k in accepted}
        )
        assign_params = inspect.signature(importer.assign).parameters
        for obj in targets:
            log.debug("assign -> %s (%s)", obj.name, obj.type)
            if "action_name" in assign_params:
                importer.assign(obj, action_name)
            else:
                importer.assign(obj)
    finally:
        root_logger.removeHandler(collector)
        for handler in previous_handlers:
            root_logger.addHandler(handler)
        root_logger.setLevel(previous_level)
        scene.frame_current = original_frame
    for line in collector.summary:
        log.info("mmd_tools: %s", line)
    if collector.warnings:
        log.warning(
            "mmd_tools reported %d warning(s); first 10: %s",
            len(collector.warnings),
            collector.warnings[:10],
        )
    return collector


def temp_root():
    path = os.path.join(tempfile.gettempdir(), TEMP_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def sweep_stale_temp(max_age=STALE_TEMP_SECONDS):
    try:
        root = temp_root()
        now = time.time()
        for name in os.listdir(root):
            if not (name.startswith(TEMP_PREFIX) and name.endswith(".vmd")):
                continue
            path = os.path.join(root, name)
            try:
                if now - os.path.getmtime(path) > max_age:
                    os.remove(path)
                    log.info("removed stale temp file %s", path)
            except OSError:
                pass
    except OSError as error:
        log.warning("temp sweep failed: %s", error)


def write_temp_vmd(data, prefix):
    handle, tmp_path = tempfile.mkstemp(suffix=".vmd", prefix=prefix, dir=temp_root())
    with os.fdopen(handle, "wb") as stream:
        stream.write(data)
    return tmp_path


def remove_file(path):
    for attempt in range(5):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.05 * (attempt + 1))
    log.warning("could not remove temp file %s; it will be swept on a later sync", path)


def morph_collections(root):
    mmd_root = getattr(root, "mmd_root", None)
    if mmd_root is None:
        return []
    names = (
        "vertex_morphs",
        "bone_morphs",
        "material_morphs",
        "uv_morphs",
        "group_morphs",
    )
    return [getattr(mmd_root, name) for name in names if hasattr(mmd_root, name)]


def resolve_morph_names(root, meshes, model):
    entries = []
    seen = set()
    for collection in morph_collections(root):
        for morph in collection:
            if morph.name not in seen:
                seen.add(morph.name)
                entries.append((morph.name, getattr(morph, "name_e", "")))
    for mesh in meshes:
        keys = getattr(getattr(mesh, "data", None), "shape_keys", None)
        if keys is None:
            continue
        for block in keys.key_blocks:
            if block.name not in seen:
                seen.add(block.name)
                entries.append((block.name, ""))
    by_english = {}
    for name, name_e in entries:
        if name_e and name_e not in by_english:
            by_english[name_e] = name
    resolved = {}
    for pmm_name in model.morph_names:
        if pmm_name in seen:
            resolved[pmm_name] = pmm_name
        elif pmm_name in by_english:
            resolved[pmm_name] = by_english[pmm_name]
        else:
            prefix = pmm_name.encode("cp932", errors="ignore")
            if prefix:
                candidates = [
                    n
                    for n, _ in entries
                    if n.encode("cp932", errors="ignore").startswith(prefix)
                ]
                if len(candidates) == 1:
                    resolved[pmm_name] = candidates[0]
    return resolved, [name for name, _ in entries]


def report_morphs(model, resolved, available):
    animated = [
        n
        for n, keys in zip(model.morph_names, model.morphs)
        if any(abs(k.weight) > 1e-6 for k in keys)
    ]
    log.info("morphs with non-zero keys: %d %s", len(animated), animated[:20])
    renamed = [(n, resolved[n]) for n in animated if n in resolved and resolved[n] != n]
    if renamed:
        log.info("morph name remap: %s", renamed)
    missing = [n for n in animated if n not in resolved]
    if missing:
        log.warning("PMM morphs with no Blender match: %s", missing)
        log.warning("Blender morph names available (first 80): %s", available[:80])


def sample_morph(keys, frame):
    if frame <= keys[0].frame:
        return keys[0].weight
    if frame >= keys[-1].frame:
        return keys[-1].weight
    for a, b in zip(keys, keys[1:]):
        if a.frame <= frame <= b.frame:
            span = b.frame - a.frame
            if span == 0:
                return a.weight
            return a.weight + (b.weight - a.weight) * (frame - a.frame) / span
    return keys[-1].weight


def group_morph_table(root):
    mmd_root = getattr(root, "mmd_root", None)
    groups = getattr(mmd_root, "group_morphs", None)
    if groups is None:
        return {}
    return {group.name: group for group in groups}


def uv_morph_table(root):
    mmd_root = getattr(root, "mmd_root", None)
    morphs = getattr(mmd_root, "uv_morphs", None)
    if morphs is None:
        return {}
    return {morph.name: morph for morph in morphs}


def expand_group_morph(groups, name, factor, depth=0):
    group = groups.get(name)
    if group is None or depth > 6:
        return []
    leaves = []
    for offset in group.data:
        scaled = factor * offset.factor
        if offset.morph_type == "group_morphs":
            leaves.extend(expand_group_morph(groups, offset.name, scaled, depth + 1))
        else:
            leaves.append((offset.morph_type, offset.name, scaled))
    return leaves


def rewrite_fcurve(fcurve, frames, values):
    points = fcurve.keyframe_points
    try:
        points.clear()
    except AttributeError:
        while len(points):
            points.remove(points[0], fast=True)
    for frame, value in zip(frames, values):
        point = points.insert(frame, value, options={"FAST"})
        point.interpolation = "LINEAR"
    fcurve.update()


def uv_key_name(leaf):
    raw = (UV_KEY_PREFIX + leaf).encode("utf-8")[:63]
    return raw.decode("utf-8", errors="ignore")


def uv_attribute_name(index):
    return "%s%d" % (UV_ATTRIBUTE_PREFIX, index)


def uv_group_axis(name, morph_name):
    if not name.startswith("UV_") or len(name) < 6:
        return None
    if name[3:-2] != morph_name or name[-2] not in "+-" or name[-1] not in "XYZW":
        return None
    return name[-2], "XYZW".index(name[-1])


def uv_offset_map(mesh_obj, morph):
    offsets = {}
    if morph.data_type == "VERTEX_GROUP":
        scale = morph.vertex_group_scale
        axes = {}
        for group in mesh_obj.vertex_groups:
            found = uv_group_axis(group.name, morph.name)
            if found is not None:
                axes[group.index] = found
        if axes:
            for vertex in mesh_obj.data.vertices:
                for member in vertex.groups:
                    entry = axes.get(member.group)
                    if entry is None or member.weight <= 0:
                        continue
                    sign, axis = entry
                    slot = offsets.setdefault(vertex.index, [0.0, 0.0, 0.0, 0.0])
                    slot[axis] += (
                        -member.weight * scale if sign == "-" else member.weight * scale
                    )
    else:
        for item in morph.data:
            slot = offsets.setdefault(item.index, [0.0, 0.0, 0.0, 0.0])
            for axis in range(4):
                slot[axis] += item.offset[axis]
    return offsets


def uv_natively_bound(mesh_obj, morph_name):
    for modifier in mesh_obj.modifiers:
        if (
            modifier.type == "UV_WARP"
            and modifier.name.startswith("mmd_bind")
            and uv_group_axis(modifier.vertex_group, morph_name)
        ):
            return True
    return False


def strip_uv_nodes(material):
    tree = getattr(material, "node_tree", None)
    if tree is None:
        return
    nodes = tree.nodes
    for head in [n for n in nodes if n.name.startswith(UV_NODE_PREFIX + "_Add0")]:
        tex = nodes.get(head.get("mmd_bridge_tex", ""))
        if tex is not None and head.inputs[0].is_linked:
            tree.links.new(head.inputs[0].links[0].from_socket, tex.inputs["Vector"])
    for node in [n for n in nodes if n.name.startswith(UV_NODE_PREFIX)]:
        nodes.remove(node)
    anim = tree.animation_data
    if anim is not None:
        for fcurve in [fc for fc in anim.drivers if UV_NODE_PREFIX in fc.data_path]:
            anim.drivers.remove(fcurve)


def clear_uv_channels(mesh_obj):
    mesh = mesh_obj.data
    for attribute in [
        a for a in mesh.attributes if a.name.startswith(UV_ATTRIBUTE_PREFIX)
    ]:
        mesh.attributes.remove(attribute)
    for material in mesh.materials:
        if material is not None:
            strip_uv_nodes(material)


def add_uv_driver(tree, weight_node, key, block_name):
    path = 'nodes["%s"].outputs[0].default_value' % weight_node.name
    fcurve = tree.driver_add(path)
    driver = fcurve.driver
    driver.type = "SUM"
    variable = driver.variables.new()
    variable.name = "w"
    variable.type = "SINGLE_PROP"
    target = variable.targets[0]
    target.id_type = "KEY"
    target.id = key
    escaped = block_name.replace("\\", "\\\\").replace('"', '\\"')
    target.data_path = 'key_blocks["%s"].value' % escaped


def find_base_texture(nodes):
    tex = nodes.get("mmd_base_tex")
    if tex is not None and tex.bl_idname == "ShaderNodeTexImage":
        return tex
    for node in nodes:
        if node.bl_idname != "ShaderNodeTexImage" or node.image is None:
            continue
        if node.name.startswith(("mmd_toon_tex", "mmd_sphere_tex", UV_NODE_PREFIX)):
            continue
        return node
    return None


def build_uv_nodes(material, specs):
    tree = getattr(material, "node_tree", None)
    if tree is None:
        return False
    nodes, links = tree.nodes, tree.links
    tex = find_base_texture(nodes)
    if tex is None:
        return False
    vector = tex.inputs["Vector"]
    if vector.is_linked:
        previous = vector.links[0].from_socket
    else:
        coord = nodes.new("ShaderNodeTexCoord")
        coord.name = UV_NODE_PREFIX + "_Coord"
        previous = coord.outputs["UV"]
    for number, spec in enumerate(specs):
        attribute = nodes.new("ShaderNodeAttribute")
        attribute.name = "%s_Attr%d" % (UV_NODE_PREFIX, number)
        attribute.attribute_type = "GEOMETRY"
        attribute.attribute_name = spec["attribute"]
        weight = nodes.new("ShaderNodeValue")
        weight.name = "%s_Weight%d" % (UV_NODE_PREFIX, number)
        scale = nodes.new("ShaderNodeVectorMath")
        scale.name = "%s_Scale%d" % (UV_NODE_PREFIX, number)
        scale.operation = "SCALE"
        add = nodes.new("ShaderNodeVectorMath")
        add.name = "%s_Add%d" % (UV_NODE_PREFIX, number)
        add.operation = "ADD"
        if number == 0:
            add["mmd_bridge_tex"] = tex.name
        links.new(attribute.outputs["Vector"], scale.inputs[0])
        links.new(weight.outputs[0], scale.inputs[3])
        links.new(previous, add.inputs[0])
        links.new(scale.outputs[0], add.inputs[1])
        previous = add.outputs[0]
        add_uv_driver(tree, weight, spec["key"], spec["block"])
    links.new(previous, vector)
    return True


def touched_material_indices(mesh, vertex_ids):
    found = set()
    for polygon in mesh.polygons:
        if polygon.material_index in found:
            continue
        for vertex in polygon.vertices:
            if vertex in vertex_ids:
                found.add(polygon.material_index)
                break
    return found


def uv_leaf_names(model, resolved, groups, uv_morphs):
    leaves = []
    for name, mkeys in zip(model.morph_names, model.morphs):
        if not any(abs(k.weight) > 1e-6 for k in mkeys):
            continue
        target = resolved.get(name)
        if target is None:
            continue
        if target in groups:
            for kind, leaf, factor in expand_group_morph(groups, target, 1.0):
                if kind == "uv_morphs" and abs(factor) > 1e-9 and leaf not in leaves:
                    leaves.append(leaf)
        elif target in uv_morphs and target not in leaves:
            leaves.append(target)
    return leaves


def prepare_uv_channels(root, meshes, model, resolved, enabled):
    for mesh_obj in meshes:
        if mesh_obj.type == "MESH" and mesh_obj.mode != "EDIT":
            clear_uv_channels(mesh_obj)
    if not enabled:
        return []
    uv_morphs = uv_morph_table(root)
    if not uv_morphs:
        return []
    leaves = uv_leaf_names(model, resolved, group_morph_table(root), uv_morphs)
    channels = []
    per_mesh = {}
    for leaf in leaves:
        morph = uv_morphs[leaf]
        if morph.uv_index != 0:
            log.warning(
                "UV morph '%s' targets additional UV %d; only the base UV is supported",
                leaf,
                morph.uv_index,
            )
            continue
        for mesh_obj in meshes:
            if mesh_obj.type != "MESH" or mesh_obj.mode == "EDIT":
                continue
            if uv_natively_bound(mesh_obj, leaf):
                log.info(
                    "UV morph '%s' on '%s' is bound by mmd_tools (UV Warp modifiers); leaving it to mmd_tools",
                    leaf,
                    mesh_obj.name,
                )
                continue
            offsets = uv_offset_map(mesh_obj, morph)
            usable = {
                i: (o[0], -o[1])
                for i, o in offsets.items()
                if abs(o[0]) > 1e-9 or abs(o[1]) > 1e-9
            }
            if not usable:
                log.warning(
                    "UV morph '%s' has no X/Y offsets on '%s'", leaf, mesh_obj.name
                )
                continue
            number = per_mesh.get(mesh_obj.as_pointer(), 0)
            per_mesh[mesh_obj.as_pointer()] = number + 1
            channels.append(
                {"leaf": leaf, "mesh": mesh_obj, "offsets": usable, "number": number}
            )
    by_mesh = {}
    for channel in channels:
        mesh_obj = channel["mesh"]
        mesh = mesh_obj.data
        count = len(mesh.vertices)
        attribute = uv_attribute_name(channel["number"])
        flat = [0.0] * (3 * count)
        for index, (dx, dy) in channel["offsets"].items():
            if 0 <= index < count:
                flat[3 * index] = dx
                flat[3 * index + 1] = dy
        created = mesh.attributes.new(
            name=attribute, type="FLOAT_VECTOR", domain="POINT"
        )
        created.data.foreach_set("vector", flat)
        if mesh.shape_keys is None:
            mesh_obj.shape_key_add(name="Basis", from_mix=False)
        block_name = uv_key_name(channel["leaf"])
        block = mesh.shape_keys.key_blocks.get(block_name)
        if block is None:
            block = mesh_obj.shape_key_add(name=block_name, from_mix=False)
            block.value = 0.0
        block.slider_min = -UV_SLIDER_LIMIT
        block.slider_max = UV_SLIDER_LIMIT
        channel["attribute"] = attribute
        channel["block"] = block.name
        channel["key"] = mesh.shape_keys
        by_mesh.setdefault(mesh_obj.as_pointer(), []).append(channel)
        mesh.update()
    applied = 0
    for group in by_mesh.values():
        mesh = group[0]["mesh"].data
        indices = set()
        for channel in group:
            channel["materials"] = touched_material_indices(
                mesh, set(channel["offsets"])
            )
            indices.update(channel["materials"])
        for index in sorted(indices):
            if index >= len(mesh.materials) or mesh.materials[index] is None:
                continue
            material = mesh.materials[index]
            specs = [c for c in group if index in c["materials"]]
            if build_uv_nodes(material, specs):
                applied += 1
                log.info(
                    "UV morph shader nodes added to material '%s': %s",
                    material.name,
                    [c["leaf"] for c in specs],
                )
            else:
                log.warning(
                    "material '%s' has no base texture node; UV morph skipped for %s",
                    material.name,
                    [c["leaf"] for c in specs],
                )
    log.info(
        "UV morph channels: %d on %d mesh(es), %d material(s) patched",
        len(channels),
        len(by_mesh),
        applied,
    )
    return channels


def collect_contributions(model, resolved, groups, uv_morphs):
    contributions = {}
    group_driven = set()
    skipped = []
    for name, mkeys in zip(model.morph_names, model.morphs):
        if not any(abs(k.weight) > 1e-6 for k in mkeys):
            continue
        target = resolved.get(name)
        if target is None:
            continue
        if target in groups:
            for kind, leaf, factor in expand_group_morph(groups, target, 1.0):
                if kind in ("vertex_morphs", "uv_morphs"):
                    contributions.setdefault((kind, leaf), []).append((mkeys, factor))
                    group_driven.add((kind, leaf))
                else:
                    skipped.append((name, kind, leaf, round(factor, 3)))
        elif target in uv_morphs:
            contributions.setdefault(("uv_morphs", target), []).append((mkeys, 1.0))
        else:
            contributions.setdefault(("vertex_morphs", target), []).append((mkeys, 1.0))
    return contributions, group_driven, skipped


def ensure_morph_keys(hosts, model, resolved, root):
    groups = group_morph_table(root)
    contributions, group_driven, skipped = collect_contributions(
        model, resolved, groups, uv_morph_table(root)
    )
    written = []
    driven_skipped = []
    for host in hosts:
        keys = getattr(getattr(host, "data", None), "shape_keys", None)
        if keys is None:
            continue
        anim = keys.animation_data
        action = anim.action if anim else None
        existing = (
            {fc.data_path for fc in iter_fcurves(action)}
            if action is not None
            else set()
        )
        driven = {fc.data_path for fc in anim.drivers} if anim is not None else set()
        blocks = {block.name: block for block in keys.key_blocks}
        for (kind, leaf), items in contributions.items():
            block = blocks.get(leaf if kind == "vertex_morphs" else uv_key_name(leaf))
            if block is None:
                continue
            path = block.path_from_id("value")
            if path in driven:
                driven_skipped.append(leaf)
                continue
            if (kind, leaf) not in group_driven and path in existing:
                continue
            frames = sorted({k.frame for mkeys, _ in items for k in mkeys})
            values = []
            for frame in frames:
                total = sum(
                    factor * sample_morph(mkeys, frame) for mkeys, factor in items
                )
                values.append(min(max(total, block.slider_min), block.slider_max))
            block.value = values[0]
            try:
                keys.keyframe_insert(data_path=path, frame=frames[0])
            except (RuntimeError, TypeError, ValueError) as error:
                log.warning("could not key shape key '%s': %s", block.name, error)
                continue
            anim = keys.animation_data
            action = anim.action if anim else None
            fcurve = (
                next((fc for fc in iter_fcurves(action) if fc.data_path == path), None)
                if action
                else None
            )
            if fcurve is None:
                log.warning("could not create an fcurve for shape key '%s'", block.name)
                continue
            rewrite_fcurve(fcurve, frames, values)
            written.append(block.name)
    if written:
        log.info(
            "keyed %d shape key(s) directly (group morph expansion, UV channels, or morphs mmd_tools skipped): %s",
            len(written),
            written[:20],
        )
    if driven_skipped:
        log.info(
            "shape keys driven by mmd_tools morph binding were left to their drivers: %s",
            sorted(set(driven_skipped))[:20],
        )
    if skipped:
        log.warning(
            "group morph offsets that are not shape keys or UV morphs were not applied (material/bone): %s",
            skipped[:20],
        )
    return written


def bind_slot(target, action):
    if not hasattr(target, "action_slot"):
        return
    suitable = getattr(target, "action_suitable_slots", None)
    slot = None
    if suitable is not None and len(suitable):
        slot = suitable[0]
    else:
        slots = getattr(action, "slots", None)
        if slots is not None and len(slots):
            slot = slots[0]
    if slot is None:
        return
    try:
        target.action_slot = slot
    except (AttributeError, TypeError, RuntimeError, ValueError):
        pass


def strips_using(anim, action):
    found = []
    for track in anim.nla_tracks:
        for strip in track.strips:
            if strip.action == action:
                found.append((track, strip))
    return found


def fit_strip(strip, action):
    start, end = action.frame_range
    if end <= start:
        end = start + 1.0
    try:
        strip.action_frame_end = end
        strip.action_frame_start = start
    except (AttributeError, TypeError, RuntimeError, ValueError):
        try:
            strip.action_frame_start = start
            strip.action_frame_end = end
        except (AttributeError, TypeError, RuntimeError, ValueError):
            log.warning("could not fit NLA strip '%s' to %s-%s", strip.name, start, end)


def find_bridge_clip(owner, track_name):
    anim = owner.animation_data
    if anim is None:
        return None
    for track in anim.nla_tracks:
        if track.name == track_name:
            for strip in track.strips:
                if strip.action is not None and is_bridge_action(strip.action):
                    return strip.action
    return None


def find_bridge_clip_any(owner, track_names):
    for track_name in track_names:
        found = find_bridge_clip(owner, track_name)
        if found is not None:
            return found
    return None


def primary_key_owner(key_owners, placeholder):
    placeholder_key = getattr(getattr(placeholder, "data", None), "shape_keys", None)
    pool = [
        k for k in key_owners if placeholder_key is None or k != placeholder_key
    ] or list(key_owners)
    if not pool:
        return None
    return max(pool, key=lambda k: len(k.key_blocks))


def add_clip_strip(anim, action, track_name):
    start = int(math.floor(action.frame_range[0]))
    reuse = next((t for t in anim.nla_tracks if t.name == track_name), None)
    candidates = [reuse] if reuse is not None else []
    candidates.append(None)
    for track in candidates:
        created = track is None
        if created:
            track = anim.nla_tracks.new()
            track.name = track_name
        try:
            strip = track.strips.new(action.name, start, action)
        except (RuntimeError, ValueError, TypeError) as error:
            log.warning("could not add NLA strip for '%s': %s", action.name, error)
            if created:
                anim.nla_tracks.remove(track)
            continue
        bind_slot(strip, action)
        fit_strip(strip, action)
        log.info(
            "clip '%s' placed on NLA track '%s' at frame %d",
            action.name,
            track.name,
            start,
        )
        return strip
    return None


def place_clip(owner, clip, track_name, clip_name, active_before, keep_active=False):
    anim = owner.animation_data
    fresh = anim.action if anim else None
    if fresh is None:
        log.warning("clip: importer left no action on '%s'", owner.name)
        return clip
    if clip is not None and clip != fresh:
        name = clip.name
        fake = clip.use_fake_user
        clip.name = name + ".old"
        clip.user_remap(fresh)
        fresh.name = name
        fresh.use_fake_user = fake
        clip.use_fake_user = False
        if clip.users == 0:
            bpy.data.actions.remove(clip)
        log.info("clip '%s' content replaced", name)
        clip = fresh
    elif clip is None:
        fresh.name = clip_name
        clip = fresh
        log.info("clip '%s' created", clip.name)
    existing = strips_using(anim, clip)
    for _track, strip in existing:
        bind_slot(strip, clip)
        fit_strip(strip, clip)
    if keep_active:
        bind_slot(anim, clip)
        log.info("clip '%s' stays the active action on '%s'", clip.name, owner.name)
        return clip
    if not existing and add_clip_strip(anim, clip, track_name) is None:
        log.warning(
            "clip '%s' kept as the active action because no NLA strip could be added",
            clip.name,
        )
        return clip
    restore = None
    if (
        active_before is not None
        and active_before != clip
        and not is_bridge_action(active_before)
    ):
        restore = active_before
    anim.action = restore
    if restore is not None:
        bind_slot(anim, restore)
        log.info("restored active action '%s' on '%s'", restore.name, owner.name)
    return clip


def release_stash(stash, restore):
    for owner, action, fake in stash.values():
        if action is None:
            continue
        try:
            action.use_fake_user = fake
        except ReferenceError:
            continue
        if restore and not is_bridge_action(action):
            anim = owner.animation_data
            if anim is not None and (
                anim.action is None or is_bridge_action(anim.action)
            ):
                anim.action = action


def import_physics_clip(item, armature, settings, label):
    path = bpy.path.abspath(item.physics_vmd) if item.physics_vmd else ""
    if not path:
        return False
    if not os.path.isfile(path):
        log.warning("physics VMD not found: %s", path)
        return False
    anim = armature.animation_data or armature.animation_data_create()
    active_before = anim.action
    stash = {
        armature.as_pointer(): (
            armature,
            active_before,
            active_before.use_fake_user if active_before else False,
        )
    }
    if active_before is not None:
        active_before.use_fake_user = True
    track = CLIP_TRACK_NAME + "_" + label + "_physics"
    existing = find_bridge_clip(armature, track)
    placed = False
    try:
        anim.action = None
        run_importer(
            path, settings, [armature], ACTION_PREFIX + label + "_physics", False
        )
        place_clip(
            armature,
            existing,
            track,
            ACTION_PREFIX + label + "_physics_clip",
            active_before,
        )
        placed = True
    finally:
        release_stash(stash, restore=not placed)
    if settings.physics_disable_rigid_body:
        world = bpy.context.scene.rigidbody_world
        if world is not None and world.enabled:
            world.enabled = False
            log.info(
                "rigid body world disabled so it does not fight the baked physics clip"
            )
    log.info("baked physics VMD applied as a clip above the PMM clip: %s", path)
    return True


def import_model(root, model, settings, item):
    if not target_usable(root):
        raise RuntimeError(
            "target '%s' has no armature or is not in the scene; reassign the model"
            % root.name
        )
    _importer_mod, model_mod, _utils_mod = mmd_tools_modules()
    rig = model_mod.Model(root)
    armature = rig.armature()
    placeholder = rig.morph_slider.placeholder()
    meshes = list(rig.meshes())
    targets = [root]
    for extra in [armature, placeholder] + meshes:
        if extra is not None and extra not in targets:
            targets.append(extra)
    log.info(
        "importing '%s' into root '%s', targets: %s",
        model.name,
        root.name,
        [(o.name, o.type) for o in targets],
    )
    diagnose_bones(root, model)
    morph_map, morph_available = resolve_morph_names(root, meshes, model)
    report_morphs(model, morph_map, morph_available)
    data = build_vmd(model, settings.interpolation_layout, morph_map)
    try:
        prepare_uv_channels(root, meshes, model, morph_map, settings.uv_morphs)
    except Exception as error:
        log.error("UV morph setup failed: %s\n%s", error, traceback.format_exc())
    owners = collect_owners(targets)
    label = model.name or model.name_en or "model"
    use_clips = bool(settings.use_clips) and armature is not None
    arm_track = CLIP_TRACK_NAME + "_" + label + "_motion"
    shape_track = CLIP_TRACK_NAME + "_" + label + "_shapekey"
    legacy_arm_track = CLIP_TRACK_NAME + "_" + label
    legacy_shape_track = legacy_arm_track + "_shapes"
    key_owners = [o for o in owners if hasattr(o, "key_blocks")] if use_clips else []
    primary_keys = primary_key_owner(key_owners, placeholder)
    arm_clip = item.clip if use_clips else None
    stash = {}
    sources = {}
    if use_clips:
        for owner in [armature] + key_owners:
            anim = owner.animation_data or owner.animation_data_create()
            action = anim.action
            stash[owner.as_pointer()] = (
                owner,
                action,
                action.use_fake_user if action else False,
            )
            if action is not None:
                action.use_fake_user = True
        sources[armature.as_pointer()] = arm_clip
        for owner in key_owners:
            found = find_bridge_clip_any(owner, (shape_track, legacy_shape_track))
            if (
                primary_keys is not None
                and owner == primary_keys
                and item.shape_clip is not None
            ):
                found = item.shape_clip
            sources[owner.as_pointer()] = found
    low, high = frame_span(model_frames(model))
    kept = snapshot_outside(owners, low, high, sources if use_clips else None)
    log.info(
        "PMM frame span for '%s': %s-%s; keeping keys outside it on %d curve(s); motion clip=%s; shapekey clip=%s",
        model.name,
        low,
        high,
        len(kept),
        arm_clip.name if arm_clip else None,
        item.shape_clip.name if item.shape_clip else None,
    )
    plain_owners = [
        o for o in owners if not (use_clips and (o == armature or o in key_owners))
    ]
    clear_bridge_nla(plain_owners)
    reset_animation(targets, keep_keys=use_clips)
    for owner in key_owners:
        owner.animation_data.action = None
    purge_actions(ACTION_PREFIX)
    tmp_path = write_temp_vmd(data, TEMP_PREFIX)
    placed = False
    try:
        try:
            run_importer(tmp_path, settings, targets, ACTION_PREFIX + label, False)
        finally:
            remove_file(tmp_path)
        ensure_morph_keys(meshes, model, morph_map, root)
        restored = restore_points(kept)
        tag_actions(plain_owners, label)
        log.info("restored %d preserved key(s) for '%s'", restored, model.name)
        describe_action("armature", armature)
        if use_clips:
            arm_ptr = armature.as_pointer()
            active_arm = stash[arm_ptr][1]
            keep_active = (
                arm_clip is not None
                and active_arm is not None
                and active_arm == arm_clip
            )
            item.clip = place_clip(
                armature,
                arm_clip,
                arm_track,
                ACTION_PREFIX + label + "_motion_clip",
                active_arm,
                keep_active,
            )
            for owner in key_owners:
                ptr = owner.as_pointer()
                is_primary = primary_keys is not None and owner == primary_keys
                clip_name = (
                    ACTION_PREFIX
                    + label
                    + ("_shapekey_clip" if is_primary else "_shapekey_" + owner.name)
                )
                shape_result = place_clip(
                    owner, sources[ptr], shape_track, clip_name, stash[ptr][1]
                )
                if is_primary and shape_result is not None:
                    item.shape_clip = shape_result
            placed = True
    finally:
        release_stash(stash, restore=not placed)
    if settings.use_physics_vmd and armature is not None:
        try:
            import_physics_clip(item, armature, settings, label)
        except Exception as error:
            log.error("physics clip failed: %s\n%s", error, traceback.format_exc())
    if settings.update_scene_settings:
        scene = bpy.context.scene
        start, end = scene.frame_start, scene.frame_end
        try:
            auto_scene = mmd_module("auto_scene_setup")
            auto_scene.setupFrameRanges()
            auto_scene.setupFps()
        except Exception as error:
            log.warning("could not update scene settings: %s", error)
        scene.frame_start = min(start, scene.frame_start)
        scene.frame_end = max(end, scene.frame_end)
        log.info("scene frame range kept at %d-%d", scene.frame_start, scene.frame_end)


def ensure_collection(scene, name):
    collection = bpy.data.collections.get(name)
    if collection is None:
        collection = bpy.data.collections.new(name)
        log.info("created collection '%s'", name)
    if collection not in scene.collection.children_recursive:
        scene.collection.children.link(collection)
    return collection


def move_to_collection(obj, collection):
    for owner in list(obj.users_collection):
        if owner != collection:
            owner.objects.unlink(obj)
    if obj.name not in collection.objects:
        collection.objects.link(obj)


def find_rig_root(collection, mmd_type):
    for obj in collection.all_objects:
        if obj.type == "EMPTY" and getattr(obj, "mmd_type", "") == mmd_type:
            return obj
    return None


def child_of_type(root, object_type):
    for child in root.children:
        if child.type == object_type:
            return child
    return None


def remove_rig(root):
    for child in list(root.children_recursive):
        data = child.data
        kind = child.type
        bpy.data.objects.remove(child, do_unlink=True)
        if data is not None and data.users == 0:
            if kind == "CAMERA":
                bpy.data.cameras.remove(data)
            elif kind == "LIGHT":
                bpy.data.lights.remove(data)
    bpy.data.objects.remove(root, do_unlink=True)


def rig_needs_rebuild(root, scale):
    return abs(float(root.get("mmd_bridge_scale", -1.0)) - scale) > 1e-9


def ensure_camera_rig(scene, settings):
    collection = ensure_collection(scene, CAMERA_COLLECTION)
    root = find_rig_root(collection, "CAMERA")
    scale = current_scale(settings)
    if root is not None and rig_needs_rebuild(root, scale):
        log.info(
            "MMD camera rig '%s' was built for a different scale; rebuilding for %.4f",
            root.name,
            scale,
        )
        remove_rig(root)
        root = None
    if root is None:
        camera_object = bpy.data.objects.new(
            "MMD_Camera_cam", bpy.data.cameras.new("MMD_Camera")
        )
        collection.objects.link(camera_object)
        rig = mmd_module("core.camera").MMDCamera.convertToMMDCamera(
            camera_object, scale=current_scale(settings)
        )
        root = rig.object()
        root["mmd_bridge_scale"] = scale
        log.info(
            "created MMD camera rig '%s' in collection '%s'",
            root.name,
            CAMERA_COLLECTION,
        )
    move_to_collection(root, collection)
    camera = child_of_type(root, "CAMERA")
    if camera is None:
        raise RuntimeError("MMD camera rig '%s' has no camera child" % root.name)
    move_to_collection(camera, collection)
    return root, camera


def ensure_light_rig(scene, settings):
    collection = ensure_collection(scene, LIGHT_COLLECTION)
    root = find_rig_root(collection, "LIGHT")
    scale = current_scale(settings)
    if root is not None and rig_needs_rebuild(root, scale):
        log.info(
            "MMD light rig '%s' was built for a different scale; rebuilding for %.4f",
            root.name,
            scale,
        )
        remove_rig(root)
        root = None
    if root is None:
        light_object = bpy.data.objects.new(
            "MMD_Sun_light", bpy.data.lights.new("MMD_Sun", type="SUN")
        )
        collection.objects.link(light_object)
        rig = mmd_module("core.light").MMDLight.convertToMMDLight(
            light_object, scale=current_scale(settings)
        )
        root = rig.object()
        root["mmd_bridge_scale"] = scale
        log.info(
            "created MMD light rig '%s' in collection '%s'", root.name, LIGHT_COLLECTION
        )
    move_to_collection(root, collection)
    light = child_of_type(root, "LIGHT")
    if light is None:
        raise RuntimeError("MMD light rig '%s' has no light child" % root.name)
    move_to_collection(light, collection)
    return root, light


def pose_bone_name(armature, mmd_name):
    for pose_bone in armature.pose.bones:
        mmd_bone = getattr(pose_bone, "mmd_bone", None)
        if mmd_bone is not None and mmd_bone.name_j == mmd_name:
            return pose_bone.name
    if mmd_name in armature.pose.bones:
        return mmd_name
    return None


def resolve_follow(project, settings):
    plan = {}
    if settings.follow_mode == "OFF":
        return plan
    items = {t.key: t for t in settings.targets}
    if settings.follow_mode == "MANUAL":
        item = items.get(settings.follow_model)
        if item is None or item.target is None:
            log.warning("manual follow: no model selected")
            return plan
        armature = find_armature(item.target)
        if armature is None or settings.follow_bone_name not in armature.pose.bones:
            log.warning(
                "manual follow: bone '%s' not found on the selected model",
                settings.follow_bone_name,
            )
            return plan
        plan[MANUAL_FOLLOW] = (armature, settings.follow_bone_name)
        log.info("manual follow: %s / %s", armature.name, settings.follow_bone_name)
        return plan
    by_index = {m.index: m for m in project.models}
    for key in project.camera_keys:
        if key.parent_model < 0 or key.parent_bone < 0:
            continue
        pair = (key.parent_model, key.parent_bone)
        if pair in plan:
            continue
        plan[pair] = None
        model = by_index.get(key.parent_model)
        if model is None:
            log.warning(
                "camera follows model %d which is not in the PMM", key.parent_model
            )
            continue
        item = items.get(model_key(model))
        if item is None or item.target is None:
            log.warning(
                "camera follows '%s' but that model has no Blender target assigned",
                model.name,
            )
            continue
        armature = find_armature(item.target)
        if armature is None:
            log.warning("camera follow: root '%s' has no armature", item.target.name)
            continue
        if key.parent_bone >= len(model.bone_names):
            log.warning(
                "camera follow: bone index %d out of range for '%s'",
                key.parent_bone,
                model.name,
            )
            continue
        mmd_name = model.bone_names[key.parent_bone]
        bone = pose_bone_name(armature, mmd_name)
        if bone is None:
            log.warning(
                "camera follow: bone '%s' not found on armature '%s'",
                mmd_name,
                armature.name,
            )
            continue
        plan[pair] = (armature, bone)
        log.info(
            "camera follow resolved: model %d bone %d -> %s / %s",
            pair[0],
            pair[1],
            armature.name,
            bone,
        )
    return plan


def make_constant(obj, path):
    action = obj.animation_data.action if obj.animation_data else None
    if action is None:
        log.warning("no action on '%s' to set constant interpolation", obj.name)
        return
    for fcurve in iter_fcurves(action):
        if fcurve.data_path == path:
            for point in fcurve.keyframe_points:
                point.interpolation = "CONSTANT"


def apply_follow(root, project, plan, settings):
    for constraint in [c for c in root.constraints if c.name.startswith(FOLLOW_PREFIX)]:
        root.constraints.remove(constraint)
    keys = sorted(project.camera_keys, key=lambda k: k.frame)
    for pair, resolved in plan.items():
        if resolved is None:
            continue
        armature, bone = resolved
        manual = pair == MANUAL_FOLLOW
        constraint = root.constraints.new(type="CHILD_OF")
        constraint.name = FOLLOW_PREFIX + ("_manual" if manual else "_%d_%d" % pair)
        constraint.target = armature
        constraint.subtarget = bone
        constraint.use_rotation_x = settings.follow_rotation
        constraint.use_rotation_y = settings.follow_rotation
        constraint.use_rotation_z = settings.follow_rotation
        constraint.use_scale_x = False
        constraint.use_scale_y = False
        constraint.use_scale_z = False
        constraint.set_inverse_pending = False
        constraint.inverse_matrix = Matrix.Identity(4)
        constraint.influence = 1.0
        if not manual:
            path = 'constraints["%s"].influence' % constraint.name
            for key in keys:
                active = (key.parent_model, key.parent_bone) == pair
                constraint.influence = 1.0 if active else 0.0
                constraint.keyframe_insert("influence", frame=key.frame)
            make_constant(root, path)
        log.info(
            "follow constraint '%s' -> %s / %s (rotation=%s)",
            constraint.name,
            armature.name,
            bone,
            settings.follow_rotation,
        )


def import_camera(scene, project, settings):
    root, camera = ensure_camera_rig(scene, settings)
    data = build_scene_vmd(project.camera_keys)
    tmp_path = write_temp_vmd(data, TEMP_PREFIX + "camera_")
    try:
        run_importer(tmp_path, settings, [camera], ACTION_PREFIX + "Scene", True)
    finally:
        remove_file(tmp_path)
    describe_action("camera root", root)
    describe_action("camera distance", camera)
    apply_follow(root, project, resolve_follow(project, settings), settings)
    if settings.set_active_camera and scene.camera != camera:
        scene.camera = camera
        log.info("scene camera set to '%s'", camera.name)


def import_light(scene, project, settings):
    root, light = ensure_light_rig(scene, settings)
    ordered = sorted(project.light_keys, key=lambda k: k.frame)
    data = light.data
    for key in ordered:
        data.color = key.color
        data.keyframe_insert("color", frame=key.frame)
        light.location = Vector(key.direction).xzy * -1
        light.keyframe_insert("location", frame=key.frame)
    for owner, label in ((data, "Light_color"), (light, "Light_loc")):
        anim = owner.animation_data
        action = anim.action if anim else None
        if action is None:
            log.warning("light: '%s' has no action after keying", owner.name)
            continue
        action.name = ACTION_PREFIX + label
        for fcurve in iter_fcurves(action):
            points = sorted(fcurve.keyframe_points, key=lambda p: p.co[0])
            for index, point in enumerate(points):
                point.interpolation = "LINEAR"
                if (
                    index + 1 < len(points)
                    and points[index + 1].co[0] - point.co[0] <= 1.0
                ):
                    point.interpolation = "CONSTANT"
        log.info(
            "light: '%s' action '%s' fcurves=%d",
            owner.name,
            action.name,
            len(iter_fcurves(action)),
        )


def import_scene_elements(scene, project, settings):
    if project.scene_error:
        log.warning("camera/light section could not be read: %s", project.scene_error)
        return 0
    want_camera = settings.sync_camera and len(project.camera_keys) > 0
    want_light = settings.sync_light and len(project.light_keys) > 0
    log.info(
        "scene elements: camera keys=%d (sync=%s), light keys=%d (sync=%s)",
        len(project.camera_keys),
        settings.sync_camera,
        len(project.light_keys),
        settings.sync_light,
    )
    if want_camera:
        first = project.camera_keys[0]
        log.info(
            "first camera key: frame=%d distance=%.3f look_at=%s angle=%s fov=%d parent=(%d,%d) perspective_raw=%d",
            first.frame,
            first.distance,
            tuple(round(v, 3) for v in first.look_at),
            tuple(round(v, 3) for v in first.angle),
            first.fov,
            first.parent_model,
            first.parent_bone,
            first.perspective,
        )
    if want_light:
        first = project.light_keys[0]
        log.info(
            "first light key: frame=%d color=%s direction=%s",
            first.frame,
            first.color,
            first.direction,
        )
    done = 0
    resettable = []
    camera_owners = []
    light_owners = []
    kept = []
    if want_camera:
        root, camera = ensure_camera_rig(scene, settings)
        resettable += [root, camera]
        camera_owners = collect_owners([root, camera])
        low, high = frame_span(k.frame for k in project.camera_keys)
        kept += snapshot_outside(camera_owners, low, high)
    if want_light:
        root, light = ensure_light_rig(scene, settings)
        resettable += [root, light]
        light_owners = collect_owners([root, light])
        low, high = frame_span(k.frame for k in project.light_keys)
        kept += snapshot_outside(light_owners, low, high)
    log.info("keeping keys outside the PMM span on %d camera/light curve(s)", len(kept))
    clear_bridge_nla(camera_owners + light_owners)
    reset_animation(resettable)
    purge_actions(ACTION_PREFIX + "Scene")
    purge_actions(ACTION_PREFIX + "Light")
    if want_camera:
        import_camera(scene, project, settings)
        done += 1
    if want_light:
        import_light(scene, project, settings)
        done += 1
    restored = restore_points(kept)
    tag_actions(camera_owners, "Scene")
    tag_actions(light_owners, "Light")
    log.info("restored %d preserved camera/light key(s)", restored)
    return done


def short_name(text, limit=48):
    out = ""
    for char in text:
        if len((out + char).encode("utf-8")) > limit:
            break
        out += char
    return out or "accessory"


def accessory_key(acc):
    return "%d:%s" % (acc.index, acc.name)


def refresh_accessories(settings, project):
    wanted = [accessory_key(a) for a in project.accessories]
    for index in reversed(range(len(settings.accessories))):
        if settings.accessories[index].key not in wanted:
            settings.accessories.remove(index)
    present = {e.key: e for e in settings.accessories}
    for acc in project.accessories:
        key = accessory_key(acc)
        if key not in present:
            entry = settings.accessories.add()
            entry.key = key
            entry.display = acc.name
            entry.enabled = True
            log.info("added accessory slot %s", key)
    if project.accessory_error:
        log.warning("accessory section could not be read: %s", project.accessory_error)


def find_accessory_file(acc, settings):
    pmm_dir = os.path.dirname(bpy.path.abspath(settings.filepath))
    base = acc.path.replace("\\", "/").rsplit("/", 1)[-1] or acc.name
    candidates = [acc.path]
    if settings.accessory_dir:
        candidates.append(os.path.join(bpy.path.abspath(settings.accessory_dir), base))
    candidates.append(os.path.join(pmm_dir, base))
    candidates.append(os.path.join(pmm_dir, "Accessory", base))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def resolve_x_texture(name, folder):
    cleaned = name.replace("\\\\", "\\").replace("\\", "/")
    base = cleaned.rsplit("/", 1)[-1]
    for candidate in (
        cleaned,
        os.path.join(folder, base),
        os.path.join(folder, "textures", base),
    ):
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def node_input(node, *names):
    for name in names:
        found = node.inputs.get(name)
        if found is not None:
            return found
    return None


def node_socket(node, identifier, outputs=False):
    for socket in node.outputs if outputs else node.inputs:
        if socket.identifier == identifier:
            return socket
    return None


def add_color_mix(nodes, links, blend, a_socket, b_socket):
    mix = nodes.new("ShaderNodeMix")
    mix.data_type = "RGBA"
    mix.blend_type = blend
    mix.inputs[0].default_value = 1.0
    links.new(a_socket, node_socket(mix, "A_Color"))
    links.new(b_socket, node_socket(mix, "B_Color"))
    return node_socket(mix, "Result_Color", outputs=True)


def add_sphere_map(nodes, links, image, additive, color_socket):
    geometry = nodes.new("ShaderNodeNewGeometry")
    transform = nodes.new("ShaderNodeVectorTransform")
    transform.vector_type = "NORMAL"
    transform.convert_from = "WORLD"
    transform.convert_to = "CAMERA"
    scale = nodes.new("ShaderNodeVectorMath")
    scale.operation = "MULTIPLY_ADD"
    scale.inputs[1].default_value = (0.5, 0.5, 0.0)
    scale.inputs[2].default_value = (0.5, 0.5, 0.0)
    texture = nodes.new("ShaderNodeTexImage")
    texture.image = image
    texture.extension = "EXTEND"
    links.new(geometry.outputs["Normal"], transform.inputs["Vector"])
    links.new(transform.outputs["Vector"], scale.inputs[0])
    links.new(scale.outputs["Vector"], texture.inputs["Vector"])
    return add_color_mix(
        nodes,
        links,
        "ADD" if additive else "MULTIPLY",
        color_socket,
        texture.outputs["Color"],
    )


def build_x_material(name, xmat, folder, settings):
    mat = bpy.data.materials.new(name)
    try:
        mat.use_nodes = True
    except Exception:
        pass
    tree = mat.node_tree
    if tree is None:
        return mat
    nodes, links = tree.nodes, tree.links
    bsdf = next((n for n in nodes if n.type == "BSDF_PRINCIPLED"), None)
    if bsdf is None:
        bsdf = nodes.new("ShaderNodeBsdfPrincipled")
        output = next(
            (n for n in nodes if n.type == "OUTPUT_MATERIAL"), None
        ) or nodes.new("ShaderNodeOutputMaterial")
        links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])
    r, g, b, a = xmat.diffuse
    base = node_input(bsdf, "Base Color")
    base.default_value = (r, g, b, 1.0)
    alpha = node_input(bsdf, "Alpha")
    alpha.default_value = a
    rough = node_input(bsdf, "Roughness")
    if rough is not None:
        rough.default_value = 0.6
    spec = node_input(bsdf, "Specular IOR Level", "Specular")
    if spec is not None:
        spec.default_value = min(1.0, max(xmat.specular) * 0.5)
    emission = node_input(bsdf, "Emission Color", "Emission")
    if emission is not None:
        emission.default_value = (
            xmat.emissive[0],
            xmat.emissive[1],
            xmat.emissive[2],
            1.0,
        )
    strength = node_input(bsdf, "Emission Strength")
    if strength is not None:
        strength.default_value = 0.25
    parts = [
        p.strip() for p in xmat.texture.replace("\\\\", "\\").split("*") if p.strip()
    ]
    spheres = [p for p in parts if p.lower().endswith((".sph", ".spa"))]
    plain = [p for p in parts if not p.lower().endswith((".sph", ".spa"))]
    color_socket = None
    translucent = a < 0.999
    if plain:
        found = resolve_x_texture(plain[0], folder)
        if found is None:
            log.warning("texture not found for %s: %s", name, plain[0])
        else:
            texture = nodes.new("ShaderNodeTexImage")
            texture.image = bpy.data.images.load(found, check_existing=True)
            color_socket = texture.outputs["Color"]
            links.new(texture.outputs["Alpha"], alpha)
            translucent = True
            if min(r, g, b) < 0.99:
                tint = nodes.new("ShaderNodeRGB")
                tint.outputs[0].default_value = (r, g, b, 1.0)
                color_socket = add_color_mix(
                    nodes, links, "MULTIPLY", color_socket, tint.outputs[0]
                )
    if spheres and settings.sphere_maps:
        if color_socket is None:
            tint = nodes.new("ShaderNodeRGB")
            tint.outputs[0].default_value = (r, g, b, 1.0)
            color_socket = tint.outputs[0]
        for sphere in spheres:
            found = resolve_x_texture(sphere, folder)
            if found is None:
                log.warning("sphere map not found for %s: %s", name, sphere)
                continue
            image = bpy.data.images.load(found, check_existing=True)
            color_socket = add_sphere_map(
                nodes, links, image, sphere.lower().endswith(".spa"), color_socket
            )
            log.info("sphere map (experimental) %s on %s", sphere, name)
    elif spheres:
        log.info("sphere maps disabled; skipping %s on %s", spheres, name)
    if color_socket is not None:
        links.new(color_socket, base)
    if translucent:
        try:
            mat.surface_render_method = "BLENDED"
        except (AttributeError, TypeError):
            try:
                mat.blend_method = "BLEND"
            except AttributeError:
                pass
    return mat


def merge_x_meshes(xscene):
    vertices = []
    faces = []
    face_materials = []
    uvs = []
    colors = []
    materials = []
    has_colors = False
    for mesh in xscene.meshes:
        vertex_offset = len(vertices)
        material_offset = len(materials)
        vertices.extend(mesh.vertices)
        for k in range(len(mesh.vertices)):
            uvs.append(mesh.uvs[k] if k < len(mesh.uvs) else (0.0, 0.0))
            colors.append(mesh.colors.get(k, (1.0, 1.0, 1.0, 1.0)))
        has_colors = has_colors or bool(mesh.colors)
        local_materials = list(mesh.materials) or [
            XMaterial(
                "Material",
                (0.8, 0.8, 0.8, 1.0),
                0.0,
                (0.0, 0.0, 0.0),
                (0.0, 0.0, 0.0),
                "",
            )
        ]
        materials.extend(local_materials)
        for index, face in enumerate(mesh.faces):
            faces.append(tuple(v + vertex_offset for v in face))
            local = (
                mesh.face_materials[index] if index < len(mesh.face_materials) else 0
            )
            face_materials.append(
                material_offset + min(max(local, 0), len(local_materials) - 1)
            )
    return (
        vertices,
        faces,
        face_materials,
        uvs,
        colors if has_colors else None,
        materials,
    )


def create_x_object(name, xscene, folder, settings):
    vertices, faces, face_materials, uvs, colors, materials = merge_x_meshes(xscene)
    points = [(x, z, y) for x, y, z in vertices]
    flipped = [tuple(reversed(face)) for face in faces]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(points, [], flipped)
    mesh.update()
    for index, xmat in enumerate(materials):
        try:
            mesh.materials.append(
                build_x_material("%s_%d" % (name, index), xmat, folder, settings)
            )
        except Exception as error:
            log.warning("material %d of %s failed: %s", index, name, error)
            mesh.materials.append(bpy.data.materials.new("%s_%d" % (name, index)))
    if len(mesh.polygons) == len(face_materials):
        mesh.polygons.foreach_set("material_index", face_materials)
    if uvs:
        flat = []
        for face in flipped:
            for vertex in face:
                u, v = uvs[vertex]
                flat.extend((u, 1.0 - v))
        if len(mesh.loops) * 2 == len(flat):
            layer = mesh.uv_layers.new(name="UVMap")
            layer.data.foreach_set("uv", flat)
    if colors is not None:
        try:
            attribute = mesh.color_attributes.new("Col", "FLOAT_COLOR", "POINT")
            attribute.data.foreach_set("color", [c for rgba in colors for c in rgba])
        except Exception as error:
            log.warning("vertex colors skipped for %s: %s", name, error)
    mesh.update()
    return bpy.data.objects.new(name, mesh)


def import_x_with_addon(path):
    before = set(bpy.data.objects)
    group = getattr(bpy.ops, "import_scene", None)
    operator = None
    for candidate in ("x", "directx", "directx_x", "x_file", "dx"):
        if group is not None and hasattr(group, candidate):
            operator = getattr(group, candidate)
            break
    if operator is None:
        raise RuntimeError(
            "binary/compressed .x needs the DirectX X Importer add-on, but its import operator was not found"
        )
    operator(filepath=path)
    return [o for o in bpy.data.objects if o not in before]


def remove_accessory_children(root):
    for child in list(root.children):
        data = child.data
        bpy.data.objects.remove(child, do_unlink=True)
        if (
            data is not None
            and getattr(data, "users", 1) == 0
            and hasattr(bpy.data, "meshes")
        ):
            try:
                for material in list(data.materials):
                    if material is not None and material.users <= 1:
                        bpy.data.materials.remove(material)
                bpy.data.meshes.remove(data)
            except (ReferenceError, RuntimeError):
                pass


def find_accessory_root(collection, key):
    for obj in collection.objects:
        if obj.get("mmd_bridge_accessory") == key:
            return obj
    return None


def first_armature(settings):
    for item in settings.targets:
        if item.enabled and target_usable(item.target):
            return find_armature(item.target)
    return None


def resolve_accessory_parent(project, settings, acc, entry):
    first = acc.keys[0]
    override = entry.parent_bone if entry is not None else ""
    by_index = {m.index: m for m in project.models}
    items = {t.key: t for t in settings.targets}
    armature = None
    model = by_index.get(first.parent_model) if first.parent_model >= 0 else None
    if model is not None:
        item = items.get(model_key(model))
        if item is not None and item.target is not None:
            armature = find_armature(item.target)
    if override:
        armature = armature or first_armature(settings)
        if armature is not None and override in armature.pose.bones:
            return armature, override
        log.warning("accessory '%s': override bone '%s' not found", acc.name, override)
        return None
    if model is None or first.parent_bone < 0:
        return None
    if armature is None:
        log.warning(
            "accessory '%s' follows model %d which has no Blender target",
            acc.name,
            first.parent_model,
        )
        return None
    if first.parent_bone >= len(model.bone_names):
        log.warning(
            "accessory '%s': bone index %d out of range", acc.name, first.parent_bone
        )
        return None
    bone = pose_bone_name(armature, model.bone_names[first.parent_bone])
    if bone is None:
        log.warning(
            "accessory '%s': bone '%s' not found on '%s'",
            acc.name,
            model.bone_names[first.parent_bone],
            armature.name,
        )
        return None
    return armature, bone


def apply_accessory_parent(root, project, settings, acc, entry):
    for constraint in [
        c for c in root.constraints if c.name.startswith(ACCESSORY_FOLLOW_PREFIX)
    ]:
        root.constraints.remove(constraint)
    parents = {(k.parent_model, k.parent_bone) for k in acc.keys}
    if len(parents) > 1:
        log.warning(
            "accessory '%s' changes parent between keyframes; only the first parent is used",
            acc.name,
        )
    resolved = resolve_accessory_parent(project, settings, acc, entry)
    if resolved is None:
        log.info("accessory '%s' has no parent bone", acc.name)
        return
    armature, bone = resolved
    constraint = root.constraints.new(type="CHILD_OF")
    constraint.name = ACCESSORY_FOLLOW_PREFIX
    constraint.target = armature
    constraint.subtarget = bone
    constraint.use_scale_x = False
    constraint.use_scale_y = False
    constraint.use_scale_z = False
    constraint.set_inverse_pending = False
    constraint.inverse_matrix = Matrix.Identity(4)
    constraint.influence = 1.0
    log.info("accessory '%s' follows %s / %s", acc.name, armature.name, bone)


def key_accessory(root, acc, unit):
    anim = root.animation_data or root.animation_data_create()
    low, high = frame_span(k.frame for k in acc.keys)
    kept = snapshot_outside([root], low, high)
    anim.action = None
    purge_actions(ACTION_PREFIX + "Acc_")
    root.rotation_mode = "YXZ"
    for key in acc.keys:
        root.location = (
            key.position[0] * unit,
            key.position[2] * unit,
            key.position[1] * unit,
        )
        root.rotation_euler = (-key.rotation[0], -key.rotation[2], -key.rotation[1])
        root.scale = (key.scale, key.scale, key.scale)
        root.keyframe_insert("location", frame=key.frame)
        root.keyframe_insert("rotation_euler", frame=key.frame)
        root.keyframe_insert("scale", frame=key.frame)
    action = anim.action
    if action is not None:
        for fcurve in iter_fcurves(action):
            for point in fcurve.keyframe_points:
                point.interpolation = "LINEAR"
    restore_points(kept)
    if anim.action is not None:
        anim.action.name = ACTION_PREFIX + "Acc_" + short_name(acc.name, 40)
    visible = {bool(k.visible) for k in acc.keys}
    for child in root.children:
        child_anim = child.animation_data
        if child_anim is not None:
            child_anim.action = None
        child.hide_viewport = False
        child.hide_render = False
        if visible == {True}:
            continue
        for key in acc.keys:
            child.hide_viewport = not key.visible
            child.hide_render = not key.visible
            child.keyframe_insert("hide_viewport", frame=key.frame)
            child.keyframe_insert("hide_render", frame=key.frame)
        child_action = child.animation_data.action if child.animation_data else None
        if child_action is not None:
            child_action.name = ACTION_PREFIX + "Acc_vis_" + short_name(acc.name, 30)
            for fcurve in iter_fcurves(child_action):
                for point in fcurve.keyframe_points:
                    point.interpolation = "CONSTANT"


def import_accessory(scene, collection, project, settings, acc, entry, unit):
    path = find_accessory_file(acc, settings)
    if path is None:
        log.warning(
            "accessory '%s': file not found (PMM path %s); set an Accessory Folder",
            acc.name,
            acc.path,
        )
        return False
    key = accessory_key(acc)
    stamp = "%s|%s" % (path, os.path.getmtime(path))
    root = find_accessory_root(collection, key)
    if root is None:
        root = bpy.data.objects.new(ACCESSORY_PREFIX + short_name(acc.name), None)
        root.empty_display_type = "PLAIN_AXES"
        root.empty_display_size = 1.0
        root["mmd_bridge_accessory"] = key
        collection.objects.link(root)
        log.info("created accessory root '%s'", root.name)
    if root.get("mmd_bridge_stamp") != stamp or not root.children:
        remove_accessory_children(root)
        with open(path, "rb") as handle:
            raw = handle.read()
        xscene = parse_x_bytes(raw)
        folder = os.path.dirname(path)
        for note in xscene.warnings:
            log.warning("accessory '%s': %s", acc.name, note)
        if xscene.mode == "txt":
            child = create_x_object(
                short_name(acc.name, 40) + "_mesh", xscene, folder, settings
            )
            collection.objects.link(child)
            children = [child]
            log.info(
                "accessory '%s': %d mesh part(s) from %s",
                acc.name,
                len(xscene.meshes),
                path,
            )
        else:
            children = import_x_with_addon(path)
            for child in children:
                for owner in list(child.users_collection):
                    owner.objects.unlink(child)
                collection.objects.link(child)
        for child in children:
            child.parent = root
        root["mmd_bridge_stamp"] = stamp
    for child in root.children:
        child.scale = (ACCESSORY_UNIT * unit,) * 3
    key_accessory(root, acc, unit)
    apply_accessory_parent(root, project, settings, acc, entry)
    return True


def import_accessories(scene, project, settings):
    if not settings.sync_accessories:
        return 0
    if project.accessory_error:
        log.warning("accessory section could not be read: %s", project.accessory_error)
        return 0
    if not project.accessories:
        return 0
    log.info(
        "accessories: static mesh .x only; no MME shader effects, skinned or animated meshes"
    )
    collection = ensure_collection(scene, ACCESSORY_COLLECTION)
    unit = current_scale(settings)
    entries = {e.key: e for e in settings.accessories}
    done = 0
    for acc in project.accessories:
        entry = entries.get(accessory_key(acc))
        if entry is not None and not entry.enabled:
            log.info("skip accessory '%s': disabled", acc.name)
            continue
        if not acc.keys:
            continue
        try:
            if import_accessory(scene, collection, project, settings, acc, entry, unit):
                done += 1
        except Exception as error:
            log.error(
                "accessory '%s' failed: %s\n%s", acc.name, error, traceback.format_exc()
            )
    return done


def log_file_state(path, data):
    digest = hashlib.md5(data).hexdigest()[:12]
    stat = os.stat(path)
    log.info(
        "PMM file: size=%d bytes, modified=%s, content hash=%s",
        stat.st_size,
        time.strftime("%H:%M:%S", time.localtime(stat.st_mtime)),
        digest,
    )
    if digest == _last_sync["digest"]:
        log.warning(
            "PMM content is unchanged since the previous sync attempt (save in MMD if you expected new data)"
        )
    _last_sync["digest"] = digest


def log_scene_keys(project):
    for key in project.camera_keys[:40]:
        log.info(
            "PMM camera key f=%d position=(%.2f, %.2f, %.2f) angle_deg=(%.2f, %.2f, %.2f) distance=%.2f fov=%d follow=(model %d, bone %d)",
            key.frame,
            *key.look_at,
            *(math.degrees(v) for v in key.angle),
            key.distance,
            key.fov,
            key.parent_model,
            key.parent_bone,
        )
    for key in project.light_keys[:40]:
        log.info(
            "PMM light key f=%d rgb=(%d, %d, %d) direction=(%.2f, %.2f, %.2f)",
            key.frame,
            *(round(v * 255) for v in key.color),
            *key.direction,
        )


def sync(scene):
    settings = scene.mmd_bridge
    path = bpy.path.abspath(settings.filepath)
    log.info("sync start: %s", path)
    sweep_stale_temp()
    with open(path, "rb") as handle:
        data = handle.read()
    log_file_state(path, data)
    project = parse_pmm(data)
    log_scene_keys(project)
    refresh_targets(settings, project)
    refresh_accessories(settings, project)
    log.info(
        "using scale %.4f (%s)",
        current_scale(settings),
        "auto from model" if settings.auto_scale else "manual",
    )
    by_key = {model_key(m): m for m in project.models}
    imported = 0
    for target in settings.targets:
        if not target.enabled:
            log.info("skip '%s': disabled", target.pmm_name)
            continue
        if target.target is None:
            log.warning("skip '%s': no target object assigned", target.pmm_name)
            continue
        model = by_key.get(target.key)
        if model is None:
            log.warning("skip '%s': not present in PMM", target.pmm_name)
            continue
        import_model(target.target, model, settings, target)
        imported += 1
    imported += import_scene_elements(scene, project, settings)
    imported += import_accessories(scene, project, settings)
    scene.frame_set(scene.frame_current)
    log.info(
        "sync done: %d item(s) imported, %d model(s) in PMM",
        imported,
        len(project.models),
    )
    return imported, len(project.models)


def run_sync(scene):
    settings = scene.mmd_bridge
    try:
        imported, total = sync(scene)
    except pmm_error_types() as error:
        log.error("PMM read failed: %s", error)
        settings.status = "PMM read failed: %s" % error
        redraw_all()
        return False
    except Exception as error:
        log.error("sync failed: %s\n%s", error, traceback.format_exc())
        settings.status = "Sync failed: %s" % error
        redraw_all()
        return False
    settings.status = "Synced %d item(s) at %s" % (imported, time.strftime("%H:%M:%S"))
    redraw_all()
    return True


def pmm_error_types():
    return (PmmError, OSError)


def draw_wrapped(layout, text, width=38):
    column = layout.column(align=True)
    column.scale_y = 0.85
    for line in textwrap.wrap(text, width):
        column.label(text=line)


def draw_dependency_notices(layout):
    for label, message, url in missing_dependencies():
        box = layout.box()
        box.alert = True
        box.label(text=message, icon="ERROR")
        box.operator("wm.url_open", text="Get %s" % label, icon="URL").url = url


class MMDBridgeTarget(PropertyGroup):
    key: StringProperty()
    pmm_name: StringProperty()
    enabled: BoolProperty(name="Enabled", default=True)
    target: PointerProperty(
        name="Target", type=bpy.types.Object, poll=is_mmd_root, update=on_target_changed
    )
    clip: PointerProperty(
        name="Motion Clip",
        description="Action that receives this model's armature (bone) animation on every sync. Leave empty to create a new clip on its own NLA track",
        type=bpy.types.Action,
        poll=clip_poll,
    )
    shape_clip: PointerProperty(
        name="Shapekey Clip",
        description="Action that receives this model's shape key (morph) animation on every sync, for the main body mesh. Leave empty to create a new clip on its own NLA track. Other shape key holders such as the morph placeholder keep automatic clips",
        type=bpy.types.Action,
        poll=clip_poll,
    )
    physics_vmd: StringProperty(
        name="Baked Physics VMD",
        description="Experimental: VMD baked from MMD with MMDBridge. Applied as a clip above the PMM clip",
        subtype="FILE_PATH",
    )


class MMDBridgeAccessory(PropertyGroup):
    key: StringProperty()
    display: StringProperty()
    enabled: BoolProperty(name="Enabled", default=True)
    parent_bone: StringProperty(
        name="Parent Bone",
        description="Overrides the parent bone stored in the PMM. Leave empty to use the PMM value",
    )


class MMDBridgeSettings(PropertyGroup):
    filepath: StringProperty(name="PMM File", subtype="FILE_PATH")
    status: StringProperty(default="Idle")
    auto_scale: BoolProperty(
        name="Auto Scale From Model",
        description="Use the scale the first enabled model was imported with in mmd_tools",
        default=True,
    )
    scale: FloatProperty(
        name="Scale",
        description="Motion scale passed to the mmd_tools VMD importer when Auto Scale is off",
        default=0.08,
        min=0.001,
    )
    scale_warning_dismissed_for: FloatProperty(default=-1.0)
    sync_accessories: BoolProperty(
        name="Sync Accessories",
        description="Import and animate .x accessories stored in the PMM. Static meshes only",
        default=True,
    )
    sphere_maps: BoolProperty(
        name="Sphere Maps (Experimental)",
        description="Rebuild .sph/.spa sphere maps from accessory textures with a camera-space normal lookup",
        default=True,
    )
    accessory_dir: StringProperty(
        name="Accessory Folder",
        description="Extra folder searched for .x files when the path stored in the PMM does not exist",
        subtype="DIR_PATH",
    )
    accessories: CollectionProperty(type=MMDBridgeAccessory)
    use_physics_vmd: BoolProperty(
        name="MMDBridge Physics (Experimental)",
        description="Layer a VMD baked with MMDBridge above the PMM clip to reproduce MMD's own rigid body results",
        default=False,
    )
    physics_disable_rigid_body: BoolProperty(
        name="Disable Rigid Body World",
        description="Turn off Blender's rigid body world while a baked physics clip is applied",
        default=True,
    )
    use_clips: BoolProperty(
        name="NLA Clips",
        description="Keep each model's motion and shapekey animation in reusable action clips on NLA tracks instead of as the active action",
        default=True,
    )
    uv_morphs: BoolProperty(
        name="UV Morphs (Eye Shapes)",
        description="Apply UV morphs, including the ones inside group morphs, by offsetting the base texture in the material nodes. Needed for effects such as the > < eyes",
        default=True,
    )
    update_scene_settings: BoolProperty(
        name="Update Frame Range",
        description="Let mmd_tools set the scene frame range and FPS on every sync; the range is only ever widened",
        default=False,
    )
    interpolation_layout: EnumProperty(
        name="Interpolation Layout",
        description="How the 16 interpolation bytes in the PMM are ordered",
        items=[
            ("CURVE", "Per Curve", "x1 y1 x2 y2 repeated for X, Y, Z, Rotation"),
            ("VMD_ROW", "VMD Row", "x1 for each curve, then y1, then x2, then y2"),
        ],
        default="CURVE",
    )
    sync_camera: BoolProperty(name="Sync Camera", default=True)
    sync_light: BoolProperty(name="Sync Light", default=True)
    set_active_camera: BoolProperty(name="Make Scene Camera", default=True)
    follow_mode: EnumProperty(
        name="Follow",
        items=[
            ("OFF", "Off", "Camera does not follow any bone"),
            (
                "PMM",
                "From PMM",
                "Use the follow model and bone stored in each PMM camera keyframe",
            ),
            ("MANUAL", "Manual", "Always follow the model and bone chosen below"),
        ],
        default="PMM",
    )
    follow_model: EnumProperty(name="Model", items=follow_model_items)
    follow_bone_name: StringProperty(name="Bone")
    follow_rotation: BoolProperty(
        name="Follow Bone Rotation",
        description="Also inherit the bone rotation, not only its position",
        default=False,
    )
    targets: CollectionProperty(type=MMDBridgeTarget)


class MMDBRIDGE_OT_sync_now(Operator):
    bl_idname = "mmd_bridge.sync_now"
    bl_label = "Sync Now"

    def execute(self, context):
        settings = context.scene.mmd_bridge
        path = bpy.path.abspath(settings.filepath)
        log.info("Sync Now pressed")
        if not mmd_tools_available():
            self.report({"ERROR"}, "mmd_tools is not enabled")
            log.error("mmd_tools operator MMD_TOOLS_OT_import_vmd not found")
            return {"CANCELLED"}
        if not os.path.isfile(path):
            self.report({"ERROR"}, "PMM file not found")
            log.error("PMM file not found: %s", path)
            return {"CANCELLED"}
        if not run_sync(context.scene):
            self.report({"ERROR"}, settings.status)
            return {"CANCELLED"}
        self.report({"INFO"}, settings.status)
        return {"FINISHED"}


class MMDBRIDGE_OT_scan(Operator):
    bl_idname = "mmd_bridge.scan"
    bl_label = "Read Models"

    def execute(self, context):
        settings = context.scene.mmd_bridge
        try:
            project = parse_pmm_file(bpy.path.abspath(settings.filepath))
        except (OSError, PmmError) as error:
            self.report({"ERROR"}, str(error))
            log.error("scan failed: %s", error)
            return {"CANCELLED"}
        refresh_targets(settings, project)
        refresh_accessories(settings, project)
        self.report(
            {"INFO"},
            "Found %d model(s), %d accessory(ies)"
            % (len(project.models), len(project.accessories)),
        )
        return {"FINISHED"}


class MMDBRIDGE_OT_dismiss_scale_warning(Operator):
    bl_idname = "mmd_bridge.dismiss_scale_warning"
    bl_label = "Dismiss"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        settings = context.scene.mmd_bridge
        settings.scale_warning_dismissed_for = current_scale(settings)
        redraw_all()
        return {"FINISHED"}


class MMDBRIDGE_PT_main(Panel):
    bl_label = "MMD PMM Bridge"
    bl_idname = "MMDBRIDGE_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MMD Bridge"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.mmd_bridge
        draw_dependency_notices(layout)
        if not mmd_tools_available():
            return
        layout.prop(settings, "filepath", text="")
        row = layout.row()
        row.scale_y = 1.5
        row.operator("mmd_bridge.sync_now", icon="FILE_REFRESH")
        layout.label(text=settings.status, icon="INFO")
        if scale_warning_active(settings):
            box = layout.box()
            header = box.row()
            header.alert = True
            header.label(
                text="Scale is %.3f, not %.1f" % (current_scale(settings), UNIT_SCALE),
                icon="ERROR",
            )
            header.operator(
                "mmd_bridge.dismiss_scale_warning", text="", icon="X", emboss=False
            )
            draw_wrapped(
                box,
                "Camera keyframe interpolation may not match MMD exactly because of the different distance scale.",
            )
        box = layout.box()
        header = box.row()
        header.label(text="Models")
        header.operator("mmd_bridge.scan", text="", icon="FILE_REFRESH")
        if not len(settings.targets):
            box.label(text="Press the refresh icon to read the PMM")
        for item in settings.targets:
            row = box.row(align=True)
            row.prop(item, "enabled", text="")
            row.label(text=item.pmm_name)
            row.prop(item, "target", text="")
            if settings.use_clips and item.target is not None:
                clip_row = box.row(align=True)
                clip_row.label(text="", icon="NLA")
                clip_row.prop(item, "clip", text="Motion")
                shape_row = box.row(align=True)
                shape_row.label(text="", icon="SHAPEKEY_DATA")
                shape_row.prop(item, "shape_clip", text="Shapekey")
                if settings.use_physics_vmd:
                    physics_row = box.row(align=True)
                    physics_row.label(text="", icon="PHYSICS")
                    physics_row.prop(item, "physics_vmd", text="")
        layout.label(text="Log: Text Editor > %s" % LOG_TEXT_NAME)


class MMDBRIDGE_PT_camera(Panel):
    bl_label = "Camera & Light"
    bl_idname = "MMDBRIDGE_PT_camera"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MMD Bridge"
    bl_parent_id = "MMDBRIDGE_PT_main"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.mmd_bridge
        layout.prop(settings, "sync_camera")
        layout.prop(settings, "sync_light")
        layout.prop(settings, "set_active_camera")
        layout.separator()
        layout.prop(settings, "follow_mode")
        if settings.follow_mode == "MANUAL":
            layout.prop(settings, "follow_model")
            armature = None
            for item in settings.targets:
                if item.key == settings.follow_model and item.target is not None:
                    armature = find_armature(item.target)
            if armature is not None:
                layout.prop_search(
                    settings, "follow_bone_name", armature.pose, "bones", text="Bone"
                )
        if settings.follow_mode != "OFF":
            layout.prop(settings, "follow_rotation")


class MMDBRIDGE_PT_readout(Panel):
    bl_label = "MMD Values (current frame)"
    bl_idname = "MMDBRIDGE_PT_readout"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MMD Bridge"
    bl_parent_id = "MMDBRIDGE_PT_camera"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        scale = current_scale(context.scene.mmd_bridge)
        camera_collection = bpy.data.collections.get(CAMERA_COLLECTION)
        root = find_rig_root(camera_collection, "CAMERA") if camera_collection else None
        camera = child_of_type(root, "CAMERA") if root else None
        if root is not None and camera is not None:
            loc = root.location
            rot = root.rotation_euler
            layout.label(
                text="Camera position: %.2f, %.2f, %.2f"
                % (loc.x / scale, loc.z / scale, loc.y / scale)
            )
            layout.label(
                text="Camera angle: %.2f, %.2f, %.2f"
                % (-math.degrees(rot.x), math.degrees(rot.z), math.degrees(rot.y))
            )
            layout.label(text="Distance: %.2f" % (-camera.location.y / scale))
            mmd_camera = getattr(root, "mmd_camera", None)
            if mmd_camera is not None:
                layout.label(text="View angle: %.1f" % math.degrees(mmd_camera.angle))
        else:
            layout.label(text="No MMD camera yet")
        light_collection = bpy.data.collections.get(LIGHT_COLLECTION)
        light_root = (
            find_rig_root(light_collection, "LIGHT") if light_collection else None
        )
        light = child_of_type(light_root, "LIGHT") if light_root else None
        if light is not None:
            color = light.data.color
            pos = light.location
            layout.label(
                text="Light RGB: %d, %d, %d"
                % (round(color[0] * 255), round(color[1] * 255), round(color[2] * 255))
            )
            layout.label(
                text="Light position: %.2f, %.2f, %.2f" % (-pos.x, -pos.z, -pos.y)
            )


class MMDBRIDGE_PT_options(Panel):
    bl_label = "Options"
    bl_idname = "MMDBRIDGE_PT_options"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MMD Bridge"
    bl_parent_id = "MMDBRIDGE_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        settings = context.scene.mmd_bridge
        layout = self.layout
        layout.prop(settings, "auto_scale")
        row = layout.row()
        row.enabled = not settings.auto_scale
        row.prop(settings, "scale")
        layout.prop(settings, "use_clips")
        layout.prop(settings, "uv_morphs")
        if settings.uv_morphs:
            draw_wrapped(
                layout,
                "UV morphs are shader based: view them in Material Preview or Rendered, not Solid. The weights live on MMDUV_ shape keys inside the Shapekey Clip.",
            )
        layout.prop(settings, "update_scene_settings")
        layout.prop(settings, "interpolation_layout")
        layout.separator()
        layout.prop(settings, "use_physics_vmd")
        if settings.use_physics_vmd:
            layout.prop(settings, "physics_disable_rigid_body")
            draw_wrapped(
                layout,
                "Experimental. MMD's physics solver cannot run inside Blender. Bake the motion in MMD with MMDBridge, set the VMD on each model row, and it is layered above the PMM clip.",
            )


class MMDBRIDGE_PT_accessories(Panel):
    bl_label = "Accessories (.x)"
    bl_idname = "MMDBRIDGE_PT_accessories"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MMD Bridge"
    bl_parent_id = "MMDBRIDGE_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        settings = context.scene.mmd_bridge
        layout.prop(settings, "sync_accessories")
        box = layout.box()
        box.alert = True
        draw_wrapped(
            box,
            "Static mesh .x files only. MME shader effects (.fx), skinned meshes and animated .x files are not supported.",
        )
        layout.prop(settings, "sphere_maps")
        layout.prop(settings, "accessory_dir")
        armature = first_armature(settings)
        if not len(settings.accessories):
            layout.label(text="Press the refresh icon in Models to read the PMM")
        for entry in settings.accessories:
            row = layout.row(align=True)
            row.prop(entry, "enabled", text="")
            row.label(text=entry.display)
            if armature is not None:
                row.prop_search(entry, "parent_bone", armature.pose, "bones", text="")


CLASSES = (
    MMDBridgeTarget,
    MMDBridgeAccessory,
    MMDBridgeSettings,
    MMDBRIDGE_OT_sync_now,
    MMDBRIDGE_OT_scan,
    MMDBRIDGE_OT_dismiss_scale_warning,
    MMDBRIDGE_PT_main,
    MMDBRIDGE_PT_camera,
    MMDBRIDGE_PT_readout,
    MMDBRIDGE_PT_options,
    MMDBRIDGE_PT_accessories,
)


def register():
    setup_logging()
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.mmd_bridge = PointerProperty(type=MMDBridgeSettings)
    sweep_stale_temp()
    log.info("MMD PMM Bridge registered")


def unregister():
    if hasattr(bpy.types.Scene, "mmd_bridge"):
        del bpy.types.Scene.mmd_bridge
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


def reload_in_place():
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except (RuntimeError, ValueError):
            pass
    if hasattr(bpy.types.Scene, "mmd_bridge"):
        del bpy.types.Scene.mmd_bridge
    register()


if __name__ == "__main__":
    reload_in_place()
