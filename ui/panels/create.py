import os

from bpy.types import Panel

from ...common.constants import NINESYNC_CATEGORY
from ...sync.mmd_tools_link import mmd_tools_available
from ...sync.project_export import template_path
from ...sync.root_motion import has_root_motion
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
        split = layout.split(factor=0.55)
        split.enabled = settings.export_animation and settings.export_camera_motion
        split.label(text="MMD Camera")
        split.prop(settings, "export_camera_object", text="")
        header, box = layout.panel("ninesync_export_models", default_closed=False)
        header.label(text="MMD Armatures (%d)" % len(settings.export_items))
        header.operator("ninesync.refresh_export", text="", icon="FILE_REFRESH", emboss=False)
        if box is not None:
            if not len(settings.export_items):
                box.label(text="Press the refresh icon to find models")
            for index, item in enumerate(settings.export_items):
                row = box.row(align=True)
                row.prop(item, "enabled", text="")
                row.label(text=item.target.name if item.target else "(missing)")
                button = row.row(align=True)
                button.enabled = item.target is not None and has_root_motion(item.target)
                convert = button.operator("ninesync.convert_root_motion", text="Bake Root Motion")
                convert.index = index
                path_row = box.row(align=True)
                path_row.alert = item.enabled and not item.pmx_path
                path_row.label(text="", icon="FILE")
                path_row.prop(item, "pmx_path", text="")
        header, box = layout.panel("ninesync_export_props", default_closed=False)
        header.label(text="Props (.x) (%d)" % len(settings.export_props))
        if box is not None:
            box.operator("ninesync.add_export_props", icon="ADD")
            for index, item in enumerate(settings.export_props):
                row = box.row(align=True)
                row.prop(item, "enabled", text="")
                row.label(text=item.target.name if item.target else "(missing)")
                row.prop(item, "show_colors", text="", icon="COLOR", toggle=True)
                remove = row.operator("ninesync.remove_export_prop", text="", icon="X", emboss=False)
                remove.index = index
                if item.show_colors:
                    colors = box.column(align=True)
                    refresh = colors.operator("ninesync.refresh_prop_colors", icon="FILE_REFRESH")
                    refresh.index = index
                    for entry in item.colors:
                        split = colors.split(factor=0.7)
                        split.label(text=entry.name)
                        split.prop(entry, "color", text="")
            if len(settings.export_props):
                box.label(text="Written to an accessories folder beside the PMM", icon="INFO")
        layout.prop(settings, "export_animation", text="Export Animation")
        row = layout.row(align=True)
        row.enabled = settings.export_animation
        row.prop(settings, "export_bones", toggle=True)
        row.prop(settings, "export_morphs", toggle=True)
        row.prop(settings, "export_visibility", toggle=True)
        row.prop(settings, "export_camera_motion", toggle=True)
        row = layout.row()
        row.scale_y = 1.5
        row.operator("ninesync.create_pmm", icon="EXPORT")
        layout.prop(settings, "export_mmd_labels")
        layout.prop(settings, "export_link_syncer")
