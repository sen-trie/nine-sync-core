import importlib
import os
import shutil
import sys
import tempfile
import textwrap

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import REPO, SKIP, Checks, stage

import bpy

# Guards, preferences owner, republish after load, hook results and cancel, and the
# internal-frame flag. Nothing here needs mmd_tools or FX.
root = stage("ninesync")
shutil.copytree(REPO, os.path.join(root, "ninesync_copy"), ignore=SKIP)
bundle = os.path.join(root, "fakebundle")
os.makedirs(bundle)
shutil.copytree(REPO, os.path.join(bundle, "ninesync_core"), ignore=SKIP)
with open(os.path.join(bundle, "__init__.py"), "w") as handle:
    handle.write(
        textwrap.dedent(
            """
            import bpy
            import importlib
            NINESYNC_PREFERENCES_OWNER = True
            bl_info = {"name": "fake bundle", "version": (0, 0, 1), "blender": (2, 80, 0), "category": "Import-Export"}
            _done = []

            def register():
                prefs = importlib.import_module(".ninesync_core.ui.preferences", __package__)
                class UnionPreferences(prefs.CorePreferenceProperties, bpy.types.AddonPreferences):
                    bl_idname = __package__
                    other: bpy.props.IntProperty(default=3)
                bpy.utils.register_class(UnionPreferences)
                core = importlib.import_module(".ninesync_core", __package__)
                if core.MIN_BLENDER > tuple(bpy.app.version):
                    core.MIN_BLENDER = tuple(bpy.app.version)
                core.register()
                _done[:] = [UnionPreferences, core]

            def unregister():
                cls, core = _done
                core.unregister()
                bpy.utils.unregister_class(cls)
                _done.clear()
            """
        )
    )

checks = Checks()


def load_core(name):
    module = importlib.import_module(name)
    if bpy.app.version < module.MIN_BLENDER:
        module.MIN_BLENDER = tuple(bpy.app.version)
    return module


core = load_core("ninesync")
copy = load_core("ninesync_copy")
hooks = importlib.import_module("ninesync.hooks")
engine = importlib.import_module("ninesync.sync.engine")
frames = importlib.import_module("ninesync.sync.frames")
phase_mod = importlib.import_module("ninesync.sync.hook_phase")
prefs_mod = importlib.import_module("ninesync.ui.preferences")


def near(value, wanted):
    return value is not None and abs(value - wanted) < 1e-6


def refused(module):
    try:
        module.register()
    except RuntimeError as error:
        return "already enabled" in str(error)
    return False


# S1: a second copy is refused before it changes anything, in both orders.
core.register()
marker = dict(hooks.registry()["core"])
checks.check("marker names its package", marker.get("package") == "ninesync", marker)
checks.check("copy refused next to an active core", refused(copy))
copy.unregister()
checks.check(
    "refused copy's unregister leaves the active one alone",
    hasattr(bpy.types.Scene, "mmd_bridge") and hooks.core_installed(),
)
checks.check("marker untouched", hooks.registry()["core"] == marker)
core.reload_in_place()
checks.check("reload_in_place still works", hasattr(bpy.types.Scene, "mmd_bridge") and hooks.core_installed())
core.unregister()
checks.check("clean after unregister", not hasattr(bpy.types.Scene, "mmd_bridge") and not hooks.core_installed())
copy.register()
checks.check("reverse order: refused core", refused(core))
core.unregister()
checks.check(
    "copy still active",
    hasattr(bpy.types.Scene, "mmd_bridge") and hooks.registry()["core"]["package"] == "ninesync_copy",
)
copy.unregister()
checks.check("reverse: clean", not hasattr(bpy.types.Scene, "mmd_bridge") and not hooks.core_installed())
bpy.types.Scene.mmd_bridge = bpy.props.IntProperty()
checks.check("foreign Scene.mmd_bridge refused", refused(core))
del bpy.types.Scene.mmd_bridge


def boom(*_a, **_k):
    raise ValueError("injected")


