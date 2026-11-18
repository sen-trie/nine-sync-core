import bpy
from mathutils import Matrix

from ..common.animation import find_fcurve
from ..common.constants import FOLLOW_PREFIX
from ..common.logger import log
from ..common.scene_utils import find_armature
from ..pmm import OUTSIDE_FREE
from .motion import outside_bindings, vmd_bone_name
from .frames import internal_frames

PARENT_PREFIX = FOLLOW_PREFIX + "Parent"


# The inverse of a bone's rest matrix in world space. MMD's outside parent moves a bone
# by how far its parent has moved from rest, and a Child Of constraint on this inverse
# does the same.
def rest_inverse(armature, bone):
    return (armature.matrix_world @ armature.data.bones[bone].matrix_local).inverted()


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
# stays live when the parent model's motion changes.
def constrain_bone(armature, own, pair, parent_armature, parent, keys):
    constraint = own.constraints.new(type="CHILD_OF")
    constraint.name = "%s_%d_%d" % (PARENT_PREFIX, pair[0], pair[1])
    constraint.target = parent_armature
    constraint.subtarget = parent.name
    for axis in "xyz":
        setattr(constraint, "use_scale_" + axis, False)
    constraint.set_inverse_pending = False
    constraint.inverse_matrix = rest_inverse(parent_armature, parent.name)
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
# key's pose until the second. The importer blends them, so the first key is made
# constant here.
def hold_across_changes(armature, own, keys, states):
    def state_at(frame):
        found = None
        for key_frame, state in states:
            if key_frame > frame:
                break
            found = state
        return found

    frames = [key.frame for key in keys]
    paths = (own.path_from_id("location"), own.path_from_id("rotation_quaternion"))
    for first, second in zip(frames, frames[1:]):
        if second - first > 1 and state_at(first) != state_at(second):
            for path in paths:
                set_interpolation(armature, path, "CONSTANT", first, first)


# MMD takes a bone out of its parent one frame after the outside-parent key that releases
# it: on that key's own frame the bone is still where its previous key left it, and the
# released pose starts on the next frame. The key on that frame is therefore replaced by
# the previous key's pose.
def hold_release_frame(armature, own, frame, before):
    for path, count in ((own.path_from_id("location"), 3), (own.path_from_id("rotation_quaternion"), 4)):
        values = []
        for index in range(count):
            fcurve = find_fcurve(armature, path, index)
            values.append(fcurve.evaluate(before) if fcurve is not None else None)
        if None in values:
            continue
        if count == 3:
            own.location = values
        else:
            own.rotation_quaternion = values
        armature.keyframe_insert(data_path=path, frame=frame)
        set_interpolation(armature, path, "LINEAR", frame, frame)


# Solves what a bone with a parent has to do: MMD drops the bone's own parent while it
# is bound and reads its keys in the outside parent's frame, so the bone sits at
# (how far the parent is from rest) x (its rest matrix) x (its PMM keys). A constraint
# cannot remove the bone's own parent chain, so for each frame the local keys that put
# it there under that chain are solved and written. jobs holds
# (pose bone, parent armature, parent pose bone, frames).
def solve_bound_bones(armature, jobs):
    scene = bpy.context.scene
    frames = sorted({f for job in jobs for f in job[3]})
    wanted = {job[0].name: {} for job in jobs}
    world = armature.matrix_world
    previous = scene.frame_current
    try:
        for frame in frames:
            with internal_frames():
                scene.frame_set(frame)
            for own, parent_armature, parent, job_frames in jobs:
                if frame not in job_frames:
                    continue
                desired = bound_matrix(armature, parent_armature, parent, own)
                chain = world @ (own.bone.matrix_local)
                if own.parent is not None:
                    chain = world @ (
                        own.parent.matrix
                        @ own.parent.bone.matrix_local.inverted()
                        @ own.bone.matrix_local
                    )
                local = chain.inverted() @ desired
                location, rotation, _scale = local.decompose()
                last = wanted[own.name].get(frame - 1)
                if last is not None and last[1].dot(rotation) < 0.0:
                    rotation.negate()
                wanted[own.name][frame] = (location, rotation)
    finally:
        with internal_frames():
            scene.frame_set(previous)
    for own, _pa, _pb, _frames in jobs:
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
                set_interpolation(armature, path, "LINEAR", low, high)
    return sum(len(v) for v in wanted.values())


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
    releases = []
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
                    free = bound_frames(keys, pair, model.last_frame)
                    earlier = [k.frame for k in model.bones[bone_index] if free and k.frame < free[0]]
                    if free and earlier:
                        releases.append((own, free[0], max(earlier)))
                        free = free[1:]
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
    for own, frame, before in releases:
        hold_release_frame(armature, own, frame, before)
    return written
