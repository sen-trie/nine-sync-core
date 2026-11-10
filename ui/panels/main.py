from bpy.types import Panel

from ...common.constants import (
    LOG_TEXT_NAME,
    NINESYNC_CATEGORY,
    NINESYNC_MAIN_LABEL,
    PMM_FPS,
    UNIT_SCALE,
)
from ...common.scene_utils import fps_differs, scene_fps
from ...sync.engine import sync_running
from ...sync.mmd_tools_link import mmd_tools_available
from ...sync.progress import progress_state
from ...sync.scale import current_scale, scale_warning_active
from ...sync.targets import item_state, item_status_text
from ..helpers import (
    draw_dependency_notices,
    draw_fx_notice,
    draw_wrapped,
    status_icon,
)
from ..relink import STALE_SETTINGS_MESSAGE, schedule_relink, settings_outdated


class MMDBRIDGE_PT_main(Panel):
    bl_label = NINESYNC_MAIN_LABEL
    bl_idname = "NINESYNC_PT_main"
    bl_order = 0
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = NINESYNC_CATEGORY

    def draw(self, context):
        layout = self.layout
        if settings_outdated(context.scene):
            box = layout.box()
            box.alert = True
            draw_wrapped(box, STALE_SETTINGS_MESSAGE)
            schedule_relink()
            return
        settings = context.scene.mmd_bridge
        draw_dependency_notices(layout)
        if not mmd_tools_available():
            return
        layout.label(text="Source PMM:")
        layout.prop(settings, "filepath", text="")
        running = sync_running()
        row = layout.row(align=True)
        row.enabled = not running
        row.scale_y = 1.5
        row.operator("ninesync.sync_now", icon="FILE_REFRESH")
        row.operator("ninesync.preview", icon="VIEWZOOM")
        if running:
            state = progress_state()
            layout.progress(factor=state["fraction"], type="BAR", text=state["text"])
            layout.label(text="Esc cancels after the current step")
        else:
            draw_wrapped(layout, settings.status)
        if fps_differs(context.scene):
            fps_box = layout.box()
            fps_box.label(
                text="Scene is %g fps, PMM is %d fps"
                % (round(scene_fps(context.scene), 3), PMM_FPS),
                icon="TIME",
            )
            fps_box.prop(settings, "retime_fps")
        if scale_warning_active(settings):
            box = layout.box()
            header = box.row()
            header.alert = True
            header.label(
                text="Scale is %.3f, not %.1f" % (current_scale(settings), UNIT_SCALE),
                icon="ERROR",
            )
            header.operator(
                "ninesync.dismiss_scale_warning", text="", icon="X", emboss=False
            )
            draw_wrapped(
                box,
                "Camera keyframe interpolation may not match MMD exactly because of the different distance scale.",
            )
            fix = box.operator("ninesync.set_import_scale", text="Import at Scale 1.0 From Now On", icon="IMPORT")
            fix.scale = UNIT_SCALE
            fix.dismiss = True
        header, box = layout.panel("ninesync_models", default_closed=False)
        header.label(text="PMX Models (%d)" % len(settings.targets))
        header.operator("ninesync.scan", text="", icon="FILE_REFRESH", emboss=False)
        if box is not None and not len(settings.targets):
            box.label(text="Press the refresh icon to read the PMM")
        for item in (settings.targets if box is not None else ()):
            state = item_state(item)
            row = box.row(align=True)
            row.prop(item, "enabled", text="")
            row.label(text="", icon=status_icon(state))
            row.label(text=item.pmm_name)
            row.prop(item, "target", text="")
            note = item_status_text(item)
            if note:
                note_row = box.row()
                note_row.alert = state == "ERROR"
                note_row.scale_y = 0.85
                note_row.label(text=note)
            if item.target is not None:
                clip_row = box.row(align=True)
                clip_row.label(text="", icon="NLA")
                clip_row.prop(item, "clip", text="Motion")
                shape_row = box.row(align=True)
                shape_row.label(text="", icon="SHAPEKEY_DATA")
                shape_row.prop(item, "shape_clip", text="Shapekey")
                if settings.use_physics_vmd:
                    physics_row = box.row(align=True)
                    physics_row.label(text="", icon="PHYSICS")
                    physics_row.prop(item, "physics_vmd", text="")
        draw_fx_notice(layout, settings)
        tools = layout.row(align=True)
        tools.operator("ninesync.clean", icon="TRASH")
        tools.operator("ninesync.copy_report", icon="COPYDOWN")
        layout.label(text="Log: Text Editor > %s" % LOG_TEXT_NAME)
