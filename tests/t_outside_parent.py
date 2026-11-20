import importlib
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import Checks, load, stage

import bpy
from mathutils import Euler, Matrix, Vector


# Outside-parent solver against a frozen copy of the original per-frame solver (one
# frame_set and one keyframe_insert per key) combined with MMD's switching rule (see
# compose), on synthetic rigs without mmd_tools: the
# bulk curve writer, FK and constrained parent chains, a moving parent object, scaled
# armatures, quaternion flips, two parents in turn with a gap, a released bone, and
# keys outside the solved span. Evaluated world matrices are compared on every frame and
# on half frames, curves on quarter and random subframes.
stage("ninesync")
core = load("ninesync")
core.register()
animation = importlib.import_module("ninesync.common.animation")
op = importlib.import_module("ninesync.sync.outside_parent")
frames_mod = importlib.import_module("ninesync.sync.frames")
checks = Checks()
scene = bpy.context.scene
LAST = 60


# --- frozen reference: the solver as it was before the bulk writer (B0) -------------
def ref_bound_matrix(armature, parent_armature, parent, own):
    own_roll = op.rest_rotation_inverse(armature, own.name).inverted()
    if parent is None:
        rest_head = Matrix.Translation(own.bone.head_local)
        return armature.matrix_world @ rest_head @ own_roll @ own.matrix_basis
    posed = parent_armature.matrix_world @ parent.matrix
    parent_roll = op.rest_rotation_inverse(parent_armature, parent.name)
    return posed @ parent_roll @ own_roll @ own.matrix_basis


def ref_set_interpolation(armature, path, kind, low, high):
    for index in range(4):
        fcurve = animation.find_fcurve(armature, path, index)
        if fcurve is None:
            continue
        for point in fcurve.keyframe_points:
            if low <= point.co[0] <= high:
                point.interpolation = kind
        fcurve.update()


def ref_solve(armature, jobs):
    frames = sorted({f for job in jobs for f in job[3]})
    wanted = {job[0].name: {} for job in jobs}
    world = armature.matrix_world
    previous = scene.frame_current
    for frame in frames:
        scene.frame_set(frame)
        for own, parent_armature, parent, job_frames in jobs:
            if frame not in job_frames:
                continue
            desired = ref_bound_matrix(armature, parent_armature, parent, own)
            chain = world @ own.bone.matrix_local
            if own.parent is not None:
                chain = world @ (own.parent.matrix @ own.parent.bone.matrix_local.inverted() @ own.bone.matrix_local)
            location, rotation, _s = (chain.inverted() @ desired).decompose()
            last = wanted[own.name].get(frame - 1)
            if last is not None and last[1].dot(rotation) < 0.0:
                rotation.negate()
            wanted[own.name][frame] = (location, rotation)
    scene.frame_set(previous)
    for own, _pa, _pb, _f in jobs:
        own.rotation_mode = "QUATERNION"
        paths = (own.path_from_id("location"), own.path_from_id("rotation_quaternion"))
        for frame, (location, rotation) in sorted(wanted[own.name].items()):
            own.location = location
            own.rotation_quaternion = rotation
            for path in paths:
                armature.keyframe_insert(data_path=path, frame=frame)
        if wanted[own.name]:
            low, high = min(wanted[own.name]), max(wanted[own.name])
            for path in paths:
                ref_set_interpolation(armature, path, "LINEAR", low, high)


# --- synthetic rigs -----------------------------------------------------------------
def make_armature(name, bones, location=(0, 0, 0), scale=(1, 1, 1)):
    data = bpy.data.armatures.new(name)
    obj = bpy.data.objects.new(name, data)
    scene.collection.objects.link(obj)
    obj.location = location
    obj.scale = scale
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    made = {}
    for bone_name, parent, head, tail, roll in bones:
        edit = data.edit_bones.new(bone_name)
        edit.head, edit.tail, edit.roll = head, tail, roll
        if parent:
            edit.parent = made[parent]
        made[bone_name] = edit
    bpy.ops.object.mode_set(mode="OBJECT")
    for pb in obj.pose.bones:
        pb.rotation_mode = "QUATERNION"
    return obj


