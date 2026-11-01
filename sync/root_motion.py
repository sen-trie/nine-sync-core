import bpy

from ..common.animation import iter_fcurves
from ..common.logger import log
from .mmd_tools_link import mmd_module


# Bones a root transform can move onto: mmd_tools adds parentless helper bones to a
# rig, and those never reach a VMD.
def parentless_bones(armature):
    return [
        b for b in armature.pose.bones if b.parent is None and not b.is_mmd_shadow_bone
    ]


# True when the root empty carries motion or a placement a PMM has nowhere to store.
def has_root_motion(root):
    anim = root.animation_data
    if anim is not None and (anim.action or anim.nla_tracks):
        return True
    return not root.matrix_basis.is_identity


# Keyframes actually placed in Blender, as frame -> interpolation of the key. Nothing
# is sampled between them: MMD has whole frames only, so in-between samples just
# round into clusters of keys.
def explicit_keys(animated, prefixes=None):
    found = {}
    anim = animated.animation_data
    if anim is None or anim.action is None:
        return found
    for fcurve in iter_fcurves(anim.action, anim.action_slot):
        if prefixes is not None and not fcurve.data_path.startswith(prefixes):
            continue
        for point in fcurve.keyframe_points:
            found.setdefault(int(round(point.co[0])), point.interpolation)
    return found


# Reads the combined pose per frame before anything is changed: each bone's matrix
# with the root applied on top, which is what keeps keys already on the bone. Blender
# holds a track past its last key, so frames that only one side has keyed still see
# the other side's held value.
def combined_poses(scene, root, bones, frames):
    saved = scene.frame_current
    poses = {b.name: [] for b in bones}
    try:
        for frame in frames:
            scene.frame_set(frame)
            for bone in bones:
                moved = root.matrix_world @ bone.matrix
                basis = bone.bone.matrix_local.inverted() @ moved
                poses[bone.name].append((frame, basis.decompose()))
    finally:
        scene.frame_set(saved)
    return poses


# Writes one bone's keys. Quaternions stay on the side of their neighbour so a key
# pair never turns the long way round. Scale is keyed only when it differs from rest,
# since it exists for Blender alone: a PMM has no bone scale.
def write_bone_keys(bone, samples, interpolation):
    stretched = any(
        max(abs(s - 1.0) for s in scale) > 1e-3 for _, (_, _, scale) in samples
    )
    previous = None
    for frame, (location, rotation, scale) in samples:
        if previous is not None and previous.dot(rotation) < 0.0:
            rotation = -rotation
        previous = rotation
        bone.location = location
        bone.rotation_quaternion = rotation
        bone.keyframe_insert("location", frame=frame, group=bone.name)
        bone.keyframe_insert("rotation_quaternion", frame=frame, group=bone.name)
        if stretched:
            bone.scale = scale
            bone.keyframe_insert("scale", frame=frame, group=bone.name)
    return stretched


# The new keys take the root key's interpolation, with automatic handles because the
# root's own handles do not map onto several bone channels at once.
def match_interpolation(armature, bones, interpolation):
    prefixes = tuple('pose.bones["%s"]' % b.name for b in bones)
    anim = armature.animation_data
    for fcurve in iter_fcurves(anim.action, anim.action_slot):
        if not fcurve.data_path.startswith(prefixes):
            continue
        for point in fcurve.keyframe_points:
            mode = interpolation.get(int(round(point.co[0])))
            if mode is None:
                continue
            point.interpolation = mode
            point.handle_left_type = "AUTO_CLAMPED"
            point.handle_right_type = "AUTO_CLAMPED"
        fcurve.update()


# Destructive: moves the root empty's motion onto the parentless bones and resets the
# root, so every world position stays and a PMM can carry the motion. Keys are made on
# the root's keyframes and on the bones' existing keyframes only.
def convert_root_motion(scene, root):
    armature = mmd_module("core.model").Model(root).armature()
    if armature is None:
        raise RuntimeError("'%s' has no armature" % root.name)
    anim = root.animation_data
    if anim is not None and anim.nla_tracks:
        raise RuntimeError("'%s' has NLA tracks on its root; bake them to an action first" % root.name)
    bones = parentless_bones(armature)
    if not bones:
        raise RuntimeError("'%s' has no parentless bone" % root.name)
    root_keys = explicit_keys(root)
    prefixes = tuple('pose.bones["%s"]' % b.name for b in bones)
    bone_keys = explicit_keys(armature, prefixes)
    frames = sorted(set(root_keys) | set(bone_keys)) or [scene.frame_current]
    poses = combined_poses(scene, root, bones, frames)
    if anim is not None:
        anim.action = None
    root.matrix_basis.identity()
    root.location = (0.0, 0.0, 0.0)
    root.rotation_euler = (0.0, 0.0, 0.0)
    root.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
    root.scale = (1.0, 1.0, 1.0)
    stretched = False
    for bone in bones:
        stretched = write_bone_keys(bone, poses[bone.name], root_keys) or stretched
    match_interpolation(armature, bones, root_keys)
    scene.frame_set(scene.frame_current)
    bpy.context.view_layer.update()
    log.info(
        "converted root motion of '%s' onto %d bone(s), %d key(s)",
        root.name,
        len(bones),
        len(frames),
    )
    return len(frames), stretched
