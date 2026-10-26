import bpy
import math

from ..common.animation import bind_slot, fit_strip, is_bridge_action, strips_using
from ..common.logger import log


# The bridge action on the named NLA track, if any.
def find_bridge_clip(owner, track_name):
    anim = owner.animation_data
    if anim is None:
        return None
    for track in anim.nla_tracks:
        if track.name == track_name:
            for strip in track.strips:
                if strip.action is not None and is_bridge_action(strip.action):
                    return strip.action
    return None


# Tries several track names so clips made under older naming are still found.
def find_bridge_clip_any(owner, track_names):
    for track_name in track_names:
        found = find_bridge_clip(owner, track_name)
        if found is not None:
            return found
    return None


# The shape key set with the most blocks, i.e. the body mesh. It owns the
# user-selectable Shapekey Clip; the morph placeholder is skipped.
def primary_key_owner(key_owners, placeholder):
    placeholder_key = getattr(getattr(placeholder, "data", None), "shape_keys", None)
    pool = [
        k for k in key_owners if placeholder_key is None or k != placeholder_key
    ] or list(key_owners)
    if not pool:
        return None
    return max(pool, key=lambda k: len(k.key_blocks))


# Puts the action on the named NLA track (reused or new) at its first frame. If the
# existing track rejects it, retries on a fresh one.
def add_clip_strip(anim, action, track_name):
    start = int(math.floor(action.frame_range[0]))
    reuse = next((t for t in anim.nla_tracks if t.name == track_name), None)
    candidates = [reuse] if reuse is not None else []
    candidates.append(None)
    for track in candidates:
        created = track is None
        if created:
            track = anim.nla_tracks.new()
            track.name = track_name
        try:
            strip = track.strips.new(action.name, start, action)
        except (RuntimeError, ValueError, TypeError) as error:
            log.warning("could not add NLA strip for '%s': %s", action.name, error)
            if created:
                anim.nla_tracks.remove(track)
            continue
        bind_slot(strip, action)
        fit_strip(strip, action)
        log.info(
            "clip '%s' placed on NLA track '%s' at frame %d",
            action.name,
            track.name,
            start,
        )
        return strip
    return None


# Turns the action the importer left active into the stable clip. An existing clip
# gets the new content swapped in with user_remap so references and strips survive.
# Then an NLA strip is ensured and the previously active action is restored.
def place_clip(owner, clip, track_name, clip_name, active_before, keep_active=False):
    anim = owner.animation_data
    fresh = anim.action if anim else None
    if fresh is None:
        log.warning("clip: importer left no action on '%s'", owner.name)
        return clip
    if clip is not None and clip != fresh:
        name = clip.name
        fake = clip.use_fake_user
        clip.name = name + ".old"
        clip.user_remap(fresh)
        fresh.name = name
        fresh.use_fake_user = fake
        clip.use_fake_user = False
        if clip.users == 0:
            bpy.data.actions.remove(clip)
        log.info("clip '%s' content replaced", name)
        clip = fresh
    elif clip is None:
        fresh.name = clip_name
        clip = fresh
        log.info("clip '%s' created", clip.name)
    existing = strips_using(anim, clip)
    for _track, strip in existing:
        bind_slot(strip, clip)
        fit_strip(strip, clip)
    if keep_active:
        bind_slot(anim, clip)
        log.info("clip '%s' stays the active action on '%s'", clip.name, owner.name)
        return clip
    if not existing and add_clip_strip(anim, clip, track_name) is None:
        log.warning(
            "clip '%s' kept as the active action because no NLA strip could be added",
            clip.name,
        )
        return clip
    restore = None
    if (
        active_before is not None
        and active_before != clip
        and not is_bridge_action(active_before)
    ):
        restore = active_before
    anim.action = restore
    if restore is not None:
        bind_slot(anim, restore)
        log.info("restored active action '%s' on '%s'", restore.name, owner.name)
    return clip


# Undoes the temporary fake user set before an import. When the import failed
# (restore=True) it also puts back the previous active action.
def release_stash(stash, restore):
    for owner, action, fake in stash.values():
        if action is None:
            continue
        try:
            action.use_fake_user = fake
        except ReferenceError:
            continue
        if restore and not is_bridge_action(action):
            anim = owner.animation_data
            if anim is not None and (
                anim.action is None or is_bridge_action(anim.action)
            ):
                anim.action = action
