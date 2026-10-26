import bpy
import importlib
import inspect
import logging
import sys

from ..common.constants import MMD_TOOLS_URL
from ..common.logger import log


# The VMD import operator is the one hard dependency; its presence means mmd_tools
# is enabled.
def mmd_tools_available():
    return hasattr(bpy.types, "MMD_TOOLS_OT_import_vmd")


# (name, message, url) for each missing dependency, shown as notices in the panel.
def missing_dependencies():
    missing = []
    if not mmd_tools_available():
        missing.append(
            ("mmd_tools", "mmd_tools is not installed or enabled", MMD_TOOLS_URL)
        )
    return missing


# mmd_tools is installed under different package names (extension or legacy add-on),
# so its package is derived from its operator's module.
def mmd_tools_base():
    return bpy.types.MMD_TOOLS_OT_import_vmd.__module__.rsplit(".operators", 1)[0]


# Imports a submodule of whichever mmd_tools package is installed.
def mmd_module(name):
    return importlib.import_module(mmd_tools_base() + "." + name)


# The importer, model and utils modules the bridge drives directly.
def mmd_tools_modules():
    return (
        mmd_module("core.vmd.importer"),
        mmd_module("core.model"),
        mmd_module("utils"),
    )


# For the support report; 'unknown' when it cannot be read.
def mmd_tools_version():
    try:
        base = mmd_tools_base()
        module = sys.modules.get(base) or importlib.import_module(base)
        info = getattr(module, "bl_info", None)
        if info and "version" in info:
            return ".".join(str(v) for v in info["version"])
    except Exception:
        pass
    return "unknown"


# Captures mmd_tools' log output during an import so its summary and warnings are
# relayed through our own logger.
class CollectHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.summary = []
        self.warnings = []

    def emit(self, record):
        message = record.getMessage()
        if record.levelno >= logging.WARNING:
            self.warnings.append(message)
        elif message.startswith("----"):
            self.summary.append(message)


# Drives mmd_tools' VMDImporter directly instead of the operator, so one VMD can be
# assigned to several objects. Constructor arguments are filtered by signature to
# tolerate mmd_tools versions that differ. Root logging is swapped for a collector
# and the frame forced to 0 during the import, then both are restored.
def run_importer(tmp_path, scale, targets, action_name, detect_changes):
    importer_mod, _model_mod, utils_mod = mmd_tools_modules()
    wanted = dict(
        filepath=tmp_path,
        scale=scale,
        bone_mapper=utils_mod.makePmxBoneMap,
        use_pose_mode=False,
        frame_margin=0,
        use_mirror=False,
        use_nla=False,
        detect_camera_changes=detect_changes,
        detect_light_changes=detect_changes,
    )
    accepted = inspect.signature(importer_mod.VMDImporter.__init__).parameters
    scene = bpy.context.scene
    original_frame = scene.frame_current
    root_logger = logging.getLogger()
    previous_level = root_logger.level
    previous_handlers = list(root_logger.handlers)
    collector = CollectHandler()
    for handler in previous_handlers:
        root_logger.removeHandler(handler)
    root_logger.addHandler(collector)
    root_logger.setLevel(logging.INFO)
    try:
        scene.frame_current = 0
        importer = importer_mod.VMDImporter(
            **{k: v for k, v in wanted.items() if k in accepted}
        )
        assign_params = inspect.signature(importer.assign).parameters
        for obj in targets:
            log.debug("assign -> %s (%s)", obj.name, obj.type)
            if "action_name" in assign_params:
                importer.assign(obj, action_name)
            else:
                importer.assign(obj)
    finally:
        root_logger.removeHandler(collector)
        for handler in previous_handlers:
            root_logger.addHandler(handler)
        root_logger.setLevel(previous_level)
        scene.frame_current = original_frame
    for line in collector.summary:
        log.info("mmd_tools: %s", line)
    if collector.warnings:
        log.warning(
            "mmd_tools reported %d warning(s); first 10: %s",
            len(collector.warnings),
            collector.warnings[:10],
        )
    return collector
