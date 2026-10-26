from .operators import (
    MMDBRIDGE_OT_clean,
    MMDBRIDGE_OT_copy_report,
    MMDBRIDGE_OT_dismiss_scale_warning,
    MMDBRIDGE_OT_scan,
    MMDBRIDGE_OT_sync_now,
    NINESYNC_OT_set_import_scale,
)
from .panels.audio import MMDBRIDGE_PT_audio
from .panels.camera import MMDBRIDGE_PT_camera
from .panels.legacy import NINESYNC_PT_legacy
from .panels.main import MMDBRIDGE_PT_main
from .panels.options import MMDBRIDGE_PT_options
from .panels.readout import MMDBRIDGE_PT_readout
from .preferences import NineSyncPreferences
from .properties import MMDBridgeSettings, MMDBridgeTarget


# Registration order matters: property groups before what uses them, parent
# panels before their children.
CLASSES = (
    MMDBridgeTarget,
    MMDBridgeSettings,
    MMDBRIDGE_OT_sync_now,
    MMDBRIDGE_OT_scan,
    MMDBRIDGE_OT_clean,
    MMDBRIDGE_OT_copy_report,
    NineSyncPreferences,
    NINESYNC_OT_set_import_scale,
    MMDBRIDGE_OT_dismiss_scale_warning,
    MMDBRIDGE_PT_main,
    MMDBRIDGE_PT_audio,
    MMDBRIDGE_PT_camera,
    MMDBRIDGE_PT_readout,
    MMDBRIDGE_PT_options,
)


LEGACY_CLASSES = (NINESYNC_PT_legacy,)
