from bpy.types import Panel

from ...common.constants import NINESYNC_CATEGORY
from ..helpers import draw_wrapped


class MMDBRIDGE_PT_options(Panel):
    bl_label = "Options"
    bl_idname = "NINESYNC_PT_options"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = NINESYNC_CATEGORY
    bl_parent_id = "NINESYNC_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        settings = context.scene.mmd_bridge
        layout = self.layout
        layout.prop(settings, "auto_scale")
        row = layout.row()
        row.enabled = not settings.auto_scale
        row.prop(settings, "scale")
        layout.prop(settings, "use_clips")
        layout.prop(settings, "uv_morphs")
        if settings.uv_morphs:
            draw_wrapped(
                layout,
                "UV morphs are shader based: view them in Material Preview or Rendered, not Solid. The weights live on MMDUV_ shape keys inside the Shapekey Clip.",
            )
        layout.prop(settings, "update_scene_settings")
        layout.prop(settings, "interpolation_layout")
        layout.separator()
        layout.prop(settings, "use_physics_vmd")
        if settings.use_physics_vmd:
            layout.prop(settings, "physics_disable_rigid_body")
            draw_wrapped(
                layout,
                "Experimental. MMD's physics solver cannot run inside Blender. Bake the motion in MMD with MMDBridge, set the VMD on each model row, and it is layered above the PMM clip.",
            )
