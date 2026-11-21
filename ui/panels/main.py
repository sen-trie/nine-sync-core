from bpy.types import Panel
from bpy.app.translations import pgettext_iface as iface_

from ...common.constants import (
    NINESYNC_CATEGORY,
    NINESYNC_MAIN_LABEL,
    UNIT_SCALE,
)
from ...sync.engine import sync_running
from ...sync.mmd_tools_link import mmd_tools_available
from ...sync.progress import progress_state
from ...sync.scale import current_scale, scale_warning_active
from ...sync.setup import pmm_linked
from ...sync.targets import item_state, item_status_text
from ..helpers import (
    draw_dependency_notices,
    draw_field,
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
            draw_wrapped(box, iface_(STALE_SETTINGS_MESSAGE))
            schedule_relink()
            return
        settings = context.scene.mmd_bridge
        draw_dependency_notices(layout)
        if not mmd_tools_available():
            return
        layout.label(text="Source PMM:")
        layout.prop(settings, "filepath", text="")
        running = sync_running()
        if not pmm_linked(settings):
            setup = layout.row()
            setup.enabled = not running
            setup.scale_y = 1.5
            setup.operator("ninesync.one_click_setup", icon="IMPORT")
        row = layout.row(align=True)
        row.enabled = not running
        row.scale_y = 1.5
        row.operator("ninesync.sync_now", icon="FILE_REFRESH")
        row.operator("ninesync.preview", icon="VIEWZOOM")
        if running:
            state = progress_state()
            layout.progress(factor=state["fraction"], type="BAR", text=state["text"])
        else:
            draw_wrapped(layout, settings.status)
        if scale_warning_active(settings):
            box = layout.box()
            header = box.row()
            header.alert = True
            header.label(
                text=iface_("Scale is %.3f, not %.1f") % (current_scale(settings), UNIT_SCALE),
                icon="ERROR",
            )
            header.operator(
                "ninesync.dismiss_scale_warning", text="", icon="X", emboss=False
            )
            draw_wrapped(
                box,
                iface_("Camera keyframe interpolation may not match MMD exactly because of the different distance scale."),
            )
            fix = box.operator("ninesync.set_import_scale", text="Import at Scale 1.0 From Now On", icon="IMPORT")
            fix.scale = UNIT_SCALE
            fix.dismiss = True
        header, box = layout.panel("ninesync_models", default_closed=False)
        header.label(text=iface_("PMX Models (%d)") % len(settings.targets))
        header.operator("ninesync.scan", text="", icon="FILE_REFRESH", emboss=False)
        if box is not None and not len(settings.targets):
            box.label(text="Press the refresh icon to read the PMM")
        crowded = len(settings.targets) > 3
        for item in (settings.targets if box is not None else ()):
            state = item_state(item)
            # One collapsible panel per PMM model, titled with the MMD model's name; the
            # tick and the status stay visible in the header while it is closed.
            header, body = box.panel("ninesync_model_" + item.key, default_closed=crowded)
            header.prop(item, "enabled", text="")
            header.label(text=item.pmm_name, icon=status_icon(state))
            if body is None:
                continue
            draw_field(body, item, "target", "Model", "ARMATURE_DATA")
            note = item_status_text(item)
            if note:
                note_row = body.row()
                note_row.alert = state == "ERROR"
                note_row.label(text=note)
            if item.target is not None:
                draw_field(body, item, "clip", "Motion", "NLA")
                draw_field(body, item, "shape_clip", "Shapekey", "SHAPEKEY_DATA")
                draw_field(body, item, "solve_leg_ik", "MMD Leg IK", "CON_KINEMATIC")
                if settings.use_physics_vmd:
                    draw_field(body, item, "physics_vmd", "Physics", "PHYSICS")
        draw_fx_notice(layout, settings)
