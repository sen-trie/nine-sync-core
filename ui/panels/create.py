import os

from bpy.types import Panel

from ...common.constants import NINESYNC_CATEGORY
from ...sync.mmd_tools_link import mmd_tools_available
from ...sync.project_export import template_path
from ..helpers import draw_wrapped
from ..relink import settings_outdated


class NINESYNC_PT_create(Panel):
    bl_label = "Create MMD Project"
    bl_idname = "NINESYNC_PT_create"
    bl_order = -1
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = NINESYNC_CATEGORY
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        if settings_outdated(context.scene) or not mmd_tools_available():
            layout.label(text="Needs mmd_tools")
            return
        settings = context.scene.mmd_bridge
        if not os.path.isfile(template_path()):
            box = layout.box()
            box.alert = True
            draw_wrapped(box, "assets/kagamine.pmm is missing, so Create PMM cannot run.")
        layout.operator("ninesync.spawn_camera", icon="CAMERA_DATA")
        layout.prop(settings, "export_camera")
        header, box = layout.panel("ninesync_export_models", default_closed=False)
        header.label(text="MMD Armatures (%d)" % len(settings.export_items))
        header.operator("ninesync.refresh_export", text="", icon="FILE_REFRESH", emboss=False)
        if box is not None:
            if not len(settings.export_items):
                box.label(text="Press the refresh icon to find models")
            for item in settings.export_items:
                row = box.row(align=True)
                row.prop(item, "enabled", text="")
                row.label(text=item.target.name if item.target else "(missing)")
                path_row = box.row(align=True)
                path_row.alert = item.enabled and not item.pmx_path
                path_row.label(text="", icon="FILE")
                path_row.prop(item, "pmx_path", text="")
        header, box = layout.panel("ninesync_export_animation", default_closed=False)
        header.prop(settings, "export_animation", text="Export Animation")
        if box is not None:
            box.enabled = settings.export_animation
            row = box.row(align=True)
            row.prop(settings, "export_bones", toggle=True)
            row.prop(settings, "export_morphs", toggle=True)
            camera = row.row(align=True)
            camera.enabled = settings.export_camera
            camera.prop(settings, "export_camera_motion", toggle=True)
        row = layout.row()
        row.scale_y = 1.5
        row.operator("ninesync.create_pmm", icon="EXPORT")
        layout.label(text="Becomes the PMM of the Syncer below", icon="LINKED")
