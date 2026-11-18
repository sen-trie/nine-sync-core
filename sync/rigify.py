import hashlib
import math
from typing import NamedTuple

import bpy
import numpy as np

from ..common.animation import iter_fcurves, remove_action
from ..common.constants import ACTION_PREFIX, CLIP_TRACK_NAME
from ..common.logger import log
from ..common.scene_utils import find_armature
from ..common.rigify_map import BIND_PROPERTY, CONTROLS, LIMB_OWNERS, MMD_IK_TARGETS, RIGIFY_FORMAT
from ..pmm import bone_track_moves
from .motion import vmd_bone_name
from .clips import owned_clip, place_clip

TORSO_MMD_NAME = "上半身"


# A rig made by MMD Tools Append's Rigify conversion: a Rigify rig that still holds the
# MMD bones, with the bind switch on its torso bone. Same test that add-on uses.
def is_converted_rig(armature):
    if armature is None or armature.type != "ARMATURE" or not armature.data.get("rig_id"):
        return False
    torso = armature.pose.bones.get("torso")
    return torso is not None and BIND_PROPERTY in torso


# The "MMD compatibility focused" conversion hands the MMD names (mmd_bone.name_j) to the
# Rigify controls themselves, so the plain PMX-mapped import already keys the controls. The
# "Rigify focused" one leaves the names on the MMD bones, which then follow the DEF bones.
# So the bone the name 上半身 points at tells them apart: a control with no copy of a
# DEF/ORG bone on it is the first kind.
def is_mmd_focused_rig(armature):
    if not is_converted_rig(armature):
        return False
    bone = next(
        (pb for pb in armature.pose.bones if getattr(getattr(pb, "mmd_bone", None), "name_j", "") == TORSO_MMD_NAME),
        None,
    )
    if bone is None:
        return False
    return not any(
        getattr(c, "subtarget", "").startswith(("DEF-", "ORG-")) for c in bone.constraints
    )


def is_rigify_armature(armature):
    return is_converted_rig(armature)


# Whether the remap runs for a target row: the choice stored on the row's Remap dropdown,
# else a legacy .blend that had the old checkbox off, else the kind of rig, as the MMD
# focused one needs nothing carried over. Reads only, so the plan can call it.
def remap_default(row):
    if row.is_property_set("rigify_remap") and not row.rigify_remap:
        return False
    return not is_mmd_focused_rig(find_armature(row.target) if row.target is not None else None)


def remap_on(row):
    return row.rigify_mode == "RIGIFY"


# What a Rigify target adds to a motion part's hash, so a new table, an edited mapping or
# turning the remap off re-runs it. None for any other rig, which keeps its old hash. No
# side effects: the plan calls it.
def rigify_signature(armature, row):
    if not is_rigify_armature(armature):
        return None
    if row is not None and not remap_on(row):
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
    if row.name_j in MMD_IK_TARGETS:
        return "active"
    if mode is not None and row.kind != "any" and (row.kind == "ik") != (mode == "IK"):
        return "inactive"
    return "active"


# (control, MMD bone) pairs to carry, parents first, for the limbs as they are switched.
# A control follows one MMD bone: when two are given the same control, the first keeps it.
def transfer_pairs(rig, item=None, fk_groups=()):
    modes = limb_modes(rig)
    for group in fk_groups:
        if group in modes:
            modes[group] = "FK"
    pairs = {}
    for row in resolved_rows(rig, item):
        if row_status(rig, modes, row) == "active":
            pairs.setdefault(row.control, row.bone)
    return sorted(pairs.items(), key=lambda kv: bake_order(rig, kv[0]))


# Where a control goes in the bake: parents first, and tweak controls after every other one,
# because a tweak sits under helper bones that the FK and IK controls move.
def bake_order(rig, control):
    return ("_tweak." in control, len(rig.pose.bones[control].parent_recursive))


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


# Key frames of the moving tracks plus the frames between them, so a straight line between
# baked frames stays close to the curve MMD draws. A short gap gets every frame: the legs
# swing along an arc that their IK solves, and a line across even half a step cuts it, which
# showed as knees and feet off by tens of degrees. A long gap (a hold, usually) gets a frame
# every few.
BAKE_DENSE_GAP = 6
BAKE_SPARSE_STEP = 2


