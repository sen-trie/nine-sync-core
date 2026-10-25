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
import json
import logging
import os
import platform
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
import numpy as np
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
    __slots__ = (
        "vertices",
        "faces",
        "uvs",
        "face_materials",
        "materials",
        "colors",
        "normals",
    )

    def __init__(self):
        self.vertices = []
        self.faces = []
        self.uvs = []
        self.face_materials = []
        self.materials = []
        self.colors = {}
        self.normals = {}


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
        elif child.kind == "MeshNormals" and cd:
            count = int(cd[0])
            m = matrix
            listed = []
            for k in range(count):
                nx, ny, nz = cd[1 + k * 3 : 4 + k * 3]
                listed.append(
                    (
                        nx * m[0] + ny * m[4] + nz * m[8],
                        nx * m[1] + ny * m[5] + nz * m[9],
                        nx * m[2] + ny * m[6] + nz * m[10],
                    )
                )
            pos = 1 + count * 3
            if pos < len(cd):
                face_count_n = int(cd[pos])
                pos += 1
                for index in range(face_count_n):
                    if pos >= len(cd):
                        break
                    n = int(cd[pos])
                    pos += 1
                    corners = [int(v) for v in cd[pos : pos + n]]
                    pos += n
                    if index < len(mesh.faces):
                        for vertex, normal in zip(mesh.faces[index], corners):
                            if 0 <= normal < len(listed):
                                mesh.normals[vertex] = listed[normal]
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


class FxError(Exception):
    pass


class FxUnsupported(FxError):
    pass


FX_COMMENT = re.compile(r'"(?:[^"\\\n]|\\.)*"|//[^\n]*|/\*.*?\*/', re.S)
FX_PP_TOKEN = re.compile(
    r'"(?:[^"\\]|\\.)*"|[A-Za-z_]\w*|\d+\.?\d*(?:[eE][+-]?\d+)?[fFhHuUlL]*|\.\d+(?:[eE][+-]?\d+)?[fFhH]?|##|#|\s+|.',
    re.S,
)
FX_IF_SAFE = re.compile(r"^[\s\d()+\-*/%<>=!&|^~.xXa-fA-F]*$")
FX_TOKEN = re.compile(
    r"""
 (?P<ws>\s+)
|(?P<str>"(?:[^"\\]|\\.)*")
|(?P<num>0[xX][0-9a-fA-F]+[uUlL]*|(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?[fFhHlLuU]*)
|(?P<id>[A-Za-z_]\w*)
|(?P<op><<=|>>=|\+\+|--|\+=|-=|\*=|/=|%=|&=|\|=|\^=|==|!=|<=|>=|&&|\|\||<<|>>|::|[-+*/%<>=!&|^~?:;,.()\[\]{}#@\\$`'])
""",
    re.X | re.S,
)
FX_SCALARS = {
    "float": "f",
    "half": "f",
    "double": "f",
    "fixed": "f",
    "min16float": "f",
    "min10float": "f",
    "int": "i",
    "uint": "i",
    "dword": "i",
    "min16int": "i",
    "min12int": "i",
    "min16uint": "i",
    "bool": "b",
}
FX_TYPE_RE = re.compile(
    r"(float|half|double|fixed|min16float|min10float|int|uint|dword|min16int|min12int|min16uint|bool)([1-4])?(?:x([1-4]))?$"
)
FX_QUALIFIERS = {
    "static",
    "uniform",
    "const",
    "shared",
    "extern",
    "volatile",
    "inline",
    "row_major",
    "column_major",
    "precise",
    "groupshared",
    "nointerpolation",
    "linear",
    "centroid",
    "noperspective",
    "sample",
    "in",
    "out",
    "inout",
    "snorm",
    "unorm",
}
FX_SPECIAL_TYPES = (
    "texture",
    "texture1d",
    "texture2d",
    "texture3d",
    "texturecube",
    "sampler",
    "sampler1d",
    "sampler2d",
    "sampler3d",
    "samplercube",
    "sampler_state",
    "samplerstate",
    "string",
    "void",
    "vector",
    "matrix",
    "pixelshader",
    "vertexshader",
)
FX_BINARY_PRECEDENCE = {
    "||": 1,
    "&&": 2,
    "|": 3,
    "^": 4,
    "&": 5,
    "==": 6,
    "!=": 6,
    "<": 7,
    ">": 7,
    "<=": 7,
    ">=": 7,
    "<<": 8,
    ">>": 8,
    "+": 9,
    "-": 9,
    "*": 10,
    "/": 10,
    "%": 10,
}
FX_ASSIGN_OPS = {"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="}
FX_SWIZZLE = {"x": 0, "y": 1, "z": 2, "w": 3, "r": 0, "g": 1, "b": 2, "a": 3}
FX_LOOP_LIMIT = 4096
FX_CALL_DEPTH = 48


