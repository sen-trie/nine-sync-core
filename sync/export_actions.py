from contextlib import contextmanager

import bpy

from ..common.animation import iter_fcurves
from ..common.constants import TEMP_PREFIX
from ..common.logger import log


EPSILON = 1e-6
KEY_VECTORS = ("co", "handle_left", "handle_right")
KEY_ENUMS = ("interpolation", "handle_left_type", "handle_right_type", "easing")


# What plays on an ID, bottom to top: (action, slot, frame offset) for each unmuted NLA
# strip, then the active action. mmd_tools' exporter reads only the active action, and a
# sync keeps its motion in NLA clips, so the export has to gather them itself. A strip
# that is scaled or repeated cannot be copied key for key; it is left out and named in
# skipped.
def playing_sources(owner):
    anim = getattr(owner, "animation_data", None)
    sources, skipped = [], []
    if anim is None:
        return sources, skipped
    if anim.use_nla:
        tracks = [t for t in anim.nla_tracks if not t.mute]
        solo = [t for t in tracks if t.is_solo]
        for track in solo or tracks:
            for strip in track.strips:
                if strip.mute or strip.action is None:
                    continue
                if abs(strip.scale - 1.0) > EPSILON or abs(strip.repeat - 1.0) > EPSILON:
                    skipped.append(strip.name)
                    continue
                offset = strip.frame_start - strip.action_frame_start
                sources.append((strip.action, strip.action_slot, offset))
    if anim.action is not None:
        sources.append((anim.action, anim.action_slot, 0.0))
    return sources, skipped


# (data_path, index) pairs something plays on an ID. A channel in here already has
# motion, so its current value is no initial pose.
def animated_channels(owner):
    sources, _ = playing_sources(owner)
    return {
        (fcurve.data_path, fcurve.array_index)
        for action, slot, _offset in sources
        for fcurve in iter_fcurves(action, slot)
    }


# The rotation property mmd_tools' exporter reads for a bone in its rotation mode.
def rotation_path(pose_bone):
    if pose_bone.rotation_mode == "QUATERNION":
        return "rotation_quaternion"
    if pose_bone.rotation_mode == "AXIS_ANGLE":
        return "rotation_axis_angle"
    return "rotation_euler"


def posed(pose_bone):
    if any(abs(v) > EPSILON for v in pose_bone.location):
        return True
    mode = pose_bone.rotation_mode
    if mode == "QUATERNION":
        w, x, y, z = pose_bone.rotation_quaternion
        return abs(abs(w) - 1.0) > EPSILON or any(abs(v) > EPSILON for v in (x, y, z))
    if mode == "AXIS_ANGLE":
        return abs(pose_bone.rotation_axis_angle[0]) > EPSILON
    return any(abs(v) > EPSILON for v in pose_bone.rotation_euler)


# Channels of a posed bone that has no keys at all. mmd_tools exports only bones with a
# curve, and gives a partly keyed bone's other channels their current value already.
def bone_channels(armature):
    animated = animated_channels(armature)
    channels = []
    for pose_bone in armature.pose.bones:
        if getattr(pose_bone, "is_mmd_shadow_bone", False) or not posed(pose_bone):
            continue
        location = pose_bone.path_from_id("location")
        rotation = pose_bone.path_from_id(rotation_path(pose_bone))
        if any(path in (location, rotation) for path, _index in animated):
            continue
        channels += [(location, i, v) for i, v in enumerate(pose_bone.location)]
        values = getattr(pose_bone, rotation_path(pose_bone))
        channels += [(rotation, i, v) for i, v in enumerate(values)]
    return channels


# Unkeyed morph sliders that are not at zero.
def morph_channels(keys):
    animated = animated_channels(keys)
    channels = []
    for block in keys.key_blocks[1:]:
        path = block.path_from_id("value")
        if abs(block.value) > EPSILON and (path, 0) not in animated:
            channels.append((path, 0, block.value))
    return channels


