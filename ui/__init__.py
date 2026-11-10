from .operators import (
    MMDBRIDGE_OT_clean,
    MMDBRIDGE_OT_copy_report,
    MMDBRIDGE_OT_dismiss_scale_warning,
    MMDBRIDGE_OT_scan,
    MMDBRIDGE_OT_sync_now,
    NINESYNC_OT_add_export_props,
    NINESYNC_OT_convert_root_motion,
    NINESYNC_OT_create_pmm,
    NINESYNC_OT_refresh_export,
    NINESYNC_OT_refresh_prop_colors,
    NINESYNC_OT_remove_export_prop,
    NINESYNC_OT_preview,
    NINESYNC_OT_set_import_scale,
    NINESYNC_OT_spawn_camera,
)
from .panels.audio import MMDBRIDGE_PT_audio
from .panels.camera import MMDBRIDGE_PT_camera
from .panels.create import NINESYNC_PT_create
from .panels.main import MMDBRIDGE_PT_main
from .panels.options import MMDBRIDGE_PT_options
from .panels.preview import NINESYNC_PT_inspector
from .panels.remap import (
    NINESYNC_OT_add_rigify_map,
    NINESYNC_OT_remove_rigify_map,
    NINESYNC_OT_close_popup,
    NINESYNC_OT_edit_rigify_map,
    NINESYNC_OT_reset_rigify_map,
    NINESYNC_OT_show_rigify_map,
    NINESYNC_PT_remap,
    NINESYNC_PT_show_map,
    NINESYNC_UL_rigify_free,
    NINESYNC_UL_rigify_map,
)
from .panels.readout import MMDBRIDGE_PT_readout
from .preferences import NineSyncPreferences
from .properties import (
    MMDBridgeControlName,
    MMDBridgeFreeBone,
    MMDBridgeRemapSlot,
    MMDBridgeExportItem,
    MMDBridgeExportProp,
    MMDBridgePropColor,
    MMDBridgeSettings,
    MMDBridgeTarget,
)


# Registration order matters: property groups before what uses them, parent
# panels before their children.
CLASSES = (
    MMDBridgeControlName,
    MMDBridgeFreeBone,
    MMDBridgeRemapSlot,
    NINESYNC_UL_rigify_map,
    NINESYNC_UL_rigify_free,
    MMDBridgeTarget,
    MMDBridgeExportItem,
    MMDBridgePropColor,
    MMDBridgeExportProp,
    MMDBridgeSettings,
    MMDBRIDGE_OT_sync_now,
    NINESYNC_OT_preview,
    MMDBRIDGE_OT_scan,
    MMDBRIDGE_OT_clean,
    MMDBRIDGE_OT_copy_report,
    NINESYNC_OT_spawn_camera,
    NINESYNC_OT_refresh_export,
    NINESYNC_OT_add_export_props,
    NINESYNC_OT_remove_export_prop,
    NINESYNC_OT_create_pmm,
    NINESYNC_OT_convert_root_motion,
    NINESYNC_OT_refresh_prop_colors,
    NineSyncPreferences,
    NINESYNC_OT_set_import_scale,
    MMDBRIDGE_OT_dismiss_scale_warning,
    NINESYNC_OT_show_rigify_map,
    NINESYNC_OT_close_popup,
    NINESYNC_OT_remove_rigify_map,
    NINESYNC_OT_add_rigify_map,
    NINESYNC_PT_show_map,
    NINESYNC_OT_edit_rigify_map,
    NINESYNC_OT_reset_rigify_map,
    NINESYNC_PT_create,
    MMDBRIDGE_PT_main,
    NINESYNC_PT_remap,
    MMDBRIDGE_PT_options,
    MMDBRIDGE_PT_camera,
    MMDBRIDGE_PT_readout,
    MMDBRIDGE_PT_audio,
    NINESYNC_PT_inspector,
)
