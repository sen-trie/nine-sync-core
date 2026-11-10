import bpy
import re
import traceback

from .. import hooks
from ..common.animation import (
    clear_bridge_nla,
    collect_owners,
    is_bridge_action,
    iter_fcurves,
    purge_actions,
    remove_action,
)
from ..common.constants import (
    ACTION_PREFIX,
    AUDIO_STRIP_NAME,
    CAMERA_COLLECTION,
    LIGHT_COLLECTION,
    UV_KEY_PREFIX,
)
from ..common.logger import log
from ..common.scene_utils import find_rig_root
from .audio import sequence_strips
from .ik import restore_ik_influence
from .mmd_tools_link import mmd_tools_modules
from .rigs import remove_rig
from .uv_morphs import clear_uv_channels
from .visibility import VISIBILITY_PATHS


BONE_PATH = re.compile(
    r'^pose\.bones\["((?:[^"\\]|\\.)*)"\]\.(?:location|rotation_quaternion|rotation_euler|scale)$'
)


KEY_PATH = re.compile(r'^key_blocks\["((?:[^"\\]|\\.)*)"\]\.value$')


INFLUENCE_PATH = re.compile(r'^pose\.bones\[".*"\]\.constraints\[".*"\]\.influence$')


# Reverts the quoting Blender applies to names inside data paths.
def unescape_name(text):
    return text.replace('\\"', '"').replace("\\\\", "\\")


# Armature, meshes and animated IDs of a rig, found the same way the import does.
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


# Collects what the bridge animated (bones, shape keys) and the actions to delete,
# detaching them first. Only these channels are reset later.
def clean_model_animation(root, counts):
    armature, _meshes, owners = model_owner_sets(root)
    bones = set()
    shape_values = []
    doomed = []
    hidden = []
    switched = False
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
                if fcurve.data_path in VISIBILITY_PATHS and owner not in hidden:
                    hidden.append(owner)
                if owner == armature and INFLUENCE_PATH.match(fcurve.data_path):
                    switched = True
        doomed.extend(actions)
    clear_bridge_nla(owners)
    # Likewise an IK the clip switched off would stay off.
    if switched:
        restore_ik_influence(armature)
    # Without its keys a mesh keeps the hide state of the current frame, and nothing
    # would show it again, so models the bridge hid are shown whatever the pose option.
    for owner in hidden:
        for path in VISIBILITY_PATHS:
            setattr(owner, path, False)
    return armature, bones, shape_values, doomed


# Rests only the given bones and shape keys; the rest of the pose stays as the user
# left it.
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


# Removes the MMDUV_ shape keys, offset attributes and shader nodes.
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


# Deletes the camera and light rigs, empties the collections and their actions.
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


# Removes the bridge's sound strip.
def clean_audio_strip(scene, counts):
    editor = scene.sequence_editor
    if editor is None:
        return
    strips = sequence_strips(editor)
    for strip in [s for s in strips if s.name == AUDIO_STRIP_NAME]:
        strips.remove(strip)
        counts["audio"] += 1


# Removes what the bridge created, as selected in options. A failure on one model is
# logged and does not stop the others. FX cleaning runs through the clean hook.
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
        # What the record describes is about to go, so the next sync must not skip.
        if options["animation"] or options["uv"]:
            item.sync_record = ""
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
        settings.scene_record = ""
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
