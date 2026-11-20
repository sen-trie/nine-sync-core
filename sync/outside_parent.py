import bpy
import time

from mathutils import Matrix

from ..common.animation import find_fcurve, merge_linear_keys
from ..common.constants import FOLLOW_PREFIX
from ..common.logger import log
from ..common.scene_utils import find_armature
from ..common.timing import accumulate, count, phase
from ..pmm import OUTSIDE_FREE
from .motion import outside_bindings, vmd_bone_name
from .frames import internal_frames
from . import solve_cache

PARENT_PREFIX = FOLLOW_PREFIX + "Parent"


# The inverse of a bone's rest matrix in its armature's space. A bone's Child Of is
# evaluated in pose space (the target is brought into the owner armature's space), so the
# armature's own object transform must not be in the inverse, or a model whose root is
# moved would be placed off by that move.
def rest_inverse(armature, bone):
    return armature.data.bones[bone].matrix_local.inverted()


# Only the rest orientation of a bone, inverted. A pose bone's matrix carries its rest
# roll (PMX bones are not aligned to the world axes), which a constraint that follows
# the bone's rotation has to take out, while the bone's position is kept as it is.
def rest_rotation_inverse(armature, bone):
    rest = armature.matrix_world @ armature.data.bones[bone].matrix_local
    return rest.to_quaternion().to_matrix().inverted().to_4x4()


# The Blender target a PMM model index is assigned to, or None.
def target_for_index(settings, index):
    for item in settings.targets:
        head = item.key.split(":", 1)[0]
        if head.isdigit() and int(head) == index and item.target is not None:
            return item
    return None


# The target names behind every parent a model's outside parents point at, so the
# motion fingerprint changes when one of them is reassigned.
def outside_targets(model, settings):
    if not outside_bindings(model) or not settings.sync_model_constraints:
        return ()
    found = []
    for pair in sorted(model.outside_bones):
        item = target_for_index(settings, pair[0])
        found.append((pair, item.target.name if item is not None else None))
    return tuple(found)


def remove_outside_constraints(armature):
    removed = 0
    for pose_bone in armature.pose.bones:
        for constraint in [c for c in pose_bone.constraints if c.name.startswith(PARENT_PREFIX)]:
            pose_bone.constraints.remove(constraint)
            removed += 1
    return removed


def set_interpolation(armature, path, kind, low=None, high=None):
    for index in range(4):
        fcurve = find_fcurve(armature, path, index)
        if fcurve is None:
            continue
        for point in fcurve.keyframe_points:
            if low is None or low <= point.co[0] <= high:
                point.interpolation = kind
        fcurve.update()


# One Child Of constraint on a parentless bone, keyed on where the PMM binds it. A bone
# without a parent has nothing of its own to cancel, so the constraint is exact and
# stays live when the parent model's motion changes. The inverse places the bone the way
# bound_matrix does (its keys relative to the parent bone's posed frame, rest rolls taken
# out), so a prop bound to a hand sits on the hand, not where its rest pose would carry
# it (checked frame by frame against MMD 9.32 on four bindings).
def constrain_bone(armature, own, pair, parent_armature, parent, keys):
    constraint = own.constraints.new(type="CHILD_OF")
    constraint.name = "%s_%d_%d" % (PARENT_PREFIX, pair[0], pair[1])
    constraint.target = parent_armature
    constraint.subtarget = parent.name
    for axis in "xyz":
        setattr(constraint, "use_scale_" + axis, False)
    constraint.set_inverse_pending = False
    constraint.inverse_matrix = (
        rest_rotation_inverse(parent_armature, parent.name)
        @ rest_rotation_inverse(armature, own.name).inverted()
        @ rest_inverse(armature, own.name)
    )
    path = constraint.path_from_id("influence")
    for frame, bound in keys:
        constraint.influence = 1.0 if bound == pair else 0.0
        armature.keyframe_insert(data_path=path, frame=frame)
    set_interpolation(armature, path, "CONSTANT")


# The frames a binding is on, one per frame from where it switches on to where the next
# key switches it off, or to the model's last frame.
def bound_frames(keys, pair, last):
    frames = []
    for index, (frame, bound) in enumerate(keys):
        if bound != pair:
            continue
        end = keys[index + 1][0] if index + 1 < len(keys) else last + 1
        frames.extend(range(frame, max(frame + 1, end)))
    return frames


