import bpy
import json
import logging
import numpy as np
import sys


MIN_BLENDER = (5, 3, 0)


NINESYNC_NAME = "\u2468-Sync"


NINESYNC_CATEGORY = "\u2468-Sync"


NINESYNC_MAIN_LABEL = NINESYNC_NAME + ": PMM Bridge"


NINESYNC_FX_LABEL = NINESYNC_NAME + ": FX Importer"


LOG_TEXT_NAME = "NineSyncLog"


LOG_TEXT_MAX_LINES = 3000


log = logging.getLogger("NineSync")


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
        "[NineSync %(asctime)s %(levelname)s] %(message)s", "%H:%M:%S"
    )
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    block = TextBlockHandler()
    block.setFormatter(formatter)
    log.addHandler(stream)
    log.addHandler(block)
    log.setLevel(logging.DEBUG)
    log.propagate = False


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


REPORT_TEXT_NAME = "NineSyncReport"


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


def model_key(model):
    return "%d:%s" % (model.index, model.name)


def redraw_all():
    wm = bpy.context.window_manager
    if wm is None:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            area.tag_redraw()


def find_armature(root):
    for child in root.children_recursive:
        if child.type == "ARMATURE":
            return child
    return None


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


def pose_bone_name(armature, mmd_name):
    for pose_bone in armature.pose.bones:
        mmd_bone = getattr(pose_bone, "mmd_bone", None)
        if mmd_bone is not None and mmd_bone.name_j == mmd_name:
            return pose_bone.name
    if mmd_name in armature.pose.bones:
        return mmd_name
    return None


def short_name(text, limit=48):
    out = ""
    for char in text:
        if len((out + char).encode("utf-8")) > limit:
            break
        out += char
    return out or "accessory"


def accessory_key(acc):
    return "%d:%s" % (acc.index, acc.name)


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


def find_accessory_root(collection, key):
    for obj in collection.objects:
        if obj.get("mmd_bridge_accessory") == key:
            return obj
    return None


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


MME_PARAM_PREFIX = "mmeP_"


MME_PARAMS_PROP = "mme_params"


MME_PARAM_BUILT_PROP = "mme_param_built"


MME_GN_MODIFIER = "MME"


MME_BACKEND_PROP = "mme_backend"


MME_PRUNED_PROP = "mme_pruned"


MME_BAKED_PROP = "mme_baked"


MME_DRIVEN_PROP = "mme_driven"


MME_OWNER_PROP = "mme_owner"


MME_GROUP_PROP = "mme_group"


MME_SIG_PROP = "mme_sig"


MME_ATTR_POS = "mme_p"


MME_ATTR_NRM = "mme_n"


MME_ATTR_UV = "mme_uv"


MME_REBUILD_DELAY = 0.6


_mme_runtimes = {}


_mme_state = {"busy": False, "errors": set(), "only": None, "rebuild": set()}


MME_CORE_VERSION = 2


MME_KEY_PROP = "mme_key"


MME_RET_PROP = "mme_ret"


MME_CORE_FILE = "mme_core.blend"


_mme_groups = {}


_mme_core = {
    "names": None,
    "path": None,
    "stats": {"built": 0, "file": 0, "library": 0},
}


def mme_load_controls(root):
    try:
        return json.loads(root.get("mme_controls", "{}"))
    except (TypeError, ValueError):
        return {}


def mme_param_meta(root):
    try:
        return json.loads(root.get(MME_PARAMS_PROP, "{}"))
    except (TypeError, ValueError):
        return {}


def mme_effect_roots(scene):
    collection = bpy.data.collections.get(ACCESSORY_COLLECTION)
    if collection is None:
        return []
    return [
        obj
        for obj in collection.objects
        if obj.get("mme_fx") and obj.get("mmd_bridge_accessory") is not None
    ]


def scene_fps(scene):
    render = scene.render
    return render.fps / (render.fps_base or 1.0)


def fps_differs(scene):
    return abs(scene_fps(scene) - PMM_FPS) > 0.01


def short_text(text, limit=80):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def mme_effect_passes(root):
    return [c for c in root.children if c.get(MME_DRAW_PROP)]
