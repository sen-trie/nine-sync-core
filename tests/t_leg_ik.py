import importlib
import math
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import Checks, load, stage

import bpy
from mathutils import Quaternion, Vector


stage("ninesync")
core = load("ninesync")
core.register()
pmm = importlib.import_module("ninesync.pmm")
leg_ik = importlib.import_module("ninesync.sync.leg_ik")
bone_morphs = importlib.import_module("ninesync.sync.bone_morphs")
checks = Checks()


# mmd_tools patches this on; reproducing it keeps the test free of mmd_tools.
def first_slot_fcurves(action):
    bags = [b for layer in action.layers for strip in layer.strips for b in strip.channelbags]
    return list(bags[0].fcurves) if bags else []


bpy.types.Action.fcurves = property(first_slot_fcurves)

# Interpolation bytes are signed chars: MMD reads a stored 211 as -45 (measured on a
# bone that sat 2.5 % of the way along a curve whose y1 was stored as 211).
checks.check("curve bytes above 127 are negative", pmm.curve_values(81, 211, 107, 107) == (81, -45, 107, 107))
checks.check("x values stay inside the key span", pmm.curve_values(200, 20, 250, 30) == (0, 20, 0, 30))
row = pmm.vmd_interpolation_row(bytes([81, 211, 107, 107] * 4))
checks.check("the VMD row carries the signed byte", row[4] == 211 and row[0] == 81 and row[8] == 107, list(row[:16]))
ease = leg_ik.bezier_ease(81, 211, 107, 107, 6 / 13)
checks.check("that curve eases to the share MMD showed", abs(ease - 0.0251) < 0.002, ease)

# A share outside 0-1 must not raise: Quaternion.slerp does, and one such curve used to
# make the whole leg solve fail.
quarter = Quaternion((1.0, 0.0, 0.0), math.pi / 2)
before = leg_ik.slerp_free(Quaternion(), quarter, -0.5)
after = leg_ik.slerp_free(Quaternion(), quarter, 1.5)
checks.check("slerp runs backwards past the start", abs(before.angle - math.pi / 4) < 1e-6 and before.axis.x < 0, (before.angle, tuple(before.axis)))
checks.check("slerp runs on past the end", abs(after.angle - 3 * math.pi / 4) < 1e-6, after.angle)
checks.check("slerp inside 0-1 is the usual one", leg_ik.slerp_free(Quaternion(), quarter, 0.5).angle - math.pi / 4 < 1e-6)

key_a = pmm.BoneKey(0, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), bytes([20, 20, 107, 107] * 4))
key_b = pmm.BoneKey(10, (10.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), bytes([62, 213, 107, 107] * 4))
track = leg_ik.KeyTrack([key_a, key_b], 1.0)
loc, _quat = track.at(5)
checks.check("a track with an undershooting curve evaluates", abs(loc.x) < 5.0, loc.x)

# Bone morphs: the weight track and the baked keys.
weights = bone_morphs.WeightTrack([types.SimpleNamespace(frame=0, weight=0.0), types.SimpleNamespace(frame=10, weight=1.0)])
checks.check(
    "morph weights are linear and held at both ends",
    (weights.at(-5), weights.at(5), weights.at(20)) == (0.0, 0.5, 1.0),
    (weights.at(-5), weights.at(5), weights.at(20)),
)

arm_data = bpy.data.armatures.new("t_arm")
arm = bpy.data.objects.new("t_arm", arm_data)
bpy.context.scene.collection.objects.link(arm)
bpy.context.view_layer.objects.active = arm
bpy.ops.object.mode_set(mode="EDIT")


def add_bone(name, head, tail, parent=None):
    bone = arm_data.edit_bones.new(name)
    bone.head, bone.tail = head, tail
    if parent is not None:
        bone.parent = arm_data.edit_bones[parent]
    return bone


add_bone("hips", (0, 0, 10), (0, 0, 11))
add_bone("thigh", (0, 0, 10), (0, 0, 5), "hips")
add_bone("knee", (0, 0, 5), (0, 0, 0), "thigh")
add_bone("ground", (0, 0, 0), (0, 0, 1))
add_bone("ik", (0, 0, 0), (0, 0, -1), "ground")
add_bone("wing", (0, 0, 12), (0, 0, 13), "hips")
bpy.ops.object.mode_set(mode="OBJECT")
pose = arm.pose.bones