# Where MMD puts a bone bound to another bone: its keys are the placement relative to
# the parent bone, in the parent's posed frame with no rest offset added (the OP dialog
# shows the key itself as "relative place"). Rest rolls are taken out of both bones
# because MMD's bone axes are the world's.
def bound_matrix(armature, parent_armature, parent, own):
    own_roll = rest_rotation_inverse(armature, own.name).inverted()
    if parent is None:
        # Released into the model's frame the key is an offset from the bone's own rest
        # head (checked against MMD's camera view), unlike a bound bone's key.
        rest_head = Matrix.Translation(own.bone.head_local)
        return armature.matrix_world @ rest_head @ own_roll @ own.matrix_basis
    posed = parent_armature.matrix_world @ parent.matrix
    parent_roll = rest_rotation_inverse(parent_armature, parent.name)
    frame = posed @ parent_roll
    return frame @ own_roll @ own.matrix_basis


# MMD does not blend between two bone keys that sit under different outside-parent
# states (a bone released from its parent, bound, or back to normal): it holds the first
# key until the frame the second key's state starts, and from that frame on uses the
# second key (compared frame by frame with MMD 9.32 on switches both ways). The importer
# blends them, so the first key is made constant and the second key's values are keyed,
# constant, on the switch frame.
def hold_across_changes(armature, own, keys, states):
    def state_at(frame):
        found = None
        for key_frame, state in states:
            if key_frame > frame:
                break
            found = state
        return found

    changes = []
    for index, (frame, state) in enumerate(states):
        if index and state != states[index - 1][1]:
            changes.append(frame)
    frames = [key.frame for key in keys]
    paths = (own.path_from_id("location"), own.path_from_id("rotation_quaternion"))
    for first, second in zip(frames, frames[1:]):
        if second - first <= 1 or state_at(first) == state_at(second):
            continue
        for path in paths:
            set_interpolation(armature, path, "CONSTANT", first, first)
        switch = max(f for f in changes if first < f <= second)
        if switch < second:
            key_values_at(armature, own, paths, second, switch)


# Keys the bone's curve values of frame source on frame target, constant until the next
# key. A channel without a curve is left alone.
def key_values_at(armature, own, paths, source, target):
    for path, size in zip(paths, (3, 4)):
        values = []
        for index in range(size):
            fcurve = find_fcurve(armature, path, index)
            values.append(fcurve.evaluate(source) if fcurve is not None else None)
        if None in values:
            continue
        if size == 3:
            own.location = values
        else:
            own.rotation_quaternion = values
        armature.keyframe_insert(data_path=path, frame=target)
        set_interpolation(armature, path, "CONSTANT", target, target)


# Object types a solve can depend on and still be evaluated on its own; anything else
# (curves, text, volumes...) as a target keeps the solve in the full scene.
CLOSURE_TYPES = {"ARMATURE", "EMPTY", "MESH"}
# Modifiers that need the scene's own time or caches (simulations, bakes).
SCENE_MODIFIERS = {"CLOTH", "SOFT_BODY", "FLUID", "DYNAMIC_PAINT", "COLLISION", "PARTICLE_SYSTEM", "OCEAN", "NODES", "EXPLODE"}


def object_pointers(struct):
    found = []
    for prop in struct.bl_rna.properties:
        if prop.type == "POINTER" and getattr(prop.fixed_type, "identifier", "") == "Object":
            value = getattr(struct, prop.identifier, None)
            if value is not None:
                found.append(value)
    return found


def constraint_targets(constraint):
    found = object_pointers(constraint)
    for target in getattr(constraint, "targets", ()) or ():
        found += object_pointers(target)
    return found


# The objects whose pose or transform the drivers of an ID read, or None when one reads
# anything else (a scene, a mesh, a text) or runs a Python expression, which may look at
# whatever scene it likes.
def driver_targets(id_data):
    anim = getattr(id_data, "animation_data", None)
    found = []
    if anim is None:
        return found
    for fcurve in anim.drivers:
        driver = fcurve.driver
        if driver.type == "SCRIPTED" and not driver.is_simple_expression:
            return None
        for variable in driver.variables:
            for target in variable.targets:
                if target.id is None:
                    continue
                if not isinstance(target.id, bpy.types.Object):
                    return None
                found.append(target.id)
    return found


