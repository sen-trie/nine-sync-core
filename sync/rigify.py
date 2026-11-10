from typing import NamedTuple

import bpy

from ..common.animation import iter_fcurves, remove_action
from ..common.constants import ACTION_PREFIX, CLIP_TRACK_NAME
from ..common.logger import log
from ..common.rigify_map import BIND_PROPERTY, CONTROLS, LIMB_OWNERS, RIGIFY_FORMAT
from ..pmm import bone_track_moves
from .motion import vmd_bone_name
from .clips import add_clip_strip


# A rig made by MMD Tools Append's Rigify conversion: a Rigify rig that still holds the
# MMD bones, with the bind switch on its torso bone. Same test that add-on uses.
def is_rigify_armature(armature):
    if armature is None or armature.type != "ARMATURE" or not armature.data.get("rig_id"):
        return False
    torso = armature.pose.bones.get("torso")
    return torso is not None and BIND_PROPERTY in torso


# What a Rigify target adds to a motion part's hash, so a new table, an edited mapping or
# turning the remap off re-runs it. None for any other rig, which keeps its old hash. No
# side effects: the plan calls it.
def rigify_signature(armature, row):
    if not is_rigify_armature(armature):
        return None
    if row is not None and not row.rigify_remap:
        return ("rigify", "off")
    edits = tuple(sorted((s.bone, s.control) for s in row.rigify_slots if s.control != s.base)) if row is not None else ()
    return ("rigify", RIGIFY_FORMAT, edits)


# Limb group -> "IK" or "FK" as the rig's switch is set; groups without a switch (torso,
# face) are absent.
def limb_modes(rig):
    modes = {}
    for group, owner in LIMB_OWNERS.items():
        bone = rig.pose.bones.get(owner)
        value = bone.get("IK_FK") if bone is not None else None
        if value is not None:
            modes[group] = "IK" if value < 0.5 else "FK"
    return modes


def mmd_bones_by_name(rig):
    return {pb.mmd_bone.name_j: pb.name for pb in rig.pose.bones if pb.mmd_bone.name_j}


# Kind and limb of a control that is not in the table, from its name: Rigify names IK and
# FK controls with "_ik" and "_fk", limbs by their bones, sides with .L and .R.
def infer_kind(control):
    low = control.lower()
    if "_ik" in low or low.startswith(("foot_ik", "hand_ik")):
        return "ik"
    return "fk" if "_fk" in low else "any"


def infer_group(control):
    low = control.lower()
    side = "_L" if low.endswith(".l") else "_R" if low.endswith(".r") else ""
    if side and any(t in low for t in ("thigh", "shin", "foot", "toe")):
        return "LEG" + side
    if side and any(t in low for t in ("upper_arm", "forearm", "hand")):
        return "ARM" + side
    return "TORSO"


# One mapping: the Rigify control ("" when switched off), the rig's MMD bone (by Blender
# name) it follows, None when the table's bone is missing from the rig, the limb group and
# IK/FK kind, the MMD name the table gave (or None), and whether the user changed it.
class RemapRow(NamedTuple):
    control: str
    bone: object
    group: str
    kind: str
    name_j: object
    custom: bool


# The mapping as it applies: the table, each row's control replaced by the user's choice
# for that MMD bone, then the mappings the user added for bones the table lacks. The MMD
# bone is fixed; the control is what is chosen. An MMD bone may have several controls.
# Read only.
def resolved_rows(rig, item):
    by_j = mmd_bones_by_name(rig)
    slots = list(item.rigify_slots) if item is not None else []
    chosen = {s.base: s.control for s in slots if s.base}
    rows = []
    for name_j, control, group, kind in CONTROLS:
        use = chosen.get(control, control)
        if use == control:
            rows.append(RemapRow(control, by_j.get(name_j), group, kind, name_j, False))
        else:
            rows.append(RemapRow(use, by_j.get(name_j), infer_group(use), infer_kind(use), name_j, True))
    for slot in slots:
        if not slot.base:
            rows.append(RemapRow(slot.control, slot.bone, infer_group(slot.control), infer_kind(slot.control), None, True))
    return rows