# Camera rig channels mmd_tools exports: the root's position, angle and view angle, and
# the camera's distance. An unkeyed rig gives no camera keys at all, so MMD falls back
# to its default camera.
def camera_channels(root, camera):
    entries = {}
    animated = animated_channels(root)
    wanted = [("location", i, v) for i, v in enumerate(root.location)]
    wanted += [("rotation_euler", i, v) for i, v in enumerate(root.rotation_euler)]
    wanted.append(("mmd_camera.angle", 0, root.mmd_camera.angle))
    channels = [c for c in wanted if c[:2] not in animated]
    if channels:
        entries[root] = channels
    if camera is not None and ("location", 1) not in animated_channels(camera):
        entries[camera] = [("location", 1, camera.location.y)]
    return entries


# Copies every key of one curve, handles and interpolation included, shifted by offset.
def copy_keys(source, target, offset):
    count = len(source.keyframe_points)
    points = target.keyframe_points
    points.clear()
    points.add(count)
    for name in KEY_VECTORS:
        values = [0.0] * (count * 2)
        source.keyframe_points.foreach_get(name, values)
        if offset:
            values[0::2] = [x + offset for x in values[0::2]]
        points.foreach_set(name, values)
    for name in KEY_ENUMS:
        values = [0] * count
        source.keyframe_points.foreach_get(name, values)
        points.foreach_set(name, values)
    target.extrapolation = source.extrapolation
    target.update()


# Fills the temp action assigned to owner with what plays on it, lower sources replaced
# channel by channel by higher ones, as Blender plays a full-influence Replace strip or
# the active action over the clips below.
def merge_sources(temp, owner, sources):
    for action, slot, offset in sources:
        for fcurve in iter_fcurves(action, slot):
            target = temp.fcurve_ensure_for_datablock(owner, fcurve.data_path, index=fcurve.array_index)
            copy_keys(fcurve, target, offset)


# For the length of the block, gives each ID one active action holding everything that
# plays on it (its clips and its active action) plus a frame 0 key for each listed
# unkeyed channel, which is what mmd_tools' exporter can read. Then every ID is put back
# as it was. The user's actions are only read; the temp actions are deleted afterwards.
# entries maps an ID to its initial (data_path, index, value) rows, empty for none.
# Returns, through the yielded list, warnings for clips that could not be read.
# Owners in always get a temp action even with nothing to merge, so the block may edit
# it without touching the user's own.
@contextmanager
def export_actions(entries, always=()):
    restore = []
    warnings = []
    try:
        for owner, channels in entries.items():
            anim = owner.animation_data
            if anim is not None and anim.use_tweak_mode:
                warnings.append("'%s' is in NLA tweak mode, so only its tweaked clip is exported" % owner.name)
                continue
            sources, skipped = playing_sources(owner)
            for name in skipped:
                warnings.append("'%s': clip '%s' is scaled or repeated and was not exported" % (owner.name, name))
            clips = len(sources) - (1 if anim is not None and anim.action is not None else 0)
            if not channels and not clips and owner not in always:
                continue
            created = anim is None
            if created:
                anim = owner.animation_data_create()
            original, slot = anim.action, anim.action_slot
            temp = bpy.data.actions.new(TEMP_PREFIX + "export")
            restore.append((owner, created, original, slot, temp))
            anim.action = temp
            merge_sources(temp, owner, sources)
            for path, index, value in channels:
                fcurve = temp.fcurve_ensure_for_datablock(owner, path, index=index)
                fcurve.keyframe_points.insert(0, value, options={"FAST"})
            log.info(
                "export: '%s' read from %d clip(s)%s and %d initial key(s)",
                owner.name,
                clips,
                " + its action" if original is not None else "",
                len(channels),
            )
        yield warnings
    finally:
        for owner, created, original, slot, temp in reversed(restore):
            anim = owner.animation_data
            if anim is not None:
                anim.action = original
                if original is not None and slot is not None:
                    anim.action_slot = slot
                if created and not anim.nla_tracks:
                    owner.animation_data_clear()
            bpy.data.actions.remove(temp)
