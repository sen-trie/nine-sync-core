import importlib
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import Checks, load, stage

import bpy


stage("ninesync")
core = load("ninesync")
core.register()
animation = importlib.import_module("ninesync.common.animation")
light = importlib.import_module("ninesync.sync.light")
pmm = importlib.import_module("ninesync.pmm")
checks = Checks()
scene = bpy.context.scene


# mmd_tools patches this on; reproducing it keeps the test free of mmd_tools. It walks
# the channelbags itself so the test still runs against an older iter_fcurves.
def first_slot_fcurves(action):
    bags = [b for layer in action.layers for strip in layer.strips for b in strip.channelbags]
    return list(bags[0].fcurves) if bags else []


bpy.types.Action.fcurves = property(first_slot_fcurves)


def light_object(name):
    obj = bpy.data.objects.new(name, bpy.data.lights.new(name, "SUN"))
    scene.collection.objects.link(obj)
    return obj


shared = light_object("shared")
for frame in (0, 10, 100):
    shared.data.keyframe_insert("color", frame=frame)
    shared.keyframe_insert("location", frame=frame)
action = shared.animation_data.action
checks.check("setup: Blender shares one action", shared.data.animation_data.action == action)
checks.check("all slots read despite the shim", len(animation.iter_fcurves(action)) == 6)
own = animation.iter_fcurves(action, shared.animation_data.action_slot)
checks.check("slot scoping", {c.data_path for c in own} == {"location"})
checks.check("find_fcurve stays on its owner", animation.find_fcurve(shared.data, "location", 0) is None)
checks.check("find_fcurve finds the second slot", animation.find_fcurve(shared, "location", 0) is not None)

saved = animation.snapshot_outside([shared, shared.data], 0, 10)
checks.check("user keys saved once per curve", len(saved) == 6, len(saved))
shared.animation_data.action = None
shared.data.animation_data.action = None
restored = animation.restore_points(saved)
color_anim = shared.data.animation_data
paths = {c.data_path for c in animation.iter_fcurves(color_anim.action, color_anim.action_slot)}
checks.check("keys restored to the right ID", restored == 6 and paths == {"color"}, paths)

rig = light_object("rig")
light.ensure_light_rig = lambda scene, settings: (rig, rig)
project = types.SimpleNamespace(
    light_keys=[
        pmm.LightKey(0, (0.0, 0.0, 0.0), (-0.5, -1.0, 0.5)),
        pmm.LightKey(10, (1.0, 0.0, 0.0), (0.0, -1.0, 0.0)),
        pmm.LightKey(11, (0.0, 1.0, 0.0), (0.5, -1.0, 0.0)),
    ]
)
light.import_light(scene, project, scene.mmd_bridge)
location = rig.animation_data.action
color = rig.data.animation_data.action
checks.check("light actions are separate", location != color, (location.name, color.name))
checks.check("action names", (location.name, color.name) == ("MMDBridge_Light_loc", "MMDBridge_Light_color"))
for label, act in (("location", location), ("colour", color)):
    points = sorted(animation.iter_fcurves(act)[0].keyframe_points, key=lambda p: p.co[0])
    modes = [p.interpolation for p in points]
    checks.check("%s: linear, cut held constant" % label, modes == ["LINEAR", "CONSTANT", "LINEAR"], modes)
direction = animation.find_fcurve(rig, "location", 2)
checks.check("direction mapped to location", abs(direction.evaluate(0) - 1.0) < 1e-5)

owner = bpy.data.objects.new("nla", None)
scene.collection.objects.link(owner)
anim = owner.animation_data_create()
bridge_track = anim.nla_tracks.new()
bridge_track.name = "bridge"
clip = bpy.data.actions.new("MMDBridge_clip")
clip.frame_range = (0, 10)
clip.use_frame_range = True
bridge_track.strips.new("clip", 0, clip)
user_track = anim.nla_tracks.new()
user_track.name = "user"
user_track.strips.new("mine", 0, bpy.data.actions.new("mine"))
animation.clear_bridge_nla([owner])
checks.check("emptied bridge track removed, user track kept", [t.name for t in anim.nla_tracks] == ["user"])
del bpy.types.Action.fcurves
core.unregister()
checks.finish()
