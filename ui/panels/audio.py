from bpy.types import Panel

from ...common.constants import NINESYNC_CATEGORY
from ..helpers import draw_wrapped


class MMDBRIDGE_PT_audio(Panel):
    bl_label = "Audio"
    bl_idname = "NINESYNC_PT_audio"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = NINESYNC_CATEGORY
    bl_parent_id = "NINESYNC_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        settings = context.scene.mmd_bridge
        layout.prop(settings, "sync_audio")
        layout.prop(settings, "audio_path")
        draw_wrapped(
            layout,
            settings.audio_note or "Press the refresh icon in Models to read the PMM",
        )
        draw_wrapped(
            layout,
            "Audio starts at frame 0, the same as MMD. The PMM stores no audio offset.",
        )