def key_bone(obj, bone, rng, frames, spin=1.0, free_handles=True):
    pb = obj.pose.bones[bone]
    for frame in frames:
        pb.location = Vector([rng.uniform(-0.3, 0.3) for _ in range(3)])
        pb.rotation_quaternion = Euler([rng.uniform(-spin, spin) for _ in range(3)]).to_quaternion()
        obj.keyframe_insert(data_path=pb.path_from_id("location"), frame=frame, group=bone)
        obj.keyframe_insert(data_path=pb.path_from_id("rotation_quaternion"), frame=frame, group=bone)
    if free_handles:
        # VMD-like keys: free handles placed away from the auto ones.
        for path in (pb.path_from_id("location"), pb.path_from_id("rotation_quaternion")):
            for index in range(4):
                fc = animation.find_fcurve(obj, path, index)
                if fc is None:
                    continue
                for point in fc.keyframe_points:
                    point.handle_left_type = point.handle_right_type = "FREE"
                    point.handle_left = (point.co[0] - 2.0, point.co[1] + rng.uniform(-0.2, 0.2))
                    point.handle_right = (point.co[0] + 2.0, point.co[1] + rng.uniform(-0.2, 0.2))
                fc.update()


PARENT_BONES = [
    ("root", None, (0, 0, 0), (0, 0, 1), 0.0),
    ("arm", "root", (0, 0, 1), (0.6, 0.1, 1.3), 0.4),
    ("hand", "arm", (0.6, 0.1, 1.3), (0.9, 0.3, 1.2), -0.7),
    ("helper", "root", (0.2, 0, 0.5), (0.2, 0, 0.9), 0.0),
]
CHILD_BONES = [
    ("all", None, (0, 0, 0), (0, 0, 0.5), 0.0),
    ("center", "all", (0, 0.1, 0.8), (0, 0.1, 1.2), 0.3),
    ("tip", "center", (0, 0.1, 1.2), (0.1, 0.1, 1.5), 0.0),
]


def build(case, seed):
    rng = random.Random(seed)
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj)
    for action in list(bpy.data.actions):
        bpy.data.actions.remove(action)
    scale = (1, 1, 1) if case != "scaled" else (1.5, -1.0, 0.7)
    parent = make_armature("P", PARENT_BONES, location=(0.4, -0.2, 0.0), scale=scale)
    child = make_armature("C", CHILD_BONES, location=(-0.3, 0.5, 0.1))
    spin = 3.0 if case == "flips" else 1.0
    key_bone(parent, "root", rng, [0, 20, 45], spin)
    key_bone(parent, "arm", rng, [0, 8, 30, 50], spin)
    key_bone(parent, "hand", rng, [0, 12, 26, 41, 60], spin)
    key_bone(parent, "helper", rng, [0, 30], spin)
    key_bone(child, "all", rng, [0, 25, 55], spin)
    key_bone(child, "center", rng, [0, 10, 18, 33, 47, 58], spin)
    if case == "constrained":
        c = parent.pose.bones["arm"].constraints.new("COPY_ROTATION")
        c.target, c.subtarget, c.influence = parent, "helper", 0.5
    if case == "ik":
        c = parent.pose.bones["hand"].constraints.new("IK")
        c.target, c.subtarget, c.chain_count = parent, "helper", 2
    if case == "moving_object":
        empty = bpy.data.objects.new("E", None)
        scene.collection.objects.link(empty)
        parent.parent = empty
        for frame, value in ((0, 0.0), (30, 1.2), (60, -0.4)):
            empty.location = (value, value * 0.5, 0.0)
            empty.rotation_euler = (0.0, value, 0.3 * value)
            empty.keyframe_insert("location", frame=frame)
            empty.keyframe_insert("rotation_euler", frame=frame)
    if case in ("driver", "scripted_driver"):
        fc = parent.pose.bones["helper"].driver_add("location", 1)
        fc.driver.expression = "frame * 0.01" if case == "driver" else "(lambda f: f * 0.01)(frame)"
    if case == "physics":
        mesh = bpy.data.meshes.new("M")
        mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
        body = bpy.data.objects.new("RB", mesh)
        scene.collection.objects.link(body)
        bpy.context.view_layer.objects.active = body
        bpy.ops.rigidbody.object_add()
        c = parent.pose.bones["helper"].constraints.new("COPY_LOCATION")
        c.target = body
    if case == "mesh_target":
        mesh = bpy.data.meshes.new("T")
        mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
        holder = bpy.data.objects.new("T", mesh)
        scene.collection.objects.link(holder)
        for frame, value in ((0, 0.0), (40, 0.8)):
            holder.location = (value, 0.2, -value)
            holder.keyframe_insert("location", frame=frame)
        c = parent.pose.bones["arm"].constraints.new("DAMPED_TRACK")
        c.target = holder
    return parent, child