# Every object the evaluated poses of a solve depend on: the armatures, their parents,
# constraint, modifier and driver targets, followed through. None when the set cannot be
# shown to be complete or to evaluate the same on its own (physics, simulations, scripted
# or non-object drivers, an unsupported object type); the solve then runs in the scene.
def solve_closure(armature, jobs):
    todo = [armature] + [job[1] for job in jobs if job[1] is not None]
    found = {}
    while todo:
        obj = todo.pop()
        key = obj.as_pointer()
        if key in found:
            continue
        if obj.type not in CLOSURE_TYPES or obj.rigid_body is not None or obj.rigid_body_constraint is not None:
            return None
        found[key] = obj
        if obj.parent is not None:
            todo.append(obj.parent)
        for constraint in obj.constraints:
            todo += constraint_targets(constraint)
        if obj.type == "ARMATURE":
            for pose_bone in obj.pose.bones:
                for constraint in pose_bone.constraints:
                    todo += constraint_targets(constraint)
        for modifier in obj.modifiers:
            if modifier.type in SCENE_MODIFIERS:
                return None
            todo += object_pointers(modifier)
        datas = [obj, obj.data] + ([obj.data.shape_keys] if obj.type == "MESH" and obj.data.shape_keys else [])
        for data in datas:
            targets = driver_targets(data) if data is not None else []
            if targets is None:
                return None
            todo += targets
    return list(found.values())


# A scene of its own holding only the solve's objects, so each frame evaluates those and
# not every mesh deformation of the project. Scene.frame_set needs no window, so this works
# in background mode too. The scene's time settings follow the home scene.
def isolated_scene(home, objects):
    work = bpy.data.scenes.new("NineSync_solve")
    work.render.fps = home.render.fps
    work.render.fps_base = home.render.fps_base
    work.render.frame_map_old = home.render.frame_map_old
    work.render.frame_map_new = home.render.frame_map_new
    for obj in objects:
        work.collection.objects.link(obj)
    return work


# Solves what a bone with a parent has to do: MMD drops the bone's own parent while it
# is bound and reads its keys in the outside parent's frame, so the bone sits at
# (how far the parent is from rest) x (its rest matrix) x (its PMM keys). A constraint
# cannot remove the bone's own parent chain, so for each frame the local keys that put
# it there under that chain are solved and written. jobs holds
# (pose bone, parent armature, parent pose bone, frames). Frames are evaluated in an
# isolated scene when solve_closure allows it, in the scene otherwise; either way the
# evaluated objects are read, not the originals.
def solve_bound_bones(armature, jobs):
    scene = bpy.context.scene
    frames = sorted({f for job in jobs for f in job[3]})
    previous = scene.frame_current
    closure = solve_closure(armature, jobs) if ISOLATE else None
    signature = solve_cache.signature(scene, armature, jobs, closure) if CACHE else None
    cached = solve_cache.lookup(signature) if signature is not None else None
    wanted = None
    work = None
    try:
        if closure is not None:
            work = isolated_scene(scene, closure)
            count("outside_parent_isolated_objects", len(closure))
        else:
            count("outside_parent_full_scene")
        if cached is not None:
            # A few frames are solved again; the stored solve is used only if they agree.
            check = evaluate_frames(armature, jobs, solve_cache.verify_frames(jobs, frames), work, scene)
            if solve_cache.agrees(cached, check):
                wanted = cached
                solve_cache.stats["hits"] += 1
                count("outside_parent_cache_hits")
            else:
                solve_cache.stats["rejected"] += 1
                count("outside_parent_cache_rejected")
        if wanted is None:
            wanted = evaluate_frames(armature, jobs, frames, work, scene)
            if signature is not None:
                solve_cache.store(signature, wanted)
                solve_cache.stats["misses"] += 1
                count("outside_parent_cache_misses")
    finally:
        if work is not None:
            bpy.data.scenes.remove(work)
        with internal_frames():
            scene.frame_set(previous)
    with phase("outside_parent_write"):
        for own in {job[0].name: job[0] for job in jobs}.values():
            write_solved(armature, own, wanted[own.name])
    return sum(len(v) for v in wanted.values())