# "active" (carried), "off" (no control chosen), "inactive" (the limb is in the other
# mode), "no control" or "no bone" (the rig lacks it).
def row_status(rig, modes, row):
    if not row.control:
        return "off"
    if row.control not in rig.pose.bones:
        return "no control"
    if row.bone is None or row.bone not in rig.pose.bones:
        return "no bone"
    mode = modes.get(row.group)
    if mode is not None and row.kind != "any" and (row.kind == "ik") != (mode == "IK"):
        return "inactive"
    return "active"


# (control, MMD bone) pairs to carry, parents first, for the limbs as they are switched.
# A control follows one MMD bone: when two are given the same control, the first keeps it.
def transfer_pairs(rig, item=None):
    modes = limb_modes(rig)
    pairs = {}
    for row in resolved_rows(rig, item):
        if row_status(rig, modes, row) == "active":
            pairs.setdefault(row.control, row.bone)
    return sorted(pairs.items(), key=lambda kv: len(rig.pose.bones[kv[0]].parent_recursive))


# Controls that two or more carried rows share: a conflict, as a control can only follow
# one MMD bone.
def shared_controls(rig, item):
    modes = limb_modes(rig)
    seen, shared = set(), set()
    for row in resolved_rows(rig, item):
        if row_status(rig, modes, row) in ("off", "no control"):
            continue
        if row.control in seen:
            shared.add(row.control)
        seen.add(row.control)
    return shared


# The MMD name of a rig bone for display, the Blender name when it has none.
def mmd_label(rig, bone):
    if not bone:
        return "-"
    pose_bone = rig.pose.bones.get(bone)
    return (pose_bone.mmd_bone.name_j if pose_bone is not None else "") or bone


# The mapping as a tree for display: each node is a mapped control placed under the
# nearest mapped control above it in the rig, grouped by limb. Nodes are
# (MMD bone, control, kind, status, group, children); groups are (group, mode, [nodes]).
def mapping_tree(rig, item=None):
    modes = limb_modes(rig)
    nodes = {}
    for row in resolved_rows(rig, item):
        label = mmd_label(rig, row.bone) if row.bone is not None else (row.name_j or "-")
        key = row.control or "~%s" % label
        nodes.setdefault(key, (label, row.control or "-", row.kind, row_status(rig, modes, row), row.group, []))
    present = {c for c in nodes if c in rig.pose.bones}

    def parent_of(control):
        bone = rig.pose.bones[control].parent
        while bone is not None:
            if bone.name in present:
                return bone.name
            bone = bone.parent
        return None

    roots = {}
    for key, node in nodes.items():
        parent = parent_of(key) if key in present else None
        if parent is not None:
            nodes[parent][5].append(node)
        else:
            roots.setdefault(node[4], []).append(node)
    order = ["TORSO", "FACE", "ARM_L", "ARM_R", "LEG_L", "LEG_R"]
    groups = [(g, modes.get(g), roots[g]) for g in order if g in roots]
    groups += [(g, modes.get(g), n) for g, n in roots.items() if g not in order]
    return groups


# Rigify controls a mapping can use: the table's, and the rig's own bones that are none
# of the generated helpers (DEF, ORG, MCH, VIS, WGT), the MMD bones or the separators.
def candidate_controls(rig):
    mmd = {pb.name for pb in rig.pose.bones if pb.mmd_bone.name_j}
    found = [row[1] for row in CONTROLS if row[1] in rig.pose.bones]
    extra = sorted(
        pb.name
        for pb in rig.pose.bones
        if pb.name not in mmd
        and not pb.name.startswith(("DEF-", "ORG-", "MCH-", "VIS", "WGT", "-", "_"))
        and pb.name not in found
    )
    return found + extra


# One slot per table row whose MMD bone the rig has, holding the control chosen for that
# bone (base is the table's). A slot the user has not touched follows the table. Slots
# the user added for other bones (base empty) are kept.
def sync_slots(item, rig):
    by_j = mmd_bones_by_name(rig)
    wanted = {control: by_j[name_j] for name_j, control, _g, _k in CONTROLS if name_j in by_j}
    slots = {s.base: s for s in item.rigify_slots if s.base}
    for control, bone in wanted.items():
        slot = slots.get(control)
        if slot is None:
            slot = item.rigify_slots.add()
            slot.base = slot.control = control
        slot.bone = bone
    for index in reversed(range(len(item.rigify_slots))):
        slot = item.rigify_slots[index]
        if slot.base and slot.base not in wanted and slot.control == slot.base:
            item.rigify_slots.remove(index)