def fx_read_text(path):
    with open(path, "rb") as handle:
        raw = handle.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    for encoding in ("utf-8", "cp932"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    return raw.decode("cp932", errors="replace")


def fx_strip_comments(text):
    def replace(match):
        found = match.group(0)
        if found.startswith('"'):
            return found
        if found.startswith("/*"):
            return "\n" * found.count("\n") or " "
        return ""

    return FX_COMMENT.sub(replace, text)


class FxPreprocessor:
    def __init__(self, root_dir, defines=None):
        self.root_dir = root_dir
        self.macros = {}
        self.files = []
        self.depth = 0
        for name, value in (defines or {}).items():
            self.macros[name] = (None, str(value))

    def run_file(self, path):
        self.files.append(os.path.abspath(path))
        return self.run_text(fx_read_text(path), os.path.dirname(os.path.abspath(path)))

    def resolve_include(self, name, base_dir):
        name = name.replace("\\", "/")
        for folder in (base_dir, self.root_dir):
            candidate = os.path.join(folder, name)
            if os.path.isfile(candidate):
                return candidate
            folder_path = os.path.dirname(candidate)
            wanted = os.path.basename(candidate).lower()
            if os.path.isdir(folder_path):
                for entry in os.listdir(folder_path):
                    if entry.lower() == wanted:
                        return os.path.join(folder_path, entry)
        return None

    def run_text(self, text, base_dir):
        text = fx_strip_comments(text)
        text = text.replace("\\\r\n", "").replace("\\\n", "").replace("\r", "")
        lines = text.split("\n")
        out = []
        stack = []
        for line in lines:
            stripped = line.strip()
            active = stack[-1][2] if stack else True
            if stripped.startswith("#"):
                directive = re.match(r"#\s*(\w+)\s*(.*)", stripped, re.S)
                out.append("")
                if not directive:
                    continue
                name, rest = directive.group(1), directive.group(2).strip()
                if name in ("if", "ifdef", "ifndef"):
                    if not active:
                        condition = False
                    elif name == "ifdef":
                        condition = bool(rest) and rest.split()[0] in self.macros
                    elif name == "ifndef":
                        condition = not (bool(rest) and rest.split()[0] in self.macros)
                    else:
                        condition = self.eval_condition(rest)
                    stack.append([active, condition, active and condition])
                elif name == "elif":
                    if not stack:
                        continue
                    parent, taken, _current = stack[-1]
                    current = parent and not taken and self.eval_condition(rest)
                    stack[-1] = [parent, taken or current, current]
                elif name == "else":
                    if not stack:
                        continue
                    parent, taken, _current = stack[-1]
                    stack[-1] = [parent, True, parent and not taken]
                elif name == "endif":
                    if stack:
                        stack.pop()
                elif not active:
                    continue
                elif name == "define":
                    self.define(rest)
                elif name == "undef":
                    self.macros.pop(rest.split()[0] if rest else "", None)
                elif name == "include":
                    found = re.match(r'["<]([^">]+)[">]', rest)
                    if not found:
                        continue
                    path = self.resolve_include(found.group(1), base_dir)
                    if path is None:
                        raise FxError("include not found: %s" % found.group(1))
                    if self.depth > 16:
                        raise FxError("include nesting too deep")
                    self.depth += 1
                    self.files.append(os.path.abspath(path))
                    try:
                        out.append(
                            self.run_text(
                                fx_read_text(path), os.path.dirname(os.path.abspath(path))
                            )
                        )
                    finally:
                        self.depth -= 1
                continue
            out.append(self.expand(line) if active else "")
        return "\n".join(out)

    def define(self, rest):
        match = re.match(r"([A-Za-z_]\w*)(\([^)]*\))?\s*(.*)", rest, re.S)
        if not match:
            return
        params = None
        if match.group(2) is not None:
            params = [p.strip() for p in match.group(2)[1:-1].split(",") if p.strip()]
        self.macros[match.group(1)] = (params, match.group(3).strip())

    def expand(self, text, hidden=frozenset()):
        if not self.macros:
            return text
        tokens = FX_PP_TOKEN.findall(text)
        out = []
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token in self.macros and token not in hidden and (token[0].isalpha() or token[0] == "_"):
                params, body = self.macros[token]
                if params is None:
                    out.append(self.expand(body, hidden | {token}))
                    i += 1
                    continue
                j = i + 1
                while j < len(tokens) and tokens[j].isspace():
                    j += 1
                if j < len(tokens) and tokens[j] == "(":
                    args, end = self.collect_args(tokens, j)
                    if args is not None:
                        expanded = [self.expand(a, hidden) for a in args]
                        body_text = self.substitute(body, params, args, expanded)
                        out.append(self.expand(body_text, hidden | {token}))
                        i = end
                        continue
            out.append(token)
            i += 1
        return "".join(out)

    def collect_args(self, tokens, start):
        depth = 0
        args = []
        current = []
        i = start
        while i < len(tokens):
            token = tokens[i]
            if token == "(":
                depth += 1
                if depth > 1:
                    current.append(token)
            elif token == ")":
                depth -= 1
                if depth == 0:
                    args.append("".join(current).strip())
                    return ([] if args == [""] else args), i + 1
                current.append(token)
            elif token == "," and depth == 1:
                args.append("".join(current).strip())
                current = []
            else:
                current.append(token)
            i += 1
        return None, start

    def substitute(self, body, params, raw, expanded):
        tokens = FX_PP_TOKEN.findall(body)
        out = []
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token == "#" and i + 1 < len(tokens) and tokens[i + 1] in params:
                out.append('"%s"' % raw[params.index(tokens[i + 1])])
                i += 2
                continue
            if token == "##":
                while out and out[-1].isspace():
                    out.pop()
                i += 1
                while i < len(tokens) and tokens[i].isspace():
                    i += 1
                if i < len(tokens):
                    nxt = tokens[i]
                    out.append(raw[params.index(nxt)] if nxt in params else nxt)
                    i += 1
                continue
            if token in params:
                index = params.index(token)
                out.append(expanded[index] if index < len(expanded) else "")
            else:
                out.append(token)
            i += 1
        return "".join(out)

    def eval_condition(self, expression):
        expression = re.sub(
            r"defined\s*\(\s*(\w+)\s*\)|defined\s+(\w+)",
            lambda m: "1" if (m.group(1) or m.group(2)) in self.macros else "0",
            expression,
        )
        expression = self.expand(expression)
        expression = re.sub(r"(\d+)[uUlL]+\b", r"\1", expression)
        expression = re.sub(r"\b(?!0[xX])[A-Za-z_]\w*\b", "0", expression)
        if not FX_IF_SAFE.match(expression):
            return False
        expression = expression.replace("&&", " and ").replace("||", " or ")
        expression = re.sub(r"!(?!=)", " not ", expression)
        expression = re.sub(r"(?<![/])/(?![/])", "//", expression)
        try:
            return bool(eval(expression, {"__builtins__": {}}, {}))
        except Exception:
            return False


def fx_tokenize(text):
    tokens = []
    for match in FX_TOKEN.finditer(text):
        kind = match.lastgroup
        if kind == "ws":
            continue
        tokens.append((kind, match.group(0)))
    tokens.append(("eof", ""))
    return tokens


class FxStruct:
    __slots__ = ("name", "members")

    def __init__(self, name):
        self.name = name
        self.members = []


class FxParam:
    __slots__ = ("name", "type", "semantic", "quals", "default", "array")

    def __init__(self, name, type_name, semantic, quals, default, array):
        self.name = name
        self.type = type_name
        self.semantic = semantic
        self.quals = quals
        self.default = default
        self.array = array


class FxFunction:
    __slots__ = ("name", "ret", "semantic", "params", "body")

    def __init__(self, name, ret, semantic, params, body):
        self.name = name
        self.ret = ret
        self.semantic = semantic
        self.params = params
        self.body = body


class FxGlobal:
    __slots__ = ("name", "type", "quals", "semantic", "register", "ann", "init", "array", "order")

    def __init__(self, name, type_name, quals, semantic, register, ann, init, array, order):
        self.name = name
        self.type = type_name
        self.quals = quals
        self.semantic = semantic
        self.register = register
        self.ann = ann
        self.init = init
        self.array = array
        self.order = order


class FxPass:
    __slots__ = ("name", "ann", "states", "vs", "ps")

    def __init__(self, name, ann):
        self.name = name
        self.ann = ann
        self.states = {}
        self.vs = None
        self.ps = None


class FxTechnique:
    __slots__ = ("name", "ann", "passes")

    def __init__(self, name, ann):
        self.name = name
        self.ann = ann
        self.passes = []


class FxProgram:
    def __init__(self):
        self.structs = {}
        self.typedefs = {}
        self.functions = {}
        self.globals = {}
        self.techniques = []
        self.folder = ""
        self.files = []

    def is_type(self, name):
        return (
            name in self.structs
            or name in self.typedefs
            or name in FX_SCALARS
            or FX_TYPE_RE.match(name) is not None
            or name.lower() in FX_SPECIAL_TYPES
        )


class FxParser:
    def __init__(self, tokens, program):
        self.tokens = tokens
        self.pos = 0
        self.program = program
        self.order = 0

    def peek(self, offset=0):
        index = min(self.pos + offset, len(self.tokens) - 1)
        return self.tokens[index]

    @property
    def text(self):
        return self.tokens[self.pos][1]

    @property
    def kind(self):
        return self.tokens[self.pos][0]

    def advance(self):
        token = self.tokens[self.pos]
        if self.pos < len(self.tokens) - 1:
            self.pos += 1
        return token

    def accept(self, text):
        if self.kind != "str" and self.text == text:
            self.advance()
            return True
        return False

    def expect(self, text):
        if self.kind == "str" or self.text != text:
            raise FxError("expected '%s' but found '%s' (token %d)" % (text, self.text, self.pos))
        return self.advance()

    def ident(self):
        if self.kind != "id":
            raise FxError("expected identifier but found '%s' (token %d)" % (self.text, self.pos))
        return self.advance()[1]

    def parse(self):
        while self.kind != "eof":
            if self.accept(";"):
                continue
            if self.text == "struct":
                self.parse_struct()
            elif self.text in ("technique", "technique9", "technique10", "technique11"):
                self.parse_technique()
            elif self.text == "typedef":
                self.advance()
                quals = self.parse_qualifiers()
                type_name = self.parse_type()
                name = self.ident()
                self.program.typedefs[name] = type_name
                self.expect(";")
            elif self.text == "cbuffer" or self.text == "tbuffer":
                self.advance()
                self.ident()
                if self.accept(":"):
                    self.parse_semantic_after_colon()
                self.expect("{")
                while not self.accept("}"):
                    self.parse_declaration_or_function()
            else:
                self.parse_declaration_or_function()
        return self.program

    def parse_qualifiers(self):
        quals = set()
        while self.kind == "id" and self.text in FX_QUALIFIERS:
            quals.add(self.advance()[1])
        return quals

    def parse_type(self):
        name = self.ident()
        if name == "unsigned":
            name = self.ident() if self.kind == "id" and self.text in ("int", "short", "char") else "int"
            return "uint"
        if name == "struct":
            return self.ident()
        lower = name.lower()
        if lower in ("vector", "matrix") and self.text == "<":
            self.advance()
            base = self.ident()
            dims = []
            while self.accept(","):
                dims.append(self.advance()[1])
            self.expect(">")
            if lower == "vector":
                return "%s%s" % (base, dims[0] if dims else "4")
            return "%s%sx%s" % (base, dims[0] if dims else "4", dims[1] if len(dims) > 1 else "4")
        if lower in ("texture1d", "texture2d", "texture3d", "texturecube", "texture") and self.text == "<":
            self.advance()
            depth = 1
            while depth and self.kind != "eof":
                if self.text == "<":
                    depth += 1
                elif self.text == ">":
                    depth -= 1
                self.advance()
        return self.program.typedefs.get(name, name)

    def parse_struct(self):
        self.expect("struct")
        name = self.ident()
        struct = FxStruct(name)
        self.program.structs[name] = struct
        self.expect("{")
        while not self.accept("}"):
            quals = self.parse_qualifiers()
            type_name = self.parse_type()
            while True:
                member = self.ident()
                array = None
                if self.accept("["):
                    array = self.parse_expression()
                    self.expect("]")
                semantic = None
                if self.accept(":"):
                    semantic = self.parse_semantic_after_colon()[0]
                struct.members.append((member, type_name, semantic, array))
                if not self.accept(","):
                    break
            self.expect(";")
        if self.kind == "id":
            self.ident()
        self.accept(";")

    def parse_semantic_after_colon(self):
        name = self.ident()
        if name in ("register", "packoffset") and self.accept("("):
            parts = []
            while not self.accept(")"):
                parts.append(self.advance()[1])
            return None, "".join(parts)
        return name, None

    def parse_annotations(self):
        ann = {}
        if not self.accept("<"):
            return ann
        while not self.accept(">"):
            if self.accept(";"):
                continue
            self.parse_qualifiers()
            self.parse_type()
            name = self.ident()
            if self.accept("["):
                while not self.accept("]"):
                    self.advance()
            self.expect("=")
            ann[name] = self.parse_annotation_value()
            self.expect(";")
        return ann

    def parse_annotation_value(self):
        if self.text == "{":
            return self.parse_initializer()
        return self.parse_ternary_no_gt()

    def parse_ternary_no_gt(self):
        saved = FX_BINARY_PRECEDENCE.pop(">")
        saved_ge = FX_BINARY_PRECEDENCE.pop(">=")
        try:
            return self.parse_assign()
        finally:
            FX_BINARY_PRECEDENCE[">"] = saved
            FX_BINARY_PRECEDENCE[">="] = saved_ge

    def parse_declaration_or_function(self):
        if self.text == "[":
            self.skip_attribute()
        quals = self.parse_qualifiers()
        type_name = self.parse_type()
        if self.text == "(" and type_name not in self.program.structs:
            raise FxError("unexpected '(' after %s" % type_name)
        name = self.ident()
        if self.text == "(":
            self.parse_function(type_name, name)
            return
        while True:
            self.parse_global_declarator(quals, type_name, name)
            if not self.accept(","):
                break
            name = self.ident()
        self.expect(";")

    def skip_attribute(self):
        self.expect("[")
        depth = 1
        while depth and self.kind != "eof":
            if self.text == "[":
                depth += 1
            elif self.text == "]":
                depth -= 1
            self.advance()

    def parse_global_declarator(self, quals, type_name, name):
        array = None
        if self.accept("["):
            array = self.parse_expression() if self.text != "]" else None
            self.expect("]")
        semantic = None
        register = None
        while self.accept(":"):
            sem, reg = self.parse_semantic_after_colon()
            semantic = sem or semantic
            register = reg or register
        ann = self.parse_annotations()
        init = None
        if self.accept("="):
            if self.text.lower() == "sampler_state":
                self.advance()
                init = ("sampler_state", self.parse_state_block())
            elif self.text == "compile":
                init = self.parse_compile()
            elif self.text == "{":
                init = self.parse_initializer()
            else:
                init = self.parse_assign()
        elif self.text == "{" and type_name.lower().startswith("sampler"):
            init = ("sampler_state", self.parse_state_block())
        self.program.globals[name] = FxGlobal(
            name, type_name, quals, semantic.upper() if semantic else None, register, ann, init, array, self.order
        )
        self.order += 1

    def parse_state_block(self):
        states = {}
        self.expect("{")
        while not self.accept("}"):
            if self.accept(";"):
                continue
            key = self.advance()[1]
            if self.accept("["):
                while not self.accept("]"):
                    self.advance()
            self.expect("=")
            value = []
            depth = 0
            while self.kind != "eof" and not (self.text == ";" and depth == 0):
                if self.text in "(<{":
                    depth += 1
                elif self.text in ")>}":
                    depth -= 1
                value.append(self.advance()[1])
            self.expect(";")
            states[key.lower()] = value
        return states

    def parse_compile(self):
        self.expect("compile")
        profile = self.ident()
        call = self.parse_postfix(self.parse_primary())
        return ("compile", profile, call)

    def parse_initializer(self):
        self.expect("{")
        items = []
        while not self.accept("}"):
            if self.text == "{":
                items.append(self.parse_initializer())
            else:
                items.append(self.parse_assign())
            if not self.accept(","):
                self.expect("}")
                break
        return ("init", items)

    def parse_function(self, ret, name):
        self.expect("(")
        params = []
        if not self.accept(")"):
            if self.text == "void" and self.peek(1)[1] == ")":
                self.advance()
                self.expect(")")
            else:
                while True:
                    quals = self.parse_qualifiers()
                    type_name = self.parse_type()
                    pname = self.ident()
                    array = None
                    if self.accept("["):
                        array = self.parse_expression()
                        self.expect("]")
                    semantic = None
                    while self.accept(":"):
                        semantic = self.parse_semantic_after_colon()[0] or semantic
                    default = None
                    if self.accept("="):
                        default = self.parse_assign()
                    params.append(
                        FxParam(pname, type_name, semantic.upper() if semantic else None, quals, default, array)
                    )
                    if not self.accept(","):
                        break
                self.expect(")")
        semantic = None
        while self.accept(":"):
            semantic = self.parse_semantic_after_colon()[0] or semantic
        if self.accept(";"):
            return
        body = self.parse_block()
        self.program.functions.setdefault(name, []).append(
            FxFunction(name, ret, semantic.upper() if semantic else None, params, body)
        )

    def parse_technique(self):
        self.advance()
        name = self.ident() if self.kind == "id" else ""
        ann = self.parse_annotations()
        technique = FxTechnique(name, ann)
        self.expect("{")
        while not self.accept("}"):
            if self.accept(";"):
                continue
            self.expect("pass")
            pass_name = self.ident() if self.kind == "id" else ""
            pass_ann = self.parse_annotations()
            fx_pass = FxPass(pass_name, pass_ann)
            self.expect("{")
            while not self.accept("}"):
                if self.accept(";"):
                    continue
                key = self.advance()[1]
                if self.text == "(":
                    self.expect("(")
                    depth = 1
                    inner = []
                    while depth and self.kind != "eof":
                        if self.text == "(":
                            depth += 1
                        elif self.text == ")":
                            depth -= 1
                            if depth == 0:
                                self.advance()
                                break
                        inner.append(self.advance())
                    self.accept(";")
                    self.parse_set_shader(fx_pass, key, inner)
                    continue
                if self.accept("["):
                    while not self.accept("]"):
                        self.advance()
                self.expect("=")
                if self.text == "compile":
                    value = self.parse_compile()
                    self.expect(";")
                else:
                    value = []
                    while self.kind != "eof" and self.text != ";":
                        value.append(self.advance()[1])
                    self.expect(";")
                lower = key.lower()
                if lower == "vertexshader":
                    fx_pass.vs = value if isinstance(value, tuple) else None
                elif lower == "pixelshader":
                    fx_pass.ps = value if isinstance(value, tuple) else None
                else:
                    fx_pass.states[lower] = " ".join(value) if isinstance(value, list) else value
            technique.passes.append(fx_pass)
        self.program.techniques.append(technique)

    def parse_set_shader(self, fx_pass, key, inner):
        lower = key.lower()
        if lower not in ("setvertexshader", "setpixelshader"):
            return
        text = " ".join(t[1] for t in inner)
        match = re.match(r"CompileShader\s*\(\s*(\w+)\s*,\s*(.*)\)\s*$", text)
        if not match:
            return
        sub = FxParser(fx_tokenize(match.group(2)), self.program)
        call = sub.parse_postfix(sub.parse_primary())
        value = ("compile", match.group(1), call)
        if lower == "setvertexshader":
            fx_pass.vs = value
        else:
            fx_pass.ps = value

    def parse_block(self):
        self.expect("{")
        statements = []
        while not self.accept("}"):
            if self.kind == "eof":
                raise FxError("unexpected end of file in block")
            statements.append(self.parse_statement())
        return ("block", statements)

    def looks_like_declaration(self):
        if self.kind != "id":
            return False
        offset = 0
        while self.peek(offset)[0] == "id" and self.peek(offset)[1] in FX_QUALIFIERS:
            offset += 1
        kind, text = self.peek(offset)
        if kind != "id" or not self.program.is_type(text):
            return False
        nxt = self.peek(offset + 1)
        if nxt[1] == "<" and text.lower() in ("vector", "matrix"):
            return True
        return nxt[0] == "id"

    def parse_statement(self):
        text = self.text
        if self.kind == "str":
            return ("expr", self.parse_expression_statement())
        if text == "{":
            return self.parse_block()
        if text == ";":
            self.advance()
            return ("empty",)
        if text == "[":
            self.skip_attribute()
            return self.parse_statement()
        if text == "if":
            self.advance()
            self.expect("(")
            condition = self.parse_expression()
            self.expect(")")
            then = self.parse_statement()
            other = None
            if self.accept("else"):
                other = self.parse_statement()
            return ("if", condition, then, other)
        if text == "for":
            self.advance()
            self.expect("(")
            if self.accept(";"):
                init = ("empty",)
            elif self.looks_like_declaration():
                init = self.parse_local_declaration()
            else:
                init = ("expr", self.parse_expression())
                self.expect(";")
            condition = None if self.text == ";" else self.parse_expression()
            self.expect(";")
            step = None if self.text == ")" else self.parse_expression()
            self.expect(")")
            return ("for", init, condition, step, self.parse_statement())
        if text == "while":
            self.advance()
            self.expect("(")
            condition = self.parse_expression()
            self.expect(")")
            return ("for", ("empty",), condition, None, self.parse_statement())
        if text == "do":
            self.advance()
            body = self.parse_statement()
            self.expect("while")
            self.expect("(")
            condition = self.parse_expression()
            self.expect(")")
            self.expect(";")
            return ("do", body, condition)
        if text == "return":
            self.advance()
            value = None if self.text == ";" else self.parse_expression()
            self.expect(";")
            return ("return", value)
        if text in ("break", "continue", "discard"):
            self.advance()
            self.expect(";")
            return (text,)
        if text == "struct":
            self.parse_struct()
            return ("empty",)
        if self.looks_like_declaration():
            return self.parse_local_declaration()
        return ("expr", self.parse_expression_statement())

    def parse_expression_statement(self):
        expression = self.parse_expression()
        self.expect(";")
        return expression

    def parse_local_declaration(self):
        quals = self.parse_qualifiers()
        type_name = self.parse_type()
        declarators = []
        while True:
            name = self.ident()
            array = None
            if self.accept("["):
                array = self.parse_expression() if self.text != "]" else None
                self.expect("]")
            if self.accept(":"):
                self.parse_semantic_after_colon()
            init = None
            if self.accept("="):
                init = self.parse_initializer() if self.text == "{" else self.parse_assign()
            declarators.append((name, array, init))
            if not self.accept(","):
                break
        self.expect(";")
        return ("decl", type_name, declarators, quals)

    def parse_expression(self):
        first = self.parse_assign()
        if self.text != ",":
            return first
        items = [first]
        while self.accept(","):
            items.append(self.parse_assign())
        return ("comma", items)

    def parse_assign(self):
        left = self.parse_ternary()
        if self.kind == "op" and self.text in FX_ASSIGN_OPS:
            op = self.advance()[1]
            return ("assign", op, left, self.parse_assign())
        return left

    def parse_ternary(self):
        condition = self.parse_binary(1)
        if self.accept("?"):
            a = self.parse_assign()
            self.expect(":")
            b = self.parse_assign()
            return ("tern", condition, a, b)
        return condition

    def parse_binary(self, minimum):
        left = self.parse_unary()
        while True:
            op = self.text
            precedence = FX_BINARY_PRECEDENCE.get(op) if self.kind == "op" else None
            if precedence is None or precedence < minimum:
                return left
            self.advance()
            right = self.parse_binary(precedence + 1)
            left = ("bin", op, left, right)

    def cast_ahead(self):
        if self.text != "(" or self.peek(1)[0] != "id":
            return None
        offset = 1
        while self.peek(offset)[0] == "id" and self.peek(offset)[1] in FX_QUALIFIERS:
            offset += 1
        name = self.peek(offset)[1]
        if not self.program.is_type(name) or name.lower() in ("texture", "sampler", "string", "void"):
            return None
        if self.peek(offset + 1)[1] == ")":
            return offset + 2
        if self.peek(offset + 1)[1] == "[":
            return None
        return None

    def parse_unary(self):
        if self.kind == "op":
            op = self.text
            if op in ("-", "+", "!", "~"):
                self.advance()
                return ("un", op, self.parse_unary())
            if op in ("++", "--"):
                self.advance()
                return ("pre", op, self.parse_unary())
            skip = self.cast_ahead()
            if skip is not None:
                self.advance()
                self.parse_qualifiers()
                type_name = self.parse_type()
                self.expect(")")
                return ("cast", type_name, self.parse_unary())
        return self.parse_postfix(self.parse_primary())

    def parse_primary(self):
        kind, text = self.advance()
        if kind == "num":
            raw = text.rstrip("uUlLhH")
            if raw.lower().startswith("0x"):
                return ("num", float(int(raw, 16)), "i")
            if raw.endswith(("f", "F")):
                return ("num", float(raw[:-1]), "f")
            if "." in raw or "e" in raw or "E" in raw:
                return ("num", float(raw), "f")
            return ("num", float(int(raw)), "i")
        if kind == "str":
            parts = [text[1:-1]]
            while self.kind == "str":
                parts.append(self.advance()[1][1:-1])
            joined = "".join(parts)
            return ("str", joined.replace('\\"', '"').replace("\\\\", "\\"))
        if kind == "id":
            if text in ("true", "TRUE"):
                return ("num", 1.0, "b")
            if text in ("false", "FALSE"):
                return ("num", 0.0, "b")
            if self.text == "(" and self.program.is_type(text):
                return ("ctor", self.program.typedefs.get(text, text), self.parse_args())
            if self.text == "<" and text.lower() in ("vector", "matrix"):
                self.pos -= 1
                type_name = self.parse_type()
                return ("ctor", type_name, self.parse_args())
            if self.text == "(":
                return ("call", text, self.parse_args())
            return ("id", text)
        if text == "(":
            inner = self.parse_expression()
            self.expect(")")
            return inner
        if text == "{":
            self.pos -= 1
            return self.parse_initializer()
        raise FxError("unexpected token '%s' (token %d)" % (text, self.pos))

    def parse_args(self):
        self.expect("(")
        args = []
        if self.accept(")"):
            return args
        while True:
            args.append(self.parse_assign())
            if self.accept(")"):
                return args
            self.expect(",")

    def parse_postfix(self, node):
        while True:
            if self.accept("."):
                name = self.ident()
                if self.text == "(":
                    node = ("mcall", node, name, self.parse_args())
                else:
                    node = ("member", node, name)
            elif self.accept("["):
                index = self.parse_expression()
                self.expect("]")
                node = ("index", node, index)
            elif self.kind == "op" and self.text in ("++", "--"):
                node = ("post", self.advance()[1], node)
            else:
                return node


def fx_parse_file(path, defines=None):
    folder = os.path.dirname(os.path.abspath(path))
    pre = FxPreprocessor(folder, defines)
    text = pre.run_file(path)
    program = FxProgram()
    program.folder = folder
    program.files = list(dict.fromkeys(pre.files))
    FxParser(fx_tokenize(text), program).parse()
    return program


def fx_resolve_type(name, program):
    if isinstance(name, FxStruct):
        return name
    name = program.typedefs.get(name, name)
    struct = program.structs.get(name)
    if struct is not None:
        return struct
    match = FX_TYPE_RE.match(name)
    if match:
        base = FX_SCALARS[match.group(1)]
        rows, cols = match.group(2), match.group(3)
        if rows is None:
            return (base, ())
        if cols is None:
            return (base, (int(rows),))
        return (base, (int(rows), int(cols)))
    lower = name.lower()
    if lower == "vector":
        return ("f", (4,))
    if lower == "matrix":
        return ("f", (4, 4))
    if lower.startswith("texture"):
        return "texture"
    if lower.startswith("sampler"):
        return "sampler"
    if lower == "string":
        return "string"
    if lower == "void":
        return "void"
    raise FxError("unknown type %s" % name)


def fx_count(dims):
    if not dims:
        return 1
    if len(dims) == 1:
        return dims[0]
    return dims[0] * dims[1]


def fx_is_numeric(t):
    return isinstance(t, tuple) and len(t) == 2 and isinstance(t[1], tuple)


class FxValue:
    __slots__ = ("t", "c")

    def __init__(self, t, c):
        self.t = t
        self.c = c


def fx_array_type(elem, size):
    return ("array", elem, size)


def fx_is_array(t):
    return isinstance(t, tuple) and len(t) == 3 and t[0] == "array"


class FxSampler:
    __slots__ = ("name", "texture", "states", "register")

    def __init__(self, name, texture, states, register):
        self.name = name
        self.texture = texture
        self.states = states
        self.register = register

    def state(self, key, default=""):
        value = self.states.get(key)
        if value is None:
            return default
        return (" ".join(value) if isinstance(value, list) else str(value)).upper()

    def point(self):
        return any(
            self.state(k, "LINEAR") in ("NONE", "POINT")
            for k in ("magfilter", "minfilter")
        )

    def address(self):
        value = self.state("addressu", "WRAP")
        if value in ("CLAMP", "BORDER"):
            return "CLAMP"
        if value == "MIRROR":
            return "MIRROR"
        return "WRAP"


class FxNumpyBackend:
    vectorized = True

    def const(self, x):
        return np.float64(x)

    def known(self, x):
        if np.ndim(x) == 0:
            return float(x)
        return None

    def any_true(self, x):
        return bool(np.any(np.asarray(x) != 0))

    def add(self, a, b):
        return np.add(a, b)

    def sub(self, a, b):
        return np.subtract(a, b)

    def mul(self, a, b):
        return np.multiply(a, b)

    def div(self, a, b):
        return np.divide(a, b)

    def neg(self, a):
        return np.negative(a)

    def mod(self, a, b):
        return np.fmod(a, b)

    def idiv(self, a, b):
        return np.trunc(np.divide(a, b))

    def pow(self, a, b):
        return np.power(a, b)

    def unary(self, name, a):
        return FX_NP_UNARY[name](a)

    def atan2(self, a, b):
        return np.arctan2(a, b)

    def min(self, a, b):
        return np.minimum(a, b)

    def max(self, a, b):
        return np.maximum(a, b)

    def cmp(self, op, a, b):
        return FX_NP_CMP[op](a, b).astype(np.float64)

    def bitop(self, op, a, b):
        x = np.asarray(a).astype(np.int64)
        y = np.asarray(b).astype(np.int64)
        if op == "&":
            r = x & y
        elif op == "|":
            r = x | y
        elif op == "^":
            r = x ^ y
        elif op == "<<":
            r = x << np.clip(y, 0, 62)
        else:
            r = x >> np.clip(y, 0, 62)
        return r.astype(np.float64)

    def bitnot(self, a):
        return (~np.asarray(a).astype(np.int64)).astype(np.float64)

    def select(self, c, a, b):
        return np.where(np.asarray(c) != 0, a, b)

    def truth(self, a):
        return (np.asarray(a) != 0).astype(np.float64)


FX_NP_UNARY = {
    "abs": np.abs,
    "sign": np.sign,
    "floor": np.floor,
    "ceil": np.ceil,
    "trunc": np.trunc,
    "round": np.round,
    "frac": lambda x: x - np.floor(x),
    "sqrt": lambda x: np.sqrt(x),
    "rsqrt": lambda x: 1.0 / np.sqrt(x),
    "exp": np.exp,
    "exp2": np.exp2,
    "log": np.log,
    "log2": np.log2,
    "log10": np.log10,
    "sin": np.sin,
    "cos": np.cos,
    "tan": np.tan,
    "asin": lambda x: np.arcsin(np.clip(x, -1, 1)),
    "acos": lambda x: np.arccos(np.clip(x, -1, 1)),
    "atan": np.arctan,
    "sinh": np.sinh,
    "cosh": np.cosh,
    "tanh": np.tanh,
}
FX_NP_CMP = {
    "<": np.less,
    "<=": np.less_equal,
    ">": np.greater,
    ">=": np.greater_equal,
    "==": np.equal,
    "!=": np.not_equal,
}


class FxFrame:
    __slots__ = ("function", "scopes", "ret", "done", "loops")

    def __init__(self, function):
        self.function = function
        self.scopes = [{}]
        self.ret = None
        self.done = 0.0
        self.loops = []


class FxLoop:
    __slots__ = ("brk", "cont")

    def __init__(self):
        self.brk = 0.0
        self.cont = 0.0


class FxExec:
    def __init__(self, program, backend, env):
        self.program = program
        self.B = backend
        self.env = env
        self.cache = {}
        self.overrides = {}
        self.frames = []
        self.mask = 1.0
        self.discard = 0.0
        self.depth = 0
        self.evaluating = set()

    def zero(self, t):
        B = self.B
        if fx_is_numeric(t):
            return FxValue(t, [B.const(0.0)] * fx_count(t[1]))
        if isinstance(t, FxStruct):
            members = {}
            for name, type_name, _sem, array in t.members:
                mt = fx_resolve_type(type_name, self.program)
                if array is not None:
                    size = int(self.const_int(array))
                    members[name] = FxValue(fx_array_type(mt, size), [self.zero(mt) for _ in range(size)])
                else:
                    members[name] = self.zero(mt)
            return FxValue(t, members)
        if fx_is_array(t):
            return FxValue(t, [self.zero(t[1]) for _ in range(t[2])])
        return FxValue(t, None)

    def const_int(self, expr):
        value = self.eval(expr)
        known = self.B.known(value.c[0])
        if known is None:
            raise FxUnsupported("array size is not constant")
        return int(known)

    def scalar(self, x, base="f"):
        return FxValue((base, ()), [x])

    def active(self):
        B = self.B
        mask = self.mask
        if not self.frames:
            return mask
        frame = self.frames[-1]
        exclusions = [frame.done]
        for loop in frame.loops:
            exclusions.append(loop.brk)
            exclusions.append(loop.cont)
        for ex in exclusions:
            known = B.known(ex)
            if known is not None:
                if known == 0.0:
                    continue
                return 0.0
            inv = B.sub(1.0, ex)
            known_mask = B.known(mask)
            if known_mask is not None:
                mask = inv if known_mask != 0.0 else 0.0
            else:
                mask = B.mul(mask, inv)
        return mask

    def inactive(self):
        known = self.B.known(self.active())
        return known is not None and known == 0.0

    def lookup(self, name):
        if self.frames:
            for scope in reversed(self.frames[-1].scopes):
                if name in scope:
                    return scope, name
        return None, name

    def global_value(self, name):
        if name in self.overrides:
            return self.overrides[name]
        if name in self.cache:
            return self.cache[name]
        g = self.program.globals.get(name)
        if g is None:
            raise FxError("unknown identifier %s" % name)
        if name in self.evaluating:
            raise FxError("recursive global %s" % name)
        self.evaluating.add(name)
        saved_frames, saved_mask = self.frames, self.mask
        self.frames, self.mask = [], 1.0
        try:
            value = self.env.global_value(self, g)
            if value is None:
                value = self.default_global(g)
        finally:
            self.frames, self.mask = saved_frames, saved_mask
            self.evaluating.discard(name)
        self.cache[name] = value
        return value

    def default_global(self, g):
        t = fx_resolve_type(g.type, self.program)
        if t == "sampler":
            texture = None
            states = {}
            if g.init is not None and g.init[0] == "sampler_state":
                states = g.init[1]
                tex = states.get("texture")
                if tex:
                    texture = "".join(tex).strip("<>() ")
            return FxValue("sampler", FxSampler(g.name, texture, states, g.register))
        if t == "texture":
            return FxValue("texture", g.name)
        if t == "string":
            return FxValue("string", self.eval(g.init).c if g.init is not None else "")
        if g.array is not None and not fx_is_array(t):
            size = self.const_int(g.array) if g.array is not None else None
            at = fx_array_type(t, size)
            if g.init is not None:
                return self.convert(self.eval_init(g.init, at), at)
            return self.zero(at)
        if g.init is None:
            return self.zero(t)
        if g.init[0] == "init":
            return self.eval_init(g.init, t)
        if g.init[0] == "compile":
            return FxValue("void", None)
        return self.convert(self.eval(g.init), t)

    def eval_init(self, node, t):
        if fx_is_numeric(t):
            comps = []
            for item in self.flatten_init(node):
                comps.extend(item.c if isinstance(item.c, list) else [item.c])
            n = fx_count(t[1])
            if len(comps) == 1 and n > 1:
                comps = comps * n
            comps = (comps + [self.B.const(0.0)] * n)[:n]
            return self.convert(FxValue(("f", t[1]), comps), t)
        if fx_is_array(t):
            items = node[1] if node[0] == "init" else [node]
            elems = []
            flat = None
            if fx_is_numeric(t[1]) and items and not (isinstance(items[0], tuple) and items[0][0] == "init"):
                flat = []
                for item in items:
                    flat.extend(self.eval(item).c)
                per = fx_count(t[1][1])
                if len(flat) >= per * t[2] and len(items) != t[2]:
                    for k in range(t[2]):
                        elems.append(self.convert(FxValue(("f", t[1][1]), flat[k * per:(k + 1) * per]), t[1]))
                    return FxValue(t, elems)
            for k in range(t[2]):
                if k < len(items):
                    item = items[k]
                    if isinstance(item, tuple) and item[0] == "init":
                        elems.append(self.eval_init(item, t[1]))
                    else:
                        elems.append(self.convert(self.eval(item), t[1]))
                else:
                    elems.append(self.zero(t[1]))
            return FxValue(t, elems)
        if isinstance(t, FxStruct):
            items = node[1] if node[0] == "init" else [node]
            value = self.zero(t)
            flat = []
            for item in items:
                if isinstance(item, tuple) and item[0] == "init":
                    flat.append(item)
                else:
                    flat.append(self.eval(item))
            k = 0
            for name, type_name, _sem, _array in t.members:
                mt = value.c[name].t
                if k >= len(flat):
                    break
                item = flat[k]
                if isinstance(item, tuple):
                    value.c[name] = self.eval_init(item, mt)
                    k += 1
                elif fx_is_numeric(mt) and fx_is_numeric(item.t) and fx_count(item.t[1]) < fx_count(mt[1]):
                    comps = []
                    while k < len(flat) and len(comps) < fx_count(mt[1]):
                        comps.extend(flat[k].c)
                        k += 1
                    value.c[name] = self.convert(FxValue(("f", mt[1]), comps[: fx_count(mt[1])]), mt)
                else:
                    value.c[name] = self.convert(item, mt)
                    k += 1
            return value
        return self.eval(node)

    def flatten_init(self, node):
        if isinstance(node, tuple) and node[0] == "init":
            out = []
            for item in node[1]:
                out.extend(self.flatten_init(item))
            return out
        return [self.eval(node)]

    def convert(self, value, t):
        B = self.B
        if t is None or t == "void":
            return value
        if fx_is_numeric(t):
            if not fx_is_numeric(value.t):
                if isinstance(value.t, FxStruct):
                    raise FxError("cannot convert struct to %s" % (t,))
                raise FxError("cannot convert %s to %s" % (value.t, t))
            base, dims = t
            sbase, sdims = value.t
            comps = value.c
            n = fx_count(dims)
            if len(dims) == 2 and len(sdims) == 2 and sdims != dims:
                r, c = dims
                sr, sc = sdims
                out = []
                for i in range(r):
                    for j in range(c):
                        out.append(comps[i * sc + j] if i < sr and j < sc else B.const(0.0))
                comps = out
            elif len(comps) == 1 and n > 1:
                comps = comps * n
            elif len(comps) >= n:
                comps = comps[:n]
            else:
                comps = comps + [B.const(0.0)] * (n - len(comps))
            if base != sbase:
                if base == "i":
                    comps = [B.unary("trunc", x) for x in comps]
                elif base == "b":
                    comps = [B.truth(x) for x in comps]
            return FxValue(t, list(comps))
        if isinstance(t, FxStruct):
            if isinstance(value.t, FxStruct):
                return FxValue(t, dict(value.c))
            if fx_is_numeric(value.t):
                out = {}
                for name, type_name, _sem, array in t.members:
                    mt = fx_resolve_type(type_name, self.program)
                    if array is not None:
                        size = int(self.const_int(array))
                        out[name] = FxValue(fx_array_type(mt, size), [self.convert(value, mt) for _ in range(size)])
                    elif fx_is_numeric(mt):
                        out[name] = self.convert(FxValue(value.t, [value.c[0]]), mt)
                    else:
                        out[name] = self.convert(value, mt)
                return FxValue(t, out)
        if fx_is_array(t):
            if fx_is_array(value.t):
                return FxValue(t, [self.convert(v, t[1]) for v in value.c][: t[2]])
        return value

    def run_function(self, function, args):
        if self.depth > FX_CALL_DEPTH:
            raise FxUnsupported("call depth exceeded")
        frame = FxFrame(function)
        scope = frame.scopes[0]
        for param, arg in zip(function.params, args):
            pt = fx_resolve_type(param.type, self.program)
            if param.array is not None and not fx_is_array(pt):
                pt = fx_array_type(pt, self.const_int(param.array))
            if arg is None:
                scope[param.name] = self.zero(pt) if param.default is None else self.convert(self.eval(param.default), pt)
            else:
                scope[param.name] = self.convert(arg, pt)
        saved_mask = self.mask
        self.mask = 1.0
        self.frames.append(frame)
        self.depth += 1
        try:
            self.exec_block(function.body[1])
        finally:
            self.depth -= 1
            self.frames.pop()
            self.mask = saved_mask
        rt = fx_resolve_type(function.ret, self.program)
        result = frame.ret if frame.ret is not None else (self.zero(rt) if rt != "void" else FxValue("void", None))
        outs = {}
        for param in function.params:
            if "out" in param.quals or "inout" in param.quals:
                outs[param.name] = scope[param.name]
        return result, outs

    def exec_block(self, statements):
        for statement in statements:
            if self.inactive():
                return
            self.exec(statement)

    def exec(self, node):
        kind = node[0]
        if kind == "expr":
            self.eval(node[1])
        elif kind == "decl":
            self.exec_decl(node)
        elif kind == "block":
            frame = self.frames[-1]
            frame.scopes.append({})
            try:
                self.exec_block(node[1])
            finally:
                frame.scopes.pop()
        elif kind == "if":
            self.exec_if(node)
        elif kind == "for":
            self.exec_for(node)
        elif kind == "do":
            self.exec_do(node)
        elif kind == "return":
            self.exec_return(node)
        elif kind == "break":
            loop = self.frames[-1].loops[-1]
            loop.brk = self.or_mask(loop.brk, self.active())
        elif kind == "continue":
            loop = self.frames[-1].loops[-1]
            loop.cont = self.or_mask(loop.cont, self.active())
        elif kind == "discard":
            self.discard = self.or_mask(self.discard, self.active())
        elif kind == "empty":
            pass
        else:
            raise FxError("unknown statement %s" % kind)

    def or_mask(self, a, b):
        B = self.B
        ka, kb = B.known(a), B.known(b)
        if ka is not None and kb is not None:
            return 1.0 if (ka or kb) else 0.0
        if ka is not None:
            return 1.0 if ka else b
        if kb is not None:
            return 1.0 if kb else a
        return B.max(a, b)

    def exec_decl(self, node):
        _kind, type_name, declarators, _quals = node
        t = fx_resolve_type(type_name, self.program)
        scope = self.frames[-1].scopes[-1]
        for name, array, init in declarators:
            vt = t
            if array is not None:
                vt = fx_array_type(t, self.const_int(array))
            if init is None:
                scope[name] = self.zero(vt)
            elif isinstance(init, tuple) and init[0] == "init":
                scope[name] = self.eval_init(init, vt)
            else:
                scope[name] = self.convert(self.eval(init), vt)

    def condition(self, expr):
        value = self.eval(expr)
        if not fx_is_numeric(value.t):
            raise FxError("condition is not numeric")
        return self.B.truth(value.c[0])

    def exec_if(self, node):
        _kind, cond_expr, then, other = node
        c = self.condition(cond_expr)
        known = self.B.known(c)
        if known is not None:
            if known:
                self.exec(then)
            elif other is not None:
                self.exec(other)
            return
        B = self.B
        saved = self.mask
        base = self.active()
        known_base = B.known(base)
        self.mask = c if known_base is not None else B.mul(base, c)
        try:
            self.exec(then)
            if other is not None:
                inv = B.sub(1.0, c)
                self.mask = inv if known_base is not None else B.mul(base, inv)
                self.exec(other)
        finally:
            self.mask = saved

    def exec_for(self, node):
        _kind, init, cond, step, body = node
        frame = self.frames[-1]
        frame.scopes.append({})
        loop = FxLoop()
        try:
            self.exec(init)
            frame.loops.append(loop)
            for _ in range(FX_LOOP_LIMIT):
                if self.inactive():
                    break
                if cond is not None:
                    c = self.condition(cond)
                    known = self.B.known(c)
                    if known is not None:
                        if not known:
                            break
                    else:
                        if not self.B.vectorized:
                            raise FxUnsupported("data-dependent loop")
                        loop.brk = self.or_mask(loop.brk, self.B.sub(1.0, c))
                        if not self.B.any_true(self.active()):
                            break
                self.exec(body)
                loop.cont = 0.0
                if step is not None and not self.inactive():
                    self.eval(step)
            else:
                raise FxUnsupported("loop did not finish")
        finally:
            if frame.loops and frame.loops[-1] is loop:
                frame.loops.pop()
            frame.scopes.pop()

    def exec_do(self, node):
        _kind, body, cond = node
        frame = self.frames[-1]
        loop = FxLoop()
        frame.loops.append(loop)
        try:
            for _ in range(FX_LOOP_LIMIT):
                if self.inactive():
                    break
                self.exec(body)
                loop.cont = 0.0
                if self.inactive():
                    break
                c = self.condition(cond)
                known = self.B.known(c)
                if known is not None:
                    if not known:
                        break
                else:
                    if not self.B.vectorized:
                        raise FxUnsupported("data-dependent loop")
                    loop.brk = self.or_mask(loop.brk, self.B.sub(1.0, c))
                    if not self.B.any_true(self.active()):
                        break
        finally:
            frame.loops.pop()

    def exec_return(self, node):
        frame = self.frames[-1]
        rt = fx_resolve_type(frame.function.ret, self.program)
        mask = self.active()
        if node[1] is not None and rt != "void":
            value = self.convert(self.eval(node[1]), rt)
            known = self.B.known(mask)
            if frame.ret is None or (known is not None and known):
                frame.ret = value
            else:
                frame.ret = self.merge(mask, value, frame.ret)
        frame.done = self.or_mask(frame.done, mask)

    def merge(self, mask, new, old):
        B = self.B
        if fx_is_numeric(new.t):
            return FxValue(new.t, [B.select(mask, a, b) for a, b in zip(new.c, old.c)])
        if isinstance(new.t, FxStruct):
            return FxValue(new.t, {k: self.merge(mask, new.c[k], old.c[k]) for k in new.c})
        if fx_is_array(new.t):
            return FxValue(new.t, [self.merge(mask, a, b) for a, b in zip(new.c, old.c)])
        return new

    def eval(self, node):
        kind = node[0]
        method = FX_EVAL.get(kind)
        if method is None:
            raise FxError("cannot evaluate %s" % kind)
        return method(self, node)

    def eval_num(self, node):
        return FxValue((node[2], ()), [self.B.const(node[1])])

    def eval_str(self, node):
        return FxValue("string", node[1])

    def eval_id(self, node):
        scope, name = self.lookup(node[1])
        if scope is not None:
            return scope[name]
        return self.global_value(name)

    def eval_comma(self, node):
        result = None
        for item in node[1]:
            result = self.eval(item)
        return result

    def eval_init_expr(self, node):
        items = self.flatten_init(node)
        comps = []
        for item in items:
            comps.extend(item.c)
        dims = () if len(comps) == 1 else (len(comps),)
        if len(comps) > 4:
            dims = (len(comps),)
        return FxValue(("f", dims), comps)

    def eval_un(self, node):
        op = node[1]
        value = self.eval(node[2])
        B = self.B
        if op == "+":
            return value
        if op == "-":
            return FxValue(value.t, [B.neg(x) for x in value.c])
        if op == "!":
            return FxValue(("b", value.t[1]), [B.sub(1.0, B.truth(x)) for x in value.c])
        if op == "~":
            return FxValue(value.t, [B.bitnot(x) for x in value.c])
        raise FxError("unary %s" % op)

    def eval_incdec(self, node):
        kind, op, target = node
        old = self.eval(target)
        one = FxValue(("i", ()), [self.B.const(1.0)])
        new = self.binary("+" if op == "++" else "-", old, one)
        self.assign(target, self.convert(new, old.t))
        return new if kind == "pre" else old

    def eval_bin(self, node):
        return self.binary(node[1], self.eval(node[2]), self.eval(node[3]))

    def broadcast(self, a, b):
        ta, tb = a.t, b.t
        if not (fx_is_numeric(ta) and fx_is_numeric(tb)):
            raise FxError("non-numeric operand")
        da, db = ta[1], tb[1]
        na, nb = len(a.c), len(b.c)
        if da == db:
            return da, a.c, b.c
        if na == 1:
            return db, a.c * nb, b.c
        if nb == 1:
            return da, a.c, b.c * na
        if len(da) == 1 and len(db) == 1:
            k = min(da[0], db[0])
            return (k,), a.c[:k], b.c[:k]
        if len(da) == 2 and len(db) == 2:
            r, c = min(da[0], db[0]), min(da[1], db[1])
            ca = [a.c[i * da[1] + j] for i in range(r) for j in range(c)]
            cb = [b.c[i * db[1] + j] for i in range(r) for j in range(c)]
            return (r, c), ca, cb
        if na == nb:
            return da, a.c, b.c
        k = min(na, nb)
        return (k,), a.c[:k], b.c[:k]

    def binary(self, op, a, b):
        B = self.B
        dims, ca, cb = self.broadcast(a, b)
        fa, fb = a.t[0], b.t[0]
        base = "f" if "f" in (fa, fb) else "i"
        if op in ("+", "-", "*", "/", "%"):
            if op == "+":
                comps = [B.add(x, y) for x, y in zip(ca, cb)]
            elif op == "-":
                comps = [B.sub(x, y) for x, y in zip(ca, cb)]
            elif op == "*":
                comps = [B.mul(x, y) for x, y in zip(ca, cb)]
            elif op == "/":
                if base == "i":
                    comps = [B.idiv(x, y) for x, y in zip(ca, cb)]
                else:
                    comps = [B.div(x, y) for x, y in zip(ca, cb)]
            else:
                comps = [B.mod(x, y) for x, y in zip(ca, cb)]
            return FxValue((base, dims), comps)
        if op in ("<", "<=", ">", ">=", "==", "!="):
            return FxValue(("b", dims), [B.cmp(op, x, y) for x, y in zip(ca, cb)])
        if op == "&&":
            return FxValue(("b", dims), [B.mul(B.truth(x), B.truth(y)) for x, y in zip(ca, cb)])
        if op == "||":
            return FxValue(("b", dims), [B.max(B.truth(x), B.truth(y)) for x, y in zip(ca, cb)])
        if op in ("&", "|", "^", "<<", ">>"):
            result_base = "b" if fa == "b" and fb == "b" and op in ("&", "|", "^") else "i"
            return FxValue((result_base, dims), [B.bitop(op, x, y) for x, y in zip(ca, cb)])
        raise FxError("binary %s" % op)

    def eval_assign(self, node):
        _kind, op, target, value_expr = node
        value = self.eval(value_expr)
        if op != "=":
            current = self.eval(target)
            value = self.binary(op[:-1], current, value)
        self.assign(target, value)
        return self.eval(target)

    def eval_tern(self, node):
        c = self.condition(node[1])
        known = self.B.known(c)
        if known is not None:
            return self.eval(node[2] if known else node[3])
        a = self.eval(node[2])
        b = self.eval(node[3])
        if fx_is_numeric(a.t) and fx_is_numeric(b.t):
            dims, ca, cb = self.broadcast(a, b)
            base = "f" if "f" in (a.t[0], b.t[0]) else a.t[0]
            return FxValue((base, dims), [self.B.select(c, x, y) for x, y in zip(ca, cb)])
        return self.merge(c, a, self.convert(b, a.t))

    def eval_cast(self, node):
        t = fx_resolve_type(node[1], self.program)
        value = self.eval(node[2])
        return self.convert(value, t)

    def eval_ctor(self, node):
        t = fx_resolve_type(node[1], self.program)
        args = [self.eval(a) for a in node[2]]
        if isinstance(t, FxStruct):
            if len(args) == 1:
                return self.convert(args[0], t)
            raise FxUnsupported("struct constructor")
        comps = []
        for arg in args:
            comps.extend(arg.c)
        n = fx_count(t[1])
        if len(comps) == 1 and n > 1:
            comps = comps * n
        comps = (comps + [self.B.const(0.0)] * n)[:n]
        source_base = "f" if any(a.t[0] == "f" for a in args) else "i"
        return self.convert(FxValue((source_base, t[1]), comps), t)

    def swizzle_indices(self, t, name):
        if not fx_is_numeric(t):
            return None
        dims = t[1]
        if len(dims) == 2:
            found = re.findall(r"_m(\d)(\d)|_(\d)(\d)", name)
            if not found or "".join(
                ("_m%s%s" % (a, b)) if a else ("_%s%s" % (c, d)) for a, b, c, d in found
            ) != name:
                return None
            out = []
            for a, b, c, d in found:
                if a:
                    out.append(int(a) * dims[1] + int(b))
                else:
                    out.append((int(c) - 1) * dims[1] + (int(d) - 1))
            return out
        if len(name) > 4 or any(ch not in FX_SWIZZLE for ch in name):
            return None
        return [FX_SWIZZLE[ch] for ch in name]

    def eval_member(self, node):
        value = self.eval(node[1])
        name = node[2]
        if isinstance(value.t, FxStruct):
            if name not in value.c:
                raise FxError("struct %s has no member %s" % (value.t.name, name))
            return value.c[name]
        indices = self.swizzle_indices(value.t, name)
        if indices is None:
            raise FxError("bad member %s" % name)
        comps = value.c
        out = []
        for i in indices:
            out.append(comps[i] if i < len(comps) else comps[0] if len(comps) == 1 else self.B.const(0.0))
        dims = () if len(out) == 1 else (len(out),)
        return FxValue((value.t[0], dims), out)

    def eval_index(self, node):
        value = self.eval(node[1])
        index = self.eval(node[2])
        known = self.B.known(index.c[0])
        if fx_is_array(value.t):
            if known is not None:
                k = int(known)
                return value.c[max(0, min(k, len(value.c) - 1))]
            return self.dynamic_pick(value.c, index.c[0])
        if fx_is_numeric(value.t):
            dims = value.t[1]
            if len(dims) == 2:
                rows = [
                    FxValue((value.t[0], (dims[1],)), value.c[r * dims[1]:(r + 1) * dims[1]])
                    for r in range(dims[0])
                ]
                if known is not None:
                    return rows[max(0, min(int(known), dims[0] - 1))]
                return self.dynamic_pick(rows, index.c[0])
            elems = [FxValue((value.t[0], ()), [x]) for x in value.c]
            if known is not None:
                return elems[max(0, min(int(known), len(elems) - 1))]
            return self.dynamic_pick(elems, index.c[0])
        raise FxError("cannot index")

    def dynamic_pick(self, items, index):
        B = self.B
        result = items[0]
        for k in range(1, len(items)):
            hit = B.cmp("==", B.unary("trunc", index), B.const(float(k)))
            result = self.merge(hit, items[k], result)
        return result

    def lvalue_path(self, node):
        path = []
        while True:
            kind = node[0]
            if kind == "id":
                return node[1], list(reversed(path))
            if kind == "member":
                path.append(("m", node[2]))
                node = node[1]
            elif kind == "index":
                path.append(("i", node[2]))
                node = node[1]
            else:
                raise FxError("expression is not assignable")

    def assign(self, target, value):
        name, path = self.lvalue_path(target)
        scope, _ = self.lookup(name)
        if scope is None:
            if name in self.overrides or name in self.program.globals:
                current = self.global_value(name)
                updated = self.write(current, path, value, self.active())
                self.cache[name] = updated
                return
            raise FxError("unknown variable %s" % name)
        scope[name] = self.write(scope[name], path, value, self.active())

    def write(self, current, path, value, mask):
        B = self.B
        if not path:
            new = self.convert(value, current.t) if current.t is not None else value
            known = B.known(mask)
            if known is not None:
                return new if known else current
            return self.merge(mask, new, current)
        kind, arg = path[0]
        if kind == "m":
            if isinstance(current.t, FxStruct):
                members = dict(current.c)
                members[arg] = self.write(members[arg], path[1:], value, mask)
                return FxValue(current.t, members)
            indices = self.swizzle_indices(current.t, arg)
            if indices is None:
                raise FxError("bad member %s" % arg)
            sub = FxValue((current.t[0], () if len(indices) == 1 else (len(indices),)), [current.c[i] for i in indices])
            sub = self.write(sub, path[1:], value, mask)
            comps = list(current.c)
            for j, i in enumerate(indices):
                comps[i] = sub.c[j]
            return FxValue(current.t, comps)
        index = self.eval(arg)
        known = B.known(index.c[0])
        if fx_is_array(current.t):
            elems = list(current.c)
            if known is not None:
                k = max(0, min(int(known), len(elems) - 1))
                elems[k] = self.write(elems[k], path[1:], value, mask)
            else:
                for k in range(len(elems)):
                    hit = B.cmp("==", B.unary("trunc", index.c[0]), B.const(float(k)))
                    sub_mask = hit if B.known(mask) is not None else B.mul(mask, hit)
                    elems[k] = self.write(elems[k], path[1:], value, sub_mask)
            return FxValue(current.t, elems)
        dims = current.t[1]
        comps = list(current.c)
        if len(dims) == 2:
            width = dims[1]
            groups = [list(range(r * width, (r + 1) * width)) for r in range(dims[0])]
            sub_type = (current.t[0], (width,))
        else:
            groups = [[i] for i in range(len(comps))]
            sub_type = (current.t[0], ())
        targets = range(len(groups)) if known is None else [max(0, min(int(known), len(groups) - 1))]
        for k in targets:
            sub_mask = mask
            if known is None:
                hit = B.cmp("==", B.unary("trunc", index.c[0]), B.const(float(k)))
                sub_mask = hit if B.known(mask) is not None else B.mul(mask, hit)
            sub = FxValue(sub_type, [comps[i] for i in groups[k]])
            sub = self.write(sub, path[1:], value, sub_mask)
            for j, i in enumerate(groups[k]):
                comps[i] = sub.c[j]
        return FxValue(current.t, comps)

    def find_function(self, name, args):
        candidates = self.program.functions.get(name)
        if not candidates:
            return None
        fitting = [f for f in candidates if len(f.params) >= len(args) and all(
            p.default is not None for p in f.params[len(args):]
        )]
        if not fitting:
            return None
        if len(fitting) == 1:
            return fitting[0]
        best = None
        best_score = -1
        for f in fitting:
            score = 0
            for p, a in zip(f.params, args):
                try:
                    pt = fx_resolve_type(p.type, self.program)
                except FxError:
                    continue
                if fx_is_numeric(pt) and fx_is_numeric(a.t) and pt[1] == a.t[1]:
                    score += 2
                elif pt == a.t:
                    score += 2
            if score > best_score:
                best, best_score = f, score
        return best

    def eval_call(self, node):
        name = node[1]
        arg_nodes = node[2]
        if name in self.program.functions:
            args = [self.eval(a) for a in arg_nodes]
            function = self.find_function(name, args)
            if function is not None:
                result, outs = self.run_function(function, args)
                for param, arg_node in zip(function.params, arg_nodes):
                    if param.name in outs:
                        self.assign(arg_node, outs[param.name])
                return result
        intrinsic = FX_INTRINSICS.get(name)
        if intrinsic is None:
            raise FxUnsupported("unknown function %s" % name)
        return intrinsic(self, arg_nodes)

    def eval_mcall(self, node):
        _kind, obj, name, args = node
        lower = name.lower()
        if lower in ("sample", "samplelevel", "samplebias", "samplegrad"):
            texture = self.eval(obj)
            sampler = self.eval(args[0])
            uv = self.eval(args[1])
            info = sampler.c
            if isinstance(info, FxSampler) and texture.t == "texture":
                info = FxSampler(info.name, texture.c, info.states, info.register)
            lod = None
            if lower == "samplelevel" and len(args) > 2:
                lod = self.eval(args[2]).c[0]
            return self.sample(info, uv.c[0], uv.c[1] if len(uv.c) > 1 else self.B.const(0.0), lod)
        raise FxUnsupported("method %s" % name)

    def sample(self, info, u, v, lod=None):
        comps = self.env.sample(self, info, u, v, lod)
        return FxValue(("f", (4,)), comps)

    def elementwise(self, fn, *values):
        if len(values) == 1:
            value = values[0]
            return FxValue(("f" if value.t[0] != "i" else "i", value.t[1]), [fn(x) for x in value.c])
        dims = values[0].t[1]
        comps = [values[0].c]
        result_dims = dims
        current = values[0]
        for other in values[1:]:
            result_dims, ca, cb = self.broadcast(current, other)
            current = FxValue(("f", result_dims), ca)
            comps = [c if len(c) == len(ca) else (c * len(ca) if len(c) == 1 else c[: len(ca)]) for c in comps]
            comps.append(cb)
        size = max(len(c) for c in comps)
        comps = [c * size if len(c) == 1 else c[:size] for c in comps]
        base = "f" if any(v.t[0] == "f" for v in values) else "i"
        return FxValue((base, result_dims), [fn(*parts) for parts in zip(*comps)])


FX_EVAL = {
    "num": FxExec.eval_num,
    "str": FxExec.eval_str,
    "id": FxExec.eval_id,
    "bin": FxExec.eval_bin,
    "un": FxExec.eval_un,
    "pre": FxExec.eval_incdec,
    "post": FxExec.eval_incdec,
    "assign": FxExec.eval_assign,
    "tern": FxExec.eval_tern,
    "cast": FxExec.eval_cast,
    "ctor": FxExec.eval_ctor,
    "member": FxExec.eval_member,
    "index": FxExec.eval_index,
    "call": FxExec.eval_call,
    "mcall": FxExec.eval_mcall,
    "comma": FxExec.eval_comma,
    "init": FxExec.eval_init_expr,
}


def fx_args(ex, nodes):
    return [ex.eval(n) for n in nodes]


def fx_unary_intrinsic(name):
    def run(ex, nodes):
        (a,) = fx_args(ex, nodes)
        return ex.elementwise(lambda x: ex.B.unary(name, x), FxValue(("f", a.t[1]), a.c))

    return run


def fx_saturate_scalar(B, x):
    return B.min(B.max(x, 0.0), 1.0)


def fx_i_saturate(ex, nodes):
    (a,) = fx_args(ex, nodes)
    return FxValue(("f", a.t[1]), [fx_saturate_scalar(ex.B, x) for x in a.c])


def fx_i_clamp(ex, nodes):
    a, lo, hi = fx_args(ex, nodes)
    B = ex.B
    return ex.elementwise(lambda x, l, h: B.min(B.max(x, l), h), a, lo, hi)


def fx_i_min(ex, nodes):
    a, b = fx_args(ex, nodes)
    return ex.elementwise(ex.B.min, a, b)


def fx_i_max(ex, nodes):
    a, b = fx_args(ex, nodes)
    return ex.elementwise(ex.B.max, a, b)


def fx_i_pow(ex, nodes):
    a, b = fx_args(ex, nodes)
    return ex.elementwise(ex.B.pow, FxValue(("f", a.t[1]), a.c), b)


def fx_i_fmod(ex, nodes):
    a, b = fx_args(ex, nodes)
    return ex.elementwise(ex.B.mod, FxValue(("f", a.t[1]), a.c), b)


def fx_i_atan2(ex, nodes):
    a, b = fx_args(ex, nodes)
    return ex.elementwise(ex.B.atan2, FxValue(("f", a.t[1]), a.c), b)


def fx_i_lerp(ex, nodes):
    a, b, t = fx_args(ex, nodes)
    B = ex.B
    return ex.elementwise(lambda x, y, s: B.add(x, B.mul(B.sub(y, x), s)), FxValue(("f", a.t[1]), a.c), b, t)


def fx_i_step(ex, nodes):
    a, b = fx_args(ex, nodes)
    B = ex.B
    return ex.elementwise(lambda edge, x: B.cmp(">=", x, edge), FxValue(("f", a.t[1]), a.c), b)


def fx_i_smoothstep(ex, nodes):
    lo, hi, x = fx_args(ex, nodes)
    B = ex.B

    def run(a, b, v):
        t = fx_saturate_scalar(B, B.div(B.sub(v, a), B.sub(b, a)))
        return B.mul(B.mul(t, t), B.sub(3.0, B.mul(2.0, t)))

    return ex.elementwise(run, FxValue(("f", lo.t[1]), lo.c), hi, x)


def fx_dot_comps(B, a, b):
    total = None
    for x, y in zip(a, b):
        term = B.mul(x, y)
        total = term if total is None else B.add(total, term)
    return total if total is not None else B.const(0.0)


def fx_i_dot(ex, nodes):
    a, b = fx_args(ex, nodes)
    _dims, ca, cb = ex.broadcast(a, b)
    return FxValue(("f", ()), [fx_dot_comps(ex.B, ca, cb)])


def fx_i_cross(ex, nodes):
    a, b = fx_args(ex, nodes)
    B = ex.B
    ax, ay, az = (a.c + [B.const(0.0)] * 3)[:3]
    bx, by, bz = (b.c + [B.const(0.0)] * 3)[:3]
    return FxValue(
        ("f", (3,)),
        [
            B.sub(B.mul(ay, bz), B.mul(az, by)),
            B.sub(B.mul(az, bx), B.mul(ax, bz)),
            B.sub(B.mul(ax, by), B.mul(ay, bx)),
        ],
    )


def fx_length_comps(B, comps):
    return B.unary("sqrt", fx_dot_comps(B, comps, comps))


def fx_i_length(ex, nodes):
    (a,) = fx_args(ex, nodes)
    return FxValue(("f", ()), [fx_length_comps(ex.B, a.c)])


def fx_i_distance(ex, nodes):
    a, b = fx_args(ex, nodes)
    diff = ex.binary("-", a, b)
    return FxValue(("f", ()), [fx_length_comps(ex.B, diff.c)])


def fx_safe_normalize(B, comps):
    length = fx_length_comps(B, comps)
    inv = B.select(B.cmp(">", length, 1e-20), B.div(1.0, length), 0.0)
    return [B.mul(x, inv) for x in comps]


def fx_i_normalize(ex, nodes):
    (a,) = fx_args(ex, nodes)
    return FxValue(("f", a.t[1]), fx_safe_normalize(ex.B, a.c))


def fx_i_reflect(ex, nodes):
    i, n = fx_args(ex, nodes)
    B = ex.B
    d = B.mul(2.0, fx_dot_comps(B, i.c, n.c))
    return FxValue(("f", i.t[1]), [B.sub(x, B.mul(d, y)) for x, y in zip(i.c, n.c)])


def fx_i_refract(ex, nodes):
    i, n, eta = fx_args(ex, nodes)
    B = ex.B
    e = eta.c[0]
    cosi = fx_dot_comps(B, i.c, n.c)
    k = B.sub(1.0, B.mul(B.mul(e, e), B.sub(1.0, B.mul(cosi, cosi))))
    root = B.unary("sqrt", B.max(k, 0.0))
    factor = B.add(B.mul(e, cosi), root)
    comps = [B.sub(B.mul(e, x), B.mul(factor, y)) for x, y in zip(i.c, n.c)]
    ok = B.cmp(">=", k, 0.0)
    return FxValue(("f", i.t[1]), [B.select(ok, x, 0.0) for x in comps])


def fx_i_faceforward(ex, nodes):
    n, i, ng = fx_args(ex, nodes)
    B = ex.B
    d = fx_dot_comps(B, i.c, ng.c)
    flip = B.cmp("<", d, 0.0)
    return FxValue(("f", n.t[1]), [B.select(flip, x, B.neg(x)) for x in n.c])


def fx_i_mul(ex, nodes):
    a, b = fx_args(ex, nodes)
    B = ex.B
    da, db = a.t[1], b.t[1]
    if len(da) == 0 or len(db) == 0:
        return ex.binary("*", a, b)
    if len(da) == 1 and len(db) == 1:
        return FxValue(("f", ()), [fx_dot_comps(B, a.c, b.c)])
    if len(da) == 1 and len(db) == 2:
        rows, cols = db
        k = min(da[0], rows)
        out = []
        for j in range(cols):
            out.append(fx_dot_comps(B, a.c[:k], [b.c[i * cols + j] for i in range(k)]))
        return FxValue(("f", (cols,)), out)
    if len(da) == 2 and len(db) == 1:
        rows, cols = da
        k = min(cols, db[0])
        out = []
        for i in range(rows):
            out.append(fx_dot_comps(B, a.c[i * cols:i * cols + k], b.c[:k]))
        return FxValue(("f", (rows,)), out)
    r1, c1 = da
    r2, c2 = db
    k = min(c1, r2)
    out = []
    for i in range(r1):
        for j in range(c2):
            out.append(fx_dot_comps(B, [a.c[i * c1 + m] for m in range(k)], [b.c[m * c2 + j] for m in range(k)]))
    return FxValue(("f", (r1, c2)), out)


def fx_i_transpose(ex, nodes):
    (a,) = fx_args(ex, nodes)
    if len(a.t[1]) != 2:
        return a
    r, c = a.t[1]
    return FxValue((a.t[0], (c, r)), [a.c[i * c + j] for j in range(c) for i in range(r)])


def fx_i_determinant(ex, nodes):
    (a,) = fx_args(ex, nodes)
    B = ex.B
    n = a.t[1][0]

    def det(rows, cols):
        if len(rows) == 1:
            return a.c[rows[0] * n + cols[0]]
        total = None
        for k, col in enumerate(cols):
            sub = det(rows[1:], cols[:k] + cols[k + 1:])
            term = B.mul(a.c[rows[0] * n + col], sub)
            if k % 2:
                term = B.neg(term)
            total = term if total is None else B.add(total, term)
        return total

    return FxValue(("f", ()), [det(list(range(n)), list(range(n)))])


def fx_i_all(ex, nodes):
    (a,) = fx_args(ex, nodes)
    B = ex.B
    total = B.const(1.0)
    for x in a.c:
        total = B.mul(total, B.truth(x))
    return FxValue(("b", ()), [total])


def fx_i_any(ex, nodes):
    (a,) = fx_args(ex, nodes)
    B = ex.B
    total = B.const(0.0)
    for x in a.c:
        total = B.max(total, B.truth(x))
    return FxValue(("b", ()), [total])


def fx_i_clip(ex, nodes):
    (a,) = fx_args(ex, nodes)
    B = ex.B
    hit = B.const(0.0)
    for x in a.c:
        hit = B.max(hit, B.cmp("<", x, 0.0))
    active = ex.active()
    known = B.known(active)
    if known is None:
        hit = B.mul(hit, active)
    elif not known:
        return FxValue("void", None)
    ex.discard = ex.or_mask(ex.discard, hit)
    return FxValue("void", None)


def fx_i_zero_like(ex, nodes):
    (a,) = fx_args(ex, nodes)
    return FxValue(("f", a.t[1]), [ex.B.const(0.0)] * len(a.c))


def fx_i_radians(ex, nodes):
    (a,) = fx_args(ex, nodes)
    return ex.elementwise(lambda x: ex.B.mul(x, math.pi / 180.0), FxValue(("f", a.t[1]), a.c))


def fx_i_degrees(ex, nodes):
    (a,) = fx_args(ex, nodes)
    return ex.elementwise(lambda x: ex.B.mul(x, 180.0 / math.pi), FxValue(("f", a.t[1]), a.c))


def fx_i_isnan(ex, nodes):
    (a,) = fx_args(ex, nodes)
    return FxValue(("b", a.t[1]), [ex.B.cmp("!=", x, x) for x in a.c])


def fx_i_sincos(ex, nodes):
    a = ex.eval(nodes[0])
    s = ex.elementwise(lambda x: ex.B.unary("sin", x), FxValue(("f", a.t[1]), a.c))
    c = ex.elementwise(lambda x: ex.B.unary("cos", x), FxValue(("f", a.t[1]), a.c))
    ex.assign(nodes[1], s)
    ex.assign(nodes[2], c)
    return FxValue("void", None)


def fx_i_ldexp(ex, nodes):
    a, b = fx_args(ex, nodes)
    B = ex.B
    return ex.elementwise(lambda x, e: B.mul(x, B.unary("exp2", e)), FxValue(("f", a.t[1]), a.c), b)


def fx_i_lit(ex, nodes):
    ndotl, ndoth, m = fx_args(ex, nodes)
    B = ex.B
    d = ndotl.c[0]
    h = ndoth.c[0]
    spec = B.select(B.mul(B.cmp(">", d, 0.0), B.cmp(">", h, 0.0)), B.pow(B.max(h, 0.0), m.c[0]), 0.0)
    return FxValue(("f", (4,)), [B.const(1.0), B.max(d, 0.0), spec, B.const(1.0)])


def fx_i_as_is(ex, nodes):
    (a,) = fx_args(ex, nodes)
    return a


def fx_texture_call(mode):
    def run(ex, nodes):
        sampler = ex.eval(nodes[0])
        coord = ex.eval(nodes[1])
        B = ex.B
        info = sampler.c
        comps = coord.c
        u = comps[0]
        v = comps[1] if len(comps) > 1 else B.const(0.0)
        lod = None
        if mode == "lod":
            lod = comps[3] if len(comps) > 3 else B.const(0.0)
        elif mode == "proj" and len(comps) > 3:
            u = B.div(u, comps[3])
            v = B.div(v, comps[3])
        elif mode == "1d":
            v = B.const(0.5)
        return ex.sample(info, u, v, lod)

    return run


FX_INTRINSICS = {
    "saturate": fx_i_saturate,
    "clamp": fx_i_clamp,
    "min": fx_i_min,
    "max": fx_i_max,
    "pow": fx_i_pow,
    "fmod": fx_i_fmod,
    "atan2": fx_i_atan2,
    "lerp": fx_i_lerp,
    "step": fx_i_step,
    "smoothstep": fx_i_smoothstep,
    "dot": fx_i_dot,
    "cross": fx_i_cross,
    "length": fx_i_length,
    "distance": fx_i_distance,
    "normalize": fx_i_normalize,
    "reflect": fx_i_reflect,
    "refract": fx_i_refract,
    "faceforward": fx_i_faceforward,
    "mul": fx_i_mul,
    "transpose": fx_i_transpose,
    "determinant": fx_i_determinant,
    "all": fx_i_all,
    "any": fx_i_any,
    "clip": fx_i_clip,
    "ddx": fx_i_zero_like,
    "ddy": fx_i_zero_like,
    "ddx_coarse": fx_i_zero_like,
    "ddy_coarse": fx_i_zero_like,
    "ddx_fine": fx_i_zero_like,
    "ddy_fine": fx_i_zero_like,
    "fwidth": fx_i_zero_like,
    "radians": fx_i_radians,
    "degrees": fx_i_degrees,
    "isnan": fx_i_isnan,
    "sincos": fx_i_sincos,
    "ldexp": fx_i_ldexp,
    "lit": fx_i_lit,
    "asfloat": fx_i_as_is,
    "asint": fx_i_as_is,
    "asuint": fx_i_as_is,
    "tex2D": fx_texture_call("plain"),
    "tex2Dlod": fx_texture_call("lod"),
    "tex2Dbias": fx_texture_call("plain"),
    "tex2Dgrad": fx_texture_call("plain"),
    "tex2Dproj": fx_texture_call("proj"),
    "tex1D": fx_texture_call("1d"),
    "tex1Dlod": fx_texture_call("lod"),
    "tex3D": fx_texture_call("plain"),
    "tex3Dlod": fx_texture_call("lod"),
    "texCUBE": fx_texture_call("plain"),
    "texCUBElod": fx_texture_call("lod"),
}
for _name in (
    "abs",
    "sign",
    "floor",
    "ceil",
    "trunc",
    "round",
    "frac",
    "sqrt",
    "rsqrt",
    "exp",
    "exp2",
    "log",
    "log2",
    "log10",
    "sin",
    "cos",
    "tan",
    "asin",
    "acos",
    "atan",
    "sinh",
    "cosh",
    "tanh",
):
    FX_INTRINSICS[_name] = fx_unary_intrinsic(_name)


def fx_const_value(program, node):
    if node is None:
        return None
    ex = FxExec(program, FxNumpyBackend(), FxConstEnv())
    try:
        value = ex.eval(node)
    except FxError:
        return None
    if value.t == "string":
        return value.c
    if fx_is_numeric(value.t):
        out = [float(x) for x in value.c]
        return out[0] if len(out) == 1 else out
    return None


class FxConstEnv:
    def global_value(self, ex, g):
        return None

    def sample(self, ex, info, u, v, lod):
        return [ex.B.const(0.0)] * 4


def fx_annotation(g, name, program, default=None):
    for key, node in g.ann.items():
        if key.lower() == name.lower():
            value = fx_const_value(program, node)
            return default if value is None else value
    return default


def fx_subset_matches(spec, index):
    if spec is None:
        return True
    for part in str(spec).replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            lo = int(lo) if lo else 0
            if hi == "":
                if index >= lo:
                    return True
            elif lo <= index <= int(hi):
                return True
        else:
            try:
                if int(part) == index:
                    return True
            except ValueError:
                pass
    return False


class FxDraw:
    __slots__ = ("technique", "fx_pass", "subset", "loops", "target", "kind", "order")

    def __init__(self, technique, fx_pass, subset, loops, target, kind, order):
        self.technique = technique
        self.fx_pass = fx_pass
        self.subset = subset
        self.loops = loops
        self.target = target
        self.kind = kind
        self.order = order

    @property
    def label(self):
        return "%s.%s" % (self.technique.name or "tech", self.fx_pass.name or "pass")


def fx_technique_pass_type(technique, program):
    for key, node in technique.ann.items():
        if key.lower() == "mmdpass":
            value = fx_const_value(program, node)
            if isinstance(value, str):
                return value.lower()
    return "object"


def fx_technique_subset(technique, program):
    for key, node in technique.ann.items():
        if key.lower() == "subset":
            value = fx_const_value(program, node)
            if value is None:
                return None
            if isinstance(value, float):
                return str(int(value))
            return str(value)
    return None


def fx_technique_script(technique, program):
    for key, node in technique.ann.items():
        if key.lower() == "script":
            value = fx_const_value(program, node)
            if isinstance(value, str):
                return value
    return None


def fx_pass_script(fx_pass, program):
    for key, node in fx_pass.ann.items():
        if key.lower() == "script":
            value = fx_const_value(program, node)
            if isinstance(value, str):
                return value
    return None


def fx_pick_technique(program, subset, pass_types=("object", "object_ss")):
    for pass_type in pass_types:
        for technique in program.techniques:
            if fx_technique_pass_type(technique, program) != pass_type:
                continue
            if fx_subset_matches(fx_technique_subset(technique, program), subset):
                return technique
    return None


def fx_global_int(program, name):
    name = name.strip()
    try:
        return int(float(name))
    except ValueError:
        pass
    g = program.globals.get(name)
    if g is None or g.init is None:
        return 1
    value = fx_const_value(program, g.init)
    if isinstance(value, list):
        value = value[0]
    return int(value) if value is not None else 1


def fx_plan_draws(program, subset_count):
    draws = []
    order = 0
    for subset in range(subset_count):
        technique = fx_pick_technique(program, subset)
        if technique is None:
            continue
        passes = {p.name: p for p in technique.passes}
        script = fx_technique_script(technique, program)
        if script is None:
            for fx_pass in technique.passes:
                kind = "buffer" if (fx_pass_script(fx_pass, program) or "").lower().replace(" ", "").find("draw=buffer") >= 0 else "geometry"
                draws.append(FxDraw(technique, fx_pass, subset, [], "", kind, order))
                order += 1
            continue
        target = ""
        stack = []
        for command in script.split(";"):
            if "=" not in command:
                continue
            key, value = command.split("=", 1)
            key = key.strip().lower()
            value = value.strip()
            if key in ("rendercolortarget", "rendercolortarget0"):
                target = value
            elif key == "loopbycount":
                stack.append([value, fx_global_int(program, value), None])
            elif key == "loopgetindex":
                if stack:
                    stack[-1][2] = value
            elif key == "loopend":
                if stack:
                    stack.pop()
            elif key == "pass":
                fx_pass = passes.get(value)
                if fx_pass is None:
                    continue
                pass_script = (fx_pass_script(fx_pass, program) or "").lower().replace(" ", "")
                kind = "buffer" if "draw=buffer" in pass_script else "geometry"
                loops = [(index or "", count, name) for name, count, index in stack]
                draws.append(FxDraw(technique, fx_pass, subset, loops, target, kind, order))
                order += 1
    return draws


def fx_find_function(program, name, argc):
    for function in program.functions.get(name, []):
        uniform = [p for p in function.params if "uniform" in p.quals]
        if len(uniform) >= argc or len(function.params) >= argc:
            return function
    return None


def fx_entry(program, compiled):
    if compiled is None:
        return None, []
    _tag, profile, call = compiled
    if call[0] != "call":
        return None, []
    function = fx_find_function(program, call[1], len(call[2]))
    return function, call[2]


def fx_bind_entry(function, compile_args):
    uniform = [i for i, p in enumerate(function.params) if "uniform" in p.quals]
    if not uniform and compile_args:
        uniform = list(range(len(function.params) - len(compile_args), len(function.params)))
    bound = {}
    for slot, arg in zip(uniform, compile_args):
        bound[slot] = arg
    return bound


def fx_norm_semantic(semantic):
    if not semantic:
        return None
    s = semantic.upper()
    if s.startswith("SV_"):
        s = s[3:]
    if s in ("COLOR", "TEXCOORD", "POSITION", "NORMAL", "TANGENT", "BINORMAL", "BLENDWEIGHT", "BLENDINDICES", "TARGET"):
        s = s + "0"
    if s == "TARGET0":
        s = "COLOR0"
    return s


def fx_run_entry(ex, function, compile_args, inputs):
    program = ex.program
    bound = fx_bind_entry(function, compile_args)
    args = []
    for index, param in enumerate(function.params):
        pt = fx_resolve_type(param.type, program)
        if index in bound:
            args.append(ex.convert(ex.eval(bound[index]), pt))
            continue
        if "out" in param.quals and "inout" not in param.quals:
            args.append(None)
            continue
        if isinstance(pt, FxStruct):
            members = {}
            for name, type_name, semantic, _array in pt.members:
                mt = fx_resolve_type(type_name, program)
                members[name] = ex.convert(inputs(fx_norm_semantic(semantic), mt), mt)
            args.append(FxValue(pt, members))
        else:
            args.append(ex.convert(inputs(fx_norm_semantic(param.semantic), pt), pt))
    result, outs = ex.run_function(function, args)
    outputs = {}
    rt = fx_resolve_type(function.ret, program)
    if isinstance(rt, FxStruct):
        for name, type_name, semantic, _array in rt.members:
            sem = fx_norm_semantic(semantic)
            if sem:
                outputs[sem] = result.c[name]
    elif function.semantic and rt != "void":
        outputs[fx_norm_semantic(function.semantic)] = result
    for param in function.params:
        if param.name in outs:
            value = outs[param.name]
            if isinstance(value.t, FxStruct):
                for name, type_name, semantic, _array in value.t.members:
                    sem = fx_norm_semantic(semantic)
                    if sem:
                        outputs[sem] = value.c[name]
            elif param.semantic:
                outputs[fx_norm_semantic(param.semantic)] = value
    return outputs


def fx_lh_perspective(fov_y, aspect, near, far):
    ys = 1.0 / math.tan(fov_y * 0.5)
    xs = ys / aspect
    q = far / (far - near)
    return np.array(
        [[xs, 0, 0, 0], [0, ys, 0, 0], [0, 0, q, 1], [0, 0, -near * q, 0]],
        dtype=np.float64,
    )


def fx_lh_look(eye, forward, up):
    f = np.asarray(forward, dtype=np.float64)
    f = f / (np.linalg.norm(f) or 1.0)
    u = np.asarray(up, dtype=np.float64)
    r = np.cross(u, f)
    if np.linalg.norm(r) < 1e-9:
        r = np.cross(np.array([0.0, 0.0, 1.0]), f)
    r = r / (np.linalg.norm(r) or 1.0)
    u = np.cross(f, r)
    e = np.asarray(eye, dtype=np.float64)
    return np.array(
        [
            [r[0], u[0], f[0], 0],
            [r[1], u[1], f[1], 0],
            [r[2], u[2], f[2], 0],
            [-r.dot(e), -u.dot(e), -f.dot(e), 1],
        ],
        dtype=np.float64,
    )


def fx_lh_ortho(width, height, near, far):
    return np.array(
        [
            [2.0 / width, 0, 0, 0],
            [0, 2.0 / height, 0, 0],
            [0, 0, 1.0 / (far - near), 0],
            [0, 0, -near / (far - near), 1],
        ],
        dtype=np.float64,
    )


FX_NEAR = 1e-3
FX_FAR = 1e7


class FxImage:
    __slots__ = ("pixels", "width", "height", "path")

    def __init__(self, pixels, path):
        self.pixels = pixels
        self.height, self.width = pixels.shape[:2]
        self.path = path

    def sample(self, u, v, point, address):
        u = np.asarray(u, dtype=np.float64)
        v = np.asarray(v, dtype=np.float64)
        shape = np.broadcast(u, v).shape
        u = np.broadcast_to(u, shape)
        v = np.broadcast_to(v, shape)
        w, h = self.width, self.height

        def wrap(i, n):
            if address == "CLAMP":
                return np.clip(i, 0, n - 1)
            if address == "MIRROR":
                period = np.mod(i, 2 * n)
                return np.where(period >= n, 2 * n - 1 - period, period)
            return np.mod(i, n)

        if point:
            x = wrap(np.floor(np.nan_to_num(u) * w).astype(np.int64), w)
            y = wrap(np.floor(np.nan_to_num(v) * h).astype(np.int64), h)
            texel = self.pixels[y, x]
        else:
            fx = np.nan_to_num(u) * w - 0.5
            fy = np.nan_to_num(v) * h - 0.5
            x0 = np.floor(fx)
            y0 = np.floor(fy)
            tx = (fx - x0)[..., None]
            ty = (fy - y0)[..., None]
            x0 = x0.astype(np.int64)
            y0 = y0.astype(np.int64)
            xa, xb = wrap(x0, w), wrap(x0 + 1, w)
            ya, yb = wrap(y0, h), wrap(y0 + 1, h)
            p = self.pixels
            texel = (p[ya, xa] * (1 - tx) + p[ya, xb] * tx) * (1 - ty) + (p[yb, xa] * (1 - tx) + p[yb, xb] * tx) * ty
        texel = np.asarray(texel, dtype=np.float64)
        if texel.ndim == 1:
            return [np.float64(texel[k]) for k in range(4)]
        return [texel[..., k] for k in range(4)]


FX_MATRIX_BASES = ("WORLDVIEWPROJECTION", "VIEWPROJECTION", "WORLDVIEW", "PROJECTION", "WORLD", "VIEW")
FX_MATERIAL_SEMANTICS = {
    "DIFFUSE",
    "AMBIENT",
    "EMISSIVE",
    "SPECULAR",
    "SPECULARPOWER",
    "TOONCOLOR",
    "EDGECOLOR",
    "GROUNDSHADOWCOLOR",
}
FX_FLAG_DEFAULTS = {
    "parthf": 1.0,
    "transp": 0.0,
    "spadd": 0.0,
    "opadd": 0.0,
    "use_toon": 0.0,
    "use_spheremap": 0.0,
    "use_subtexture": 0.0,
    "use_selfshadow": 0.0,
}


def fx_split_matrix_semantic(semantic):
    for base in FX_MATRIX_BASES:
        if semantic.startswith(base):
            suffix = semantic[len(base):]
            if suffix in ("", "INVERSE", "TRANSPOSE", "INVERSETRANSPOSE"):
                return base, suffix
    return None


def fx_classify_global(g, program):
    semantic = g.semantic
    obj = str(fx_annotation(g, "Object", program, "") or "").lower()
    if semantic:
        matrix = fx_split_matrix_semantic(semantic)
        if matrix is not None:
            return ("matrix", matrix[0], matrix[1], "light" if obj == "light" else "camera")
        if semantic == "POSITION":
            return ("position", "light" if obj == "light" else "camera")
        if semantic == "DIRECTION":
            return ("direction", "light" if obj == "light" else "camera")
        if semantic in FX_MATERIAL_SEMANTICS:
            if obj == "light":
                return ("light_color", semantic)
            return ("material", semantic)
        if semantic == "TIME":
            return ("time",)
        if semantic == "ELAPSEDTIME":
            return ("elapsed",)
        if semantic in ("VIEWPORTPIXELSIZE", "VIEWPORTSIZE"):
            return ("viewport",)
        if semantic == "CONTROLOBJECT":
            name = fx_annotation(g, "name", program, "") or ""
            item = fx_annotation(g, "item", program, "") or ""
            return ("control", str(name), str(item))
        if semantic in ("MOUSEPOSITION", "LEFTMOUSEDOWN", "MIDDLEMOUSEDOWN", "RIGHTMOUSEDOWN"):
            return ("zero",)
        return None
    if g.init is None and g.name in FX_FLAG_DEFAULTS:
        return ("flag", g.name)
    if g.init is None and g.name == "use_texture":
        return ("flag", g.name)
    return None


def fx_source_is_runtime(source):
    if source is None:
        return False
    return source[0] in ("matrix", "position", "direction", "light_color", "time", "elapsed", "viewport", "control")


class FxSceneState:
    def __init__(self):
        self.world = np.identity(4)
        self.view = fx_lh_look((0.0, 10.0, -45.0), (0.0, 0.0, 1.0), (0.0, 1.0, 0.0))
        self.proj = fx_lh_perspective(math.radians(30.0), 16.0 / 9.0, FX_NEAR, FX_FAR)
        self.camera_pos = np.array([0.0, 10.0, -45.0])
        self.camera_dir = np.array([0.0, 0.0, 1.0])
        self.light_dir = np.array([-0.5, -1.0, 0.5])
        self.light_color = np.array([154.0, 154.0, 154.0]) / 255.0
        self.time = 0.0
        self.elapsed = 1.0 / 30.0
        self.viewport = (1920.0, 1080.0)
        self.controls = {}
        self.flags = dict(FX_FLAG_DEFAULTS)
        self.material = {
            "diffuse": (1.0, 1.0, 1.0, 1.0),
            "ambient": (1.0, 1.0, 1.0),
            "specular": (0.0, 0.0, 0.0),
            "power": 0.0,
            "emissive": (0.0, 0.0, 0.0),
            "texture": "",
        }

    def light_matrices(self):
        direction = self.light_dir / (np.linalg.norm(self.light_dir) or 1.0)
        eye = -direction * 1000.0
        view = fx_lh_look(eye, direction, (0.0, 1.0, 0.0))
        proj = fx_lh_ortho(400.0, 400.0, 1.0, 4000.0)
        return view, proj

    def matrix(self, base, suffix, target):
        if target == "light":
            view, proj = self.light_matrices()
        else:
            view, proj = self.view, self.proj
        world = self.world
        if base == "WORLD":
            m = world
        elif base == "VIEW":
            m = view
        elif base == "PROJECTION":
            m = proj
        elif base == "WORLDVIEW":
            m = world @ view
        elif base == "VIEWPROJECTION":
            m = view @ proj
        else:
            m = world @ view @ proj
        if "INVERSE" in suffix:
            try:
                m = np.linalg.inv(m)
            except np.linalg.LinAlgError:
                m = np.identity(4)
        if "TRANSPOSE" in suffix:
            m = m.T
        return [float(x) for x in np.asarray(m).reshape(-1)]

    def values(self, source, g):
        kind = source[0]
        if kind == "matrix":
            return self.matrix(source[1], source[2], source[3])
        if kind == "position":
            if source[1] == "light":
                d = self.light_dir / (np.linalg.norm(self.light_dir) or 1.0)
                return [float(x) for x in (-d * 1000.0)] + [1.0]
            return [float(x) for x in self.camera_pos] + [1.0]
        if kind == "direction":
            d = self.light_dir if source[1] == "light" else self.camera_dir
            d = d / (np.linalg.norm(d) or 1.0)
            return [float(x) for x in d] + [0.0]
        if kind == "light_color":
            if source[1] == "DIFFUSE":
                return [0.0, 0.0, 0.0, 1.0]
            return [float(x) for x in self.light_color] + [1.0]
        if kind == "material":
            m = self.material
            sem = source[1]
            if sem == "DIFFUSE":
                return list(m["diffuse"])
            if sem == "AMBIENT":
                return list(m["ambient"]) + [1.0]
            if sem == "EMISSIVE":
                return list(m["emissive"]) + [1.0]
            if sem == "SPECULAR":
                return list(m["specular"]) + [1.0]
            if sem == "SPECULARPOWER":
                return [float(m["power"])]
            if sem == "TOONCOLOR":
                return [1.0, 1.0, 1.0, 1.0]
            if sem == "EDGECOLOR":
                return [0.0, 0.0, 0.0, 1.0]
            return [0.0, 0.0, 0.0, 0.5]
        if kind == "time":
            return [float(self.time)]
        if kind == "elapsed":
            return [float(self.elapsed)]
        if kind == "viewport":
            return [float(self.viewport[0]), float(self.viewport[1])]
        if kind == "control":
            found = self.controls.get(g.name)
            if found is None:
                return [0.0]
            return list(found)
        if kind == "flag":
            if source[1] == "use_texture":
                return [1.0 if self.material.get("texture") else 0.0]
            return [float(self.flags.get(source[1], 0.0))]
        return [0.0]


def fx_value_from_list(ex, values, t):
    comps = [ex.B.const(float(v)) for v in values]
    if not fx_is_numeric(t):
        return None
    n = fx_count(t[1])
    if len(t[1]) == 2 and len(comps) == 16 and t[1] != (4, 4):
        full = FxValue(("f", (4, 4)), comps)
        return ex.convert(full, t)
    if len(comps) == 1 and n > 1:
        comps = comps * n
    comps = (comps + [ex.B.const(0.0)] * n)[:n]
    return ex.convert(FxValue(("f", t[1]), comps), t)


class FxTextureStore:
    def __init__(self, program, loader):
        self.program = program
        self.loader = loader
        self.images = {}

    def texture_global(self, info):
        if info is None:
            return None
        name = info.texture
        if not name:
            return None
        return self.program.globals.get(name)

    def resource_path(self, g, material_texture=""):
        if g is None:
            return material_texture or None
        if g.semantic in ("MATERIALTEXTURE",):
            return material_texture or None
        if g.semantic and g.semantic not in ("",):
            if g.semantic.startswith(("RENDERCOLORTARGET", "RENDERDEPTHSTENCILTARGET", "OFFSCREENRENDERTARGET", "ANIMATEDTEXTURE", "MATERIALSPHEREMAP", "MATERIALTOONTEXTURE")):
                return None
        resource = fx_annotation(g, "ResourceName", self.program, None)
        if not isinstance(resource, str) or not resource:
            return None
        return fx_find_file(self.program.folder, resource)

    def image(self, path):
        if path is None:
            return None
        if path not in self.images:
            try:
                self.images[path] = self.loader(path)
            except Exception:
                self.images[path] = None
        return self.images[path]


def fx_find_file(folder, name):
    name = name.replace("\\", "/")
    candidate = os.path.join(folder, name)
    if os.path.isfile(candidate):
        return candidate
    parent = os.path.dirname(candidate)
    base = os.path.basename(candidate).lower()
    stem = os.path.splitext(base)[0]
    if os.path.isdir(parent):
        entries = os.listdir(parent)
        for entry in entries:
            if entry.lower() == base:
                return os.path.join(parent, entry)
        for entry in entries:
            if os.path.splitext(entry)[0].lower() == stem:
                return os.path.join(parent, entry)
    return None


def fx_sampler_texture_global(program, info):
    if info is None or not isinstance(info, FxSampler):
        return None
    if info.texture:
        return program.globals.get(info.texture)
    return None


def fx_is_render_target(g):
    return g is not None and g.semantic is not None and g.semantic.startswith(("RENDERCOLORTARGET", "OFFSCREENRENDERTARGET", "RENDERDEPTHSTENCILTARGET"))


class FxNumpyEnv:
    def __init__(self, program, state, textures):
        self.program = program
        self.state = state
        self.textures = textures
        self.sources = {}

    def source(self, g):
        if g.name not in self.sources:
            self.sources[g.name] = fx_classify_global(g, self.program)
        return self.sources[g.name]

    def global_value(self, ex, g):
        source = self.source(g)
        if source is None:
            return None
        t = fx_resolve_type(g.type, self.program)
        if not fx_is_numeric(t):
            return None
        return fx_value_from_list(ex, self.state.values(source, g), t)

    def sample(self, ex, info, u, v, lod):
        g = fx_sampler_texture_global(self.program, info)
        if fx_is_render_target(g):
            return [ex.B.const(0.0)] * 4
        material_texture = self.state.material.get("texture", "")
        if g is None and info is not None and info.texture is None:
            path = material_texture if (info.register or "").lower().replace(" ", "") in ("s0", "") else None
        else:
            path = self.textures.resource_path(g, material_texture)
        image = self.textures.image(path)
        if image is None:
            return [ex.B.const(0.0)] * 4
        return image.sample(u, v, info.point() if info else False, info.address() if info else "WRAP")


def fx_pil_loader(path):
    from PIL import Image

    with Image.open(path) as handle:
        rgba = handle.convert("RGBA")
        data = np.asarray(rgba, dtype=np.float64) / 255.0
    return FxImage(data, path)


def fx_vertex_inputs(positions, normals, uvs, indices, colors=None):
    def provide(ex):
        B = ex.B

        def get(semantic, t):
            n = len(positions)
            if semantic == "POSITION0":
                comps = [positions[:, 0], positions[:, 1], positions[:, 2], np.ones(n)]
            elif semantic == "NORMAL0":
                comps = [normals[:, 0], normals[:, 1], normals[:, 2], np.zeros(n)]
            elif semantic == "TEXCOORD0":
                comps = [uvs[:, 0], uvs[:, 1], np.zeros(n), np.ones(n)]
            elif semantic in ("_INDEX", "INDEX"):
                comps = [indices.astype(np.float64)]
            elif semantic == "COLOR0" and colors is not None:
                comps = [colors[:, k] for k in range(4)]
            elif semantic in ("COLOR0", "COLOR1"):
                comps = [np.ones(n)] * 4
            else:
                comps = [np.zeros(n), np.zeros(n), np.zeros(n), np.ones(n)]
            if not fx_is_numeric(t):
                return ex.zero(t)
            count = fx_count(t[1])
            comps = (comps + [np.zeros(n)] * 16)[:count]
            return FxValue(("f", t[1]), comps)

        return get

    return provide


def fx_clip_to_world(clip, view_proj_inverse, proj):
    x, y, z, w = clip
    a = proj[2, 2]
    b = proj[3, 2]
    w = np.asarray(w, dtype=np.float64)
    z = np.asarray(z, dtype=np.float64)
    tiny = np.abs(w) <= 1e-12
    natural = a * w + b
    front = w > 1e-12
    outside = front & ((z < -1e-6 * np.maximum(1.0, np.abs(w))) | (z > w * (1 + 1e-9) + 1e-6))
    behind = (w < -1e-12) & (np.abs(z - natural) > 1e-4 * (np.abs(w) + 1.0))
    culled = tiny | outside | behind
    homo = np.stack([np.asarray(x, dtype=np.float64) * np.ones_like(w), np.asarray(y, dtype=np.float64) * np.ones_like(w), natural, w], axis=-1)
    world = homo @ view_proj_inverse
    ww = world[:, 3:4]
    ww = np.where(np.abs(ww) < 1e-20, 1.0, ww)
    pos = world[:, :3] / ww
    pos = np.where(np.isfinite(pos), pos, 0.0)
    culled = culled | ~np.isfinite(world).all(axis=1)
    return pos, culled


ACTION_PREFIX = "MMDBridge_"
CAMERA_COLLECTION = "MMD_Camera"
LIGHT_COLLECTION = "MMD_Sun"
FOLLOW_PREFIX = "MMDBridgeFollow"
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
PMM_FPS = 30.0
AUDIO_STATE_SIZE = 28
AUDIO_PATH_LENGTH = 256
AUDIO_STRIP_NAME = "MMDBridge_Audio"
AUDIO_EXTENSIONS = (".wav", ".mp3", ".ogg", ".flac", ".aac", ".m4a")
REPORT_TEXT_NAME = "MMDBridgeReport"
REPORT_LOG_LINES = 300
ACCESSORY_COLLECTION = "MMD_Accessories"
ACCESSORY_PREFIX = "MMDAcc_"
ACCESSORY_FOLLOW_PREFIX = "MMDBridgeAcc"
ACCESSORY_UNIT = 10.0
X_IMPORTER_MODULES = ("io_directx_x", "io_scene_x")
X_IMPORTER_URL = "https://extensions.blender.org/add-ons/io-directx-x/"
MMD_TOOLS_URL = "https://extensions.blender.org/add-ons/mmd-tools/"

_last_sync = {"digest": None, "failed": 0}
_status_icons = {}


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
    self.state = "NEW"
    self.status_text = ""


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
            channels.append(
                {
                    "leaf": leaf,
                    "mesh": mesh_obj,
                    "offsets": usable,
                    "number": leaves.index(leaf),
                }
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
    pending = {}
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
            specs = pending.setdefault(material.as_pointer(), (material, {}))[1]
            for channel in group:
                if index in channel["materials"]:
                    specs.setdefault(channel["leaf"], channel)
    applied = 0
    for material, specs in pending.values():
        ordered = [specs[leaf] for leaf in leaves if leaf in specs]
        if build_uv_nodes(material, ordered):
            applied += 1
            log.info(
                "UV morph shader nodes added to material '%s': %s",
                material.name,
                [c["leaf"] for c in ordered],
            )
        else:
            log.warning(
                "material '%s' has no base texture node; UV morph skipped for %s",
                material.name,
                [c["leaf"] for c in ordered],
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
    if not settings.follow_camera:
        return plan
    items = {t.key: t for t in settings.targets}
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
        constraint = root.constraints.new(type="CHILD_OF")
        constraint.name = FOLLOW_PREFIX + "_%d_%d" % pair
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


def bone_rest_inverse(armature, bone_name):
    data_bone = armature.data.bones.get(bone_name)
    if data_bone is None:
        return Matrix.Identity(4)
    rotation = data_bone.matrix_local.to_3x3().normalized()
    return rotation.inverted().to_4x4()


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
    constraint.inverse_matrix = bone_rest_inverse(armature, bone)
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
        root["mme_tr"] = float(key.opacity)
        root["mme_si"] = float(key.scale)
        root.keyframe_insert("location", frame=key.frame)
        root.keyframe_insert("rotation_euler", frame=key.frame)
        root.keyframe_insert("scale", frame=key.frame)
        root.keyframe_insert('["mme_tr"]', frame=key.frame)
        root.keyframe_insert('["mme_si"]', frame=key.frame)
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


MME_DRAW_PROP = "mme_draw"
MME_VARYINGS_PROP = "mme_varyings"
MME_UNIFORMS_PROP = "mme_uniforms"
MME_ATTR_SRC = "mme_src"
MME_ATTR_INST = "mme_inst"
MME_ATTR_CULL = "mme_cull"
MME_VARYING_PREFIX = "mmeV_"
MME_LOOP_PREFIX = "mmeL_"
MME_UNIFORM_PREFIX = "mmeU_"
MME_CONTROL_PREFIX = "mmeC_"
MME_IMAGE_PREFIX = "MME_"
MME_GAMMA = 2.2
MME_PRUNE_TR = (1.0, 0.8, 0.6, 0.4, 0.2, 0.0)
MME_SWAP = np.array(
    [[1.0, 0, 0, 0], [0, 0, 1.0, 0], [0, 1.0, 0, 0], [0, 0, 0, 1.0]], dtype=np.float64
)
MME_SWAP3 = MME_SWAP[:3, :3]
MME_SELF_NAMES = ("(self)", "(offscreenowner)")

_mme_runtimes = {}
_mme_state = {"busy": False, "errors": set()}


class MmeSock:
    __slots__ = ("socket", "depth")

    def __init__(self, socket, depth):
        self.socket = socket
        self.depth = depth


MME_NODE_UNARY = {
    "abs": "ABSOLUTE",
    "sign": "SIGN",
    "floor": "FLOOR",
    "ceil": "CEIL",
    "trunc": "TRUNC",
    "round": "ROUND",
    "frac": "FRACT",
    "sqrt": "SQRT",
    "rsqrt": "INVERSE_SQRT",
    "exp": "EXPONENT",
    "sin": "SINE",
    "cos": "COSINE",
    "tan": "TANGENT",
    "asin": "ARCSINE",
    "acos": "ARCCOSINE",
    "atan": "ARCTANGENT",
    "sinh": "SINH",
    "cosh": "COSH",
    "tanh": "TANH",
}


class MmeNodeBackend:
    vectorized = False

    def __init__(self, tree):
        self.tree = tree
        self.nodes = tree.nodes
        self.links = tree.links
        self.np = FxNumpyBackend()
        self.cache = {}
        self.rows = {}
        self.keep = []

    def const(self, x):
        return float(x)

    def known(self, x):
        if isinstance(x, MmeSock):
            return None
        return float(x)

    def any_true(self, x):
        raise FxUnsupported("data-dependent loop in a pixel shader")

    def key(self, x):
        if isinstance(x, MmeSock):
            return ("s", id(x))
        value = float(x)
        return ("c", "nan" if value != value else value)

    def place(self, node, depth):
        row = self.rows.get(depth, 0)
        self.rows[depth] = row + 1
        node.location = (depth * 220.0, -row * 170.0)

    def feed(self, socket, value):
        if isinstance(value, MmeSock):
            self.links.new(value.socket, socket)
        else:
            socket.default_value = float(value)

    def depth_of(self, *args):
        return 1 + max((a.depth for a in args if isinstance(a, MmeSock)), default=0)

    def fold(self, fn, *args):
        if any(isinstance(a, MmeSock) for a in args):
            return None
        with np.errstate(all="ignore"):
            return float(fn(*[np.float64(a) for a in args]))

    def math(self, op, *args):
        key = ("math", op) + tuple(self.key(a) for a in args)
        found = self.cache.get(key)
        if found is not None:
            return found
        node = self.nodes.new("ShaderNodeMath")
        node.operation = op
        for index, arg in enumerate(args):
            self.feed(node.inputs[index], arg)
        result = MmeSock(node.outputs[0], self.depth_of(*args))
        self.place(node, result.depth)
        self.cache[key] = result
        self.keep.append(result)
        return result

    def ordered(self, a, b):
        return (a, b) if repr(self.key(a)) <= repr(self.key(b)) else (b, a)

    def add(self, a, b):
        folded = self.fold(self.np.add, a, b)
        if folded is not None:
            return folded
        if self.known(a) == 0.0:
            return b
        if self.known(b) == 0.0:
            return a
        return self.math("ADD", *self.ordered(a, b))

    def sub(self, a, b):
        folded = self.fold(self.np.sub, a, b)
        if folded is not None:
            return folded
        if self.known(b) == 0.0:
            return a
        if self.key(a) == self.key(b):
            return 0.0
        return self.math("SUBTRACT", a, b)

    def mul(self, a, b):
        folded = self.fold(self.np.mul, a, b)
        if folded is not None:
            return folded
        if self.known(a) == 1.0:
            return b
        if self.known(b) == 1.0:
            return a
        if self.known(a) == 0.0 or self.known(b) == 0.0:
            return 0.0
        return self.math("MULTIPLY", *self.ordered(a, b))

    def div(self, a, b):
        folded = self.fold(self.np.div, a, b)
        if folded is not None:
            return folded
        if self.known(b) == 1.0:
            return a
        if self.known(a) == 0.0:
            return 0.0
        return self.math("DIVIDE", a, b)

    def neg(self, a):
        if not isinstance(a, MmeSock):
            return -float(a)
        return self.math("MULTIPLY", a, -1.0)

    def mod(self, a, b):
        folded = self.fold(self.np.mod, a, b)
        if folded is not None:
            return folded
        return self.math("MODULO", a, b)

    def idiv(self, a, b):
        folded = self.fold(self.np.idiv, a, b)
        if folded is not None:
            return folded
        return self.unary("trunc", self.div(a, b))

    def pow(self, a, b):
        folded = self.fold(self.np.pow, a, b)
        if folded is not None:
            return folded
        if self.known(b) == 1.0:
            return a
        if self.known(b) == 0.0:
            return 1.0
        return self.math("POWER", a, b)

    def unary(self, name, a):
        folded = self.fold(lambda x: self.np.unary(name, x), a)
        if folded is not None:
            return folded
        if name == "exp2":
            return self.math("POWER", 2.0, a)
        if name == "log":
            return self.math("LOGARITHM", a, math.e)
        if name == "log2":
            return self.math("LOGARITHM", a, 2.0)
        if name == "log10":
            return self.math("LOGARITHM", a, 10.0)
        op = MME_NODE_UNARY.get(name)
        if op is None:
            raise FxUnsupported("function %s in a pixel shader" % name)
        return self.math(op, a)

    def atan2(self, a, b):
        folded = self.fold(self.np.atan2, a, b)
        if folded is not None:
            return folded
        return self.math("ARCTAN2", a, b)

    def min(self, a, b):
        folded = self.fold(self.np.min, a, b)
        if folded is not None:
            return folded
        if self.key(a) == self.key(b):
            return a
        return self.math("MINIMUM", *self.ordered(a, b))

    def max(self, a, b):
        folded = self.fold(self.np.max, a, b)
        if folded is not None:
            return folded
        if self.key(a) == self.key(b):
            return a
        return self.math("MAXIMUM", *self.ordered(a, b))

    def cmp(self, op, a, b):
        folded = self.fold(lambda x, y: self.np.cmp(op, x, y), a, b)
        if folded is not None:
            return folded
        if op == "<":
            return self.math("LESS_THAN", a, b)
        if op == ">":
            return self.math("GREATER_THAN", a, b)
        if op == "<=":
            return self.sub(1.0, self.math("GREATER_THAN", a, b))
        if op == ">=":
            return self.sub(1.0, self.math("LESS_THAN", a, b))
        equal = self.math("COMPARE", a, b, 0.0)
        if op == "==":
            return equal
        return self.sub(1.0, equal)

    def bitop(self, op, a, b):
        folded = self.fold(lambda x, y: self.np.bitop(op, x, y), a, b)
        if folded is not None:
            return folded
        raise FxUnsupported("bit operation in a pixel shader")

    def bitnot(self, a):
        folded = self.fold(self.np.bitnot, a)
        if folded is not None:
            return folded
        raise FxUnsupported("bit operation in a pixel shader")

    def truth(self, a):
        known = self.known(a)
        if known is not None:
            return 1.0 if known != 0.0 else 0.0
        return self.sub(1.0, self.cmp("==", a, 0.0))

    def select(self, c, a, b):
        known = self.known(c)
        if known is not None:
            return a if known != 0.0 else b
        if self.key(a) == self.key(b):
            return a
        key = ("select", self.key(c), self.key(a), self.key(b))
        found = self.cache.get(key)
        if found is not None:
            return found
        node = self.nodes.new("ShaderNodeMix")
        node.data_type = "FLOAT"
        node.clamp_factor = True
        self.feed(node_socket(node, "Factor_Float"), c)
        self.feed(node_socket(node, "A_Float"), b)
        self.feed(node_socket(node, "B_Float"), a)
        result = MmeSock(node_socket(node, "Result_Float", outputs=True), self.depth_of(c, a, b))
        self.place(node, result.depth)
        self.cache[key] = result
        self.keep.append(result)
        return result

    def attribute(self, name, kind="GEOMETRY"):
        key = ("attr", kind, name)
        found = self.cache.get(key)
        if found is not None:
            return found
        node = self.nodes.new("ShaderNodeAttribute")
        node.attribute_type = kind
        node.attribute_name = name
        result = MmeSock(node.outputs["Fac"], 0)
        self.place(node, 0)
        self.cache[key] = result
        self.keep.append(result)
        return result

    def image_sample(self, image, interpolation, extension, u, v):
        key = ("img", image.name, interpolation, extension, self.key(u), self.key(v))
        found = self.cache.get(key)
        if found is not None:
            return found
        flipped = self.sub(1.0, v)
        combine = self.nodes.new("ShaderNodeCombineXYZ")
        self.feed(combine.inputs[0], u)
        self.feed(combine.inputs[1], flipped)
        combine.inputs[2].default_value = 0.0
        depth = self.depth_of(u, flipped)
        self.place(combine, depth)
        texture = self.nodes.new("ShaderNodeTexImage")
        texture.image = image
        texture.interpolation = interpolation
        texture.extension = extension
        self.links.new(combine.outputs[0], texture.inputs["Vector"])
        self.place(texture, depth + 1)
        separate = self.nodes.new("ShaderNodeSeparateColor")
        self.links.new(texture.outputs["Color"], separate.inputs[0])
        self.place(separate, depth + 2)
        result = [MmeSock(separate.outputs[k], depth + 3) for k in range(3)]
        result.append(MmeSock(texture.outputs["Alpha"], depth + 2))
        self.cache[key] = result
        self.keep.extend(result)
        return result


def mme_prop_name(prefix, name, index=None):
    text = prefix + name if index is None else "%s%s_%d" % (prefix, name, index)
    if len(text.encode("utf-8")) > 63:
        digest = hashlib.md5(text.encode("utf-8")).hexdigest()[:10]
        text = (prefix + digest) if index is None else "%s%s_%d" % (prefix, digest, index)
    return text


def mme_varying_name(semantic, index):
    return "%s%s_%d" % (MME_VARYING_PREFIX, semantic, index)


def mme_effect_image(path):
    norm = os.path.normcase(os.path.abspath(path))
    for image in bpy.data.images:
        if image.get("mme_path") == norm:
            return image
    image = bpy.data.images.load(path, check_existing=False)
    image.name = MME_IMAGE_PREFIX + os.path.basename(path)
    image["mme_path"] = norm
    try:
        image.colorspace_settings.name = "Non-Color"
    except (TypeError, ValueError):
        pass
    try:
        image.alpha_mode = "CHANNEL_PACKED"
    except (TypeError, ValueError):
        pass
    return image


def mme_load_pixels(path):
    image = mme_effect_image(path)
    width, height = image.size
    if width == 0 or height == 0:
        raise FxError("image has no pixels: %s" % path)
    buffer = np.empty(width * height * 4, dtype=np.float32)
    image.pixels.foreach_get(buffer)
    data = buffer.reshape(height, width, 4)[::-1].astype(np.float64)
    return FxImage(data, path)


class MmeNodeEnv:
    def __init__(self, program, state, textures, controls_runtime):
        self.program = program
        self.state = state
        self.textures = textures
        self.controls_runtime = controls_runtime
        self.uniforms = {}
        self.rt_lookup = None

    def global_value(self, ex, g):
        source = fx_classify_global(g, self.program)
        if source is None:
            return None
        t = fx_resolve_type(g.type, self.program)
        if not fx_is_numeric(t):
            return None
        values = self.state.values(source, g)
        runtime = fx_source_is_runtime(source)
        if source[0] == "control" and g.name not in self.controls_runtime:
            runtime = False
        if not runtime:
            return fx_value_from_list(ex, values, t)
        comps = []
        for index in range(len(values)):
            prop = mme_prop_name(MME_UNIFORM_PREFIX, g.name, index)
            self.uniforms[prop] = (g.name, index)
            comps.append(ex.B.attribute(prop, "OBJECT"))
        dims = (4, 4) if len(comps) == 16 else (() if len(comps) == 1 else (len(comps),))
        return ex.convert(FxValue(("f", dims), comps), t)

    def sample(self, ex, info, u, v, lod):
        g = fx_sampler_texture_global(self.program, info)
        if fx_is_render_target(g):
            if self.rt_lookup is not None:
                return self.rt_lookup(ex, g.name)
            return [0.0, 0.0, 0.0, 0.0]
        material_texture = self.state.material.get("texture", "")
        if g is None and info is not None and info.texture is None:
            path = material_texture if (info.register or "").lower().replace(" ", "") in ("s0", "") else None
        else:
            path = self.textures.resource_path(g, material_texture)
        if not path:
            return [0.0, 0.0, 0.0, 0.0]
        try:
            image = mme_effect_image(path)
        except RuntimeError:
            return [0.0, 0.0, 0.0, 0.0]
        point = info.point() if info else False
        address = info.address() if info else "WRAP"
        extension = {"CLAMP": "EXTEND", "MIRROR": "MIRROR"}.get(address, "REPEAT")
        return ex.B.image_sample(image, "Closest" if point else "Linear", extension, u, v)


def mme_ps_inputs(vs_outputs):
    def provide(ex):
        def get(semantic, t):
            if semantic in vs_outputs:
                count = vs_outputs[semantic]
                comps = [ex.B.attribute(mme_varying_name(semantic, k)) for k in range(count)]
            elif semantic == "VFACE":
                comps = [1.0]
            else:
                comps = [0.0, 0.0, 0.0, 0.0]
            dims = () if len(comps) == 1 else (len(comps),)
            return FxValue(("f", dims), comps)

        return get

    return provide


def mme_blend_mode(states):
    def state(name, default):
        value = states.get(name)
        if value is None:
            return default
        return str(value).replace(" ", "").upper()

    if state("alphablendenable", "TRUE") in ("FALSE", "0"):
        return "OPAQUE"
    src = state("srcblend", "SRCALPHA")
    dst = state("destblend", "INVSRCALPHA")
    op = state("blendop", "ADD")
    if op not in ("ADD", "1"):
        return "ALPHA"
    if dst == "ONE":
        return "ADD_ALPHA" if src == "SRCALPHA" else "ADD"
    if src == "ZERO" and dst == "SRCCOLOR" or src == "DESTCOLOR" and dst == "ZERO":
        return "MULTIPLY"
    if src == "ONE" and dst == "INVSRCALPHA":
        return "PREMULTIPLIED"
    if src == "ONE" and dst == "ZERO":
        return "OPAQUE"
    return "ALPHA"


def mme_shader_output(B, tree, color, discard, mode):
    nodes, links = tree.nodes, tree.links
    r, g, b, a = (list(color) + [0.0, 0.0, 0.0, 1.0])[:4]
    alpha = B.min(B.max(a, 0.0), 1.0)
    alpha = B.mul(alpha, B.sub(1.0, B.attribute(MME_ATTR_CULL)))
    if B.known(discard) is None or B.known(discard) != 0.0:
        alpha = B.mul(alpha, B.sub(1.0, discard))
    if mode in ("ADD_ALPHA",):
        rgb = [B.mul(x, alpha) for x in (r, g, b)]
    elif mode == "ADD":
        rgb = [B.mul(x, B.sub(1.0, B.attribute(MME_ATTR_CULL))) for x in (r, g, b)]
    else:
        rgb = [r, g, b]
    linear = [B.pow(B.max(x, 0.0), MME_GAMMA) for x in rgb]
    depth = B.depth_of(*linear, alpha)
    combine = nodes.new("ShaderNodeCombineColor")
    for index, value in enumerate(linear):
        B.feed(combine.inputs[index], value)
    B.place(combine, depth)
    output = nodes.new("ShaderNodeOutputMaterial")
    B.place(output, depth + 3)
    if mode == "MULTIPLY":
        transparent = nodes.new("ShaderNodeBsdfTransparent")
        links.new(combine.outputs[0], transparent.inputs["Color"])
        B.place(transparent, depth + 1)
        links.new(transparent.outputs[0], output.inputs["Surface"])
        return
    emission = nodes.new("ShaderNodeEmission")
    links.new(combine.outputs[0], emission.inputs["Color"])
    emission.inputs["Strength"].default_value = 1.0
    B.place(emission, depth + 1)
    if mode == "OPAQUE":
        links.new(emission.outputs[0], output.inputs["Surface"])
        return
    transparent = nodes.new("ShaderNodeBsdfTransparent")
    B.place(transparent, depth + 1)
    if mode in ("ADD", "ADD_ALPHA"):
        shader = nodes.new("ShaderNodeAddShader")
        links.new(transparent.outputs[0], shader.inputs[0])
        links.new(emission.outputs[0], shader.inputs[1])
    elif mode == "PREMULTIPLIED":
        mix = nodes.new("ShaderNodeMixShader")
        B.feed(mix.inputs[0], alpha)
        black = nodes.new("ShaderNodeEmission")
        black.inputs["Color"].default_value = (0.0, 0.0, 0.0, 1.0)
        links.new(transparent.outputs[0], mix.inputs[1])
        links.new(black.outputs[0], mix.inputs[2])
        B.place(black, depth + 1)
        B.place(mix, depth + 2)
        shader = nodes.new("ShaderNodeAddShader")
        links.new(mix.outputs[0], shader.inputs[0])
        links.new(emission.outputs[0], shader.inputs[1])
    else:
        shader = nodes.new("ShaderNodeMixShader")
        B.feed(shader.inputs[0], alpha)
        links.new(transparent.outputs[0], shader.inputs[1])
        links.new(emission.outputs[0], shader.inputs[2])
    B.place(shader, depth + 2)
    links.new(shader.outputs[0], output.inputs["Surface"])


def mme_prune_tree(tree):
    output = next((n for n in tree.nodes if n.bl_idname == "ShaderNodeOutputMaterial"), None)
    if output is None:
        return
    upstream = {}
    for link in tree.links:
        upstream.setdefault(link.to_node.name, set()).add(link.from_node.name)
    keep = set()
    stack = [output.name]
    while stack:
        name = stack.pop()
        if name in keep:
            continue
        keep.add(name)
        stack.extend(upstream.get(name, ()))
    for node in [n for n in tree.nodes if n.name not in keep]:
        tree.nodes.remove(node)


def mme_material_setup(mat, mode, states):
    blended = mode not in ("OPAQUE",)
    try:
        mat.surface_render_method = "BLENDED" if blended else "DITHERED"
    except (AttributeError, TypeError):
        try:
            mat.blend_method = "BLEND" if blended else "OPAQUE"
        except (AttributeError, TypeError):
            pass
    cull = str(states.get("cullmode", "")).replace(" ", "").upper()
    try:
        mat.use_backface_culling = cull in ("CCW", "3")
    except AttributeError:
        pass
    for name, value in (("use_transparency_overlap", True), ("show_transparent_back", True)):
        if hasattr(mat, name):
            try:
                setattr(mat, name, value)
            except (AttributeError, TypeError):
                pass
    try:
        mat.diffuse_color = (1.0, 0.55, 0.2, 0.3)
    except (AttributeError, TypeError):
        pass


def mme_vs_signature(fx_pass):
    return repr(fx_pass.vs)


def mme_compile_material(name, runtime, draw, state, vs_outputs):
    program = runtime.program
    mat = bpy.data.materials.new(name)
    try:
        mat.use_nodes = True
    except Exception:
        pass
    tree = mat.node_tree
    for node in list(tree.nodes):
        tree.nodes.remove(node)
    B = MmeNodeBackend(tree)
    env = MmeNodeEnv(program, state, runtime.textures, runtime.controls_runtime)
    provider = mme_ps_inputs(vs_outputs)
    loops = runtime.loop_overrides_nodes(draw, B)
    visiting = set()

    def run_ps(fx_pass):
        function, args = fx_entry(program, fx_pass.ps)
        if function is None:
            raise FxUnsupported("pass %s has no pixel shader" % fx_pass.name)
        ex = FxExec(program, B, env)
        ex.overrides.update(loops)
        outputs = fx_run_entry(ex, function, args, provider(ex))
        color = outputs.get("COLOR0")
        if color is None:
            return [0.0, 0.0, 0.0, 1.0], ex.discard
        comps = ex.convert(color, ("f", (4,))).c
        return comps, ex.discard

    def rt_lookup(ex, texture_name):
        writers = [
            d
            for d in runtime.draws
            if d.target == texture_name
            and d.kind == "geometry"
            and d.subset == draw.subset
            and d.order < draw.order
            and mme_vs_signature(d.fx_pass) == mme_vs_signature(draw.fx_pass)
            and [(l[0], l[1]) for l in d.loops] == [(l[0], l[1]) for l in draw.loops]
        ]
        if not writers or texture_name in visiting:
            return [0.0, 0.0, 0.0, 0.0]
        visiting.add(texture_name)
        try:
            comps, _discard = run_ps(writers[-1].fx_pass)
        finally:
            visiting.discard(texture_name)
        return list(comps)

    env.rt_lookup = rt_lookup
    color, discard = run_ps(draw.fx_pass)
    mode = mme_blend_mode(draw.fx_pass.states)
    mme_shader_output(B, tree, color, discard, mode)
    mme_prune_tree(tree)
    mme_material_setup(mat, mode, draw.fx_pass.states)
    varyings = []
    uniforms = []
    for node in tree.nodes:
        if node.bl_idname != "ShaderNodeAttribute":
            continue
        if node.attribute_type == "GEOMETRY" and node.attribute_name.startswith(MME_VARYING_PREFIX):
            varyings.append(node.attribute_name)
        elif node.attribute_type == "OBJECT" and node.attribute_name in env.uniforms:
            gname, index = env.uniforms[node.attribute_name]
            uniforms.append([node.attribute_name, gname, index])
    loop_attrs = sorted(
        {n.attribute_name for n in tree.nodes if n.bl_idname == "ShaderNodeAttribute" and n.attribute_name.startswith(MME_LOOP_PREFIX)}
    )
    return mat, sorted(set(varyings)), uniforms, mode, loop_attrs, len(tree.nodes)


def mme_fallback_material(name, runtime, subset, mode="ALPHA"):
    xmat = runtime.materials[subset] if subset < len(runtime.materials) else None
    mat = bpy.data.materials.new(name)
    try:
        mat.use_nodes = True
    except Exception:
        pass
    tree = mat.node_tree
    for node in list(tree.nodes):
        tree.nodes.remove(node)
    B = MmeNodeBackend(tree)
    color = [1.0, 1.0, 1.0, 1.0]
    if xmat is not None:
        color = list(xmat.diffuse)
        texture = xmat.texture.split("*")[0].strip() if xmat.texture else ""
        found = resolve_x_texture(texture, runtime.x_folder) if texture else None
        if found:
            sampled = B.image_sample(mme_effect_image(found), "Linear", "REPEAT", B.attribute(mme_varying_name("TEXCOORD0", 0)), B.attribute(mme_varying_name("TEXCOORD0", 1)))
            color = [B.mul(c, s) for c, s in zip(color, sampled)]
    mme_shader_output(B, tree, color, 0.0, mode)
    mme_prune_tree(tree)
    mme_material_setup(mat, mode, {})
    return mat


def x_effect_geometry(xscene):
    vertices, faces, face_materials, uvs, _colors, materials = merge_x_meshes(xscene)
    normals = []
    for mesh in xscene.meshes:
        for k in range(len(mesh.vertices)):
            normals.append(mesh.normals.get(k))
    positions = np.array(vertices, dtype=np.float64).reshape(-1, 3)
    uv_array = np.array(uvs, dtype=np.float64).reshape(-1, 2)
    normal_array = np.zeros_like(positions)
    missing = np.ones(len(positions), dtype=bool)
    for index, value in enumerate(normals):
        if value is not None:
            normal_array[index] = value
            missing[index] = False
    if missing.any() and faces:
        accum = np.zeros_like(positions)
        for face in faces:
            if len(face) < 3:
                continue
            a, b, c = positions[face[0]], positions[face[1]], positions[face[2]]
            n = np.cross(b - a, c - a)
            for vertex in face:
                accum[vertex] += n
        lengths = np.linalg.norm(accum, axis=1, keepdims=True)
        accum = np.where(lengths > 1e-12, accum / np.maximum(lengths, 1e-12), 0.0)
        normal_array[missing] = accum[missing]
    return positions, normal_array, uv_array, faces, np.array(face_materials, dtype=np.int64), materials


class MmePassData:
    __slots__ = ("draw", "src", "inst", "loop_values", "positions", "normals", "uvs", "count", "stamp")

    def __init__(self, draw, src, inst, loop_values, runtime):
        self.draw = draw
        self.src = src
        self.inst = inst
        self.loop_values = loop_values
        self.positions = runtime.positions[src]
        self.normals = runtime.normals[src]
        self.uvs = runtime.uvs[src]
        self.count = len(src)
        self.stamp = None


class MmeRuntime:
    def __init__(self, fx_path, x_path):
        self.fx_path = fx_path
        self.x_path = x_path
        self.x_folder = os.path.dirname(x_path)
        self.program = fx_parse_file(fx_path)
        with open(x_path, "rb") as handle:
            xscene = parse_x_bytes(handle.read())
        if xscene.mode != "txt":
            raise FxUnsupported("binary .x files cannot drive effects")
        (
            self.positions,
            self.normals,
            self.uvs,
            self.faces,
            self.face_subsets,
            self.materials,
        ) = x_effect_geometry(xscene)
        self.textures = FxTextureStore(self.program, mme_load_pixels)
        self.draws = fx_plan_draws(self.program, max(1, len(self.materials)))
        self.passes = {}
        self.controls_runtime = set()
        self.stamp = mme_effect_stamp(fx_path, x_path, self.program)

    def draw_by_order(self, order):
        for draw in self.draws:
            if draw.order == order:
                return draw
        return None

    def loop_overrides_nodes(self, draw, B):
        out = {}
        for var, _count, _name in draw.loops:
            if var:
                out[var] = FxValue(("i", ()), [B.attribute(MME_LOOP_PREFIX + var)])
        return out

    def material_state(self, state, subset):
        xmat = self.materials[subset] if subset < len(self.materials) else None
        if xmat is None:
            return
        texture = ""
        if xmat.texture:
            first = xmat.texture.split("*")[0].strip()
            if first and not first.lower().endswith((".sph", ".spa")):
                texture = resolve_x_texture(first, self.x_folder) or ""
        diffuse = tuple(float(v) for v in xmat.diffuse)
        state.material = {
            "diffuse": diffuse,
            "ambient": diffuse[:3],
            "specular": tuple(float(v) for v in xmat.specular),
            "power": float(xmat.power),
            "emissive": tuple(float(v) for v in xmat.emissive),
            "texture": texture,
        }


def mme_effect_stamp(fx_path, x_path, program=None):
    files = list(program.files) if program is not None else [fx_path]
    parts = [x_path, str(os.path.getmtime(x_path))]
    for path in files:
        try:
            parts.append("%s=%s" % (os.path.basename(path), os.path.getmtime(path)))
        except OSError:
            parts.append(path)
    return "|".join(parts)


def mme_runtime_for(root):
    fx_path = root.get("mme_fx", "")
    x_path = root.get("mme_x", "")
    if not fx_path or not x_path:
        return None
    key = root.name
    runtime = _mme_runtimes.get(key)
    if runtime is not None and runtime.fx_path == fx_path and runtime.x_path == x_path:
        return runtime
    if not (os.path.isfile(fx_path) and os.path.isfile(x_path)):
        return None
    runtime = MmeRuntime(fx_path, x_path)
    controls = mme_load_controls(root)
    runtime.controls_runtime = {name for name, desc in controls.items() if desc.get("kind") != "const"}
    _mme_runtimes[key] = runtime
    return runtime


def mme_load_controls(root):
    try:
        return json.loads(root.get("mme_controls", "{}"))
    except (TypeError, ValueError):
        return {}


def mme_np_matrix(matrix):
    return np.array([list(row) for row in matrix], dtype=np.float64)


def mme_world_row(blender_world, unit):
    inv_unit = np.diag([1.0 / unit, 1.0 / unit, 1.0 / unit, 1.0])
    col = inv_unit @ MME_SWAP @ blender_world @ MME_SWAP
    return col.T


def mme_camera_state(state, scene, depsgraph, unit):
    camera = scene.camera
    if camera is None:
        return
    evaluated = camera.evaluated_get(depsgraph) if depsgraph is not None else camera
    world = mme_np_matrix(evaluated.matrix_world)
    eye = MME_SWAP3 @ world[:3, 3] / unit
    forward = MME_SWAP3 @ (-world[:3, 2])
    up = MME_SWAP3 @ world[:3, 1]
    state.view = fx_lh_look(eye, forward, up)
    state.camera_pos = eye
    state.camera_dir = forward / (np.linalg.norm(forward) or 1.0)
    render = scene.render
    width = render.resolution_x * render.pixel_aspect_x
    height = render.resolution_y * render.pixel_aspect_y
    aspect = width / height if height else 1.0
    data = evaluated.data
    if getattr(data, "type", "PERSP") == "ORTHO":
        scale = data.ortho_scale / unit
        if aspect >= 1.0:
            state.proj = fx_lh_ortho(scale, scale / aspect, FX_NEAR, FX_FAR)
        else:
            state.proj = fx_lh_ortho(scale * aspect, scale, FX_NEAR, FX_FAR)
        state.ortho = True
        return
    fit = getattr(data, "sensor_fit", "AUTO")
    angle = data.angle
    if fit == "VERTICAL":
        fov_y = data.angle_y
    elif fit == "HORIZONTAL" or aspect >= 1.0:
        fov_x = data.angle_x if fit == "HORIZONTAL" else angle
        fov_y = 2.0 * math.atan(math.tan(fov_x * 0.5) / aspect)
    else:
        fov_y = angle
    state.proj = fx_lh_perspective(max(fov_y, 1e-4), aspect, FX_NEAR, FX_FAR)


def mme_light_state(state, depsgraph):
    collection = bpy.data.collections.get(LIGHT_COLLECTION)
    root = find_rig_root(collection, "LIGHT") if collection else None
    light = child_of_type(root, "LIGHT") if root else None
    if light is None:
        return
    evaluated = light.evaluated_get(depsgraph) if depsgraph is not None else light
    loc = evaluated.location
    direction = np.array([-loc.x, -loc.z, -loc.y], dtype=np.float64)
    if np.linalg.norm(direction) > 1e-9:
        state.light_dir = direction
    state.light_color = np.array(list(evaluated.data.color), dtype=np.float64)


def mme_eval_prop(obj, depsgraph, name, default=0.0):
    source = obj.evaluated_get(depsgraph) if depsgraph is not None else obj
    try:
        return float(source.get(name, obj.get(name, default)))
    except (TypeError, ValueError):
        return default


def mme_object_items(obj, depsgraph, unit, item, t):
    evaluated = obj.evaluated_get(depsgraph) if depsgraph is not None else obj
    world = mme_np_matrix(evaluated.matrix_world)
    pos = MME_SWAP3 @ world[:3, 3] / unit
    euler = evaluated.rotation_euler
    rotation = (-euler.x, -euler.z, -euler.y)
    lower = item.lower()
    if lower == "x":
        return [pos[0]]
    if lower == "y":
        return [pos[1]]
    if lower == "z":
        return [pos[2]]
    if lower == "xyz":
        return list(pos)
    if lower == "rx":
        return [rotation[0]]
    if lower == "ry":
        return [rotation[1]]
    if lower == "rz":
        return [rotation[2]]
    if lower == "rxyz":
        return list(rotation)
    if lower == "si":
        return [mme_eval_prop(obj, depsgraph, "mme_si", 1.0) * ACCESSORY_UNIT]
    if lower == "tr":
        return [mme_eval_prop(obj, depsgraph, "mme_tr", 1.0)]
    if lower:
        return [0.0]
    if fx_is_numeric(t) and len(t[1]) == 2:
        child_scale = np.diag([ACCESSORY_UNIT * unit] * 3 + [1.0])
        return list(mme_world_row(world @ child_scale, unit).reshape(-1))
    if fx_is_numeric(t) and t[0] == "b":
        return [1.0 if obj.visible_get() or not obj.hide_render else 0.0]
    return list(pos) + [1.0]


def mme_control_values(desc, root, depsgraph, unit, t):
    kind = desc.get("kind")
    if kind == "const":
        return list(desc.get("values", [0.0]))
    if kind == "self":
        return mme_object_items(root, depsgraph, unit, desc.get("item", ""), t)
    if kind == "prop":
        return [mme_eval_prop(root, depsgraph, desc.get("prop", ""), 0.0)]
    if kind == "object":
        other = bpy.data.objects.get(desc.get("object", ""))
        if other is None:
            return [0.0]
        return mme_object_items(other, depsgraph, unit, desc.get("item", ""), t)
    if kind == "bone":
        armature = bpy.data.objects.get(desc.get("object", ""))
        if armature is None or armature.type != "ARMATURE":
            return [0.0]
        evaluated = armature.evaluated_get(depsgraph) if depsgraph is not None else armature
        bone = evaluated.pose.bones.get(desc.get("bone", ""))
        if bone is None:
            return [0.0]
        world = mme_np_matrix(evaluated.matrix_world) @ mme_np_matrix(bone.matrix)
        if fx_is_numeric(t) and len(t[1]) == 2:
            return list(mme_world_row(world, unit).reshape(-1))
        pos = MME_SWAP3 @ world[:3, 3] / unit
        return list(pos) + [1.0]
    if kind == "model":
        if fx_is_numeric(t) and t[0] == "b":
            return [1.0]
        target = bpy.data.objects.get(desc.get("object", ""))
        if target is None:
            return [0.0]
        return mme_object_items(target, depsgraph, unit, "", t)
    return [0.0]


def mme_frame_state(scene, depsgraph, root, runtime, unit):
    state = FxSceneState()
    state.ortho = False
    render = scene.render
    pct = render.resolution_percentage / 100.0
    state.viewport = (render.resolution_x * pct, render.resolution_y * pct)
    mme_camera_state(state, scene, depsgraph, unit)
    mme_light_state(state, depsgraph)
    fps = scene_fps(scene) or PMM_FPS
    state.time = scene.frame_current / fps
    state.elapsed = 1.0 / fps
    controls = mme_load_controls(root)
    program = runtime.program
    for name, desc in controls.items():
        g = program.globals.get(name)
        if g is None:
            continue
        t = fx_resolve_type(g.type, program)
        try:
            state.controls[name] = [float(v) for v in mme_control_values(desc, root, depsgraph, unit, t)]
        except Exception as error:
            log.debug("control %s failed: %s", name, error)
            state.controls[name] = [0.0]
    return state


def mme_self_tr_globals(runtime, root):
    found = []
    for name, desc in mme_load_controls(root).items():
        if desc.get("kind") == "self" and desc.get("item", "").lower() == "tr":
            found.append(name)
    return found


def mme_loop_values(draw, inst, density):
    values = {}
    counts = [mme_loop_count(count, density) for _var, count, _name in draw.loops]
    remaining = inst.copy()
    for (var, _count, _name), count in reversed(list(zip(draw.loops, counts))):
        if var:
            values[var] = (remaining % count).astype(np.float64)
        remaining = remaining // count
    return values


def mme_loop_count(count, density):
    return max(1, int(round(count * max(0.0, min(1.0, density)))))


def mme_run_vs(runtime, data, state):
    program = runtime.program
    function, args = fx_entry(program, data.draw.fx_pass.vs)
    if function is None:
        raise FxUnsupported("pass %s has no vertex shader" % data.draw.fx_pass.name)
    runtime.material_state(state, data.draw.subset)
    ex = FxExec(program, FxNumpyBackend(), FxNumpyEnv(program, state, runtime.textures))
    for var, values in data.loop_values.items():
        ex.overrides[var] = FxValue(("i", ()), [values])
    provide = fx_vertex_inputs(data.positions, data.normals, data.uvs, data.src)
    with np.errstate(all="ignore"):
        outputs = fx_run_entry(ex, function, args, provide(ex))
    return outputs, ex


def mme_clip_world(outputs, state, count):
    position = outputs.get("POSITION0")
    if position is None:
        return np.zeros((count, 3)), np.ones(count, dtype=bool)
    comps = [np.broadcast_to(np.asarray(c, dtype=np.float64), (count,)) for c in (list(position.c) + [np.float64(1.0)] * 4)[:4]]
    view_proj = state.view @ state.proj
    try:
        inverse = np.linalg.inv(view_proj)
    except np.linalg.LinAlgError:
        inverse = np.identity(4)
    if getattr(state, "ortho", False):
        homo = np.stack(comps, axis=-1)
        world = homo @ inverse
        w = np.where(np.abs(world[:, 3:4]) < 1e-20, 1.0, world[:, 3:4])
        culled = (np.abs(comps[3]) < 1e-12) | (comps[2] < 0) | (comps[2] > comps[3])
        return world[:, :3] / w, culled
    with np.errstate(all="ignore"):
        return fx_clip_to_world(comps, inverse, state.proj)


def mme_prune_state(state):
    probe = FxSceneState()
    probe.__dict__.update(state.__dict__)
    probe.controls = dict(state.controls)
    eye = np.array([0.0, 0.0, -1.0e5])
    probe.view = fx_lh_look(eye, (0.0, 0.0, 1.0), (0.0, 1.0, 0.0))
    probe.proj = fx_lh_perspective(math.radians(30.0), 1.0, FX_NEAR, FX_FAR)
    probe.camera_pos = eye
    probe.camera_dir = np.array([0.0, 0.0, 1.0])
    probe.ortho = False
    return probe


def mme_prune_mask(runtime, data, state, tr_names):
    if not tr_names:
        samples = [None]
    else:
        samples = MME_PRUNE_TR
    state = mme_prune_state(state)
    keep = np.zeros(data.count, dtype=bool)
    for value in samples:
        if value is not None:
            for name in tr_names:
                state.controls[name] = [value]
        outputs, _ex = mme_run_vs(runtime, data, state)
        _world, culled = mme_clip_world(outputs, state, data.count)
        keep |= ~culled
    return keep


def mme_build_mesh(name, positions, faces):
    mesh = bpy.data.meshes.new(name)
    count = len(positions)
    mesh.vertices.add(count)
    mesh.vertices.foreach_set("co", positions.astype(np.float32).reshape(-1))
    sizes = np.array([len(f) for f in faces], dtype=np.int64)
    flat = np.concatenate([np.asarray(f, dtype=np.int64) for f in faces]) if faces else np.zeros(0, dtype=np.int64)
    starts = np.zeros(len(faces), dtype=np.int64)
    if len(faces):
        starts[1:] = np.cumsum(sizes)[:-1]
    mesh.loops.add(len(flat))
    mesh.loops.foreach_set("vertex_index", flat.astype(np.int32))
    mesh.polygons.add(len(faces))
    mesh.polygons.foreach_set("loop_start", starts.astype(np.int32))
    mesh.update(calc_edges=True)
    return mesh


def mme_set_attribute(mesh, name, values, kind="FLOAT"):
    attribute = mesh.attributes.get(name)
    if attribute is None or attribute.data_type != kind or attribute.domain != "POINT":
        if attribute is not None:
            mesh.attributes.remove(attribute)
        attribute = mesh.attributes.new(name=name, type=kind, domain="POINT")
    if kind == "INT":
        attribute.data.foreach_set("value", np.asarray(values, dtype=np.int32))
    else:
        attribute.data.foreach_set("value", np.asarray(values, dtype=np.float32))


def mme_read_attribute(mesh, name, kind="INT"):
    attribute = mesh.attributes.get(name)
    if attribute is None:
        return None
    out = np.empty(len(attribute.data), dtype=np.int32 if kind == "INT" else np.float32)
    attribute.data.foreach_get("value", out)
    return out


def mme_child_matrix(root, unit):
    return mme_np_matrix(root.matrix_world) @ np.diag([ACCESSORY_UNIT * unit] * 3 + [1.0])


def mme_build_pass(runtime, draw, state, root, collection, unit, density, label):
    subset_faces = [f for f, s in zip(runtime.faces, runtime.face_subsets) if s == draw.subset]
    if not subset_faces:
        return None
    used = np.unique(np.concatenate([np.asarray(f, dtype=np.int64) for f in subset_faces]))
    remap = {int(v): i for i, v in enumerate(used)}
    local_faces = [tuple(remap[int(v)] for v in reversed(f)) for f in subset_faces]
    counts = [mme_loop_count(count, density) for _var, count, _name in draw.loops]
    instances = int(np.prod(counts)) if counts else 1
    per = len(used)
    src = np.tile(used, instances)
    inst = np.repeat(np.arange(instances, dtype=np.int64), per)
    data = MmePassData(draw, src, inst, mme_loop_values(draw, inst, density), runtime)
    data.loop_values = mme_loop_values(draw, inst, density)
    state.world = mme_world_row(mme_child_matrix(root, unit), unit)
    tr_names = mme_self_tr_globals(runtime, root)
    keep = mme_prune_mask(runtime, data, state, tr_names)
    faces = []
    for k in range(instances):
        offset = k * per
        for face in local_faces:
            faces.append(tuple(v + offset for v in face))
    face_keep = [any(keep[v] for v in face) for face in faces]
    faces = [f for f, ok in zip(faces, face_keep) if ok]
    if not faces:
        log.info("effect pass %s draws nothing visible; skipped", label)
        return None
    alive = np.zeros(len(src), dtype=bool)
    for face in faces:
        alive[list(face)] = True
    new_index = -np.ones(len(src), dtype=np.int64)
    new_index[alive] = np.arange(int(alive.sum()))
    faces = [tuple(int(new_index[v]) for v in face) for face in faces]
    src = src[alive]
    inst = inst[alive]
    data = MmePassData(draw, src, inst, mme_loop_values(draw, inst, density), runtime)
    outputs, _ex = mme_run_vs(runtime, data, state)
    vs_outputs = {}
    for semantic, value in outputs.items():
        if semantic == "POSITION0" or not fx_is_numeric(value.t):
            continue
        vs_outputs[semantic] = len(value.c)
    world, culled = mme_clip_world(outputs, state, data.count)
    local = mme_world_to_local(world, culled, mme_child_matrix(root, unit), unit)
    mesh = mme_build_mesh(label, local, faces)
    mme_set_attribute(mesh, MME_ATTR_SRC, src, "INT")
    mme_set_attribute(mesh, MME_ATTR_INST, inst, "INT")
    mme_set_attribute(mesh, MME_ATTR_CULL, culled.astype(np.float32))
    for var, values in data.loop_values.items():
        mme_set_attribute(mesh, MME_LOOP_PREFIX + var, values)
    runtime.material_state(state, draw.subset)
    note = ""
    try:
        mat, varyings, uniforms, mode, _loop_attrs, node_count = mme_compile_material(
            label, runtime, draw, state, vs_outputs
        )
        note = "%s, %d nodes" % (mode.lower(), node_count)
    except FxError as error:
        log.warning("effect pass %s: pixel shader not converted (%s); using a plain material", label, error)
        mat = mme_fallback_material(label, runtime, draw.subset)
        varyings = [mme_varying_name("TEXCOORD0", 0), mme_varying_name("TEXCOORD0", 1)]
        uniforms = []
        note = "fallback material"
    mesh.materials.append(mat)
    obj = bpy.data.objects.new(label, mesh)
    collection.objects.link(obj)
    obj.parent = root
    obj[MME_DRAW_PROP] = json.dumps(
        {
            "order": draw.order,
            "technique": draw.technique.name,
            "pass": draw.fx_pass.name,
            "subset": draw.subset,
            "loops": [[var, count] for var, count in zip([l[0] for l in draw.loops], counts)],
            "density": density,
        }
    )
    obj[MME_VARYINGS_PROP] = json.dumps(varyings)
    obj[MME_UNIFORMS_PROP] = json.dumps(uniforms)
    for prop, gname, index in uniforms:
        obj[prop] = 0.0
    try:
        obj.visible_shadow = False
    except AttributeError:
        pass
    for name in ("visible_diffuse", "visible_glossy", "visible_transmission", "visible_volume_scatter"):
        if hasattr(obj, name):
            try:
                setattr(obj, name, False)
            except (AttributeError, TypeError):
                pass
    mme_write_outputs(obj, mesh, outputs, data.count, varyings)
    log.info(
        "effect pass %s: %d vertices, %d faces, %d instance(s), %s",
        label,
        len(src),
        len(faces),
        instances,
        note,
    )
    return obj


def mme_world_to_local(world, culled, child_matrix, unit):
    blender = (world @ MME_SWAP3.T) * unit
    inverse = np.linalg.inv(child_matrix)
    local = blender @ inverse[:3, :3].T + inverse[:3, 3]
    local[culled] = 0.0
    return np.where(np.isfinite(local), local, 0.0)


def mme_write_outputs(obj, mesh, outputs, count, varyings):
    for name in varyings:
        body = name[len(MME_VARYING_PREFIX):]
        semantic, _sep, index = body.rpartition("_")
        value = outputs.get(semantic)
        if value is None or not fx_is_numeric(value.t):
            arr = np.zeros(count)
        else:
            k = int(index)
            comp = value.c[k] if k < len(value.c) else np.float64(0.0)
            arr = np.broadcast_to(np.asarray(comp, dtype=np.float64), (count,))
        mme_set_attribute(mesh, name, np.nan_to_num(arr, nan=0.0, posinf=1e6, neginf=-1e6))


def mme_pass_data(runtime, obj):
    try:
        info = json.loads(obj.get(MME_DRAW_PROP, "{}"))
    except (TypeError, ValueError):
        return None
    mesh = obj.data
    stamp = (mesh.name, len(mesh.vertices), obj.get(MME_DRAW_PROP))
    cached = runtime.passes.get(obj.name)
    if cached is not None and cached.stamp == stamp:
        return cached
    draw = runtime.draw_by_order(info.get("order", -1))
    if draw is None or draw.fx_pass.name != info.get("pass") or draw.technique.name != info.get("technique"):
        return None
    src = mme_read_attribute(mesh, MME_ATTR_SRC)
    inst = mme_read_attribute(mesh, MME_ATTR_INST)
    if src is None or inst is None or len(src) != len(mesh.vertices):
        return None
    src = src.astype(np.int64)
    inst = inst.astype(np.int64)
    if len(src) and src.max() >= len(runtime.positions):
        return None
    data = MmePassData(draw, src, inst, mme_loop_values(draw, inst, info.get("density", 1.0)), runtime)
    data.stamp = stamp
    runtime.passes[obj.name] = data
    return data


def mme_update_pass(runtime, obj, root, state, unit, depsgraph=None):
    data = mme_pass_data(runtime, obj)
    if data is None:
        return False
    evaluated = obj.evaluated_get(depsgraph) if depsgraph is not None else obj
    child = mme_np_matrix(evaluated.matrix_world)
    state.world = mme_world_row(child, unit)
    outputs, _ex = mme_run_vs(runtime, data, state)
    world, culled = mme_clip_world(outputs, state, data.count)
    local = mme_world_to_local(world, culled, child, unit)
    mesh = obj.data
    mesh.vertices.foreach_set("co", local.astype(np.float32).reshape(-1))
    mme_set_attribute(mesh, MME_ATTR_CULL, culled.astype(np.float32))
    try:
        varyings = json.loads(obj.get(MME_VARYINGS_PROP, "[]"))
    except (TypeError, ValueError):
        varyings = []
    mme_write_outputs(obj, mesh, outputs, data.count, varyings)
    try:
        uniforms = json.loads(obj.get(MME_UNIFORMS_PROP, "[]"))
    except (TypeError, ValueError):
        uniforms = []
    program = runtime.program
    for prop, gname, index in uniforms:
        g = program.globals.get(gname)
        if g is None:
            continue
        source = fx_classify_global(g, program)
        if source is None:
            continue
        values = state.values(source, g)
        obj[prop] = float(values[index]) if index < len(values) else 0.0
    mesh.update()
    obj.update_tag()
    return True


def mme_effect_roots(scene):
    collection = bpy.data.collections.get(ACCESSORY_COLLECTION)
    if collection is None:
        return []
    return [
        obj
        for obj in collection.objects
        if obj.get("mme_fx") and obj.get("mmd_bridge_accessory") is not None
    ]


def mme_update_scene(scene, depsgraph=None, force=False):
    settings = getattr(scene, "mmd_bridge", None)
    if settings is None or not getattr(settings, "mme_effects", False):
        return 0
    rendering = False
    try:
        rendering = bpy.app.is_job_running("RENDER")
    except (AttributeError, TypeError):
        pass
    if not (force or rendering or settings.mme_live):
        return 0
    updated = 0
    for root in mme_effect_roots(scene):
        children = [c for c in root.children if c.get(MME_DRAW_PROP)]
        if not children:
            continue
        if rendering:
            children = [c for c in children if not c.hide_render]
        else:
            children = [c for c in children if not c.hide_viewport and not c.hide_get()]
        if not children:
            continue
        try:
            runtime = mme_runtime_for(root)
        except Exception as error:
            key = (root.name, str(error))
            if key not in _mme_state["errors"]:
                _mme_state["errors"].add(key)
                log.error("effect for '%s' could not be loaded: %s", root.name, error)
            continue
        if runtime is None:
            continue
        unit = float(root.get("mme_unit", 1.0)) or 1.0
        state = mme_frame_state(scene, depsgraph, root, runtime, unit)
        for child in children:
            try:
                if mme_update_pass(runtime, child, root, state, unit, depsgraph):
                    updated += 1
            except Exception as error:
                key = (child.name, str(error))
                if key not in _mme_state["errors"]:
                    _mme_state["errors"].add(key)
                    log.error("effect pass '%s' failed: %s\n%s", child.name, error, traceback.format_exc())
    return updated


@bpy.app.handlers.persistent
def mme_frame_handler(scene, depsgraph=None):
    if _mme_state["busy"]:
        return
    _mme_state["busy"] = True
    try:
        mme_update_scene(scene, depsgraph)
    finally:
        _mme_state["busy"] = False


@bpy.app.handlers.persistent
def mme_load_handler(*_args):
    _mme_runtimes.clear()
    _mme_state["errors"].clear()
    try:
        if settings_outdated(bpy.context.scene):
            schedule_relink()
    except AttributeError:
        pass


def mme_read_emm(pmm_path):
    path = os.path.splitext(pmm_path)[0] + ".emm"
    if not os.path.isfile(path):
        return {}, {}
    try:
        text = fx_read_text(path)
    except OSError:
        return {}, {}
    sections = {}
    current = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip().lower()
            sections.setdefault(current, {})
            continue
        if current and "=" in line:
            key, value = line.split("=", 1)
            sections[current][key.strip().lower()] = value.strip()
    return sections.get("object", {}), sections.get("effect", {})


def mme_find_effect(acc, x_path, settings, entry):
    if entry is not None and not entry.use_effect:
        return None
    if entry is not None and entry.effect_path:
        override = bpy.path.abspath(entry.effect_path)
        return override if os.path.isfile(override) else None
    pmm_path = bpy.path.abspath(settings.filepath)
    objects, effects = mme_read_emm(pmm_path)
    if effects:
        base = acc.path.replace("\\", "/").rsplit("/", 1)[-1].lower()
        slot = None
        for key, value in objects.items():
            if key.startswith("acc") and value.replace("\\", "/").rsplit("/", 1)[-1].lower() == base:
                slot = key
                break
        if slot is None:
            slot = "acc%d" % (acc.index + 1)
        value = effects.get(slot)
        if value is not None:
            if value.lower() in ("none", ""):
                return None
            for candidate in (value, os.path.join(os.path.dirname(pmm_path), value)):
                if os.path.isfile(candidate):
                    return candidate
            found = fx_find_file(os.path.dirname(x_path), value.replace("\\", "/").rsplit("/", 1)[-1])
            if found:
                return found
    stem = os.path.splitext(os.path.basename(x_path))[0].lower()
    folder = os.path.dirname(x_path)
    try:
        for name in os.listdir(folder):
            lower = name.lower()
            if lower == stem + ".fx":
                return os.path.join(folder, name)
    except OSError:
        pass
    return None


def mme_resolve_controls(program, project, settings, acc, root):
    controls = {}
    by_path = {}
    for model in project.models:
        base = model.path.replace("\\", "/").rsplit("/", 1)[-1].lower()
        by_path[base] = model
        if model.name:
            by_path.setdefault(model.name.lower(), model)
    accessories = {}
    for other in project.accessories:
        accessories[other.name.lower()] = other
        accessories[other.path.replace("\\", "/").rsplit("/", 1)[-1].lower()] = other
    items = {t.key: t for t in settings.targets}
    collection = bpy.data.collections.get(ACCESSORY_COLLECTION)
    morph_tracks = {}
    for g in program.globals.values():
        source = fx_classify_global(g, program)
        if source is None or source[0] != "control":
            continue
        name, item = source[1], source[2]
        lower = name.lower()
        desc = {"kind": "const", "values": [0.0]}
        if lower in MME_SELF_NAMES:
            desc = {"kind": "self", "item": item}
        elif lower.endswith((".x", ".vac")):
            other = accessories.get(lower)
            if other is not None and collection is not None:
                other_root = find_accessory_root(collection, accessory_key(other))
                if other_root is not None and other_root != root:
                    desc = {"kind": "object", "object": other_root.name, "item": item}
                elif other_root is root:
                    desc = {"kind": "self", "item": item}
        else:
            model = by_path.get(lower)
            if model is None:
                stem = os.path.splitext(lower)[0]
                model = next((m for k, m in by_path.items() if os.path.splitext(k)[0] == stem), None)
            if model is not None:
                item_obj = items.get(model_key(model))
                target = item_obj.target if item_obj is not None else None
                if item and item in model.morph_names:
                    prop = mme_prop_name(MME_CONTROL_PREFIX, g.name)
                    morph_tracks[prop] = model.morphs[model.morph_names.index(item)]
                    desc = {"kind": "prop", "prop": prop}
                elif item and item in model.bone_names and target is not None:
                    armature = find_armature(target)
                    bone = pose_bone_name(armature, item) if armature is not None else None
                    if bone is not None:
                        desc = {"kind": "bone", "object": armature.name, "bone": bone}
                elif not item:
                    desc = {"kind": "model", "object": target.name if target is not None else ""}
        controls[g.name] = desc
    return controls, morph_tracks


def mme_key_morph_props(root, morph_tracks):
    anim = root.animation_data or root.animation_data_create()
    for prop, keys in morph_tracks.items():
        if not keys:
            root[prop] = 0.0
            continue
        for key in keys:
            root[prop] = float(key.weight)
            root.keyframe_insert('["%s"]' % prop, frame=key.frame)
    action = anim.action
    if action is not None:
        for fcurve in iter_fcurves(action):
            if fcurve.data_path.startswith('["%s' % MME_CONTROL_PREFIX):
                for point in fcurve.keyframe_points:
                    point.interpolation = "LINEAR"


def mme_build_effect(scene, collection, settings, acc, root, x_path, fx_path, unit, controls):
    _mme_runtimes.pop(root.name, None)
    runtime = MmeRuntime(fx_path, x_path)
    root["mme_fx"] = fx_path
    root["mme_x"] = x_path
    root["mme_unit"] = unit
    root["mme_controls"] = json.dumps(controls)
    runtime.controls_runtime = {name for name, desc in controls.items() if desc.get("kind") != "const"}
    root["mme_tr"] = float(acc.keys[0].opacity) if acc.keys else 1.0
    root["mme_si"] = float(acc.keys[0].scale) if acc.keys else 1.0
    for name, desc in controls.items():
        if desc.get("kind") == "prop":
            root[desc["prop"]] = 0.0
    depsgraph = bpy.context.evaluated_depsgraph_get()
    state = mme_frame_state(scene, depsgraph, root, runtime, unit)
    density = settings.mme_density
    children = []
    base = short_name(acc.name, 24)
    emitted = [d for d in runtime.draws if d.target == "" and d.kind == "geometry"]
    for draw in emitted:
        label = "%s_%s_%s" % (base, draw.technique.name or "tech", draw.fx_pass.name or str(draw.order))
        if draw.fx_pass.vs is None:
            log.info("effect pass %s uses fixed-function drawing; skipped", label)
            continue
        try:
            obj = mme_build_pass(runtime, draw, state, root, collection, unit, density, label)
        except FxError as error:
            log.warning("effect pass %s skipped: %s", label, error)
            continue
        if obj is not None:
            children.append(obj)
    covered = {d.subset for d in runtime.draws}
    uncovered = [s for s in range(len(runtime.materials)) if s not in covered]
    offscreen = sorted({d.target for d in runtime.draws if d.target})
    skipped = [d.label for d in runtime.draws if d.kind == "buffer"]
    if offscreen:
        log.info(
            "effect '%s': offscreen targets %s are emulated by drawing their source shading directly",
            os.path.basename(fx_path),
            offscreen,
        )
    if skipped:
        log.info("effect '%s': screen-space passes not emulated: %s", os.path.basename(fx_path), skipped)
    _mme_runtimes[root.name] = runtime
    return children, uncovered, None, len(emitted)


def mme_clear_root_props(root):
    for name in [k for k in root.keys() if k in ("mme_fx", "mme_x", "mme_unit", "mme_controls") or k.startswith(MME_CONTROL_PREFIX)]:
        del root[name]
    _mme_runtimes.pop(root.name, None)


def remove_subset_faces(obj, subsets):
    mesh = obj.data
    if not subsets or not len(mesh.polygons):
        return
    indices = np.empty(len(mesh.polygons), dtype=np.int32)
    mesh.polygons.foreach_get("material_index", indices)
    import bmesh

    bm = bmesh.new()
    bm.from_mesh(mesh)
    doomed = [f for f in bm.faces if f.material_index in subsets]
    if doomed:
        bmesh.ops.delete(bm, geom=doomed, context="FACES")
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()


def effect_setup(project, settings, acc, entry, path, root):
    if not settings.mme_effects:
        return None, None, {}, {}
    fx_path = mme_find_effect(acc, path, settings, entry)
    if not fx_path:
        return None, None, {}, {}
    try:
        program = fx_parse_file(fx_path)
        controls, tracks = mme_resolve_controls(program, project, settings, acc, root)
    except (FxError, OSError, UnicodeError) as error:
        log.error("accessory '%s': effect %s could not be read: %s", acc.name, fx_path, error)
        if entry is not None:
            entry.effect_note = "Effect error: %s" % short_text(error, 60)
        return None, None, {}, {}
    return fx_path, program, controls, tracks


def import_accessory(scene, collection, project, settings, acc, entry, unit):
    path = find_accessory_file(acc, settings)
    if path is None:
        log.warning(
            "accessory '%s': file not found (PMM path %s); set an Accessory Folder",
            acc.name,
            acc.path,
        )
        if entry is not None:
            entry.effect_note = "File not found"
        return False
    key = accessory_key(acc)
    root = find_accessory_root(collection, key)
    if root is None:
        root = bpy.data.objects.new(ACCESSORY_PREFIX + short_name(acc.name), None)
        root.empty_display_type = "PLAIN_AXES"
        root.empty_display_size = 1.0
        root["mmd_bridge_accessory"] = key
        collection.objects.link(root)
        log.info("created accessory root '%s'", root.name)
    fx_path, program, controls, tracks = effect_setup(project, settings, acc, entry, path, root)
    stamp = "%s|%s" % (path, os.path.getmtime(path))
    if fx_path:
        stamp += "|%s|density=%.4f|unit=%.6f|%s" % (
            mme_effect_stamp(fx_path, path, program),
            settings.mme_density,
            unit,
            json.dumps(sorted((k, v.get("kind")) for k, v in controls.items())),
        )
    if root.get("mmd_bridge_stamp") != stamp or not root.children:
        remove_accessory_children(root)
        mme_clear_root_props(root)
        with open(path, "rb") as handle:
            raw = handle.read()
        xscene = parse_x_bytes(raw)
        folder = os.path.dirname(path)
        for note in xscene.warnings:
            log.warning("accessory '%s': %s", acc.name, note)
        children = None
        if fx_path and xscene.mode != "txt":
            log.warning(
                "accessory '%s': effects need a text .x file; %s is %s, so it is imported without its effect",
                acc.name,
                os.path.basename(path),
                xscene.mode,
            )
        elif fx_path:
            started = time.time()
            try:
                children, uncovered, _tracks, total = mme_build_effect(
                    scene, collection, settings, acc, root, path, fx_path, unit, controls
                )
                if uncovered:
                    static = create_x_object(
                        short_name(acc.name, 40) + "_mesh", xscene, folder, settings
                    )
                    collection.objects.link(static)
                    remove_subset_faces(static, set(range(len(static.data.materials))) - set(uncovered))
                    children.append(static)
                note = "%s: %d of %d pass(es) running" % (
                    os.path.basename(fx_path),
                    len([c for c in children if c.get(MME_DRAW_PROP)]),
                    total,
                )
                log.info(
                    "accessory '%s': effect %s built in %.1fs (%s)",
                    acc.name,
                    fx_path,
                    time.time() - started,
                    note,
                )
                if entry is not None:
                    entry.effect_note = note
            except Exception as error:
                log.error(
                    "accessory '%s': effect %s failed, importing the plain mesh instead: %s\n%s",
                    acc.name,
                    fx_path,
                    error,
                    traceback.format_exc(),
                )
                for child in list(root.children):
                    remove_accessory_children_single(child)
                mme_clear_root_props(root)
                if entry is not None:
                    entry.effect_note = "Effect failed: %s" % short_text(error, 60)
                children = None
        elif entry is not None:
            entry.effect_note = ""
        if children is None:
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
    if root.get("mme_fx"):
        root["mme_controls"] = json.dumps(controls)
        root["mme_unit"] = unit
        mme_key_morph_props(root, tracks)
        runtime = _mme_runtimes.get(root.name)
        if runtime is not None:
            runtime.controls_runtime = {
                name for name, desc in controls.items() if desc.get("kind") != "const"
            }
    apply_accessory_parent(root, project, settings, acc, entry)
    return True


def remove_accessory_children_single(child):
    data = child.data
    bpy.data.objects.remove(child, do_unlink=True)
    if data is not None and getattr(data, "users", 1) == 0:
        try:
            for material in list(data.materials):
                if material is not None and material.users <= 1:
                    bpy.data.materials.remove(material)
            bpy.data.meshes.remove(data)
        except (ReferenceError, RuntimeError):
            pass


def import_accessories(scene, project, settings):
    if not settings.sync_accessories:
        return 0
    if project.accessory_error:
        log.warning("accessory section could not be read: %s", project.accessory_error)
        return 0
    if not project.accessories:
        return 0
    log.info(
        "accessories: MME effects %s, effect density %.2f",
        "on" if settings.mme_effects else "off",
        settings.mme_density,
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


def scene_fps(scene):
    render = scene.render
    return render.fps / (render.fps_base or 1.0)


def fps_differs(scene):
    return abs(scene_fps(scene) - PMM_FPS) > 0.01


def retime_factor(scene, settings):
    if not settings.retime_fps or settings.update_scene_settings:
        return 1.0
    if not fps_differs(scene):
        return 1.0
    return scene_fps(scene) / PMM_FPS


def retime_keys(keys, factor, hold_cuts=False):
    scaled = {}
    previous_old = None
    previous_new = None
    for key in sorted(keys, key=lambda k: k.frame):
        new = int(round(key.frame * factor))
        if hold_cuts and previous_old is not None and key.frame - previous_old <= 1:
            new = previous_new + 1
        scaled[new] = key._replace(frame=new)
        previous_old, previous_new = key.frame, new
    return [scaled[frame] for frame in sorted(scaled)]


def retime_project(project, factor):
    if abs(factor - 1.0) < 1e-6:
        return
    for model in project.models:
        model.bones = [retime_keys(keys, factor) for keys in model.bones]
        model.morphs = [retime_keys(keys, factor) for keys in model.morphs]
        model.last_frame = int(round(model.last_frame * factor))
    project.camera_keys = retime_keys(project.camera_keys, factor, True)
    project.light_keys = retime_keys(project.light_keys, factor, True)
    for acc in project.accessories:
        acc.keys = retime_keys(acc.keys, factor)


def find_audio_file(project, settings):
    pmm_dir = os.path.dirname(bpy.path.abspath(settings.filepath))
    stored = project.audio_path.replace("\\", "/")
    base = stored.rsplit("/", 1)[-1]
    candidates = []
    if settings.audio_path:
        candidates.append(bpy.path.abspath(settings.audio_path))
    candidates += [project.audio_path, stored]
    if base:
        candidates += [
            os.path.join(pmm_dir, base),
            os.path.join(pmm_dir, "Audio", base),
            os.path.join(pmm_dir, "BGM", base),
        ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def describe_audio(project, settings):
    if settings.audio_path:
        found = os.path.isfile(bpy.path.abspath(settings.audio_path))
        return "Override file %s" % ("found" if found else "not found")
    if not project.audio_enabled or not project.audio_path:
        return "PMM has no audio"
    base = project.audio_path.replace("\\", "/").rsplit("/", 1)[-1]
    return "PMM audio: %s (%s)" % (
        base,
        (
            "found"
            if find_audio_file(project, settings)
            else "not found, set an Audio File"
        ),
    )


def sequence_strips(editor):
    strips = getattr(editor, "strips", None)
    if strips is None:
        strips = getattr(editor, "sequences", None)
    return strips


def import_audio(scene, project, settings):
    if not settings.sync_audio:
        return False
    if not settings.audio_path and not (project.audio_enabled and project.audio_path):
        log.info("PMM has no audio")
        return False
    path = find_audio_file(project, settings)
    if path is None:
        log.warning(
            "audio file not found (PMM path %s); set an Audio File in the Audio panel",
            project.audio_path,
        )
        return False
    editor = scene.sequence_editor or scene.sequence_editor_create()
    strips = sequence_strips(editor)
    existing = next((s for s in strips if s.name == AUDIO_STRIP_NAME), None)
    if existing is not None:
        sound = getattr(existing, "sound", None)
        current = (
            os.path.normcase(os.path.abspath(bpy.path.abspath(sound.filepath)))
            if sound is not None
            else ""
        )
        if current == os.path.normcase(os.path.abspath(path)):
            log.info(
                "audio strip '%s' already uses %s; position left as is",
                existing.name,
                path,
            )
            return True
        strips.remove(existing)
    channel = max((s.channel for s in strips), default=0) + 1
    strips.new_sound(
        name=AUDIO_STRIP_NAME, filepath=path, channel=channel, frame_start=0
    )
    if hasattr(scene, "use_audio_scrub"):
        scene.use_audio_scrub = True
    log.info("audio strip added on channel %d at frame 0: %s", channel, path)
    if fps_differs(scene) and not settings.retime_fps:
        log.warning(
            "scene is %.3f fps and retiming is off, so animation and audio will drift apart",
            scene_fps(scene),
        )
    return True


class WarningCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.messages = []

    def emit(self, record):
        text = record.getMessage()
        if not text.startswith("mmd_tools reported"):
            self.messages.append(text)


def short_text(text, limit=80):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def sync_target(target, model, settings):
    capture = WarningCapture()
    log.addHandler(capture)
    try:
        import_model(target.target, model, settings, target)
    except Exception as error:
        log.error(
            "model '%s' failed: %s\n%s", target.pmm_name, error, traceback.format_exc()
        )
        target.state = "ERROR"
        target.status_text = short_text(error)
        return False
    finally:
        log.removeHandler(capture)
    if capture.messages:
        target.state = "WARN"
        target.status_text = "%d warning(s): %s" % (
            len(capture.messages),
            short_text(capture.messages[0], 70),
        )
    else:
        target.state = "OK"
        target.status_text = ""
    return True


def item_state(item):
    if not item.enabled:
        return "OFF"
    if item.target is None:
        return "ERROR"
    return item.state or "NEW"


def item_status_text(item):
    if not item.enabled:
        return ""
    if item.target is None:
        return "No target assigned"
    return item.status_text


def status_icon(state):
    if not _status_icons:
        available = set()
        try:
            available = set(
                bpy.types.UILayout.bl_rna.functions["label"]
                .parameters["icon"]
                .enum_items.keys()
            )
        except Exception:
            pass

        def pick(*names):
            for name in names:
                if name in available:
                    return name
            return names[-1]

        _status_icons.update(
            {
                "ERROR": pick("STRIP_COLOR_01", "SEQUENCE_COLOR_01", "ERROR"),
                "WARN": pick("STRIP_COLOR_03", "SEQUENCE_COLOR_03", "ERROR"),
                "OK": pick("STRIP_COLOR_04", "SEQUENCE_COLOR_04", "CHECKMARK"),
                "NEW": pick("STRIP_COLOR_05", "SEQUENCE_COLOR_05", "DOT"),
                "OFF": pick("STRIP_COLOR_09", "SEQUENCE_COLOR_09", "RADIOBUT_OFF"),
            }
        )
    return _status_icons.get(state, "BLANK1")


def redact_paths(text):
    text = re.sub(r"([A-Za-z]:[\\/]+Users[\\/]+)[^\\/\"']+", r"\1<user>", text)
    text = re.sub(r"(/(?:home|Users)/)[^/\"'\s]+", r"\1<user>", text)
    return text


def mmd_tools_version():
    try:
        base = mmd_tools_base()
        module = sys.modules.get(base) or importlib.import_module(base)
        info = getattr(module, "bl_info", None)
        if info and "version" in info:
            return ".".join(str(v) for v in info["version"])
    except Exception:
        pass
    return "unknown"


def build_report(context):
    scene = context.scene
    settings = scene.mmd_bridge
    lines = ["MMD PMM Bridge support report"]
    lines.append(
        "add-on %s | Blender %s | %s"
        % (
            ".".join(str(v) for v in bl_info["version"]),
            bpy.app.version_string,
            platform.system(),
        )
    )
    lines.append(
        "mmd_tools %s (available: %s) | DirectX X Importer available: %s"
        % (mmd_tools_version(), mmd_tools_available(), x_importer_available())
    )
    lines.append(
        "scene fps %.3f (PMM is %d) | retime factor %.4f"
        % (scene_fps(scene), PMM_FPS, retime_factor(scene, settings))
    )
    lines.append(
        "PMM file: %s"
        % (os.path.basename(bpy.path.abspath(settings.filepath)) or "none")
    )
    lines.append("status: %s" % settings.status)
    lines.append(
        "options: auto_scale=%s scale=%.4f clips=%s uv_morphs=%s retime=%s audio=%s accessories=%s sphere_maps=%s physics=%s frame_range=%s layout=%s camera=%s light=%s"
        % (
            settings.auto_scale,
            current_scale(settings),
            settings.use_clips,
            settings.uv_morphs,
            settings.retime_fps,
            settings.sync_audio,
            settings.sync_accessories,
            settings.sphere_maps,
            settings.use_physics_vmd,
            settings.update_scene_settings,
            settings.interpolation_layout,
            settings.sync_camera,
            settings.sync_light,
        )
    )
    lines.append("audio: %s" % (settings.audio_note or "not read"))
    for item in settings.targets:
        lines.append(
            "model '%s' -> %s | %s | motion=%s | shapekey=%s | %s"
            % (
                item.pmm_name,
                item.target.name if item.target else "none",
                item_state(item),
                item.clip.name if item.clip else "none",
                item.shape_clip.name if item.shape_clip else "none",
                item_status_text(item) or "no issues",
            )
        )
    for entry in settings.accessories:
        lines.append(
            "accessory '%s' enabled=%s parent_override=%s effect=%s %s"
            % (
                entry.display,
                entry.enabled,
                entry.parent_bone or "none",
                entry.use_effect,
                entry.effect_note or "",
            )
        )
    block = bpy.data.texts.get(LOG_TEXT_NAME)
    lines.append("")
    lines.append("---- last %d log lines ----" % REPORT_LOG_LINES)
    if block is not None:
        lines.extend(block.as_string().splitlines()[-REPORT_LOG_LINES:])
    else:
        lines.append("no log text block")
    return redact_paths("\n".join(lines))


BONE_PATH = re.compile(
    r'^pose\.bones\["((?:[^"\\]|\\.)*)"\]\.(?:location|rotation_quaternion|rotation_euler|scale)$'
)
KEY_PATH = re.compile(r'^key_blocks\["((?:[^"\\]|\\.)*)"\]\.value$')


def unescape_name(text):
    return text.replace('\\"', '"').replace("\\\\", "\\")


def model_owner_sets(root):
    _importer, model_mod, _utils = mmd_tools_modules()
    rig = model_mod.Model(root)
    armature = rig.armature()
    placeholder = rig.morph_slider.placeholder()
    meshes = list(rig.meshes())
    targets = [root]
    for extra in [armature, placeholder] + meshes:
        if extra is not None and extra not in targets:
            targets.append(extra)
    return armature, meshes, collect_owners(targets)


def remove_action(action):
    try:
        action.use_fake_user = False
        if action.users == 0:
            bpy.data.actions.remove(action)
            return True
    except ReferenceError:
        pass
    return False


def clean_model_animation(root, counts):
    armature, _meshes, owners = model_owner_sets(root)
    bones = set()
    shape_values = []
    doomed = []
    for owner in owners:
        anim = owner.animation_data
        if anim is None:
            continue
        actions = [
            strip.action
            for track in anim.nla_tracks
            for strip in track.strips
            if is_bridge_action(strip.action)
        ]
        if is_bridge_action(anim.action):
            actions.append(anim.action)
            anim.action = None
        for action in actions:
            for fcurve in iter_fcurves(action):
                matched = BONE_PATH.match(fcurve.data_path)
                if matched:
                    bones.add(unescape_name(matched.group(1)))
                matched = KEY_PATH.match(fcurve.data_path)
                if matched and hasattr(owner, "key_blocks"):
                    shape_values.append((owner, unescape_name(matched.group(1))))
        doomed.extend(actions)
    clear_bridge_nla(owners)
    return armature, bones, shape_values, doomed


def reset_animated(armature, bones, shape_values):
    count = 0
    if armature is not None:
        for name in bones:
            bone = armature.pose.bones.get(name)
            if bone is None:
                continue
            bone.location = (0.0, 0.0, 0.0)
            bone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
            bone.rotation_euler = (0.0, 0.0, 0.0)
            bone.scale = (1.0, 1.0, 1.0)
            count += 1
    for keys, name in shape_values:
        try:
            block = keys.key_blocks.get(name)
        except ReferenceError:
            continue
        if block is not None and block.name != "Basis":
            block.value = 0.0
            count += 1
    return count


def clean_uv_data(root, counts):
    _armature, meshes, _owners = model_owner_sets(root)
    for mesh_obj in meshes:
        if mesh_obj.type != "MESH" or mesh_obj.mode == "EDIT":
            continue
        clear_uv_channels(mesh_obj)
        keys = mesh_obj.data.shape_keys
        if keys is None:
            continue
        for block in [b for b in keys.key_blocks if b.name.startswith(UV_KEY_PREFIX)]:
            mesh_obj.shape_key_remove(block)
            counts["uv_keys"] += 1


def clean_camera_light(counts):
    for name in (CAMERA_COLLECTION, LIGHT_COLLECTION):
        collection = bpy.data.collections.get(name)
        if collection is None:
            continue
        for mmd_type in ("CAMERA", "LIGHT"):
            root = find_rig_root(collection, mmd_type)
            while root is not None:
                remove_rig(root)
                counts["rigs"] += 1
                root = find_rig_root(collection, mmd_type)
        if not collection.objects and not collection.children:
            bpy.data.collections.remove(collection)
    purge_actions(ACTION_PREFIX + "Scene")
    purge_actions(ACTION_PREFIX + "Light")


def clean_accessory_objects(counts):
    _mme_runtimes.clear()
    collection = bpy.data.collections.get(ACCESSORY_COLLECTION)
    if collection is not None:
        for obj in [
            o for o in collection.objects if o.get("mmd_bridge_accessory") is not None
        ]:
            remove_accessory_children(obj)
            bpy.data.objects.remove(obj, do_unlink=True)
            counts["accessories"] += 1
        if not collection.objects and not collection.children:
            bpy.data.collections.remove(collection)
    purge_actions(ACTION_PREFIX + "Acc_")


def clean_audio_strip(scene, counts):
    editor = scene.sequence_editor
    if editor is None:
        return
    strips = sequence_strips(editor)
    for strip in [s for s in strips if s.name == AUDIO_STRIP_NAME]:
        strips.remove(strip)
        counts["audio"] += 1


def run_clean(scene, settings, options):
    counts = {
        "clips": 0,
        "reset": 0,
        "uv_keys": 0,
        "rigs": 0,
        "accessories": 0,
        "audio": 0,
    }
    for item in settings.targets:
        root = item.target
        if root is None:
            continue
        try:
            if options["animation"]:
                if item.clip is not None and is_bridge_action(item.clip):
                    item.clip = None
                if item.shape_clip is not None and is_bridge_action(item.shape_clip):
                    item.shape_clip = None
                armature, bones, shape_values, doomed = clean_model_animation(
                    root, counts
                )
                seen = set()
                for action in doomed:
                    try:
                        pointer = action.as_pointer()
                    except ReferenceError:
                        continue
                    if pointer in seen:
                        continue
                    seen.add(pointer)
                    if remove_action(action):
                        counts["clips"] += 1
                if options["pose"]:
                    counts["reset"] += reset_animated(armature, bones, shape_values)
            if options["uv"]:
                clean_uv_data(root, counts)
        except Exception as error:
            log.error(
                "clean failed for '%s': %s\n%s",
                item.pmm_name,
                error,
                traceback.format_exc(),
            )
            continue
        item.state = "NEW"
        item.status_text = "cleaned"
    if options["animation"]:
        purge_actions(ACTION_PREFIX)
    if options["scene"]:
        clean_camera_light(counts)
    if options["accessories"]:
        clean_accessory_objects(counts)
    if options["audio"]:
        clean_audio_strip(scene, counts)
    scene.frame_set(scene.frame_current)
    log.info("clean done: %s", counts)
    return counts


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
    factor = retime_factor(scene, settings)
    if factor != 1.0:
        retime_project(project, factor)
        log.info(
            "retimed PMM keys by x%.4f for a %.3f fps scene (PMM is %d fps)",
            factor,
            scene_fps(scene),
            PMM_FPS,
        )
    elif settings.retime_fps and settings.update_scene_settings and fps_differs(scene):
        log.info(
            "retiming skipped because Update Frame Range will switch the scene to 30 fps"
        )
    log_scene_keys(project)
    refresh_targets(settings, project)
    refresh_accessories(settings, project)
    settings.audio_note = describe_audio(project, settings)
    log.info(
        "using scale %.4f (%s)",
        current_scale(settings),
        "auto from model" if settings.auto_scale else "manual",
    )
    by_key = {model_key(m): m for m in project.models}
    imported = 0
    failed = 0
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
            target.state = "ERROR"
            target.status_text = "Not present in the PMM"
            continue
        if sync_target(target, model, settings):
            imported += 1
        else:
            failed += 1
    _last_sync["failed"] = failed
    imported += import_scene_elements(scene, project, settings)
    imported += import_accessories(scene, project, settings)
    if settings.mme_effects and mme_effect_roots(scene):
        if not scene.render.use_lock_interface:
            scene.render.use_lock_interface = True
            log.info("render interface locking enabled so effects can update safely while rendering")
        if settings.mme_standard_view and scene.view_settings.view_transform != "Standard":
            try:
                scene.view_settings.view_transform = "Standard"
                log.info("view transform set to Standard so effect colors match MMD")
            except TypeError:
                pass
    try:
        if import_audio(scene, project, settings):
            imported += 1
    except Exception as error:
        log.error("audio failed: %s\n%s", error, traceback.format_exc())
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
    if _last_sync["failed"]:
        settings.status = "Synced %d item(s), %d model(s) failed at %s" % (
            imported,
            _last_sync["failed"],
            time.strftime("%H:%M:%S"),
        )
    else:
        settings.status = "Synced %d item(s) at %s" % (
            imported,
            time.strftime("%H:%M:%S"),
        )
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
    state: StringProperty(default="NEW")
    status_text: StringProperty()
    physics_vmd: StringProperty(
        name="Baked Physics VMD",
        description="Experimental: VMD baked from MMD with MMDBridge. Applied as a clip above the PMM clip",
        subtype="FILE_PATH",
    )


class MMDBridgeAccessory(PropertyGroup):
    key: StringProperty()
    display: StringProperty()
    enabled: BoolProperty(name="Enabled", default=True)
    use_effect: BoolProperty(
        name="Use Effect",
        description="Run this accessory's MME effect (.fx). Turn off to import the plain mesh",
        default=True,
    )
    effect_path: StringProperty(
        name="Effect File",
        description="Effect (.fx) to use instead of the one found next to the .x or in the PMM's .emm file",
        subtype="FILE_PATH",
    )
    effect_note: StringProperty()
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
    mme_effects: BoolProperty(
        name="MME Effects (.fx)",
        description="Run the MikuMikuEffect file of each accessory: the same-name .fx next to the .x, or the one assigned in the PMM's .emm file. Vertex shaders run every frame, pixel shaders become material nodes",
        default=True,
    )
    mme_density: FloatProperty(
        name="Effect Density",
        description="Share of each LoopByCount loop to build. 1 matches MMD. Lower values make heavy effects such as Nuke2 faster to scrub. Applied on the next sync",
        default=1.0,
        min=0.02,
        max=1.0,
        subtype="FACTOR",
    )
    mme_live: BoolProperty(
        name="Live Effect Preview",
        description="Recompute effect geometry whenever the frame changes in the viewport. Renders always recompute",
        default=True,
    )
    mme_standard_view: BoolProperty(
        name="MMD Colors (Standard View)",
        description="Switch the scene view transform to Standard when effects are synced so effect colors match MMD",
        default=True,
    )
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
    retime_fps: BoolProperty(
        name="Retime to Scene FPS",
        description="PMM keyframes are 30 fps. Scale every key frame by scene fps / 30 so motion keeps its real speed in a scene with a different frame rate",
        default=True,
    )
    sync_audio: BoolProperty(
        name="Sync Audio",
        description="Add the audio file stored in the PMM to the video sequencer, starting at frame 0",
        default=True,
    )
    audio_path: StringProperty(
        name="Audio File",
        description="Audio file to use instead of the one stored in the PMM. Leave empty to use the PMM path, then the PMM folder",
        subtype="FILE_PATH",
    )
    audio_note: StringProperty()
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
    follow_camera: BoolProperty(
        name="Follow Bone",
        description="Make the camera follow the model and bone stored in each PMM camera keyframe",
        default=True,
    )
    follow_rotation: BoolProperty(
        name="Follow Bone Rotation",
        description="Also inherit the bone rotation, not only its position",
        default=False,
    )
    targets: CollectionProperty(type=MMDBridgeTarget)


STALE_SETTINGS_MESSAGE = "An older copy of MMD PMM Bridge replaced this add-on's settings. They were re-linked; press the button again, and remove the old copy in Preferences > Add-ons"
_relink = {"pending": False}


def settings_outdated(scene):
    settings = getattr(scene, "mmd_bridge", None)
    return settings is None or not hasattr(settings, "mme_effects")


def relink_settings():
    _relink["pending"] = False
    try:
        scene = bpy.context.scene
    except AttributeError:
        scene = None
    if scene is not None and not settings_outdated(scene):
        return None
    reload_in_place()
    log.warning(
        "settings were registered by an older copy of MMD PMM Bridge; re-linked to this version. Remove the old copy in Preferences > Add-ons"
    )
    redraw_all()
    return None


def schedule_relink():
    if _relink["pending"]:
        return
    _relink["pending"] = True
    bpy.app.timers.register(relink_settings, first_interval=0.01)


def refuse_if_outdated(operator, context):
    if not settings_outdated(context.scene):
        return False
    schedule_relink()
    operator.report({"WARNING"}, STALE_SETTINGS_MESSAGE)
    return True


class MMDBRIDGE_OT_sync_now(Operator):
    bl_idname = "mmd_bridge.sync_now"
    bl_label = "Sync Now"

    def execute(self, context):
        if refuse_if_outdated(self, context):
            return {"CANCELLED"}
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
        if refuse_if_outdated(self, context):
            return {"CANCELLED"}
        settings = context.scene.mmd_bridge
        try:
            project = parse_pmm_file(bpy.path.abspath(settings.filepath))
        except (OSError, PmmError) as error:
            self.report({"ERROR"}, str(error))
            log.error("scan failed: %s", error)
            return {"CANCELLED"}
        refresh_targets(settings, project)
        refresh_accessories(settings, project)
        settings.audio_note = describe_audio(project, settings)
        self.report(
            {"INFO"},
            "Found %d model(s), %d accessory(ies)"
            % (len(project.models), len(project.accessories)),
        )
        return {"FINISHED"}


class MMDBRIDGE_OT_clean(Operator):
    bl_idname = "mmd_bridge.clean"
    bl_label = "Clean"
    bl_description = "Remove data created by this add-on. Your own actions, clips and shape keys are never touched"
    bl_options = {"REGISTER", "UNDO"}

    animation: BoolProperty(
        name="Model animation clips",
        description="Remove bridge-created motion, shapekey and physics clips and their NLA tracks",
        default=True,
    )
    pose: BoolProperty(
        name="Reset bones and shape keys the clips animated",
        description="Return only the bones and shape keys that the removed clips animated to their rest values",
        default=True,
    )
    uv: BoolProperty(
        name="UV morph data",
        description="Remove the MMDUV_ shape keys, offset attributes, shader nodes and drivers",
        default=True,
    )
    scene: BoolProperty(
        name="Camera and light rigs",
        description="Delete the MMD camera and sun rigs and their animation",
        default=False,
    )
    accessories: BoolProperty(
        name="Accessories",
        description="Delete imported accessory objects and their animation",
        default=False,
    )
    audio: BoolProperty(
        name="Audio strip",
        description="Remove the sound strip added by the bridge",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        return mmd_tools_available()

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=380)

    def draw(self, context):
        layout = self.layout
        draw_wrapped(
            layout,
            "Only data created by this add-on is removed. Actions, clips and shape keys you made or picked yourself are kept.",
            52,
        )
        layout.prop(self, "animation")
        row = layout.row()
        row.enabled = self.animation
        row.prop(self, "pose")
        layout.prop(self, "uv")
        layout.prop(self, "scene")
        layout.prop(self, "accessories")
        layout.prop(self, "audio")

    def execute(self, context):
        if refuse_if_outdated(self, context):
            return {"CANCELLED"}
        settings = context.scene.mmd_bridge
        options = {
            "animation": self.animation,
            "pose": self.pose,
            "uv": self.uv,
            "scene": self.scene,
            "accessories": self.accessories,
            "audio": self.audio,
        }
        counts = run_clean(context.scene, settings, options)
        settings.status = "Cleaned at %s" % time.strftime("%H:%M:%S")
        self.report(
            {"INFO"},
            "Removed %d clip(s), %d UV key(s), %d rig(s), %d accessory(ies), %d audio strip(s); reset %d channel(s)"
            % (
                counts["clips"],
                counts["uv_keys"],
                counts["rigs"],
                counts["accessories"],
                counts["audio"],
                counts["reset"],
            ),
        )
        redraw_all()
        return {"FINISHED"}


class MMDBRIDGE_OT_copy_report(Operator):
    bl_idname = "mmd_bridge.copy_report"
    bl_label = "Support Report"
    bl_description = "Copy a support report with versions, settings, model status and the recent log to the clipboard. User folders are redacted, but check it before posting"

    def execute(self, context):
        text = build_report(context)
        context.window_manager.clipboard = text
        block = bpy.data.texts.get(REPORT_TEXT_NAME) or bpy.data.texts.new(
            REPORT_TEXT_NAME
        )
        block.clear()
        block.write(text)
        self.report(
            {"INFO"},
            "Support report copied (%d lines) and saved to the %s text"
            % (len(text.splitlines()), REPORT_TEXT_NAME),
        )
        return {"FINISHED"}


class MMDBRIDGE_OT_refresh_effects(Operator):
    bl_idname = "mmd_bridge.refresh_effects"
    bl_label = "Refresh Effects"
    bl_description = "Reload effect files and recompute effect geometry for the current frame"

    def execute(self, context):
        if refuse_if_outdated(self, context):
            return {"CANCELLED"}
        _mme_runtimes.clear()
        _mme_state["errors"].clear()
        count = mme_update_scene(
            context.scene, context.evaluated_depsgraph_get(), force=True
        )
        self.report({"INFO"}, "Updated %d effect pass(es)" % count)
        redraw_all()
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
        if settings_outdated(context.scene):
            box = layout.box()
            box.alert = True
            draw_wrapped(box, STALE_SETTINGS_MESSAGE)
            schedule_relink()
            return
        settings = context.scene.mmd_bridge
        draw_dependency_notices(layout)
        if not mmd_tools_available():
            return
        layout.prop(settings, "filepath", text="")
        row = layout.row()
        row.scale_y = 1.5
        row.operator("mmd_bridge.sync_now", icon="FILE_REFRESH")
        layout.label(text=settings.status, icon="INFO")
        if fps_differs(context.scene):
            fps_box = layout.box()
            fps_box.label(
                text="Scene is %g fps, PMM is %d fps"
                % (round(scene_fps(context.scene), 3), PMM_FPS),
                icon="TIME",
            )
            fps_box.prop(settings, "retime_fps")
            if settings.retime_fps and settings.update_scene_settings:
                draw_wrapped(
                    fps_box,
                    "Update Frame Range also switches the scene to 30 fps, so retiming is skipped.",
                )
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
            state = item_state(item)
            row = box.row(align=True)
            row.prop(item, "enabled", text="")
            row.label(text="", icon=status_icon(state))
            row.label(text=item.pmm_name)
            row.prop(item, "target", text="")
            note = item_status_text(item)
            if note:
                note_row = box.row()
                note_row.alert = state == "ERROR"
                note_row.scale_y = 0.85
                note_row.label(text=short_text(note, 60))
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
        tools = layout.row(align=True)
        tools.operator("mmd_bridge.clean", icon="TRASH")
        tools.operator("mmd_bridge.copy_report", icon="COPYDOWN")
        layout.label(text="Log: Text Editor > %s" % LOG_TEXT_NAME)


class MMDBRIDGE_PT_audio(Panel):
    bl_label = "Audio"
    bl_idname = "MMDBRIDGE_PT_audio"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MMD Bridge"
    bl_parent_id = "MMDBRIDGE_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        settings = context.scene.mmd_bridge
        layout.prop(settings, "sync_audio")
        layout.prop(settings, "audio_path")
        draw_wrapped(
            layout,
            settings.audio_note or "Press the refresh icon in Models to read the PMM",
        )
        draw_wrapped(
            layout,
            "Audio starts at frame 0, the same as MMD. The PMM stores no audio offset.",
        )


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
        layout.prop(settings, "follow_camera")
        if settings.follow_camera:
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
        layout.prop(settings, "sphere_maps")
        layout.prop(settings, "accessory_dir")
        box = layout.box()
        box.prop(settings, "mme_effects")
        if settings.mme_effects:
            box.prop(settings, "mme_density", slider=True)
            box.prop(settings, "mme_live")
            box.prop(settings, "mme_standard_view")
            box.operator("mmd_bridge.refresh_effects", icon="SHADERFX")
            draw_wrapped(
                box,
                "Effects show in Material Preview and Rendered mode. Offscreen blur and post passes are approximated.",
            )
        armature = first_armature(settings)
        if not len(settings.accessories):
            layout.label(text="Press the refresh icon in Models to read the PMM")
        for entry in settings.accessories:
            row = layout.row(align=True)
            row.prop(entry, "enabled", text="")
            row.label(text=entry.display)
            if settings.mme_effects:
                row.prop(entry, "use_effect", text="", icon="SHADERFX")
            if armature is not None:
                row.prop_search(entry, "parent_bone", armature.pose, "bones", text="")
            if settings.mme_effects and entry.use_effect:
                sub = layout.row(align=True)
                sub.prop(entry, "effect_path", text="Effect")
            if entry.effect_note:
                note_row = layout.row()
                note_row.scale_y = 0.85
                note_row.alert = entry.effect_note.startswith(("Effect failed", "Effect error"))
                note_row.label(text=short_text(entry.effect_note, 60), icon="INFO")


CLASSES = (
    MMDBridgeTarget,
    MMDBridgeAccessory,
    MMDBridgeSettings,
    MMDBRIDGE_OT_sync_now,
    MMDBRIDGE_OT_scan,
    MMDBRIDGE_OT_clean,
    MMDBRIDGE_OT_copy_report,
    MMDBRIDGE_OT_refresh_effects,
    MMDBRIDGE_OT_dismiss_scale_warning,
    MMDBRIDGE_PT_main,
    MMDBRIDGE_PT_audio,
    MMDBRIDGE_PT_camera,
    MMDBRIDGE_PT_readout,
    MMDBRIDGE_PT_options,
    MMDBRIDGE_PT_accessories,
)


def remove_handlers():
    for handlers, name in (
        (bpy.app.handlers.frame_change_post, "mme_frame_handler"),
        (bpy.app.handlers.load_post, "mme_load_handler"),
    ):
        for handler in [h for h in handlers if getattr(h, "__name__", "") == name]:
            handlers.remove(handler)


def register():
    setup_logging()
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.mmd_bridge = PointerProperty(type=MMDBridgeSettings)
    remove_handlers()
    bpy.app.handlers.frame_change_post.append(mme_frame_handler)
    bpy.app.handlers.load_post.append(mme_load_handler)
    _mme_runtimes.clear()
    sweep_stale_temp()
    if not bpy.app.timers.is_registered(startup_check):
        bpy.app.timers.register(startup_check, first_interval=1.0)
    log.info("MMD PMM Bridge registered")


def startup_check():
    try:
        scene = bpy.context.scene
    except AttributeError:
        return None
    if scene is not None and settings_outdated(scene):
        schedule_relink()
    return None


def unregister():
    remove_handlers()
    _mme_runtimes.clear()
    if hasattr(bpy.types.Scene, "mmd_bridge"):
        del bpy.types.Scene.mmd_bridge
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


def reload_in_place():
    remove_handlers()
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