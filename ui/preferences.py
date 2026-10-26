import bpy
from bpy.props import BoolProperty, FloatProperty

from .. import hooks
from ..common.constants import ADDON_PACKAGE


IMPORT_SCALE_OPERATORS = ("mmd_tools.import_model", "mmd_tools.import_vmd")


# Preferences of this add-on, or None while it is not registered.
def addon_preferences():
    addon = bpy.context.preferences.addons.get(ADDON_PACKAGE)
    return addon.preferences if addon is not None else None


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


class NineSyncPreferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_PACKAGE

    use_import_scale: BoolProperty(
        name="Set Import Scale",
        description="Preset the scale of mmd_tools' model and motion importers (and NineSync: FX's accessory importer) every time Blender starts",
        default=False,
    )
    import_scale: FloatProperty(
        name="Import Scale",
        description="Scale preset for mmd_tools and NineSync imports",
        default=1.0,
        min=0.0001,
        soft_max=10.0,
    )

    def draw(self, context):
        row = self.layout.row()
        row.prop(self, "use_import_scale")
        sub = row.row()
        sub.active = self.use_import_scale
        sub.prop(self, "import_scale")