real_pointer = core.PointerProperty
core.PointerProperty = boom
try:
    core.register()
    failed = False
except ValueError:
    failed = True
core.PointerProperty = real_pointer
checks.check("failed register raises", failed)
checks.check(
    "failed register left nothing",
    not hasattr(bpy.types.Scene, "mmd_bridge") and not hooks.core_installed() and not core._install["classes"],
)
core.register()
checks.check("register works after an aborted one", hooks.core_installed())
core.unregister()

# S4 / S8: standalone preferences, then through a bundle-owned union class.
import addon_utils

addon_utils.enable("ninesync", default_set=True)
prefs = prefs_mod.addon_preferences()
checks.check("standalone prefs resolve", prefs is not None and hasattr(prefs, "import_scale"))
prefs.use_import_scale = True
prefs.import_scale = 0.08
checks.check("preference change republishes", near(hooks.registry()["shared"].get("import_scale"), 0.08))
saved = os.path.join(tempfile.mkdtemp(), "t.blend")
bpy.ops.wm.save_as_mainfile(filepath=saved)
for number in range(2):
    bpy.ops.wm.open_mainfile(filepath=saved)
    checks.check(
        "import_scale survives File > Open (%d)" % (number + 1),
        near(hooks.registry()["shared"].get("import_scale"), 0.08),
        hooks.registry()["shared"],
    )
    checks.check("marker rebuilt after load (%d)" % number, hooks.core_installed())
prefs = prefs_mod.addon_preferences()
prefs.import_scale = 0.5
hooks.registry()["shared"].clear()
sys.modules["ninesync"].ninesync_load_handler()
checks.check("load handler publishes the live value", near(hooks.registry()["shared"].get("import_scale"), 0.5))
prefs.use_import_scale = False
checks.check("disabling removes the key", "import_scale" not in hooks.registry()["shared"])
addon_utils.disable("ninesync", default_set=True)
checks.check("standalone disabled cleanly", not hasattr(bpy.types.Scene, "mmd_bridge"))

addon_utils.enable("fakebundle", default_set=True)
nested = importlib.import_module("fakebundle.ninesync_core.ui.preferences")
found = nested.addon_preferences()
checks.check(
    "bundle: accessor finds the union class",
    found is not None and found.bl_rna.identifier == "UnionPreferences",
    found,
)
checks.check("bundle: core registered no preferences class of its own", not hasattr(bpy.types, "NineSyncPreferences"))
found.use_import_scale = True
found.import_scale = 0.25
checks.check("bundle: value published", near(hooks.registry()["shared"].get("import_scale"), 0.25))
bpy.app.driver_namespace.pop(hooks.KEY, None)
importlib.import_module("fakebundle.ninesync_core").ninesync_load_handler()
checks.check("bundle: republished after registry reset", near(hooks.registry()["shared"].get("import_scale"), 0.25))
checks.check(
    "bundle: marker package is the component",
    hooks.registry()["core"]["package"] == "fakebundle.ninesync_core",
)
addon_utils.disable("fakebundle", default_set=True)
checks.check("bundle disabled cleanly", not hasattr(bpy.types.Scene, "mmd_bridge") and not hooks.core_installed())

# S6: hook results.
SyncRun = engine.SyncRun
Cancelled = engine.SyncCancelled


def run_phase(fns):
    for owner in list(hooks.registry()["post_sync"]):
        hooks.remove(owner)
    for owner, fn in fns.items():
        hooks.add("post_sync", owner, fn)
    run = SyncRun()
    cancelled = False
    try:
        for _ in phase_mod.hook_steps(run, object(), Cancelled, 0.88, 0.94):
            pass
    except Cancelled:
        cancelled = True
    for owner in fns:
        hooks.remove(owner)
    return run, cancelled


def bad(_ctx):
    raise ValueError("effect exploded")