# Wing morph: keyed rotation at frame 0, a quarter turn at full weight by frame 10.
pose["wing"].rotation_mode = "QUATERNION"
pose["wing"].rotation_quaternion = Quaternion()
pose["wing"].keyframe_insert("rotation_quaternion", frame=0)
pose["wing"].keyframe_insert("rotation_quaternion", frame=10)
bpy.context.view_layer.update()
offset = Quaternion((0.0, 0.0, 1.0), math.pi / 2)
root = types.SimpleNamespace(
    mmd_root=types.SimpleNamespace(
        bone_morphs=[
            types.SimpleNamespace(
                name="flap",
                data=[types.SimpleNamespace(bone="wing", location=(0.0, 0.0, 0.0), rotation=tuple(offset))],
            )
        ]
    )
)
model = types.SimpleNamespace(
    morph_names=["flap"],
    morphs=[[types.SimpleNamespace(frame=0, weight=0.0), types.SimpleNamespace(frame=10, weight=1.0)]],
)
checks.check("a bone morph with weight keys is picked up", len(bone_morphs.animated_bone_morphs(root, model)) == 1)
checks.check("no weights, nothing to bake", bone_morphs.animated_bone_morphs(root, types.SimpleNamespace(morph_names=[], morphs=[])) == [])
fingerprint = bone_morphs.bone_morph_fingerprint(root, model)
model.morphs[0][1] = types.SimpleNamespace(frame=10, weight=0.5)
checks.check("a changed weight changes the fingerprint", fingerprint != bone_morphs.bone_morph_fingerprint(root, model))
model.morphs[0][1] = types.SimpleNamespace(frame=10, weight=1.0)
written = bone_morphs.apply_bone_morphs(arm, root, model)
curves = [leg_ik.find_fcurve(arm, 'pose.bones["wing"].rotation_quaternion', i) for i in range(4)]
mid = Quaternion([c.evaluate(5) for c in curves])
end = Quaternion([c.evaluate(10) for c in curves])
checks.check("one bone was baked", written == 1, written)
checks.check("half weight turns the bone half way", abs(mid.angle - math.pi / 4) < 1e-4, mid.angle)
checks.check("full weight turns it the whole way", abs(end.angle - math.pi / 2) < 1e-4, end.angle)

# The leg solve: a straight leg whose IK bone is lifted and moved back has to reach it.
keys = {
    "ik": leg_ik.KeyTrack([pmm.BoneKey(0, (0.0, 3.0, 2.0), (0.0, 0.0, 0.0, 1.0), bytes([20, 20, 107, 107] * 4))], 1.0),
}
chain = leg_ik.LegChain(pose["thigh"], pose["knee"], pose["ik"], 0, (-math.pi, -0.0087), iterations=60, step_limit=1.0)


def ankle_after(chain, tracks, frame):
    thigh, bend = chain.solve(tracks, frame)
    hip, _keyed, parent_rot = leg_ik.chain_pose(chain.thigh_path, tracks, frame)
    thigh_world = parent_rot @ thigh
    knee_world = thigh_world @ Quaternion((1.0, 0.0, 0.0), bend)
    ankle = hip + thigh_world @ chain.thigh_vector + knee_world @ chain.shin_vector
    return ankle, leg_ik.chain_pose(chain.ik_path, tracks, frame)[0], bend


ankle, target, bend = ankle_after(chain, keys, 0)
checks.check("the solved ankle meets the IK bone", (ankle - target).length < 5e-3, (tuple(ankle), tuple(target)))
checks.check("the knee bends inside its range", -math.pi <= bend <= 0.0, bend)

# The same leg with a different step limit and loop count still converges, which is
# what a model's own PMX values may ask for.
slow = leg_ik.LegChain(pose["thigh"], pose["knee"], pose["ik"], 0, (-math.pi, -0.0087), iterations=250, step_limit=0.2)
ankle, target, _bend = ankle_after(slow, keys, 0)
checks.check("a small angle limit and many passes converge too", (ankle - target).length < 1e-2, (ankle - target).length)

# A leg stretched past its reach points at the target instead of failing.
far = {"ik": leg_ik.KeyTrack([pmm.BoneKey(0, (0.0, 3.0, 30.0), (0.0, 0.0, 0.0, 1.0), bytes([20, 20, 107, 107] * 4))], 1.0)}
ankle, target, _bend = ankle_after(chain, far, 0)
hip = leg_ik.chain_pose(chain.thigh_path, far, 0)[0]
reach = (ankle - hip).normalized().dot((target - hip).normalized())
checks.check("an out-of-reach target is faced, not failed", reach > 0.999, reach)

# An append (waist-cancel) bone in the parent chain hands on the opposite turn.
class FakeMMD:
    def __init__(self, rotation=False, source="", share=1.0):
        self.has_additional_rotation = rotation
        self.has_additional_location = False
        self.additional_transform_bone = source
        self.additional_transform_influence = share


class FakePose:
    def __init__(self, name, mmd):
        self.name = name
        self.mmd_bone = mmd
        self.id_data = None


class FakeArmature:
    def __init__(self, bones):
        self.pose = types.SimpleNamespace(bones={b.name: b for b in bones})
        for b in bones:
            b.id_data = self


waist = FakePose("waist", FakeMMD())
cancel = FakePose("cancel", FakeMMD(True, "waist", -1.0))
FakeArmature([waist, cancel])
turn = pmm.BoneKey(0, (0.0, 0.0, 0.0), (0.0, math.sin(0.25), 0.0, math.cos(0.25)), bytes([20, 20, 107, 107] * 4))
append_tracks = {"waist": leg_ik.KeyTrack([turn], 1.0)}
_loc, own = leg_ik.bone_local(cancel, append_tracks, 0)
checks.check("an append bone takes the opposite share of its source", abs(own.angle - 0.5) < 1e-6 and own.axis.y < 0, (own.angle, tuple(own.axis)))

del bpy.types.Action.fcurves
core.unregister()
checks.finish()