# The solved (location, rotation) of every job's bone on the given frames,
# {bone name: {frame: (location, rotation)}}. Frames step work (the isolated scene) when
# there is one, the scene otherwise. A rotation is turned to the side of the one solved
# on the frame before, when that frame is among them.
def evaluate_frames(armature, jobs, frames, work, scene):
    stepped = work or scene
    wanted = {job[0].name: {} for job in jobs}
    wanted_frames = set(frames)
    by_frame = {}
    for job in jobs:
        for frame in job[3]:
            if frame in wanted_frames:
                by_frame.setdefault(frame, []).append(job)
    for frame in frames:
        if frame not in by_frame:
            continue
        started = time.perf_counter()
        with internal_frames():
            stepped.frame_set(frame)
        accumulate("outside_parent_eval", time.perf_counter() - started)
        count("outside_parent_frame_sets")
        depsgraph = work.view_layers[0].depsgraph if work is not None else bpy.context.view_layer.depsgraph
        arm = armature.evaluated_get(depsgraph)
        world = arm.matrix_world
        for own, parent_armature, parent, _job_frames in by_frame[frame]:
            bone = arm.pose.bones[own.name]
            other = parent_armature.evaluated_get(depsgraph)
            target = other.pose.bones[parent.name] if parent is not None else None
            desired = bound_matrix(arm, other, target, bone)
            chain = world @ (bone.bone.matrix_local)
            if bone.parent is not None:
                chain = world @ (
                    bone.parent.matrix
                    @ bone.parent.bone.matrix_local.inverted()
                    @ bone.bone.matrix_local
                )
            local = chain.inverted() @ desired
            location, rotation, _scale = local.decompose()
            last = wanted[own.name].get(frame - 1)
            if last is not None and last[1].dot(rotation) < 0.0:
                rotation.negate()
            wanted[own.name][frame] = (location, rotation)
    return wanted


# Off only to compare against the full-scene and uncached paths (benchmarks and tests).
ISOLATE = True
CACHE = True


# How many of a bone's first solved frames are keyed with keyframe_insert before the
# rest are written in bulk. Keying a frame inside a Bezier segment splits the segment and
# reshapes its neighbours' handles; the key before the span and the first key's left
# handle are what the curve keeps outside the span, and only the first two inserts touch
# them. Everything after them is linear, so its handles do not count.
BOUNDARY_INSERTS = 2


# The runs of consecutive frames in a sorted list, as (first, last) pairs.
def frame_runs(frames):
    runs = []
    for frame in frames:
        if runs and frame == runs[-1][1] + 1:
            runs[-1][1] = frame
        else:
            runs.append([frame, frame])
    return [tuple(run) for run in runs]


# The bone's own (unbound) pose on the frame before and the frame after each solved run,
# read from its curves before anything is written: MMD plays the bone's own keys right up
# to the frame a binding switches on and again from the frame it switches off, so these
# frames are keyed to keep the solved span from bleeding into the curve around it. A
# bone without curves is at rest. Frames before 0 are never keyed.
def own_pose_edges(armature, own, paths, runs):
    edges = set()
    for low, high in runs:
        if low - 1 >= 0:
            edges.add(low - 1)
        edges.add(high + 1)
    rest = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0))
    found = {}
    for frame in sorted(edges):
        pose = []
        for path, default in zip(paths, rest):
            values = []
            for index, value in enumerate(default):
                fcurve = find_fcurve(armature, path, index)
                values.append(fcurve.evaluate(frame) if fcurve is not None else value)
            pose.append(values)
        found[frame] = pose
    return found


# Writes a bone's solved (location, rotation) per frame into its curves: linear within
# each run of solved frames, switching to and from the bone's own keys on the frames next
# to a run (see own_pose_edges), which are keyed first so the own curve around a run
# keeps its shape (keying inside a Bezier segment subdivides it in place). The key before
# a run and a run's last key hold until the next frame, as MMD switches on the frame.
# The first solved frames go through keyframe_insert (see BOUNDARY_INSERTS), the rest
# straight into the keyframe arrays. Without an active action (or in NLA tweak mode)
# every frame goes through keyframe_insert.
def write_solved(armature, own, solved):
    own.rotation_mode = "QUATERNION"
    if not solved:
        return
    paths = (own.path_from_id("location"), own.path_from_id("rotation_quaternion"))
    frames = sorted(solved)
    runs = frame_runs(frames)
    edges = own_pose_edges(armature, own, paths, runs)
    for frame in sorted(edges):
        own.location, own.rotation_quaternion = edges[frame]
        for path in paths:
            armature.keyframe_insert(data_path=path, frame=frame)
        count("outside_parent_edge_keys", len(paths))
    hold = {low - 1 for low, _high in runs if low - 1 in edges} | {high for _low, high in runs}
    anim = armature.animation_data
    bulk = anim is not None and anim.action is not None and not anim.use_tweak_mode
    head = frames[:BOUNDARY_INSERTS] if bulk else frames
    for frame in head:
        own.location, own.rotation_quaternion = solved[frame]
        for path in paths:
            armature.keyframe_insert(data_path=path, frame=frame)
        count("outside_parent_key_inserts", len(paths))
    rest = frames[len(head):]
    for path, part, size in ((paths[0], 0, 3), (paths[1], 1, 4)):
        for index in range(size):
            fcurve = find_fcurve(armature, path, index)
            if fcurve is None:
                continue
            values = [solved[frame][part][index] for frame in rest]
            count("outside_parent_keys_added", merge_linear_keys(fcurve, rest, values, runs, hold))
            count("outside_parent_keys_written", len(rest))


