import bpy
import hashlib
import importlib
import inspect
import logging
import math
import os
import platform
import re
import sys
import tempfile
import time
import traceback
import types
from . import hooks
from mathutils import Matrix, Vector

from .common import (
    ACTION_PREFIX,
    AUDIO_STRIP_NAME,
    CAMERA_COLLECTION,
    CLIP_TRACK_NAME,
    FOLLOW_PREFIX,
    FRAME_EPSILON,
    LIGHT_COLLECTION,
    LOG_TEXT_NAME,
    MMD_TOOLS_URL,
    NINESYNC_NAME,
    PMM_FPS,
    REPORT_LOG_LINES,
    STALE_TEMP_SECONDS,
    TEMP_DIR_NAME,
    TEMP_PREFIX,
    UNIT_SCALE,
    UV_ATTRIBUTE_PREFIX,
    UV_KEY_PREFIX,
    UV_NODE_PREFIX,
    UV_SLIDER_LIMIT,
    _last_sync,
    _status_icons,
    child_of_type,
    ensure_collection,
    find_armature,
    find_rig_root,
    fps_differs,
    iter_fcurves,
    log,
    model_key,
    move_to_collection,
    pose_bone_name,
    redraw_all,
    scene_fps,
    short_text,
)
from .pmm import (
    PmmError,
    build_scene_vmd,
    build_vmd,
    parse_pmm,
)


def mmd_tools_available():
    return hasattr(bpy.types, "MMD_TOOLS_OT_import_vmd")


def addon_loaded(modules):
    try:
        names = list(bpy.context.preferences.addons.keys())
    except Exception:
        return False
    return any(name.rsplit(".", 1)[-1] in modules for name in names)


def missing_dependencies():
    missing = []
    if not mmd_tools_available():
        missing.append(
            ("mmd_tools", "mmd_tools is not installed or enabled", MMD_TOOLS_URL)
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


def is_mmd_root(self, obj):
    return getattr(obj, "mmd_type", "") == "ROOT"


def clip_poll(self, action):
    return not (
        action.name.startswith(ACTION_PREFIX + "Scene")
        or action.name.startswith(ACTION_PREFIX + "Light")
    )


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


def snapshot_outside(owners, low, high, sources=None, keep_all=None):
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
            everything = keep_all is not None and keep_all(fcurve.data_path)
            for point in fcurve.keyframe_points:
                frame = point.co[0]
                if everything or frame < low - FRAME_EPSILON or frame > high + FRAME_EPSILON:
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
    for attempt in ((index, -1) if index == 0 else (index,)):
        try:
            if group:
                owner.keyframe_insert(
                    data_path=data_path, index=attempt, frame=frame, group=group
                )
            else:
                owner.keyframe_insert(data_path=data_path, index=attempt, frame=frame)
        except (TypeError, RuntimeError, ValueError, KeyError):
            continue
        found = find_fcurve(owner, data_path, index)
        if found is not None:
            return found
    return None


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
    lines = [NINESYNC_NAME + " support report"]
    lines.append(
        "add-on %s | Blender %s | %s"
        % (
            ".".join(str(v) for v in addon_version()),
            bpy.app.version_string,
            platform.system(),
        )
    )
    lines.append(
        "mmd_tools %s (available: %s) | NineSync FX installed: %s"
        % (mmd_tools_version(), mmd_tools_available(), hooks.fx_installed())
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
        "options: auto_scale=%s scale=%.4f clips=%s uv_morphs=%s retime=%s audio=%s physics=%s frame_range=%s layout=%s camera=%s light=%s accessories_in_pmm=%d"
        % (
            settings.auto_scale,
            current_scale(settings),
            settings.use_clips,
            settings.uv_morphs,
            settings.retime_fps,
            settings.sync_audio,
            settings.use_physics_vmd,
            settings.update_scene_settings,
            settings.interpolation_layout,
            settings.sync_camera,
            settings.sync_light,
            settings.pmm_accessory_count,
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
    for extra in hooks.run("report", context):
        lines.extend(extra or [])
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
    if options.get("fx"):
        for result in hooks.run("clean", scene, options):
            for name, value in (result or {}).items():
                counts[name] = counts.get(name, 0) + value
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
    settings.pmm_accessory_count = len(project.accessories)
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
    if project.accessories and not hooks.fx_installed():
        log.info(
            "PMM has %d accessory(ies); install NineSync: FX to import them",
            len(project.accessories),
        )
    context = hook_context(scene, project, path, settings)
    for result in hooks.run("post_sync", context):
        if isinstance(result, int):
            imported += result
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


def hook_context(scene, project, path, settings):
    targets = {}
    for item in settings.targets:
        if item.target is not None:
            targets[item.key] = item.target
    return types.SimpleNamespace(
        scene=scene,
        project=project,
        pmm_path=path,
        targets=targets,
        unit=current_scale(settings),
        retimed=True,
        integrated=True,
        core_version=addon_version(),
    )


def addon_version():
    try:
        return tuple(importlib.import_module(__package__).bl_info["version"])
    except Exception:
        return (0, 0, 0)
