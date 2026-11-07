import importlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import REPO, Checks, load, stage

import bpy


# Change detection and planning without mmd_tools, so CI covers them. Rigs here are plain
# armatures; tests/manual/validate_sync_plan.py runs the same rules against real imports.
stage("ninesync")
core = load("ninesync")
core.register()
pmm = importlib.import_module("ninesync.pmm")
motion = importlib.import_module("ninesync.sync.motion")
plan_mod = importlib.import_module("ninesync.sync.plan")
state = importlib.import_module("ninesync.sync.sync_state")
analysis = importlib.import_module("ninesync.sync.analysis")
engine = importlib.import_module("ninesync.sync.engine")
progress = importlib.import_module("ninesync.sync.progress")
timing = importlib.import_module("ninesync.common.timing")
scene_utils = importlib.import_module("ninesync.common.scene_utils")
checks = Checks()
INTERP = bytes(range(16))


def make_model(index=0, name="m", shift=0.0):
    model = pmm.PmmModel()
    model.index = index
    model.name = name
    model.bone_names = ["center"]
    model.bones = [
        [
            pmm.BoneKey(0, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), INTERP),
            pmm.BoneKey(10, (shift, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0), INTERP),
        ]
    ]
    model.morph_names = ["smile"]
    model.morphs = [[pmm.MorphKey(0, 0.0), pmm.MorphKey(5, 1.0)]]
    return model


# Fingerprints.
base = make_model()
checks.check("fingerprint is deterministic", motion.motion_fingerprint(base) == motion.motion_fingerprint(make_model()))
checks.check("value change detected", motion.motion_fingerprint(base) != motion.motion_fingerprint(make_model(shift=1.0)))
changed = make_model()
key = changed.bones[0][1]
changed.bones[0][1] = key._replace(interpolation=bytes(reversed(INTERP)))
checks.check("interpolation-only change detected", motion.motion_fingerprint(base) != motion.motion_fingerprint(changed))
changed = make_model()
changed.morphs[0][1] = pmm.MorphKey(5, 0.5)
checks.check("morph change leaves motion alone", motion.motion_fingerprint(base) == motion.motion_fingerprint(changed))
checks.check("morph change detected", motion.shape_fingerprint(base) != motion.shape_fingerprint(changed))
renamed = make_model()
renamed.bone_names = ["other"]
checks.check("bone name is part of the fingerprint", motion.motion_fingerprint(base) != motion.motion_fingerprint(renamed))
table = motion.track_digests(base.bone_names, base.bones, motion.pack_bone_key)
other = make_model(shift=1.0)
moved = motion.track_digests(other.bone_names, other.bones, motion.pack_bone_key)
checks.check("track digests name the change", plan_mod.changed_tracks(table, moved) == ["center"])
checks.check("unchanged tracks are not named", plan_mod.changed_tracks(table, dict(table)) == [])
checks.check("removed tracks are named", plan_mod.changed_tracks(table, {}) == ["center"])
checks.check("records without tracks stay unknown", plan_mod.changed_tracks(None, moved) is None)
empty = pmm.PmmModel()
checks.check("empty tracks fingerprint", len(motion.motion_fingerprint(empty)) == 32)
twin_a, twin_b = make_model(0, "same"), make_model(1, "same")
checks.check("duplicate names keep separate keys", scene_utils.model_key(twin_a) != scene_utils.model_key(twin_b))

# Summaries.
rest = pmm.BoneKey(0, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), INTERP)
base.bone_names.append("idle")
base.bones.append([rest])
base.morph_names.append("unused")
base.morphs.append([pmm.MorphKey(0, 0.0)])
summary = motion.summarize_model(base)
checks.check("lone rest key is not animation", summary.animated_bones == 1 and summary.bone_tracks == 2, summary.animated_bones)
checks.check("zero morph is not animation", summary.animated_morphs == 1)
checks.check("key totals", summary.bone_keys == 3 and summary.morph_keys == 3)
checks.check("ranges", summary.bone_range == (0, 10) and summary.morph_range == (0, 5))
checks.check("long names reach mmd_tools cut", len(motion.vmd_bone_name("あ" * 10).encode("cp932")) <= 15)

