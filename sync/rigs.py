import bpy

from ..common.constants import CAMERA_COLLECTION, LIGHT_COLLECTION
from ..common.logger import log
from ..common.scene_utils import (
    child_of_type,
    ensure_collection,
    find_rig_root,
    move_to_collection,
)
from .mmd_tools_link import mmd_module
from .scale import current_scale


# Deletes a camera or light rig with its children and any camera or light data left
# unused.
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


# Rigs are built for a scale, which is stored on the root; a different current scale
# means rebuilding.
def rig_needs_rebuild(root, scale):
    return abs(float(root.get("mmd_bridge_scale", -1.0)) - scale) > 1e-9


# Finds or creates the mmd_tools camera rig in the bridge's collection, rebuilding
# it when the scale changed.
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


# Finds or creates the mmd_tools light rig in the bridge's collection, rebuilding it
# when the scale changed.
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
