bl_info = {
    "name": "NineSync",
    "author": "",
    "version": (0, 1, 0),
    "blender": (5, 3, 0),
    "location": "View3D > Sidebar > NineSync",
    "description": "\u2468-Sync: live-sync MikuMikuDance .pmm projects through mmd_tools and import their MME effects (.fx)",
    "category": "Import-Export",
}

import importlib as _importlib
import sys as _sys

for _name in ("common", "pmm", "xfile", "fx", "mme_nodes", "mme", "bridge", "ui"):
    _module = _sys.modules.get(__name__ + "." + _name)
    if _module is not None:
        _importlib.reload(_module)

import bpy
from bpy.props import PointerProperty

from .common import (
    MIN_BLENDER,
    MME_PARAMS_PROP,
    MME_REBUILD_DELAY,
    NINESYNC_NAME,
    _mme_runtimes,
    _mme_state,
    log,
    setup_logging,
)
from .mme import (
    mme_progress_tick,
    mme_rebuild_needed,
    mme_run_rebuilds,
    mme_update_scene,
)
from .bridge import (
    sweep_stale_temp,
)
from .ui import (
    CLASSES,
    MMDBridgeSettings,
    schedule_relink,
    settings_outdated,
)


@bpy.app.handlers.persistent
def mme_frame_handler(scene, depsgraph=None):
    if _mme_state.get("progress") is not None:
        mme_progress_tick(scene.frame_current)
    if _mme_state["busy"]:
        return
    _mme_state["busy"] = True
    try:
        updated = mme_update_scene(scene, depsgraph)
        if updated and depsgraph is not None and _mme_state.get("only") is not None:
            depsgraph.update()
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


@bpy.app.handlers.persistent
def mme_param_watch(scene, depsgraph=None):
    if _mme_state["busy"] or _mme_state.get("only") is not None or depsgraph is None:
        return
    screen = getattr(bpy.context, "screen", None)
    if screen is not None and screen.is_animation_playing:
        return
    if bpy.app.is_job_running("RENDER"):
        return
    for update in depsgraph.updates:
        obj = update.id.original if isinstance(update.id, bpy.types.Object) else None
        if obj is None or not obj.get(MME_PARAMS_PROP):
            continue
        if obj.name in _mme_state["rebuild"]:
            continue
        if mme_rebuild_needed(obj):
            _mme_state["rebuild"].add(obj.name)
            if bpy.app.timers.is_registered(mme_run_rebuilds):
                bpy.app.timers.unregister(mme_run_rebuilds)
            bpy.app.timers.register(mme_run_rebuilds, first_interval=MME_REBUILD_DELAY)


def remove_handlers():
    for handlers, name in (
        (bpy.app.handlers.frame_change_post, "mme_frame_handler"),
        (bpy.app.handlers.load_post, "mme_load_handler"),
        (bpy.app.handlers.depsgraph_update_post, "mme_param_watch"),
    ):
        for handler in [h for h in handlers if getattr(h, "__name__", "") == name]:
            handlers.remove(handler)


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
    remove_handlers()
    bpy.app.handlers.frame_change_post.append(mme_frame_handler)
    bpy.app.handlers.load_post.append(mme_load_handler)
    bpy.app.handlers.depsgraph_update_post.append(mme_param_watch)
    _mme_runtimes.clear()
    sweep_stale_temp()
    if not bpy.app.timers.is_registered(startup_check):
        bpy.app.timers.register(startup_check, first_interval=1.0)
    log.info("%s registered", NINESYNC_NAME)


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