# Selective VMD.
full = pmm.build_vmd(make_model())
bones_only = pmm.build_vmd(make_model(), morphs=False)
morphs_only = pmm.build_vmd(make_model(), bones=False)
checks.check("full VMD is unchanged by the options", full == pmm.build_vmd(make_model(), bones=True, morphs=True))
checks.check("bones-only VMD drops morph frames", len(bones_only) == len(full) - 2 * pmm.MORPH_FRAME.size)
checks.check("morphs-only VMD drops bone frames", len(morphs_only) == len(full) - 2 * pmm.BONE_FRAME.size)

# Output comparison.
entry = {"owner": "arm", "action": "a", "digest": "1", "linked": True}
checks.check("same output ok", state.compare_outputs([entry], [dict(entry)], True)[0] == "ok")
checks.check("content change is an edit", state.compare_outputs([entry], [dict(entry, digest="2")], True)[0] == "edited")
checks.check("new action is a switch", state.compare_outputs([entry], [dict(entry, action="b", digest="2")], True)[0] == "switched")
checks.check("renamed action stays valid", state.compare_outputs([entry], [dict(entry, action="b")], True)[0] == "ok")
checks.check("no clips: other action is an edit", state.compare_outputs([entry], [dict(entry, action="b", digest="2")], False)[0] == "edited")
checks.check("gone action", state.compare_outputs([entry], [dict(entry, action="", digest="")], True)[0] == "missing")
checks.check("detached action", state.compare_outputs([entry], [dict(entry, linked=False)], True)[0] == "missing")
checks.check("gone owner", state.compare_outputs([entry], [], True)[0] == "missing")


# Decision table.
def decide(saved, current=None, changed=False, settings="s"):
    plan = plan_mod.PartPlan("motion")
    plan.exists = True
    plan.settings = settings
    plan_mod.decide(plan, saved, "Not synced yet", current if current is not None else [dict(entry)], changed, True)
    return plan


saved = {"settings": "s", "outputs": [entry]}
edited = [dict(entry, digest="2")]
checks.check("no record updates", decide(None).operation == plan_mod.UPDATE)
checks.check("unchanged skips", decide(saved).operation == plan_mod.SKIP)
checks.check("edit alone is kept", decide(saved, edited).operation == plan_mod.SKIP)
checks.check("settings update", decide(saved, settings="x").operation == plan_mod.UPDATE)
checks.check("settings over an edit block", decide(saved, edited, settings="x").operation == plan_mod.BLOCKED)
source = decide(saved, edited, changed=True)
checks.check("new keys over an edit warn", source.operation == plan_mod.UPDATE and source.warnings, source.operation)

# Action digest and records with real Blender data.
action = bpy.data.actions.new("t")
slot = action.slots.new(id_type="OBJECT", name="t")
strip = action.layers.new("l").strips.new(type="KEYFRAME")
bag = strip.channelbags.new(slot)
curve = bag.fcurves.new("location", index=0)
curve.keyframe_points.insert(1.0, 1.0)
first = state.action_digest(action)
checks.check("digest repeats", first == state.action_digest(action))
curve.keyframe_points[0].co[1] = 2.0
checks.check("digest sees a value edit", first != state.action_digest(action))
second = state.action_digest(action)
curve.keyframe_points[0].interpolation = "CONSTANT"
checks.check("digest sees an interpolation edit", second != state.action_digest(action))

# A plan over a scene with no matching rig.
scene = bpy.context.scene
settings = scene.mmd_bridge
settings.filepath = "//none.pmm"
project = pmm.PmmProject()
project.models = [make_model(0, "unmapped"), make_model(1, "off")]
disabled = settings.targets.add()
disabled.key = scene_utils.model_key(project.models[1])
disabled.pmm_name = "off"
disabled.enabled = False
before = (len(bpy.data.objects), len(bpy.data.actions), len(bpy.data.texts))
plan = plan_mod.build_plan(scene, project)
checks.check("planning writes no data", before == (len(bpy.data.objects), len(bpy.data.actions), len(bpy.data.texts)))
checks.check("no target blocks", plan.items[0].operation == plan_mod.BLOCKED and plan.items[0].reason, plan.items[0].operation)
checks.check("unassigned mapping", plan.items[0].mapping == "unassigned")
checks.check("disabled is not selected", not plan.items[1].selected and not plan.items[1].run_parts)
checks.check("planning added no rows", len(settings.targets) == 1)