def jobs_for(case, parent, child):
    center = child.pose.bones["center"]
    hand = parent.pose.bones["hand"]
    if case == "two_parents":
        arm = parent.pose.bones["arm"]
        return [(center, parent, hand, set(range(5, 22))), (center, parent, arm, set(range(26, 50)))]
    if case == "released":
        return [(center, child, None, set(range(12, 40)))]
    if case == "self_armature":
        tip_parent = child.pose.bones["all"]
        return [(center, child, tip_parent, set(range(3, 44)))]
    return [(center, parent, hand, set(range(6, 52)))]


def world_samples(arm, bone):
    found = []
    for frame in range(0, LAST + 2):
        for sub in (0.0, 0.5):
            scene.frame_set(frame, subframe=sub)
            found.append((frame + sub, (arm.matrix_world @ arm.pose.bones[bone].matrix).copy()))
    scene.frame_set(0)
    return found


# Quarter frames plus arbitrary subframes (seeded), so no fixed grid hides a difference.
CURVE_TIMES = [t / 4.0 for t in range(-4, (LAST + 2) * 4)] + random.Random(5).sample([t / 1000.0 for t in range(-1000, (LAST + 2) * 1000)], 400)


def curve_samples(arm, bone):
    found = {}
    for path in ("location", "rotation_quaternion"):
        for index in range(4 if path == "rotation_quaternion" else 3):
            fc = animation.find_fcurve(arm, 'pose.bones["%s"].%s' % (bone, path), index)
            found[(path, index)] = [fc.evaluate(t) for t in CURVE_TIMES] if fc else None
    return found


def max_errors(a, b):
    trans = angle = 0.0
    for (_t, ma), (_u, mb) in zip(a, b):
        if ma is None or mb is None:
            continue
        trans = max(trans, (ma.to_translation() - mb.to_translation()).length)
        qa, qb = ma.to_quaternion(), mb.to_quaternion()
        angle = max(angle, qa.rotation_difference(qb).angle if qa.dot(qb) >= 0 else qa.rotation_difference(-qb).angle)
    return trans, angle


def run(case, solver, isolate=True):
    parent, child = build(case, 7)
    jobs = jobs_for(case, parent, child)
    op.ISOLATE = isolate
    isolated = op.solve_closure(child, jobs) is not None
    scenes = len(bpy.data.scenes)
    solver(child, jobs)
    op.ISOLATE = True
    leftover = len(bpy.data.scenes) != scenes or bpy.context.scene != scene
    return world_samples(child, "center"), world_samples(child, "tip"), curve_samples(child, "center"), isolated, leftover


# What MMD shows (checked frame by frame against MMD 9.32): on bound frames the bound
# placement (the frozen per-frame solve, linear between bound frames), everywhere else the
# bone's own motion untouched, switching on the frame: the half frame before a run holds
# the own pose of the frame before it, the half frame after a run holds its last bound
# pose. samples are (time, value) lists over the same times; own is sampled before solving.
# With world=True the half frames at a switch are left out (None): only the bone's own
# channels hold there while its parent keeps moving; the curve check covers them.
def compose(own, solved, bound, world=False):
    by_own, by_solved = dict(own), dict(solved)
    found = []
    for time, _value in own:
        frame = math.floor(time)
        switch = time != frame and (frame in bound) != (frame + 1 in bound)
        if switch and world:
            pick = None
        elif frame in bound:
            pick = by_solved[float(frame) if switch else time]
        else:
            pick = by_own[float(frame) if switch else time]
        found.append((time, pick))
    return found


def curve_lists(curves):
    return {key: None if values is None else list(zip(CURVE_TIMES, values)) for key, values in curves.items()}


