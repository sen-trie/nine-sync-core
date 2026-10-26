import bpy
import importlib

from .constants import ADDON_PACKAGE


# The old combined add-on registers the same scene.mmd_bridge pointer with an
# extended type (it has mme_backend). Detecting that lets us register only a warning
# panel instead of clashing with it.
def legacy_monolith_active():
    prop = bpy.types.Scene.bl_rna.properties.get("mmd_bridge")
    fixed = getattr(prop, "fixed_type", None) if prop is not None else None
    return fixed is not None and "mme_backend" in fixed.bl_rna.properties


# Reads the version from the root package's bl_info, so only __init__.py holds it.
def addon_version():
    try:
        return tuple(importlib.import_module(ADDON_PACKAGE).bl_info["version"])
    except Exception:
        return (0, 0, 0)
