import bpy
import importlib

from ..common.constants import ADDON_PACKAGE
from ..common.logger import log
from ..common.scene_utils import redraw_all


STALE_SETTINGS_MESSAGE = "Another copy of NineSync: PMM Bridge replaced this add-on's settings. They were re-linked; press the button again, and remove the other copy in Preferences > Add-ons"


_relink = {"pending": False}


# True when scene.mmd_bridge comes from another copy of the add-on (say a second
# install in a different folder) that registered first and lacks newer properties.
def settings_outdated(scene):
    settings = getattr(scene, "mmd_bridge", None)
    if settings is None or not hasattr(settings, "sync_audio"):
        return True
    row_type = settings.bl_rna.properties["targets"].fixed_type
    return "sync_record" not in row_type.bl_rna.properties


# Re-registers this add-on so its properties replace the stale ones.
def relink_settings():
    _relink["pending"] = False
    try:
        scene = bpy.context.scene
    except AttributeError:
        scene = None
    if scene is not None and not settings_outdated(scene):
        return None
    importlib.import_module(ADDON_PACKAGE).reload_in_place()
    log.warning(
        "settings were registered by another copy of the add-on; re-linked to this version. Remove the other copy in Preferences > Add-ons"
    )
    redraw_all()
    return None


# Deferred through a timer; re-registering is not safe while a panel is drawing.
def schedule_relink():
    if _relink["pending"]:
        return
    _relink["pending"] = True
    bpy.app.timers.register(relink_settings, first_interval=0.01)


# Operators call this first: on stale settings it schedules a relink and tells the
# user to press the button again.
def refuse_if_outdated(operator, context):
    if not settings_outdated(context.scene):
        return False
    schedule_relink()
    operator.report({"WARNING"}, STALE_SETTINGS_MESSAGE)
    return True
