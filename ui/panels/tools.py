from bpy.types import Panel
from bpy.app.translations import pgettext_iface as iface_

from ...common.constants import LOG_TEXT_NAME, NINESYNC_CATEGORY
from ...sync.mmd_tools_link import mmd_tools_available
from ..relink import settings_outdated


# Clean, the support report and the log pointer, last under the main panel (below the
# PMM Motion Inspector) so they sit out of the way of the everyday controls.
class NINESYNC_PT_tools(Panel):
    bl_label = "Tools"
    bl_idname = "NINESYNC_PT_tools"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = NINESYNC_CATEGORY
    bl_parent_id = "NINESYNC_PT_main"
    bl_order = 5
    bl_options = {"HIDE_HEADER"}

    @classmethod
    def poll(cls, context):
        return not settings_outdated(context.scene) and mmd_tools_available()

    def draw(self, context):
        layout = self.layout
        tools = layout.row(align=True)
        tools.operator("ninesync.clean", icon="TRASH")
        tools.operator("ninesync.copy_report", icon="COPYDOWN")
        layout.label(text=iface_("Log: Text Editor > %s") % LOG_TEXT_NAME)
