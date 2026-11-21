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
        # One top-level row per sub-option: a column nested in a row packs its rows
        # tighter than the panel's own.
        children = ["follow_camera"] + (["follow_rotation"] if settings.follow_camera else [])
        for name in children:
            row = layout.row()
            row.enabled = settings.sync_camera
            row.separator(factor=2.0)
            row.prop(settings, name)
        layout.prop(settings, "sync_light")