# live(model, bone) for outside_parent_order: True when apply_outside_parents will bind
# that bone with a constraint, which is when its bone on the mapped armature has no parent.
# Unmapped models and unknown bones count as solved, which keeps the order they need.
def live_bindings(settings, bone_table):
    tables = {}

    def live(model, bone_index):
        if not 0 <= bone_index < len(model.bone_names):
            return False
        if model.index not in tables:
            item = target_for_index(settings, model.index)
            armature = find_armature(item.target) if item is not None else None
            tables[model.index] = bone_table(armature) if armature is not None else None
        table = tables[model.index]
        if table is None:
            return False
        name = model.bone_names[bone_index]
        own = table.get(vmd_bone_name(name)) or table.get(name)
        return own is not None and own.parent is None

    return live


# Binds the bones MMD binds to another model's bone (a weapon riding on the body, a prop
# on a hand). table is the VMD name table of this armature and bone_table builds that of
# a parent's. Returns how many constraints and solved frames were written.
def apply_outside_parents(armature, model, table, settings, bone_table):
    remove_outside_constraints(armature)
    bindings = outside_bindings(model)
    if not bindings or not settings.sync_model_constraints:
        return 0
    tables = {}
    jobs = []
    written = 0
    for bone_index, keys in bindings.items():
        if not 0 <= bone_index < len(model.bone_names):
            continue
        name = model.bone_names[bone_index]
        own = table.get(vmd_bone_name(name)) or table.get(name)
        if own is None:
            log.warning("outside parent of '%s' skipped: no such bone on '%s'", name, armature.name)
            continue
        if 0 <= bone_index < len(model.bones):
            hold_across_changes(armature, own, model.bones[bone_index], keys)
        for pair in sorted({parent for _frame, parent in keys if parent is not None}):
            if pair[0] == OUTSIDE_FREE:
                if own.parent is not None:
                    # Released from its parent from the key's own frame on, as MMD shows it.
                    free = bound_frames(keys, pair, model.last_frame)
                    jobs.append((own, armature, None, set(free)))
                    log.info("outside parent: '%s' released from its parent (solved keys)", name)
                continue
            if pair[0] == model.index:
                parent_armature = armature
            else:
                item = target_for_index(settings, pair[0])
                parent_armature = find_armature(item.target) if item is not None else None
            if parent_armature is None:
                log.warning(
                    "outside parent of '%s' skipped: model %d has no Blender target assigned",
                    name,
                    pair[0],
                )
                continue
            pointer = parent_armature.as_pointer()
            if pointer not in tables:
                tables[pointer] = bone_table(parent_armature)
            parent_name = model.outside_bones.get(pair, "")
            parent_table = tables[pointer]
            parent = parent_table.get(vmd_bone_name(parent_name)) or parent_table.get(parent_name)
            if parent is None or (parent_armature == armature and parent.name == own.name):
                log.warning(
                    "outside parent of '%s' skipped: bone '%s' not found on '%s'",
                    name,
                    parent_name,
                    parent_armature.name,
                )
                continue
            if own.parent is None:
                constrain_bone(armature, own, pair, parent_armature, parent, keys)
                written += 1
                how = "constraint"
            else:
                frames = bound_frames(keys, pair, model.last_frame)
                jobs.append((own, parent_armature, parent, set(frames)))
                how = "solved keys"
            log.info(
                "outside parent: '%s' follows '%s' on '%s' (%s)",
                name,
                parent_name,
                parent_armature.name,
                how,
            )
    if jobs:
        written += solve_bound_bones(armature, jobs)
    return written