def bake_frames(model):
    keyed = sorted({k.frame for keys in model.bones if bone_track_moves(keys) for k in keys})
    frames = set(keyed)
    for a, b in zip(keyed, keyed[1:]):
        step = 1 if b - a <= BAKE_DENSE_GAP else BAKE_SPARSE_STEP
        frames.update(range(a + step, b, step))
    return sorted(frames)


def control_paths(pose_bone):
    mode = pose_bone.rotation_mode
    rotation = (
        "rotation_quaternion"
        if mode == "QUATERNION"
        else "rotation_axis_angle"
        if mode == "AXIS_ANGLE"
        else "rotation_euler"
    )
    return ("location", rotation, "scale")


# Version of the baked-key layout; part of the signature that decides whether an earlier
# bake can be reused frame by frame.
BAKE_VERSION = 1
BAKE_SIGNATURE_KEY = "ninesync_bake_sig"
BAKE_HASHES_KEY = "ninesync_bake_hashes"
# Past this share of changed frames a full bake is as cheap as patching.
BAKE_PATCH_LIMIT = 0.7

LINEAR = bpy.types.Keyframe.bl_rna.properties["interpolation"].enum_items["LINEAR"].value


# What the baked keys depend on besides the per-frame motion: the controls and their
# sources, the limb modes, the frame list and every rest pose involved.
def bake_signature(rig, pairs, fk_groups, solved, frames, rest):
    digest = hashlib.blake2b(digest_size=10)
    digest.update(repr((BAKE_VERSION, RIGIFY_FORMAT, pairs, fk_groups, solved, frames)).encode())
    digest.update(repr([rig.pose.bones[n].rotation_mode for n, _m in pairs]).encode())
    digest.update(np.round(np.array([rest[n] for n, _m in pairs], dtype=np.float64), 5).tobytes())
    return digest.hexdigest()


# Fingerprint of everything the controls are computed from in one frame.
def moves_hash(moves, sources):
    array = np.round(np.array([moves[m] for m in sources], dtype=np.float64), 6)
    return hashlib.blake2b(array.tobytes(), digest_size=6).hexdigest()


# The values of an earlier bake, {(bone, path, index): array}, or None when it cannot be
# reused: no stored fingerprint, a different signature or a curve that does not line up.
def earlier_bake(clip, signature, frames, pairs, rig):
    if clip is None or clip.get(BAKE_SIGNATURE_KEY) != signature or not clip.slots:
        return None, None
    hashes = str(clip.get(BAKE_HASHES_KEY, "")).split(",")
    if len(hashes) != len(frames):
        return None, None
    curves = {(fc.data_path, fc.array_index): fc for fc in iter_fcurves(clip, clip.slots[0])}
    values = {}
    for name, _m in pairs:
        for path in control_paths(rig.pose.bones[name]):
            size = len(getattr(rig.pose.bones[name], path))
            for index in range(size):
                fc = curves.get((f'pose.bones["{name}"].{path}', index))
                if fc is None or len(fc.keyframe_points) != len(frames):
                    return None, None
                co = np.empty(2 * len(frames))
                fc.keyframe_points.foreach_get("co", co)
                if not np.array_equal(co[0::2], frames):
                    return None, None
                values[(name, path, index)] = co[1::2].copy()
    return values, hashes


# A patched span can land a turn away from its neighbours, which a straight line between
# keys would unwind; euler angles and quaternions are brought next to the previous key.
def keep_continuous(values):
    for (name, path, index), column in values.items():
        if path == "rotation_euler":
            for i in range(1, len(column)):
                column[i] += 2 * math.pi * round((column[i - 1] - column[i]) / (2 * math.pi))
    quats = {}
    for (name, path, index), column in values.items():
        if path == "rotation_quaternion":
            quats.setdefault(name, {})[index] = column
    for columns in quats.values():
        stack = np.array([columns[i] for i in range(4)])
        for i in range(1, stack.shape[1]):
            if np.dot(stack[:, i - 1], stack[:, i]) < 0:
                stack[:, i] = -stack[:, i]
        for k in range(4):
            columns[k][:] = stack[k]


