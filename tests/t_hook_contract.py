import importlib
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import REPO, Checks, load, stage

import bpy


CTX_FIELDS = {
    "scene",
    "project",
    "pmm_path",
    "targets",
    "unit",
    "retimed",
    "integrated",
    "core_version",
}
stage("ninesync")
core = load("ninesync")
core.register()
hooks = importlib.import_module("ninesync.hooks")
engine = importlib.import_module("ninesync.sync.engine")
pmm = importlib.import_module("ninesync.pmm")
model_key = importlib.import_module("ninesync.common.scene_utils").model_key
checks = Checks()

checks.check("API_VERSION is 1", hooks.API_VERSION == 1)
checks.check("hook kinds", hooks.KINDS == ("post_sync", "clean", "report"))
reg = hooks.registry()
checks.check("registry stored in driver_namespace", bpy.app.driver_namespace[hooks.KEY] is reg)
checks.check("core publishes its version", reg["core"]["version"] == tuple(core.bl_info["version"]))
checks.check("FX absent", not hooks.fx_installed())


def boom(*_args):
    raise ValueError("expected test failure")


hooks.add("post_sync", "t_bad", boom)
hooks.add("post_sync", "t_good", lambda ctx: 7)
checks.check("a failing hook does not stop the others", hooks.run("post_sync", None) == [7])
hooks.remove("t_bad")
hooks.remove("t_good")
checks.check("remove clears the owner", not reg["post_sync"])
hooks.add("report", "t_old", lambda context: ["x"])
reg["api_version"] = hooks.API_VERSION + 1
checks.check("version mismatch skips hooks", not hooks.compatible() and hooks.run("report", None) == [])
reg["api_version"] = hooks.API_VERSION
hooks.remove("t_old")

# The call order and ctx timing are the contract, so everything around them is stubbed
# and a project with one model and one accessory runs through sync() at 60 fps.
order = []
seen = {}
project = pmm.PmmProject()
model = pmm.PmmModel()
model.name = "m"
model.bones = [[pmm.BoneKey(30, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), None)]]
model.morphs = [[pmm.MorphKey(30, 1.0)]]
model.last_frame = 30
project.models = [model]
accessory = pmm.PmmAccessory()
accessory.name = "a.x"
accessory.keys = [pmm.AccessoryKey(30, 0, -1, 0, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 1.0, 0, 1.0)]
project.accessories = [accessory]
engine.parse_pmm = lambda data: project
engine.import_scene_elements = lambda *a: order.append("scene_elements") or 0
engine.import_audio = lambda *a: order.append("audio") or False
engine.describe_audio = lambda *a: ""
engine.sync_target = lambda *a: order.append("model") or True
engine.refresh_targets = lambda *a: None
plan_mod = importlib.import_module("ninesync.sync.plan")
entry = plan_mod.PlanItem(model_key(model), model.name)
entry.parts["motion"] = plan_mod.PartPlan("motion")
entry.parts["motion"].operation = plan_mod.UPDATE
plan = plan_mod.SyncPlan()
plan.items = [entry]
engine.build_plan = lambda *a, **k: plan


def post_sync(ctx):
    order.append("post_sync")
    seen["ctx"] = ctx
    seen["accessory_frame"] = ctx.project.accessories[0].keys[0].frame
    seen["bone_frame"] = ctx.project.models[0].bones[0][0].frame
    return 2


hooks.add("post_sync", "t_probe", post_sync)
scene = bpy.context.scene
scene.render.fps = 60
scene.render.fps_base = 1.0
settings = scene.mmd_bridge
settings.filepath = os.path.join(REPO, "assets", "kagamine.pmm")
settings.retime_fps = True
target = settings.targets.add()
target.key = model_key(model)
target.pmm_name = model.name
target.enabled = True
target.target = bpy.data.objects.new("root", None)
imported, total = engine.sync(scene)
ctx = seen["ctx"]
checks.check(
    "order: models, camera/light, post_sync, audio",
    order == ["model", "scene_elements", "post_sync", "audio"],
    order,
)
checks.check("post_sync result counted", imported == 3, imported)
checks.check("ctx is a SimpleNamespace", isinstance(ctx, types.SimpleNamespace))
checks.check("ctx fields", CTX_FIELDS <= set(vars(ctx)), sorted(vars(ctx)))
checks.check("ctx.targets maps key to root", ctx.targets == {target.key: target.target})
checks.check("ctx flags", ctx.retimed is True and ctx.integrated is True)
checks.check("ctx.core_version", tuple(ctx.core_version) == tuple(core.bl_info["version"]))
checks.check("hooks see retimed accessory keys", seen["accessory_frame"] == 60, seen["accessory_frame"])
checks.check("hooks see retimed bone keys", seen["bone_frame"] == 60, seen["bone_frame"])
checks.check("accessory count stored for the notice", settings.pmm_accessory_count == 1)
hooks.remove("t_probe")
core.unregister()
checks.finish()
