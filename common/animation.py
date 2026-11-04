import bpy
import math

from .constants import ACTION_PREFIX, FRAME_EPSILON
from .logger import log


# Fcurves live in layers, strips and channelbags, one bag per slot. Always walks those:
# mmd_tools patches an Action.fcurves that only returns the first slot's curves. Give
# a slot to read just that ID's curves from an action shared between IDs.
def iter_fcurves(action, slot=None):
    found = []
    for layer in action.layers:
        for strip in layer.strips:
            for bag in getattr(strip, "channelbags", []):
                if slot is None or bag.slot_handle == slot.handle:
                    found.extend(bag.fcurves)
    return found


# The fcurve for one channel on the owner's active action, or None.
def find_fcurve(owner, data_path, index):
    data = owner.animation_data
    action = data.action if data else None
    if action is None:
        return None
    for fcurve in iter_fcurves(action, data.action_slot):
        if fcurve.data_path == data_path and fcurve.array_index == index:
            return fcurve
    return None


# Creates a missing fcurve by keying the property, because restore_points needs a
# curve to exist before it can re-insert saved keys. Index 0 is retried as -1 so
# non-array properties work too.
def create_fcurve(owner, data_path, index, group, frame):
    for attempt in ((index, -1) if index == 0 else (index,)):
        try:
            if group:
                owner.keyframe_insert(
                    data_path=data_path, index=attempt, frame=frame, group=group
                )
            else:
                owner.keyframe_insert(data_path=data_path, index=attempt, frame=frame)
        except (TypeError, RuntimeError, ValueError, KeyError):
            continue
        found = find_fcurve(owner, data_path, index)
        if found is not None:
            return found
    return None


# Replaces every key with linear ones. Used for shape keys the bridge computes
# itself.
def rewrite_fcurve(fcurve, frames, values):
    points = fcurve.keyframe_points
    try:
        points.clear()
    except AttributeError:
        while len(points):
            points.remove(points[0], fast=True)
    for frame, value in zip(frames, values):
        point = points.insert(frame, value, options={"FAST"})
        point.interpolation = "LINEAR"
    fcurve.update()


# Saves the user's own keys that lie outside the PMM frame span, so a re-sync only
# overwrites the range the PMM covers. 'sources' overrides which action is read
# (clip workflow), 'keep_all' marks paths saved in full. Constraint curves are
# skipped because the bridge recreates them.
def snapshot_outside(owners, low, high, sources=None, keep_all=None):
    saved = []
    for owner in owners:
        key = owner.as_pointer()
        slot = None
        if sources is not None and key in sources:
            action = sources[key]
        else:
            data = owner.animation_data
            action = data.action if data else None
            slot = data.action_slot if data else None
        if action is None:
            continue
        for fcurve in iter_fcurves(action, slot):
            if fcurve.data_path.startswith("constraints["):
                continue
            points = []
            everything = keep_all is not None and keep_all(fcurve.data_path)
            for point in fcurve.keyframe_points:
                frame = point.co[0]
                if everything or frame < low - FRAME_EPSILON or frame > high + FRAME_EPSILON:
                    points.append(
                        (
                            frame,
                            point.co[1],
                            point.interpolation,
                            point.easing,
                            tuple(point.handle_left),
                            tuple(point.handle_right),
                            point.handle_left_type,
                            point.handle_right_type,
                            point.type,
                        )
                    )
            if points:
                group = fcurve.group.name if fcurve.group else ""
                saved.append(
                    (owner, fcurve.data_path, fcurve.array_index, group, points)
                )
    return saved


# Re-inserts keys saved by snapshot_outside, recreating the fcurve when the import
# discarded it. Returns how many keys were restored.
def restore_points(saved):
    restored = 0
    for owner, data_path, index, group, points in saved:
        fcurve = find_fcurve(owner, data_path, index)
        if fcurve is None:
            fcurve = create_fcurve(owner, data_path, index, group, points[0][0])
        if fcurve is None:
            log.warning(
                "could not restore %d key(s) on '%s' %s[%d]",
                len(points),
                owner.name,
                data_path,
                index,
            )
            continue
        for (
            frame,
            value,
            interpolation,
            easing,
            left,
            right,
            left_type,
            right_type,
            key_type,
        ) in points:
            point = fcurve.keyframe_points.insert(
                frame, value, options={"FAST"}, keyframe_type=key_type
            )
            point.interpolation = interpolation
            point.easing = easing
            point.handle_left_type = left_type
            point.handle_right_type = right_type
            point.handle_left = left
            point.handle_right = right
        fcurve.update()
        restored += len(points)
    return restored


# (min, max) of the frames. Empty input gives (inf, -inf) so every existing key
# counts as outside the span and is kept.
def frame_span(frames):
    frames = list(frames)
    if not frames:
        return math.inf, -math.inf
    return min(frames), max(frames)


# Blender 5 actions need a slot bound to the animated ID. Picks the first suitable
# one, or the action's first slot. Does nothing on versions without slots.
def bind_slot(target, action):
    if not hasattr(target, "action_slot"):
        return
    suitable = getattr(target, "action_suitable_slots", None)
    slot = None
    if suitable is not None and len(suitable):
        slot = suitable[0]
    else:
        slots = getattr(action, "slots", None)
        if slots is not None and len(slots):
            slot = slots[0]
    if slot is None:
        return
    try:
        target.action_slot = slot
    except (AttributeError, TypeError, RuntimeError, ValueError):
        pass