# Bones in the chains of Rigify's own IK constraints (not MMD's leg IK, which is muted for
# the bake): a constraint-driven pose cannot be read back for their roots.
def ik_chain_bones(rig):
    found = set()
    for pose_bone in rig.pose.bones:
        for constraint in pose_bone.constraints:
            if constraint.type != "IK" or constraint.target is not rig:
                continue
            target = rig.pose.bones.get(constraint.subtarget)
            if target is not None and target.mmd_bone.name_j in MMD_IK_TARGETS:
                continue
            bone = pose_bone
            for _ in range(constraint.chain_count or 99):
                if bone is None:
                    break
                found.add(bone.name)
                bone = bone.parent
    return found


# A control's local transform as the constraint left it, in its own rotation mode and
# turned toward its previous value so a straight line between keys does not spin.
def constrained_values(rig, pose_bone, previous):
    basis = rig.convert_space(pose_bone=pose_bone, matrix=pose_bone.matrix, from_space="POSE", to_space="LOCAL")
    location, quaternion, scale = basis.decompose()
    mode = pose_bone.rotation_mode
    before = previous.get(pose_bone.name)
    if mode in ("QUATERNION", "AXIS_ANGLE"):
        if before is not None:
            quaternion.make_compatible(before)
        previous[pose_bone.name] = quaternion
        if mode == "QUATERNION":
            rotation = tuple(quaternion)
        else:
            axis, angle = quaternion.to_axis_angle()
            rotation = (angle, *axis)
    else:
        euler = quaternion.to_euler(mode) if before is None else quaternion.to_euler(mode, before)
        previous[pose_bone.name] = euler
        rotation = tuple(euler)
    return {"location": tuple(location), control_paths(pose_bone)[1]: rotation, "scale": tuple(scale)}


# Writes the baked values straight into a new action: one slot, one layer, a curve per
# channel, all points set in one call, far cheaper than a keyframe_insert per channel.
def write_bake(rig, action, values, frames, switches):
    slot = action.slots.new(id_type="OBJECT", name=rig.name)
    bag = action.layers.new("Retargeted").strips.new(type="KEYFRAME").channelbag(slot, ensure=True)
    count = len(frames)

    def curve(path, index, group, column):
        fc = bag.fcurves.new(data_path=path, index=index, group_name=group)
        fc.keyframe_points.add(len(column))
        co = np.empty(2 * len(column))
        co[0::2] = frames if len(column) == count else (frames[0], frames[-1])
        co[1::2] = column
        fc.keyframe_points.foreach_set("co", co)
        fc.keyframe_points.foreach_set("interpolation", [LINEAR] * len(column))
        fc.update()

    for (name, path, index), column in values.items():
        curve(f'pose.bones["{name}"].{path}', index, name, column)
    for bone in switches:
        curve(f'pose.bones["{bone.name}"]["IK_FK"]', 0, bone.name, (1.0, 1.0))
    return slot


# Moves a Rigify rig converted with MMD Tools Append the way the PMM moves the model. The
# VMD is already on the rig's MMD bones (the motion clip); the Rigify controls drive
# those bones, so the rig is unbound first, which makes it the plain MMD model whose
# bones show the VMD pose, IK included. Each frame the move of an MMD bone from its
# rest pose is applied to its control from that control's own rest pose. Moving, not
# copying, the pose is what keeps this right when the conversion re-rolled the bones.
# Controls are set parents first with a depsgraph update between levels, as their
# parents are driven by other controls, tweak controls last. Which controls are used
# follows each limb's IK/FK switch as set, except MMD's leg IK targets, which are always
# carried (see MMD_IK_TARGETS). The keys go to a bridge action on its own NLA track; the
# binds are restored. Returns the number of baked frames.
def leg_ik_constraints(rig):
    found = []
    for pose_bone in rig.pose.bones:
        for constraint in pose_bone.constraints:
            target = rig.pose.bones.get(constraint.subtarget) if constraint.type == "IK" else None
            if target is not None and target.mmd_bone.name_j in MMD_IK_TARGETS:
                found.append(constraint)
    return found