run, _ = run_phase({"legacy": lambda ctx: 3})
checks.check("legacy int result counts", run.imported == 3 and run.hook_failed == 0)
run, _ = run_phase({"d": lambda ctx: {"imported": 2, "failed": 1, "warnings": ["slow"]}})
checks.check("dict result", (run.imported, run.hook_failed, len(run.hook_warnings)) == (2, 1, 1), run.hook_warnings)
run, _ = run_phase({"x": bad, "ok": lambda ctx: 4})
checks.check("failing hook counted, the others still run", (run.imported, run.hook_failed) == (4, 1))
checks.check("failure is intelligible", any("effect exploded" in w for w in run.hook_warnings), run.hook_warnings)
run, _ = run_phase({"m": lambda ctx: {"imported": "many", "failed": -2, "warnings": 5}})
checks.check(
    "malformed fields are defaulted with warnings",
    (run.imported, run.hook_failed) == (0, 0) and len(run.hook_warnings) == 3,
    run.hook_warnings,
)
run, _ = run_phase({"n": lambda ctx: None, "s": lambda ctx: "weird"})
checks.check("None and odd results ignored", run.imported == 0 and run.hook_failed == 0)
run, _ = run_phase({"allfail": lambda ctx: {"imported": 0, "failed": 2}})
checks.check("fully failed phase shows in the status", "2 accessory/effect item(s) failed" in engine.status_line(run))

# A generator hook runs one step per tick, so Esc lands between items; close runs its cleanup.
log_ = []
box = {}


def long_hook(ctx):
    try:
        for index in range(5):
            box["run"].cancelled = index == 2
            log_.append(index)
            yield (index / 5.0, "item %d" % index)
        return {"imported": 5}
    finally:
        log_.append("closed")


hooks.add("post_sync", "long", long_hook)
run = SyncRun()
box["run"] = run
cancelled = False
try:
    for _ in phase_mod.hook_steps(run, object(), Cancelled, 0.88, 0.94):
        pass
except Cancelled:
    cancelled = True
hooks.remove("long")
checks.check("cancel lands between hook items", cancelled and log_ == [0, 1, 2, "closed"], log_)


def finishing(ctx):
    yield 0.5
    yield "half"
    return 2


run, cancelled = run_phase({"gen": finishing})
checks.check("generator hook return value is its result", run.imported == 2 and not cancelled)
ctx = engine.hook_context.__code__.co_varnames
checks.check("hook_context takes the run for progress and cancelled", "run" in ctx)

# P-1: the flag.
checks.check("flag off at rest", not frames.internal_frames_active())
try:
    with frames.internal_frames():
        with frames.internal_frames():
            inner = frames.internal_frames_active()
        outer = frames.internal_frames_active()
        raise KeyError("boom")
except KeyError:
    pass
checks.check("nested loops keep it set until the outermost exits", inner and outer)
checks.check("exception releases it", not frames.internal_frames_active())
with frames.internal_frames():
    bpy.app.driver_namespace.pop(hooks.KEY, None)
checks.check("registry replaced mid-loop does not leave it set", not frames.internal_frames_active())

seen = []


def watcher(*_args):
    seen.append(frames.internal_frames_active())


bpy.app.handlers.frame_change_post.append(watcher)
scene = bpy.context.scene
root_motion = importlib.import_module("ninesync.sync.root_motion")
accessory_export = importlib.import_module("ninesync.sync.accessory_export")
empty = bpy.data.objects.new("e", None)
scene.collection.objects.link(empty)
scene.frame_set(7)
checks.check("a normal frame_set is not flagged", seen == [False], seen)
seen.clear()
root_motion.combined_poses(scene, empty, [], [1, 2, 3])
checks.check("root motion frames are flagged", len(seen) == 4 and all(seen), seen)
seen.clear()
accessory_export.sample_objects(scene, {empty: {1, 2}})
checks.check("exporter frames are flagged, the restore too", len(seen) == 3 and all(seen), seen)
checks.check("flag clear afterwards", not frames.internal_frames_active())
bpy.app.handlers.frame_change_post.remove(watcher)
checks.finish()
