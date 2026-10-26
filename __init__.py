ADDON_VERSION = (0, 2, 0)

bl_info = {
    "name": "NineSync: PMM Bridge",
    "author": "",
    "version": ADDON_VERSION,
    "blender": (5, 3, 0),
    "location": "View3D > Sidebar > NineSync",
    "description": "⑨-Sync: live-sync MikuMikuDance .pmm projects (models, camera, light, audio) through mmd_tools",
    "category": "Import-Export",
}

import importlib as _importlib
import sys as _sys

for _name in ("common", "hooks", "pmm", "bridge", "ui"):
    _module = _sys.modules.get(__name__ + "." + _name)
    if _module is not None:
        _importlib.reload(_module)

import bpy
from bpy.props import PointerProperty

from . import hooks
from .common import (
    MIN_BLENDER,
    NINESYNC_NAME,
    legacy_monolith_active,
    log,
    setup_logging,
)
from .bridge import sweep_stale_temp
from .ui import (
    CLASSES,
    LEGACY_CLASSES,
    MMDBridgeSettings,
    addon_preferences,
    apply_import_scale,
    schedule_relink,
    settings_outdated,
)


_state = {"legacy": False}


@bpy.app.handlers.persistent
def ninesync_load_handler(*_args):
    if _state["legacy"]:
        return
    hooks.register_core(ADDON_VERSION)
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
    if legacy_monolith_active():
        _state["legacy"] = True
        for cls in LEGACY_CLASSES:
            bpy.utils.register_class(cls)
        log.error(
            "the old combined NineSync is enabled; disable it before using NineSync: PMM Bridge"
        )
        return
    _state["legacy"] = False
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.mmd_bridge = PointerProperty(type=MMDBridgeSettings)
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
    if _state["legacy"]:
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
    remove_handlers()
    hooks.unregister_core()
    if _state["legacy"]:
        for cls in reversed(LEGACY_CLASSES):
            try:
                bpy.utils.unregister_class(cls)
            except (RuntimeError, ValueError):
                pass
        _state["legacy"] = False
        return
    if hasattr(bpy.types.Scene, "mmd_bridge"):
        del bpy.types.Scene.mmd_bridge
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


def reload_in_place():
    unregister()
    register()


if __name__ == "__main__":
    reload_in_place()
