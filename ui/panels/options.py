from bpy.types import Panel

from ...common.constants import NINESYNC_CATEGORY


class MMDBRIDGE_PT_options(Panel):
    bl_label = "Options"
    bl_idname = "NINESYNC_PT_options"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = NINESYNC_CATEGORY
    bl_parent_id = "NINESYNC_PT_main"
    bl_order = 1
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        settings = context.scene.mmd_bridge
        layout = self.layout
        layout.prop(settings, "auto_scale")
        row = layout.row()
        row.enabled = not settings.auto_scale
        row.prop(settings, "scale")
        layout.prop(settings, "uv_morphs")
        layout.prop(settings, "sync_visibility")
        layout.prop(settings, "update_scene_settings")
        layout.separator()
        layout.prop(settings, "use_physics_vmd")
