ADDON_VERSION = (0, 8, 3)

bl_info = {
    "name": "NineSync: PMM Bridge",
    "author": "sentrie",
    "version": ADDON_VERSION,
    "blender": (5, 3, 0),
    "location": "View3D > Sidebar > NineSync",
    "description": "⑨-Sync: live-sync MikuMikuDance .pmm projects (models, camera, light, audio) through mmd_tools",
    "doc_url": "https://github.com/sen-trie/nine-sync-core",
    "tracker_url": "https://github.com/sen-trie/nine-sync-core/issues",
    "category": "Import-Export",
}

import sys as _sys

for _name in [n for n in _sys.modules if n.startswith(__name__ + ".")]:
    del _sys.modules[_name]

import bpy
from bpy.props import BoolProperty, CollectionProperty, IntProperty, PointerProperty

from . import hooks
from .common.constants import MIN_BLENDER, NINESYNC_NAME
from .common.logger import log, setup_logging
from .sync.analysis import clear_analysis
from .sync.engine import abandon_sync
from .sync.frames import clear_internal_frames
from .sync.tempfiles import sweep_stale_temp
from .ui import CLASSES
from .ui.preferences import (
    NineSyncPreferences,
    addon_preferences,
    apply_import_scale,
    bundle_package,
    republish_shared,
)
from .ui.properties import MMDBridgeControlName, MMDBridgeFreeBone, MMDBridgeSettings
from .ui.relink import schedule_relink, settings_outdated


_install = {"registered": False, "classes": []}


# Publishes the Core marker with this copy's package, so another copy can tell whose it is.
def publish_core():
    hooks.register_core(ADDON_VERSION)
    hooks.registry()["core"]["package"] = __package__


# Another Core that is registered and is not this one: its Scene property is there, or its
# marker names a different package. Looked at before anything is registered.
def conflicting_copy():
    if _install["registered"]:
        return False
    marker = hooks.registry().get("core")
    other = marker is not None and marker.get("package", "") not in ("", __package__)
    return hasattr(bpy.types.Scene, "mmd_bridge") or other


def active_classes():
    return [c for c in CLASSES if not (c is NineSyncPreferences and bundle_package())]


@bpy.app.handlers.persistent
def ninesync_load_handler(*_args):
    if not _install["registered"]:
        return
    publish_core()
    republish_shared()
    abandon_sync()
    clear_analysis()
    try:
        if settings_outdated(bpy.context.scene):
            schedule_relink()
    except AttributeError:
        pass


def remove_handlers():
    for handler in [
        h
        for h in bpy.app.handlers.load_post
        if getattr(h, "__name__", "") == "ninesync_load_handler"
    ]:
        bpy.app.handlers.load_post.remove(handler)


def register():
    if conflicting_copy():
        raise RuntimeError(
            "NineSync: PMM Bridge is already enabled from another install (a standalone copy or the "
            "combined NineSync extension); disable that copy first. Nothing was changed."
        )
    if bpy.app.version < MIN_BLENDER:
        raise RuntimeError(
            "NineSync needs Blender %d.%d or newer (this is %s)"
            % (MIN_BLENDER[0], MIN_BLENDER[1], bpy.app.version_string)
        )
    try:
        setup_logging()
        for cls in active_classes():
            _install["classes"].append(cls)
            bpy.utils.register_class(cls)
        bpy.types.Scene.mmd_bridge = PointerProperty(type=MMDBridgeSettings)
        bpy.types.WindowManager.ninesync_rigify_free = CollectionProperty(type=MMDBridgeFreeBone)
        bpy.types.WindowManager.ninesync_rigify_target = IntProperty()
        bpy.types.WindowManager.ninesync_rigify_controls = CollectionProperty(type=MMDBridgeControlName)
        bpy.types.WindowManager.ninesync_rigify_only_missing = BoolProperty(
            name="Only Missing", description="List only the bones the PMM moves that no Rigify control follows"
        )
        publish_core()
        _install["registered"] = True
        remove_handlers()
        bpy.app.handlers.load_post.append(ninesync_load_handler)
        republish_shared()
    except Exception:
        teardown()
        raise
    sweep_stale_temp()
    if not bpy.app.timers.is_registered(startup_check):
        bpy.app.timers.register(startup_check, first_interval=1.0)
    log.info("%s PMM Bridge registered", NINESYNC_NAME)


def startup_check():
    try:
        scene = bpy.context.scene
    except AttributeError:
        return None
    try:
        prefs = addon_preferences()
        if prefs is None:
            log.warning("NineSync preferences were not found; the saved import scale is not applied")
        elif prefs.use_import_scale:
            apply_import_scale(prefs.import_scale)
    except (AttributeError, RuntimeError):
        pass
    if scene is not None and settings_outdated(scene):
        schedule_relink()
    return None


# Removes only what this copy registered. Also the rollback of a register() that failed
# half way, so it tolerates anything missing.
def teardown():
    remove_handlers()
    marker = hooks.registry().get("core")
    if _install["registered"] and marker is not None and marker.get("package", __package__) == __package__:
        hooks.unregister_core()
    _install["registered"] = False
    if hasattr(bpy.types.Scene, "mmd_bridge"):
        del bpy.types.Scene.mmd_bridge
    for name in (
        "ninesync_rigify_free",
        "ninesync_rigify_target",
        "ninesync_rigify_controls",
        "ninesync_rigify_only_missing",
    ):
        if hasattr(bpy.types.WindowManager, name):
            delattr(bpy.types.WindowManager, name)
    classes, _install["classes"] = _install["classes"], []
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except (RuntimeError, ValueError):
            pass


# A copy that refused to register (or was never registered) owns nothing, so disabling it
# must not touch the active copy's properties, operators or marker.
def unregister():
    if not _install["registered"]:
        return
    abandon_sync()
    clear_analysis()
    clear_internal_frames()
    teardown()


def reload_in_place():
    unregister()
    register()


if __name__ == "__main__":
    reload_in_place()
