import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import Checks, load, stage

import bpy


# 0.9.2 fixes, without mmd_tools: baked camera and light tracks are retimed like any other
# while isolated cuts stay cuts; the export merges the NLA clips a sync leaves with the
# active action and adds initial keys, then puts every ID back; a hidden bone holding one
# pose is not reported as keyed.
stage("ninesync")
core = load("ninesync")
core.register()
pmm = importlib.import_module("ninesync.pmm")
retime = importlib.import_module("ninesync.sync.retime")
export_actions = importlib.import_module("ninesync.sync.export_actions")
project_export = importlib.import_module("ninesync.sync.project_export")
checks = Checks()


def frames(keys):
    return [k.frame for k in keys]


def visibility(frame_list):
    return [pmm.VisibilityKey(f, 1) for f in frame_list]


baked = visibility(range(300))
for factor, last in ((0.8, 239), (2.0, 598)):
    out = retime.retime_keys(baked, factor, True)
    checks.check("baked track retimed at x%g ends on %d" % (factor, last), frames(out)[-1] == last, frames(out)[-3:])
cuts = retime.retime_keys(visibility([0, 100, 101, 200]), 0.8, True)
checks.check("isolated cut kept at 24 fps", frames(cuts) == [0, 80, 81, 160], frames(cuts))
cuts = retime.retime_keys(visibility([0, 100, 101, 200]), 2.0, True)
checks.check("isolated cut kept at 60 fps", frames(cuts) == [0, 200, 201, 400], frames(cuts))
pair_then_run = retime.retime_keys(visibility([0, 50, 51, 100, 101, 102]), 2.0, True)
checks.check("a run of three is not a cut", frames(pair_then_run) == [0, 100, 101, 200, 202, 204], frames(pair_then_run))

# Export sources: a clip on the NLA plus the user's active action above it.
obj = bpy.data.objects.new("ExportProbe", None)
bpy.context.scene.collection.objects.link(obj)
clip = bpy.data.actions.new("MMDBridge_probe_clip")
anim = obj.animation_data_create()
anim.action = clip
for frame, value in ((0, 1.0), (10, 2.0)):
    obj.location.x = value
    obj.keyframe_insert("location", index=0, frame=frame)
    obj.location.z = value * 10
    obj.keyframe_insert("location", index=2, frame=frame)
anim.action = None
track = anim.nla_tracks.new()
strip = track.strips.new(clip.name, 0, clip)
mine = bpy.data.actions.new("UserEdits")
anim.action = mine
obj.location.z = 99.0
obj.keyframe_insert("location", index=2, frame=5)
obj.rotation_euler.y = 0.5
actions_before = set(bpy.data.actions)
with export_actions.export_actions({obj: [("rotation_euler", 1, 0.5)]}) as warnings:
    merged = obj.animation_data.action
    curves = {(c.data_path, c.array_index): c for c in export_actions.iter_fcurves(merged)}
    checks.check("export reads a temp action", merged not in (clip, mine), merged.name)
    checks.check("clip channel exported", ("location", 0) in curves and [p.co.x for p in curves[("location", 0)].keyframe_points] == [0.0, 10.0])
    checks.check("active action replaces the clip channel", [p.co.y for p in curves[("location", 2)].keyframe_points] == [99.0])
    checks.check("initial key added at frame 0", [tuple(p.co) for p in curves[("rotation_euler", 1)].keyframe_points] == [(0.0, 0.5)])
    checks.check("no warnings for a plain clip", warnings == [], warnings)
checks.check("active action restored", obj.animation_data.action == mine)
checks.check("clip untouched", [tuple(p.co) for p in export_actions.iter_fcurves(clip)[0].keyframe_points] == [(0.0, 1.0), (10.0, 2.0)])
checks.check("temp action removed", set(bpy.data.actions) == actions_before)

strip.scale = 2.0
with export_actions.export_actions({obj: []}) as warnings:
    pass
checks.check("scaled clip is named, not exported", len(warnings) == 1 and "scaled" in warnings[0], warnings)

key = pmm.BoneKey if hasattr(pmm, "BoneKey") else None
if key is not None:
    still = [key(0, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), b""), key(1, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), b"")]
    moves = still + [key(9, (0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0), b"")]
    checks.check("hidden bone holding one pose is not keyed", not project_export.plays_later(still))
    checks.check("hidden bone that moves later is keyed", project_export.plays_later(moves))
else:
    checks.check("pmm.BoneKey exists", False)

bone_morphs = importlib.import_module("ninesync.sync.bone_morphs")
frames_ = list(range(1001))
rows = [(1.0, 0.0) if f < 600 else (1.0 + (f - 600) * 0.01, 0.0) for f in frames_]
kept = bone_morphs.thin_linear(frames_, rows)
kept_frames = [frames_[i] for i in kept]
checks.check("held stretch dropped whole, ramp keyed at most every 120 frames", kept_frames[:2] == [0, 600] and kept_frames[-1] == 1000 and all(b - a <= 120 for a, b in zip(kept_frames[1:], kept_frames[2:])) and len(kept_frames) <= 6, kept_frames)
wave = [(float(f % 2),) for f in range(20)]
checks.check("alternating values keep every key", len(bone_morphs.thin_linear(list(range(20)), wave)) == 20)

core.unregister()
checks.finish()
