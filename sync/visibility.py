import bpy

from ..common.animation import fit_strip, remove_action
from ..common.constants import ACTION_PREFIX, CLIP_TRACK_NAME
from ..common.logger import log
from .motion import visibility_hides


VISIBILITY_PATHS = ("hide_viewport", "hide_render")


NLA_NAME_BYTES = 63


# Name of the NLA track a model's show/hide plays from, as Blender stores it: a track
# name holds 63 bytes and Japanese model names easily pass that, so lookups must
# compare against the cut form or they never find the track again.
def visibility_track_name(label):
    raw = (CLIP_TRACK_NAME + "_" + label + "_visibility").encode("utf-8")
    return raw[:NLA_NAME_BYTES].decode("utf-8", "ignore")


def visibility_action_name(label):
    return ACTION_PREFIX + label + "_visibility"


# The bridge visibility action on a mesh, found by its track, or None.
def find_visibility_action(owner, label):
    anim = owner.animation_data
    if anim is None:
        return None
    name = visibility_track_name(label)
    for track in anim.nla_tracks:
        if track.name == name:
            for strip in track.strips:
                if strip.action is not None and strip.action.name.startswith(ACTION_PREFIX):
                    return strip.action
    return None


# Removes the visibility tracks of a model from its meshes and deletes their actions
# once unused. Other tracks and the active action are left alone.
def remove_visibility(meshes, label):
    name = visibility_track_name(label)
    doomed = []
    for mesh in meshes:
        anim = mesh.animation_data
        if anim is None:
            continue
        for track in [t for t in anim.nla_tracks if t.name == name]:
            doomed.extend(s.action for s in track.strips if s.action is not None)
            anim.nla_tracks.remove(track)
    removed = 0
    seen = set()
    for action in doomed:
        try:
            pointer = action.as_pointer()
        except ReferenceError:
            continue
        if pointer not in seen:
            seen.add(pointer)
            removed += 1 if remove_action(action) else 0
    return removed


# Model show/hide from the PMM display track. MMD hides the whole model, and in
# Blender hide_viewport and hide_render are per object, so every mesh of the rig gets
# them. One action with one slot shared by all meshes plays from its own NLA track on
# each, so the user's active action is never replaced and still plays on top. Keys are
# constant: MMD has no fade. A track that never hides removes the old animation and
# leaves the meshes shown.
def apply_visibility(meshes, model, label):
    removed = remove_visibility(meshes, label)
    if not visibility_hides(model.visibility):
        for mesh in meshes:
            for path in VISIBILITY_PATHS:
                setattr(mesh, path, False)
        log.info(
            "visibility '%s': PMM never hides the model; removed %d old action(s)",
            label,
            removed,
        )
        return None
    action = bpy.data.actions.new(visibility_action_name(label))
    slot = action.slots.new(id_type="OBJECT", name=label)
    bag = action.layers.new("Layer").strips.new(type="KEYFRAME").channelbags.new(slot)
    for path in VISIBILITY_PATHS:
        fcurve = bag.fcurves.new(path)
        for key in model.visibility:
            point = fcurve.keyframe_points.insert(
                key.frame, 0.0 if key.visible else 1.0, options={"FAST"}
            )
            point.interpolation = "CONSTANT"
        fcurve.update()
    name = visibility_track_name(label)
    start = model.visibility[0].frame
    for mesh in meshes:
        anim = mesh.animation_data or mesh.animation_data_create()
        track = anim.nla_tracks.new()
        track.name = name
        strip = track.strips.new(action.name, start, action)
        strip.action_slot = slot
        strip.extrapolation = "HOLD"
        fit_strip(strip, action)
    hidden = [k.frame for k in model.visibility if not k.visible]
    log.info(
        "visibility '%s': %d key(s) on %d mesh(es), hidden from frame(s) %s",
        label,
        len(model.visibility),
        len(meshes),
        hidden[:10],
    )
    return action