# (track, strip) pairs on an NLA stack that play the given action.
def strips_using(anim, action):
    found = []
    for track in anim.nla_tracks:
        for strip in track.strips:
            if strip.action == action:
                found.append((track, strip))
    return found


# Resizes a strip to the action's range. Blender rejects start > end mid-update, so
# the assignment order is retried the other way round.
def fit_strip(strip, action):
    start, end = action.frame_range
    if end <= start:
        end = start + 1.0
    try:
        strip.action_frame_end = end
        strip.action_frame_start = start
    except (AttributeError, TypeError, RuntimeError, ValueError):
        try:
            strip.action_frame_start = start
            strip.action_frame_end = end
        except (AttributeError, TypeError, RuntimeError, ValueError):
            log.warning("could not fit NLA strip '%s' to %s-%s", strip.name, start, end)


# Bridge actions are recognised only by the MMDBridge_ name prefix; that is what
# separates them from the user's own actions.
def is_bridge_action(action):
    return action is not None and action.name.startswith(ACTION_PREFIX)


# Deletes unused actions with the prefix. Anything still referenced stays.
def purge_actions(prefix):
    removed = 0
    for action in list(bpy.data.actions):
        if action.name.startswith(prefix) and action.users == 0:
            bpy.data.actions.remove(action)
            removed += 1
    if removed:
        log.info("purged %d unused action(s) with prefix '%s'", removed, prefix)


# Drops the fake user, then removes the action if nothing else uses it. Tolerates
# references freed by an earlier removal.
def remove_action(action):
    try:
        action.use_fake_user = False
        if action.users == 0:
            bpy.data.actions.remove(action)
            return True
    except ReferenceError:
        pass
    return False


# Removes bridge strips from NLA tracks and deletes tracks left empty. User strips
# and tracks are untouched, and so are tracks named in keep, which another part owns.
def clear_bridge_nla(owners, keep=()):
    for owner in owners:
        data = owner.animation_data
        if data is None:
            continue
        for track in list(data.nla_tracks):
            if track.name in keep:
                continue
            stale = [strip for strip in track.strips if is_bridge_action(strip.action)]
            for strip in stale:
                track.strips.remove(strip)
            if stale and len(track.strips) == 0:
                name = track.name
                data.nla_tracks.remove(track)
                log.info("removed empty NLA track '%s' from '%s'", name, owner.name)


# Gives actions created by the importer the bridge prefix so later syncs and Clean
# can recognise them.
def tag_actions(owners, base):
    for owner in owners:
        data = owner.animation_data
        action = data.action if data else None
        if action is not None and not action.name.startswith(ACTION_PREFIX):
            action.name = ACTION_PREFIX + base + "_" + owner.name


# Every ID that can carry animation for an object: the object, camera or light data,
# and shape keys.
def animated_ids(obj):
    ids = [obj]
    data = getattr(obj, "data", None)
    if obj.type in {"CAMERA", "LIGHT"} and data is not None:
        ids.append(data)
    shape_keys = getattr(data, "shape_keys", None)
    if shape_keys is not None:
        ids.append(shape_keys)
    return ids


# Deduplicated animated IDs for several objects.
def collect_owners(objects):
    owners = []
    for obj in objects:
        for item in animated_ids(obj):
            if item not in owners:
                owners.append(item)
    return owners


# Detaches actions and returns pose bones and shape keys to rest before an import,
# so stale values do not leak into the result. keep_keys leaves shape key actions
# alone when clips manage them.
def reset_animation(targets, keep_keys=False):
    for obj in targets:
        if obj.animation_data:
            obj.animation_data.action = None
        data = getattr(obj, "data", None)
        if obj.type in {"CAMERA", "LIGHT"} and data is not None and data.animation_data:
            data.animation_data.action = None
        shape_keys = getattr(data, "shape_keys", None)
        if shape_keys is not None and not keep_keys:
            if shape_keys.animation_data:
                shape_keys.animation_data.action = None
            for block in shape_keys.key_blocks:
                if block.name != "Basis":
                    block.value = 0.0
        if obj.type == "ARMATURE":
            for bone in obj.pose.bones:
                bone.location = (0.0, 0.0, 0.0)
                bone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
                bone.rotation_euler = (0.0, 0.0, 0.0)
                bone.scale = (1.0, 1.0, 1.0)


# Logs what the importer left on an object. Diagnoses 'nothing animated' reports
# from the support log.
def describe_action(label, obj):
    data = obj.animation_data if obj else None
    action = data.action if data else None
    if action is None:
        log.warning(
            "%s: '%s' has no action after import", label, obj.name if obj else None
        )
        return
    fcurves = iter_fcurves(action)
    log.info(
        "%s: '%s' action '%s' fcurves=%d frame_range=%s",
        label,
        obj.name,
        action.name,
        len(fcurves),
        tuple(action.frame_range),
    )
