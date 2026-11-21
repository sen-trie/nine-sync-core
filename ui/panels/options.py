from bpy.types import Panel

from bpy.app.translations import pgettext_iface as iface_

from ...common.constants import NINESYNC_CATEGORY
from ...common.scene_utils import fps_differs, scene_fps


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
        scene = context.scene
        retime = layout.row()
        retime.enabled = fps_differs(scene)
        if fps_differs(scene):
            text = iface_("Retime to Scene FPS (Scene is %g fps)") % round(scene_fps(scene), 3)
        else:
            text = iface_("Retime to Scene FPS")
        retime.prop(settings, "retime_fps", text=text)
        layout.prop(settings, "auto_scale")
        row = layout.row()
        row.enabled = not settings.auto_scale
        row.prop(settings, "scale")
        layout.prop(settings, "uv_morphs")
        layout.prop(settings, "sync_visibility")
        layout.prop(settings, "sync_model_constraints")
        layout.prop(settings, "use_physics_vmd")
