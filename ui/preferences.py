import sys

import bpy
from bpy.props import BoolProperty, FloatProperty

from .. import hooks
from ..common.constants import ADDON_PACKAGE


IMPORT_SCALE_OPERATORS = ("mmd_tools.import_model", "mmd_tools.import_vmd")


# Name of the package whose preferences hold Core's settings. A component inside a
# combined extension has the extension's root package for its parent. When that package
# sets NINESYNC_PREFERENCES_OWNER it registers the one AddonPreferences class (the union
# of Core's and FX's properties) and Core registers none of its own.
def bundle_package():
    parent = ADDON_PACKAGE.rpartition(".")[0]
    module = sys.modules.get(parent) if parent else None
    if module is not None and getattr(module, "NINESYNC_PREFERENCES_OWNER", False):
        return parent
    return None


# Preferences that hold Core's properties, or None while there are none. The bundle's
# class is preferred, then this add-on's own. Every package prefix is tried, so no
# naming scheme is assumed, and a class without Core's properties is never accepted: the
# FX class that sits in the bundle's slot of an older build is skipped, not misread.
def addon_preferences():
    owner = bundle_package()
    candidates = [owner] if owner else []
    parts = ADDON_PACKAGE.split(".")
    candidates += [".".join(parts[:n]) for n in range(len(parts), 0, -1)]
    addons = bpy.context.preferences.addons
    for name in candidates:
        addon = addons.get(name)
        prefs = addon.preferences if addon is not None else None
        if prefs is not None and hasattr(prefs, "import_scale") and hasattr(prefs, "use_import_scale"):
            return prefs
    return None


# Publishes Core's shared keys to the hook registry. Core owns "import_scale" only; it is
# present while "Set Import Scale" is on and absent otherwise. The value is read from
# the preferences now, never remembered, so a file load or a changed preference cannot
# leave an old one. Without a preferences object the key is left as it is.
def republish_shared():
    prefs = addon_preferences()
    if prefs is None:
        return
    shared = hooks.registry()["shared"]
    if prefs.use_import_scale:
        shared["import_scale"] = prefs.import_scale
    else:
        shared.pop("import_scale", None)


def preference_changed(self, _context):
    republish_shared()


# Presets the last-used scale of mmd_tools' importers and publishes it to FX through
# the hook registry. Returns the importers that were updated.
def apply_import_scale(scale):
    applied = []
    hooks.registry()["shared"]["import_scale"] = scale
    wm = bpy.context.window_manager
    for idname in IMPORT_SCALE_OPERATORS:
        try:
            props = wm.operator_properties_last(idname)
        except (RuntimeError, KeyError, TypeError, AttributeError):
            props = None
        if props is not None and hasattr(props, "scale"):
            props.scale = scale
            applied.append(idname)
    return applied


# Core's preference properties, kept apart from the class so the combined extension's
# union class can inherit them and keep one definition.
class CorePreferenceProperties:
    use_import_scale: BoolProperty(
        name="Set Import Scale",
        description="Preset the scale of mmd_tools' model and motion importers (and NineSync: FX's accessory importer) every time Blender starts",
        default=False,
        update=preference_changed,
    )
    import_scale: FloatProperty(
        name="Import Scale",
        description="Scale preset for mmd_tools and NineSync imports",
        default=1.0,
        min=0.0001,
        soft_max=10.0,
        update=preference_changed,
    )

    def draw_core(self, layout):
        row = layout.row()
        row.prop(self, "use_import_scale")
        sub = row.row()
        sub.active = self.use_import_scale
        sub.prop(self, "import_scale")


class NineSyncPreferences(CorePreferenceProperties, bpy.types.AddonPreferences):
    bl_idname = ADDON_PACKAGE

    def draw(self, context):
        self.draw_core(self.layout)
