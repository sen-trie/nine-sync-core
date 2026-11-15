from bpy.types import Panel

from ...common.constants import NINESYNC_CATEGORY


class MMDBRIDGE_PT_camera(Panel):
    bl_label = "Camera & Light"
    bl_idname = "NINESYNC_PT_camera"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = NINESYNC_CATEGORY
    bl_parent_id = "NINESYNC_PT_main"
    bl_order = 2

    def draw(self, context):
        layout = self.layout
        settings = context.scene.mmd_bridge
        layout.prop(settings, "sync_camera")
        row = layout.row()
        row.separator(factor=2.0)
        column = row.column()
        column.enabled = settings.sync_camera
        column.prop(settings, "follow_camera")
        if settings.follow_camera:
            column.prop(settings, "follow_rotation")
        layout.prop(settings, "sync_light")