# The slots whose MMD bone or control match every word of the search, as keys (bone and
# base). No search lists the slots with a control.
def matching_slots(rig, item, text):
    terms = text.lower().split()
    found = []
    for slot in item.rigify_slots:
        if not terms:
            if slot.control:
                found.append(slot_key(slot))
            continue
        hay = " ".join((slot.bone, mmd_label(rig, slot.bone), slot.control)).lower()
        if all(t in hay for t in terms):
            found.append(slot_key(slot))
    return found


# "IK" or "FK" for a slot whose MMD bone has more than one slot (an IK and an FK control),
# so the editor tells them apart; "" otherwise. Kind comes from the table, or the name.
def slot_tag(item, slot):
    if sum(1 for s in item.rigify_slots if s.bone == slot.bone) < 2:
        return ""
    kinds = {control: kind for _n, control, _g, kind in CONTROLS}
    kind = kinds.get(slot.base) or infer_kind(slot.control or slot.base)
    return kind.upper() if kind != "any" else ""


def slot_key(slot):
    return slot.bone + "|" + slot.base


# The bones a PMM model moves that the rig has, as (name in the PMM, rig bone) pairs. A VMD
# cuts long names, so those are matched by the cut name too.
def pmm_moving_bones(rig, model):
    by_j = mmd_bones_by_name(rig)
    for name, bone in list(by_j.items()):
        by_j.setdefault(vmd_bone_name(name), bone)
    found = []
    for name, keys in zip(model.bone_names, model.bones):
        bone = by_j.get(name) or by_j.get(vmd_bone_name(name))
        if bone is not None and bone_track_moves(keys):
            found.append((name, bone))
    return found


# The bones the PMM moves (see pmm_moving_bones) that lack a Rigify control, as rig bone
# names in PMM order. Never the rig's other bones: helper bones are not missing, the PMM
# does not move them. No mapping at all, or a mapping the user cleared (an FK control
# following the bone does not hide a cleared IK one).
def missing_mmd_bones(rig, item, found):
    rows = resolved_rows(rig, item)
    used = {row.bone for row in rows if row.control}
    cleared = {row.bone for row in rows if not row.control}
    return [bone for _name, bone in found if bone not in used or bone in cleared]


# The bones of a list matching every word of the search against their Blender or MMD name.
def matching_bones(rig, bones, text):
    terms = text.lower().split()
    return [b for b in bones if all(t in (b + " " + mmd_label(rig, b)).lower() for t in terms)]


# The rig's own append properties on the torso bone (the bind switch and the per-part
# ones). All at 0 the rig is the plain MMD model again: its drivers turn the MMD bones'
# copies off and MMD's own IK on.
def append_properties(rig):
    torso = rig.pose.bones["torso"]
    return {k: torso[k] for k in torso.keys() if k.startswith("mmd_append_") and isinstance(torso[k], (int, float))}


def set_append_properties(rig, values):
    torso = rig.pose.bones["torso"]
    for key, value in values.items():
        torso[key] = value
    rig.update_tag()
    bpy.context.view_layer.update()


# The remap clip a model's earlier sync left on the rig, and its action.
def clear_remap(rig, label):
    anim = rig.animation_data
    if anim is None:
        return
    track_name = CLIP_TRACK_NAME + "_" + label + "_rigify"
    for track in list(anim.nla_tracks):
        if track.name == track_name:
            doomed = [s.action for s in track.strips if s.action is not None]
            anim.nla_tracks.remove(track)
            for action in doomed:
                remove_action(action)