# The pairs in groups that can be set together: a control goes after every carried control its
# pose is computed from, through its parent chain and through constraint targets, helper
# bones included. Parent depth alone would cost a depsgraph update per helper level.
def bake_levels(rig, pairs):
    carried = {name for name, _mmd in pairs}
    bones = rig.pose.bones

    def inputs(bone):
        names = {bone.parent.name} if bone.parent else set()
        for constraint in bone.constraints:
            if getattr(constraint, "target", None) is rig:
                names.add(getattr(constraint, "subtarget", ""))
                names.add(getattr(constraint, "pole_subtarget", ""))
            if constraint.type == "ARMATURE":
                names.update(t.subtarget for t in constraint.targets if t.target is rig)
        return [n for n in names if n and n in bones and n != bone.name]

    reach = {}

    def carried_inputs(name, stack):
        if name not in reach:
            found = set()
            if name not in stack:
                for n in inputs(bones[name]):
                    if n in carried:
                        found.add(n)
                    found |= carried_inputs(n, stack | {name})
            reach[name] = found
        return reach[name]

    level = {}

    def level_of(name):
        if name not in level:
            level[name] = 0
            level[name] = 1 + max((level_of(n) for n in carried_inputs(name, frozenset()) if n != name), default=0)
        return level[name]

    groups = {}
    for name, mmd_name in pairs:
        groups.setdefault(level_of(name), []).append((name, mmd_name))
    return [groups[k] for k in sorted(groups)]


# Each depsgraph update costs about 14 ms in a busy scene and about 4 ms with the rig
# alone, and the bake needs one per level per frame, so it runs in a scene of its own
# holding only the rig and its parents. Needs a window to switch; without one (background
# mode) the bake stays in the scene. Returns (home scene, bake scene) or None.
def enter_bake_scene(rig):
    window = bpy.context.window
    if window is None:
        return None
    home = window.scene
    work = bpy.data.scenes.new("NineSync_bake")
    work.render.fps = home.render.fps
    work.render.fps_base = home.render.fps_base
    obj = rig
    while obj is not None:
        work.collection.objects.link(obj)
        obj = obj.parent
    window.scene = work
    return home, work


def leave_bake_scene(stage):
    home, work = stage
    bpy.context.window.scene = home
    bpy.data.scenes.remove(work)


