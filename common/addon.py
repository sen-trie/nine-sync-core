import importlib

from .constants import ADDON_PACKAGE


# Reads the version from the root package's bl_info, so only __init__.py holds it.
def addon_version():
    try:
        return tuple(importlib.import_module(ADDON_PACKAGE).bl_info["version"])
    except Exception:
        return (0, 0, 0)