# Key frames of the moving tracks plus the middle of every gap, so a straight line
# between baked frames stays close to the curve MMD draws.
def bake_frames(model):
    keyed = sorted({k.frame for keys in model.bones if bone_track_moves(keys) for k in keys})
    frames = set(keyed)
    for a, b in zip(keyed, keyed[1:]):
        if b - a >= 2:
            frames.add((a + b) // 2)
    return sorted(frames)


def key_control(pose_bone, frame):
    mode = pose_bone.rotation_mode
    rotation = (
        "rotation_quaternion"
        if mode == "QUATERNION"
        else "rotation_axis_angle"
        if mode == "AXIS_ANGLE"
        else "rotation_euler"
    )
    for path in ("location", rotation, "scale"):
        pose_bone.keyframe_insert(path, frame=frame, group=pose_bone.name)


# Moves a Rigify rig converted with MMD Tools Append the way the PMM moves the model. The
# VMD is already on the rig's MMD bones (the motion clip); the Rigify controls drive
# those bones, so the rig is unbound first, which makes it the plain MMD model whose
# bones show the VMD pose, IK included. Each frame the move of an MMD bone from its
# rest pose is applied to its control from that control's own rest pose. Moving, not
# copying, the pose is what keeps this right when the conversion re-rolled the bones.
# Controls are set parents first with a depsgraph update between levels, as their
# parents are driven by other controls. Which controls are used follows each limb's
# IK/FK switch as set. The keys go to a bridge action on its own NLA track; the
# binds are restored. Returns the number of baked frames.
def retarget(rig, model, label, item=None):
    pairs = transfer_pairs(rig, item)
    frames = bake_frames(model)
    if not pairs or not frames:
        log.warning("Rigify remap for '%s': nothing to carry over", label)
        return 0
    log.info(
        "Rigify remap '%s': %d control(s), %d frame(s), limbs %s",
        label,
        len(pairs),
        len(frames),
        limb_modes(rig),
    )
    scene = bpy.context.scene
    track_name = CLIP_TRACK_NAME + "_" + label + "_rigify"
    anim = rig.animation_data or rig.animation_data_create()
    clear_remap(rig, label)
    before = anim.action
    saved = append_properties(rig)
    original_frame = scene.frame_current
    meshes = [
        o
        for o in (rig.parent.children_recursive if rig.parent is not None else rig.children_recursive)
        if o.type == "MESH" and not o.hide_viewport
    ]
    physics = scene.rigidbody_world
    physics_on = physics.enabled if physics is not None else False
    action = bpy.data.actions.new(ACTION_PREFIX + label + "_rigify")
    try:
        for obj in meshes:
            obj.hide_viewport = True
        if physics is not None:
            physics.enabled = False
        anim.action = action
        set_append_properties(rig, {k: 0.0 for k in saved})
        rest = {n: rig.data.bones[n].matrix_local.copy() for n, _m in pairs}
        mmd_rest_inverse = {m: rig.data.bones[m].matrix_local.inverted() for _n, m in pairs}
        levels = {}
        for name, mmd_name in pairs:
            levels.setdefault(len(rig.pose.bones[name].parent_recursive), []).append((name, mmd_name))
        layers = [levels[k] for k in sorted(levels)]
        layer = bpy.context.view_layer
        # Makes the depsgraph the active one, so evaluated poses reach the objects headless.
        bpy.context.evaluated_depsgraph_get()
        for frame in frames:
            scene.frame_set(frame)
            moves = {m: rig.pose.bones[m].matrix @ mmd_rest_inverse[m] for _n, m in pairs}
            for layer_pairs in layers:
                for name, mmd_name in layer_pairs:
                    rig.pose.bones[name].matrix = moves[mmd_name] @ rest[name]
                layer.update()
            for name, _m in pairs:
                key_control(rig.pose.bones[name], frame)
        for fcurve in iter_fcurves(action, anim.action_slot if hasattr(anim, "action_slot") else None):
            for point in fcurve.keyframe_points:
                point.interpolation = "LINEAR"
            fcurve.update()
    finally:
        for obj in meshes:
            obj.hide_viewport = False
        if physics is not None:
            physics.enabled = physics_on
        set_append_properties(rig, saved)
        anim.action = before
        scene.frame_set(original_frame)
    if add_clip_strip(anim, action, track_name) is None:
        log.warning("Rigify remap for '%s': could not add its NLA strip", label)
    return len(frames)
