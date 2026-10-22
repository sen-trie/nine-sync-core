bl_info = {
    "name": "MMD PMM Bridge",
    "author": "",
    "version": (0, 0, 1),
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


class PmmProject:
    __slots__ = (
        "version",
        "models",
        "selected_model",
        "camera_keys",
        "light_keys",
        "scene_error",
    )

    def __init__(self):
        self.version = 0
        self.models = []
        self.selected_model = 0
        self.camera_keys = []
        self.light_keys = []
        self.scene_error = ""


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


def build_vmd(model, layout="CURVE"):
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
        packed_name = encode_name(name)
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

_follow_items = [("NONE", "None", "")]
_last_sync = {"digest": None}


def mmd_tools_available():
    return hasattr(bpy.types, "MMD_TOOLS_OT_import_vmd")


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


def snapshot_outside(owners, low, high):
    saved = []
    for owner in owners:
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


def reset_animation(targets):
    for obj in targets:
        if obj.animation_data:
            obj.animation_data.action = None
        data = getattr(obj, "data", None)
        if obj.type in {"CAMERA", "LIGHT"} and data is not None and data.animation_data:
            data.animation_data.action = None
        shape_keys = getattr(data, "shape_keys", None)
        if shape_keys is not None:
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


def import_model(root, model, settings):
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
    data = build_vmd(model, settings.interpolation_layout)
    owners = collect_owners(targets)
    low, high = frame_span(model_frames(model))
    kept = snapshot_outside(owners, low, high)
    log.info(
        "PMM frame span for '%s': %s-%s; keeping keys outside it on %d curve(s)",
        model.name,
        low,
        high,
        len(kept),
    )
    clear_bridge_nla(owners)
    reset_animation(targets)
    purge_actions(ACTION_PREFIX)
    tmp_path = write_temp_vmd(data, TEMP_PREFIX)
    try:
        run_importer(
            tmp_path,
            settings,
            targets,
            ACTION_PREFIX + (model.name or model.name_en or "model"),
            False,
        )
    finally:
        remove_file(tmp_path)
    restored = restore_points(kept)
    tag_actions(owners, model.name or model.name_en or "model")
    log.info("restored %d preserved key(s) for '%s'", restored, model.name)
    describe_action("armature", armature)
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
        if owner is not collection:
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
    if settings.set_active_camera and scene.camera is not camera:
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
        import_model(target.target, model, settings)
        imported += 1
    imported += import_scene_elements(scene, project, settings)
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


class MMDBridgeTarget(PropertyGroup):
    key: StringProperty()
    pmm_name: StringProperty()
    enabled: BoolProperty(name="Enabled", default=True)
    target: PointerProperty(
        name="Target", type=bpy.types.Object, poll=is_mmd_root, update=on_target_changed
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
        self.report({"INFO"}, "Found %d model(s)" % len(project.models))
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
        if not mmd_tools_available():
            box = layout.box()
            box.alert = True
            box.label(text="Enable the mmd_tools add-on first", icon="ERROR")
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
        layout.prop(settings, "update_scene_settings")
        layout.prop(settings, "interpolation_layout")


CLASSES = (
    MMDBridgeTarget,
    MMDBridgeSettings,
    MMDBRIDGE_OT_sync_now,
    MMDBRIDGE_OT_scan,
    MMDBRIDGE_OT_dismiss_scale_warning,
    MMDBRIDGE_PT_main,
    MMDBRIDGE_PT_camera,
    MMDBRIDGE_PT_readout,
    MMDBRIDGE_PT_options,
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
