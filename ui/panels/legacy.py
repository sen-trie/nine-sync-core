from bpy.types import Panel

from ...common.constants import NINESYNC_CATEGORY, NINESYNC_MAIN_LABEL
from ..helpers import draw_wrapped


class NINESYNC_PT_legacy(Panel):
    bl_label = NINESYNC_MAIN_LABEL
    bl_idname = "NINESYNC_PT_legacy"
    bl_order = 0
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = NINESYNC_CATEGORY

    def draw(self, context):
        box = self.layout.box()
        box.alert = True
        box.label(text="Old combined NineSync is enabled", icon="ERROR")
        draw_wrapped(box, "Disable the old combined NineSync in Preferences > Add-ons, then restart Blender.")