def reference(case, edit=None):
    parent, child = build(case, 7)
    jobs = jobs_for(case, parent, child)
    if edit is not None:
        jobs = edit(parent, child, jobs) or jobs
    own = (world_samples(child, "center"), world_samples(child, "tip"), curve_lists(curve_samples(child, "center")))
    ref_solve(child, jobs)
    solved = (world_samples(child, "center"), world_samples(child, "tip"), curve_lists(curve_samples(child, "center")))
    bound = {f for job in jobs for f in job[3]}
    curves = {}
    for key, values in own[2].items():
        if values is None or solved[2][key] is None:
            curves[key] = None if solved[2][key] is None else [v for _t, v in solved[2][key]]
            continue
        curves[key] = [v for _t, v in compose(values, solved[2][key], bound)]
    return compose(own[0], solved[0], bound, True), compose(own[1], solved[1], bound, True), curves


def run_reference(case, solver=None):
    center, tip, curves = reference(case)
    return center, tip, curves, None, False


CASES = ["fk", "constrained", "ik", "moving_object", "scaled", "flips", "two_parents", "released", "self_armature", "driver", "scripted_driver", "physics", "mesh_target"]
FALLBACK = {"scripted_driver", "physics"}
for case in CASES:
    ref = run_reference(case)
    new = run(case, op.solve_bound_bones)
    full = run(case, op.solve_bound_bones, isolate=False)
    checks.check("%s: isolated scene %s" % (case, "refused" if case in FALLBACK else "used"), new[3] == (case not in FALLBACK))
    checks.check("%s: no scene left behind" % case, not new[4] and not full[4])
    f_trans, f_angle = max_errors(ref[0], full[0])
    checks.check("%s: full-scene path matches too" % case, f_trans <= 1e-5 and f_angle <= 1e-4, "%.2e / %.2e" % (f_trans, f_angle))
    trans, angle = max_errors(ref[0], new[0])
    tip_trans, tip_angle = max_errors(ref[1], new[1])
    curve_err = 0.0
    for key, values in ref[2].items():
        other = new[2][key]
        if values is None or other is None:
            curve_err = math.inf if (values is None) != (other is None) else curve_err
            continue
        curve_err = max(curve_err, max(abs(x - y) for x, y in zip(values, other)))
    checks.check(
        "%s: bound bone world matrix matches the per-frame solver" % case,
        trans <= 1e-5 and angle <= 1e-4 and tip_trans <= 1e-5 and tip_angle <= 1e-4,
        "translation %.2e angle %.2e, child bone %.2e / %.2e" % (trans, angle, tip_trans, tip_angle),
    )
    checks.check("%s: curves match on quarter and arbitrary subframes" % case, curve_err <= 1e-5, "max %.2e" % curve_err)

# Solve cache: the same inputs rebuilt from scratch hit (after a verified spot check) and
# give the same keys; any edit to what the solve reads misses.
cache = importlib.import_module("ninesync.sync.solve_cache")


def cached_run(case, edit=None, seed=7):
    parent, child = build(case, seed)
    jobs = jobs_for(case, parent, child)
    if edit is not None:
        jobs = edit(parent, child, jobs) or jobs
    before = dict(cache.stats)
    op.solve_bound_bones(child, jobs)
    after = {k: cache.stats[k] - before[k] for k in before}
    return after, world_samples(child, "center")


def edit_parent_key(parent, child, jobs):
    fc = animation.find_fcurve(parent, 'pose.bones["arm"].location', 1)
    fc.keyframe_points[1].co[1] += 0.05
    fc.update()


def edit_constraint(parent, child, jobs):
    c = parent.pose.bones["hand"].constraints.new("COPY_ROTATION")
    c.target, c.subtarget, c.influence = parent, "helper", 0.3


def edit_still_value(parent, child, jobs):
    parent.pose.bones["hand"].scale = (1.0, 1.2, 1.0)


def edit_rest(parent, child, jobs):
    bpy.context.view_layer.objects.active = parent
    bpy.ops.object.mode_set(mode="EDIT")
    parent.data.edit_bones["arm"].tail.z += 0.1
    bpy.ops.object.mode_set(mode="OBJECT")


