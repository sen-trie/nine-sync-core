ADDON_VERSION = (0, 8, 0)

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
from .sync.tempfiles import sweep_stale_temp
from .ui import CLASSES
from .ui.preferences import addon_preferences, apply_import_scale
from .ui.properties import MMDBridgeControlName, MMDBridgeFreeBone, MMDBridgeSettings
from .ui.relink import schedule_relink, settings_outdated


@bpy.app.handlers.persistent
def ninesync_load_handler(*_args):
    hooks.register_core(ADDON_VERSION)
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
    if bpy.app.version < MIN_BLENDER:
        raise RuntimeError(
            "NineSync needs Blender %d.%d or newer (this is %s)"
            % (MIN_BLENDER[0], MIN_BLENDER[1], bpy.app.version_string)
        )
    setup_logging()
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.mmd_bridge = PointerProperty(type=MMDBridgeSettings)
    bpy.types.WindowManager.ninesync_rigify_free = CollectionProperty(type=MMDBridgeFreeBone)
    bpy.types.WindowManager.ninesync_rigify_target = IntProperty()
    bpy.types.WindowManager.ninesync_rigify_controls = CollectionProperty(type=MMDBridgeControlName)
    bpy.types.WindowManager.ninesync_rigify_only_missing = BoolProperty(
        name="Only Missing", description="List only the bones the PMM moves that no Rigify control follows"
    )
    hooks.register_core(ADDON_VERSION)
    remove_handlers()
    bpy.app.handlers.load_post.append(ninesync_load_handler)
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
        if prefs is not None and prefs.use_import_scale:
            apply_import_scale(prefs.import_scale)
    except (AttributeError, RuntimeError):
        pass
    if scene is not None and settings_outdated(scene):
        schedule_relink()
    return None


def unregister():
    abandon_sync()
    clear_analysis()
    remove_handlers()
    hooks.unregister_core()
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
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


def reload_in_place():
    unregister()
    register()


if __name__ == "__main__":
    reload_in_place()
