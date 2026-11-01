import bpy
import importlib
import inspect
import logging
import sys

from ..common.constants import MMD_TOOLS_URL
from ..common.logger import log


# Oldest mmd_tools this bridge is tested against. mmd_tools is a separate extension that the user installs
# and updates; this add-on never installs, replaces or unregisters it. There is no upper bound: the API check
# below decides, so a newer release that keeps these names keeps working.
MMD_TOOLS_MIN_VERSION = (4, 5, 5)

# (submodule, names) this add-on calls directly. A release that drops one is reported as incompatible
# instead of failing in the middle of a sync.
REQUIRED_API = (
    ("core.vmd.importer", ("VMDImporter",)),
    ("core.vmd.exporter", ()),
    ("core.model", ("Model",)),
    ("core.camera", ("MMDCamera",)),
    ("core.light", ("MMDLight",)),
    ("core.pmx", ("load",)),
    ("core.pmd.importer", ("import_pmd_to_pmx",)),
    ("translations", ("translateFromJp",)),
    ("auto_scene_setup", ()),
    ("utils", ("makePmxBoneMap",)),
)

_status_cache = {}


# The VMD import operator is the one hard dependency; its presence means mmd_tools
# is enabled.
def mmd_tools_installed():
    return hasattr(bpy.types, "MMD_TOOLS_OT_import_vmd")


# mmd_tools is installed under different package names (extension or legacy add-on),
# so its package is derived from its operator's module.
def mmd_tools_base():
    return bpy.types.MMD_TOOLS_OT_import_vmd.__module__.rsplit(".operators", 1)[0]


# Imports a submodule of whichever mmd_tools package is installed.
def mmd_module(name):
    return importlib.import_module(mmd_tools_base() + "." + name)


# The version as a tuple, from the attribute mmd_tools' own __init__ sets (extension), else bl_info (legacy
# add-on); None when it cannot be read.
def mmd_tools_version_tuple():
    try:
        base = mmd_tools_base()
        module = sys.modules.get(base) or importlib.import_module(base)
        raw = getattr(module, "MMD_TOOLS_VERSION", None)
        if raw is None:
            info = getattr(module, "bl_info", None)
            raw = info.get("version") if info else None
        if raw is None:
            return None
        if isinstance(raw, str):
            raw = raw.split(".")
        return tuple(int("".join(c for c in str(part) if c.isdigit()) or 0) for part in raw)
    except Exception:
        return None


# Returns (state, detail). State is "ok", "missing" (not installed or not enabled) or "incompatible"
# (too old, or an API this add-on calls is gone). Read-only: nothing of mmd_tools is touched. The result is
# cached per registered operator class, so a re-enable or update re-checks.
def mmd_tools_status():
    operator = getattr(bpy.types, "MMD_TOOLS_OT_import_vmd", None)
    if operator is None:
        _status_cache.clear()
        return "missing", "mmd_tools is not installed or enabled"
    key = (operator.__module__, id(operator))
    if key in _status_cache:
        return _status_cache[key]
    result = ("ok", "")
    version = mmd_tools_version_tuple()
    wanted = ".".join(str(v) for v in MMD_TOOLS_MIN_VERSION)
    if version is not None and version < MMD_TOOLS_MIN_VERSION:
        result = ("incompatible", "mmd_tools %s is too old; update it to %s or newer" % (".".join(map(str, version)), wanted))
    else:
        for name, attributes in REQUIRED_API:
            try:
                module = mmd_module(name)
            except Exception as error:
                result = ("incompatible", "mmd_tools has no usable module '%s' (%s); this version is not supported, use %s or newer" % (name, error, wanted))
                break
            absent = [a for a in attributes if not hasattr(module, a)]
            if absent:
                result = ("incompatible", "mmd_tools '%s' lacks %s; this version is not supported, use %s or newer" % (name, ", ".join(absent), wanted))
                break
    _status_cache.clear()
    _status_cache[key] = result
    return result


# True only when mmd_tools is enabled and passes the version and API checks.
def mmd_tools_available():
    return mmd_tools_status()[0] == "ok"


# (name, message, url) for each missing or unusable dependency, shown as notices in the panel.
def missing_dependencies():
    state, detail = mmd_tools_status()
    if state == "ok":
        return []
    return [("mmd_tools", detail, MMD_TOOLS_URL)]


# The importer, model and utils modules the bridge drives directly.
def mmd_tools_modules():
    return (
        mmd_module("core.vmd.importer"),
        mmd_module("core.model"),
        mmd_module("utils"),
    )


# For the support report; 'unknown' when it cannot be read.
def mmd_tools_version():
    version = mmd_tools_version_tuple() if mmd_tools_installed() else None
    return ".".join(str(v) for v in version) if version else "unknown"


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