def edit_mapping(parent, child, jobs):
    return [(jobs[0][0], parent, parent.pose.bones["arm"], jobs[0][3])]


cache.clear()
first, first_samples = cached_run("constrained")
again, again_samples = cached_run("constrained")
checks.check("cache: first solve misses and is stored", first["misses"] == 1 and first["stored"] == 1, first)
checks.check("cache: same inputs rebuilt hit", again["hits"] == 1 and again["misses"] == 0, again)
trans, angle = max_errors(first_samples, again_samples)
checks.check("cache: a hit writes the same motion", trans <= 1e-6 and angle <= 1e-6, "%.2e / %.2e" % (trans, angle))
for label, edit in (("parent key", edit_parent_key), ("constraint", edit_constraint), ("unkeyed pose value", edit_still_value), ("rest pose", edit_rest), ("mapping", edit_mapping)):
    stats, samples = cached_run("constrained", edit)
    trans, angle = max_errors(reference("constrained", edit)[0], samples)
    checks.check("cache: %s edit misses and solves afresh" % label, stats["hits"] == 0 and stats["misses"] == 1 and trans <= 1e-5 and angle <= 1e-4, "%s | %.2e / %.2e" % (stats, trans, angle))
# A stored solve that no longer matches (a dependency the key cannot see) is caught by
# the spot check and solved again.
cache.clear()
cached_run("fk")
key = next(iter(cache._entries))
for frames in cache._entries[key].values():
    for frame in list(frames):
        location, rotation = frames[frame]
        frames[frame] = (location + Vector((0.01, 0, 0)), rotation)
stats, samples = cached_run("fk")
trans, angle = max_errors(reference("fk")[0], samples)
checks.check("cache: a stale entry is rejected by the spot check", stats["rejected"] == 1 and stats["misses"] == 1 and trans <= 1e-5, "%s | %.2e" % (stats, trans))
op.CACHE = False
cache.clear()
cached_run("fk")
checks.check("cache: off stores nothing", not cache._entries)
op.CACHE = True
cache.clear()

# merge_linear_keys on a lone curve: replaced keys keep their (free) handles shifted by the
# value change, added keys get auto clamped handles, keys outside the span are untouched.
for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj)
holder = bpy.data.objects.new("H", None)
scene.collection.objects.link(holder)
twin = bpy.data.objects.new("H2", None)
scene.collection.objects.link(twin)
rng = random.Random(3)
for obj in (holder, twin):
    rng.seed(3)
    for frame in (0, 10, 20, 30, 40, 70):
        obj.location = (rng.uniform(-1, 1), 0, 0)
        obj.keyframe_insert("location", index=0, frame=frame)
    fc = animation.find_fcurve(obj, "location", 0)
    for point in fc.keyframe_points:
        point.handle_left_type = point.handle_right_type = "FREE"
        point.handle_left = (point.co[0] - 3, point.co[1] + 0.4)
        point.handle_right = (point.co[0] + 3, point.co[1] - 0.25)
    fc.update()
solved = [15, 16, 17, 20, 21, 22, 28, 29, 30, 31, 32]
values = [math.sin(f * 0.3) for f in solved]
for frame, value in zip(solved, values):
    holder.location[0] = value
    holder.keyframe_insert("location", index=0, frame=frame)
ref_curve = animation.find_fcurve(holder, "location", 0)
for point in ref_curve.keyframe_points:
    if solved[0] <= point.co[0] <= solved[-1]:
        point.interpolation = "LINEAR"
ref_curve.update()
new_curve = animation.find_fcurve(twin, "location", 0)
head = op.BOUNDARY_INSERTS
for frame, value in zip(solved[:head], values[:head]):
    twin.location[0] = value
    twin.keyframe_insert("location", index=0, frame=frame)
added = animation.merge_linear_keys(new_curve, solved[head:], values[head:], (solved[0], solved[-1]))
checks.check("merge adds only the missing frames", added == len(solved) - head - 2, added)
# Handles inside the linear span are never evaluated, nor is the left handle of the first
# key after it (that segment is linear too), so only keys, interpolation, the other
# handles outside the span and the first key's left handle have to agree.
after = min((p.co[0] for p in ref_curve.keyframe_points if p.co[0] > solved[-1]), default=None)