# Records.
row = settings.targets[0]
row.sync_record = "not json"
checks.check("bad record reads as none", state.read_record(row) is None)
row.sync_record = json.dumps({"version": state.RECORD_VERSION + 1})
checks.check("future record reads as none", state.read_record(row) is None)
row.sync_record = json.dumps({"version": state.RECORD_VERSION, "parts": {"motion": {}, "shape": {}}})
state.forget_parts(row, ("motion",))
checks.check("failure forgets only its part", sorted(state.read_record(row)["parts"]) == ["shape"])
row.sync_record = ""

# Preview of unreadable input.
analysis.clear_analysis()
settings.filepath = ""
checks.check("no PMM selected", analysis.analyze(scene).error == "No PMM selected")
settings.filepath = os.path.join(REPO, "__init__.py")
result = analysis.analyze(scene)
checks.check("malformed PMM reported", "PMM" in result.error and result.plan is None, result.error)
settings.filepath = os.path.join(REPO, "assets", "kagamine.pmm")
result = analysis.analyze(scene)
checks.check("real PMM analysed", result.plan is not None and not result.error, result.error)
checks.check("fresh preview is not stale", not analysis.current_analysis(scene).stale)
settings.uv_morphs = not settings.uv_morphs
checks.check("setting change marks it stale", analysis.current_analysis(scene).stale)
settings.uv_morphs = not settings.uv_morphs

# Progress.
bar = progress.SyncProgress()
bar.begin()
bar.update(0.5, "half")
bar.update(0.2, "earlier")
checks.check("progress never runs backward", progress.progress_state()["fraction"] == 0.5)
bar.end()
bar.end()
checks.check("progress ends and repeats safely", not progress.progress_state()["active"])

# Driver exits.
settings.filepath = os.path.join(REPO, "__init__.py")
checks.check("failed sync returns False", engine.run_sync(scene) is False)
checks.check("failed sync released", not engine.sync_running() and not progress.progress_state()["active"])
checks.check("failure reaches the status", "PMM read failed" in settings.status, settings.status)
settings.filepath = os.path.join(REPO, "assets", "kagamine.pmm")
driver = engine.SyncDriver(scene)
driver.step()
driver.cancel()
while driver.step():
    pass
checks.check("cancel outcome", driver.outcome == "cancelled", driver.outcome)
checks.check("cancel released", not engine.sync_running() and not progress.progress_state()["active"])
driver = engine.SyncDriver(scene)
driver.step()
engine.abandon_sync()
checks.check("abandoned sync released", not engine.sync_running() and not progress.progress_state()["active"])

# Keys on bones hidden in the PMX: still imported, but reported because MMD ignores them.
arm_data = bpy.data.armatures.new("hidden_rig")
rig = bpy.data.objects.new("hidden_rig", arm_data)
bpy.context.scene.collection.objects.link(rig)
bpy.context.view_layer.objects.active = rig
bpy.ops.object.mode_set(mode="EDIT")
for bone_name in ("center", "still"):
    edit_bone = arm_data.edit_bones.new(bone_name)
    edit_bone.tail = (0.0, 0.0, 1.0)
bpy.ops.object.mode_set(mode="OBJECT")
hidden_model = make_model()
hidden_model.bone_names = ["center", "still"]
hidden_model.bones.append([pmm.BoneKey(0, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), INTERP)])
table = {bone.name: bone for bone in rig.pose.bones}
checks.check("visible bones not reported", plan_mod.map_bones(hidden_model, table).hidden == [])
for bone in rig.pose.bones:
    if hasattr(bone, "hide"):
        bone.hide = True
    else:
        bone.bone.hide = True
report = plan_mod.map_bones(hidden_model, table)
checks.check("moving hidden bone reported", report.hidden == ["center"], report.hidden)
checks.check("hidden bone keys still imported", report.matched == 2 and report.lost_keys == 0)
bpy.data.objects.remove(rig)

# Timing.
timings = timing.begin_timings()
with timing.phase("good"):
    pass
try:
    with timing.phase("bad"):
        raise ValueError("expected test failure")
except ValueError:
    pass
checks.check("both outcomes timed", set(timings.totals) == {"good", "bad"})
timing.finish_timings("ok")
with timing.phase("idle"):
    pass
checks.check("no run, no record", "idle" not in timings.totals)

core.unregister()
checks.finish()