def retarget(rig, model, label, item=None):
    # Rigify's own leg IK picks its own knee, like Blender's IK on a plain rig, so with the
    # solved MMD legs the legs run in FK for the baked frames: the switch is keyed to FK and
    # the FK controls follow the MMD bones, which already carry MMD's solved pose.
    solved = bool(getattr(item, "solve_leg_ik", False)) and bool(model.constraints)
    fk_groups = tuple(g for g in ("LEG_L", "LEG_R") if g in limb_modes(rig)) if solved else ()
    pairs = transfer_pairs(rig, item, fk_groups)
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
    clip = owned_clip(rig, getattr(item, "rigify_clip", None), (track_name,))
    if clip is None:
        clear_remap(rig, label)
    before = anim.action
    saved = append_properties(rig)
    original_frame = scene.frame_current
    action = bpy.data.actions.new(ACTION_PREFIX + label + "_rigify")
    slot = None
    switched = {}
    muted = {}
    follow = {}
    meshes = []
    physics = None
    physics_on = False
    stage = None
    try:
        stage = enter_bake_scene(rig)
        if stage is None:
            meshes = [
                o
                for o in (rig.parent.children_recursive if rig.parent is not None else rig.children_recursive)
                if o.type == "MESH" and not o.hide_viewport
            ]
            physics = scene.rigidbody_world
            physics_on = physics.enabled if physics is not None else False
            for obj in meshes:
                obj.hide_viewport = True
            if physics is not None:
                physics.enabled = False
        work = stage[1] if stage is not None else scene
        rest = {n: rig.data.bones[n].matrix_local.copy() for n, _m in pairs}
        mmd_rest_inverse = {m: rig.data.bones[m].matrix_local.inverted() for _n, m in pairs}
        sources = sorted({m for _n, m in pairs})
        signature = bake_signature(rig, pairs, fk_groups, solved, frames, rest)
        earlier, earlier_hashes = earlier_bake(clip, signature, frames, pairs, rig)
        layers = bake_levels(rig, pairs)
        layer = bpy.context.view_layer
        # Makes the depsgraph the active one, so evaluated poses reach the objects headless.
        bpy.context.evaluated_depsgraph_get()
        set_append_properties(rig, {k: 0.0 for k in saved})
        # Unbound, the rig's MMD leg IK is driven back on (influence 1 - bind), which would
        # have Blender re-solve the knees the solver already placed; mute it for the bake.
        if solved:
            for constraint in leg_ik_constraints(rig):
                muted[constraint] = constraint.mute
                constraint.mute = True
        switches = [rig.pose.bones[LIMB_OWNERS[g]] for g in fk_groups]
        for bone in switches:
            switched[bone.name] = bone["IK_FK"]
            bone["IK_FK"] = 1.0
        def moves_at(frame):
            work.frame_set(frame)
            return {m: rig.pose.bones[m].matrix @ mmd_rest_inverse[m] for m in sources}

        # With an earlier bake of the same shape, only frames whose source motion changed
        # are baked again and spliced in; the rest of its keys are carried over.
        todo = list(range(len(frames)))
        hashes = [None] * len(frames)
        values = None
        if earlier is not None:
            hashes = [moves_hash(moves_at(f), sources) for f in frames]
            todo = [i for i, h in enumerate(hashes) if h != earlier_hashes[i]]
            if len(todo) <= BAKE_PATCH_LIMIT * len(frames):
                values = earlier
            else:
                todo = list(range(len(frames)))
        if values is None:
            values = {
                (name, path, index): np.zeros(len(frames))
                for name, _m in pairs
                for path in control_paths(rig.pose.bones[name])
                for index in range(len(getattr(rig.pose.bones[name], path)))
            }
        # In a scene of its own most controls follow an empty through a Copy Transforms
        # constraint, solved in one update; the roots of Rigify's own IK chains are set by
        # matrix in levels instead, as a constraint does not reproduce their pose.
        roots = ik_chain_bones(rig) & {n for n, _m in pairs} if stage is not None else set()
        followers = [(n, m) for n, m in pairs if n not in roots] if stage is not None else []
        root_layers = bake_levels(rig, [(n, m) for n, m in pairs if n in roots]) if roots else []
        if stage is not None:
            for name, _m in followers:
                empty = bpy.data.objects.new("NineSync_bake_" + name, None)
                work.collection.objects.link(empty)
                constraint = rig.pose.bones[name].constraints.new("COPY_TRANSFORMS")
                constraint.name = "NineSync_bake"
                constraint.target = empty
                follow[name] = (empty, constraint)
            rig.update_tag()
            layer.update()
        previous = {}
        world = rig.matrix_world
        for i in todo:
            moves = moves_at(frames[i])
            hashes[i] = moves_hash(moves, sources)
            if stage is not None:
                for name, mmd_name in followers:
                    follow[name][0].matrix_world = world @ (moves[mmd_name] @ rest[name])
                layer.update()
                for layer_pairs in root_layers:
                    for name, mmd_name in layer_pairs:
                        rig.pose.bones[name].matrix = moves[mmd_name] @ rest[name]
                    layer.update()
            else:
                for layer_pairs in layers:
                    for name, mmd_name in layer_pairs:
                        rig.pose.bones[name].matrix = moves[mmd_name] @ rest[name]
                    layer.update()
            for name, _m in pairs:
                bone = rig.pose.bones[name]
                if name in follow:
                    read = constrained_values(rig, bone, previous)
                else:
                    read = {path: getattr(bone, path) for path in control_paths(bone)}
                for path, sequence in read.items():
                    for index, value in enumerate(sequence):
                        values[(name, path, index)][i] = value
        if earlier is not None and todo:
            keep_continuous(values)
        slot = write_bake(rig, action, values, frames, switches)
        action[BAKE_SIGNATURE_KEY] = signature
        action[BAKE_HASHES_KEY] = ",".join(hashes)
        log.info("Rigify remap '%s': %d of %d frame(s) baked", label, len(todo), len(frames))
    finally:
        for name, (empty, constraint) in follow.items():
            rig.pose.bones[name].constraints.remove(constraint)
            bpy.data.objects.remove(empty)
        for obj in meshes:
            obj.hide_viewport = False
        if physics is not None:
            physics.enabled = physics_on
        set_append_properties(rig, saved)
        for name, value in switched.items():
            rig.pose.bones[name]["IK_FK"] = value
        for constraint, value in muted.items():
            constraint.mute = value
        anim.action = before
        if stage is not None:
            leave_bake_scene(stage)
        scene.frame_set(original_frame)
    # Same placement as the motion clip: a chosen clip gets the new keys swapped in, so its
    # strips and references survive; otherwise the keys become a clip on their own track.
    anim.action = action
    if slot is not None:
        anim.action_slot = slot
    placed = place_clip(rig, clip, track_name, ACTION_PREFIX + label + "_rigify", before)
    if item is not None:
        item.rigify_clip = placed
    return len(frames)