def left_counts(point):
    return point.co[0] < solved[0] or point.co[0] == solved[0] or (point.co[0] > solved[-1] and point.co[0] != after)


def right_counts(point):
    return point.co[0] < solved[0] or point.co[0] > solved[-1]


same_points = len(ref_curve.keyframe_points) == len(new_curve.keyframe_points) and all(
    abs(a.co[0] - b.co[0]) < 1e-6
    and abs(a.co[1] - b.co[1]) < 1e-6
    and a.interpolation == b.interpolation
    and (not left_counts(a) or (a.handle_left - b.handle_left).length < 1e-5)
    and (not right_counts(a) or (a.handle_right - b.handle_right).length < 1e-5)
    for a, b in zip(ref_curve.keyframe_points, new_curve.keyframe_points)
)
checks.check("merge matches keyframe_insert on every point that is evaluated", same_points)
err = max(abs(ref_curve.evaluate(t / 8.0) - new_curve.evaluate(t / 8.0)) for t in range(-8, 80 * 8))
checks.check("merge evaluates like keyframe_insert", err < 1e-6, "%.2e" % err)

# Writing into a bone with no curves yet still lands every key.
parent, child = build("fk", 11)
center = child.pose.bones["center"]
for index in range(4):
    for path in ("location", "rotation_quaternion"):
        fc = animation.find_fcurve(child, center.path_from_id(path), index)
        if fc is not None:
            fc.id_data.layers[0].strips[0].channelbag(child.animation_data.action_slot).fcurves.remove(fc)
op.solve_bound_bones(child, [(center, parent, parent.pose.bones["hand"], set(range(4, 20)))])
fc = animation.find_fcurve(child, center.path_from_id("rotation_quaternion"), 3)
checks.check("missing curves are created", fc is not None and len(fc.keyframe_points) == 18, fc and len(fc.keyframe_points))
checks.check("internal frame marker released", not frames_mod.internal_frames_active())

# A parentless bound bone's Child Of places it where the baked solve would (bound_matrix).
parent, child = build("fk", 5)
root = child.pose.bones["all"]
hand = parent.pose.bones["hand"]
child.rotation_euler = (0.3, -0.2, 0.7)  # a moved model root (the armature is offset too)
bpy.context.view_layer.update()
op.constrain_bone(child, root, (1, 2), parent, hand, [(0, (1, 2))])
worst = 0.0
for frame in (0, 7, 19, 33, 52):
    scene.frame_set(frame)
    placed = child.matrix_world @ root.matrix
    expected = op.bound_matrix(child, parent, hand, root)
    worst = max(worst, (placed.to_translation() - expected.to_translation()).length, placed.to_quaternion().rotation_difference(expected.to_quaternion()).angle)
scene.frame_set(0)
checks.check("child of places a root bone like bound_matrix", worst <= 1e-4, "%.2e" % worst)

# Keys straddling a state change: the first key holds until the switch frame, the second
# key's values apply from the switch frame on.
from types import SimpleNamespace

parent, child = build("fk", 5)
center = child.pose.bones["center"]
for path in ("location", "rotation_quaternion"):
    for index in range(4):
        fc = animation.find_fcurve(child, center.path_from_id(path), index)
        if fc is not None:
            fc.id_data.layers[0].strips[0].channelbag(child.animation_data.action_slot).fcurves.remove(fc)
for frame, x in ((10, 0.2), (20, 0.9)):
    center.location = (x, 0, 0)
    child.keyframe_insert(data_path=center.path_from_id("location"), frame=frame)
    child.keyframe_insert(data_path=center.path_from_id("rotation_quaternion"), frame=frame)
op.hold_across_changes(child, center, [SimpleNamespace(frame=10), SimpleNamespace(frame=20)], [(0, None), (15, (1, 2))])
fc = animation.find_fcurve(child, center.path_from_id("location"), 0)
held = all(abs(fc.evaluate(t / 2.0) - (0.2 if t < 30 else 0.9)) < 1e-6 for t in range(20, 41))
checks.check("straddling keys hold, then switch on the state's frame", held, [round(fc.evaluate(f), 3) for f in range(10, 21)])

core.unregister()
checks.finish()
