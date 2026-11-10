from ..common.animation import find_fcurve
from ..common.logger import log
from .motion import ik_switches, vmd_bone_name


# IK bone name -> the constraints its switch turns on and off, chosen as mmd_tools' own
# mmd_ik_toggle chooses them: every IK constraint aimed at the bone, and the
# LIMIT_ROTATION of each bone in that constraint's chain. One pass over the armature.
def ik_switch_table(armature):
    table = {}
    for bone in armature.pose.bones:
        for constraint in bone.constraints:
            if constraint.type != "IK" or not constraint.subtarget:
                continue
            found = table.setdefault(constraint.subtarget, [])
            found.append(constraint)
            chain = bone if constraint.use_tail else bone.parent
            if chain is None:
                continue
            for link in ([chain] + list(chain.parent_recursive))[: constraint.chain_count]:
                limit = next(
                    (c for c in link.constraints if c.type == "LIMIT_ROTATION" and not c.mute),
                    None,
                )
                if limit is not None:
                    found.append(limit)
    return table


def ik_switch_constraints(armature, ik_bone):
    return ik_switch_table(armature).get(ik_bone.name, [])


def set_constant(fcurve):
    for point in fcurve.keyframe_points:
        point.interpolation = "CONSTANT"
    fcurve.update()


# Keys the PMM's IK switches on the active (motion) action of the armature, CONSTANT,
# so an IK turns off and on exactly on its key frame as in MMD. Influence is keyed
# directly rather than through mmd_tools' property frames, which need its Use Property
# Driver and would move model visibility onto mmd_root.show_meshes. Every switchable
# constraint first goes back to its bone's mmd_ik_toggle, so keys an earlier PMM had
# leave nothing behind. table is the VMD name table the import used. Returns how many
# constraints were keyed.
def apply_ik_keys(armature, model, table):
    switches = ik_switches(model)
    anim = armature.animation_data
    driven = {fc.data_path for fc in anim.drivers} if anim is not None else set()
    constraints = ik_switch_table(armature)
    keyed = 0
    for index, bone_index in enumerate(model.constraints):
        if not 0 <= bone_index < len(model.bone_names):
            continue
        name = model.bone_names[bone_index]
        ik_bone = table.get(vmd_bone_name(name)) or table.get(name)
        points = switches.get(index)
        if ik_bone is None:
            if points:
                log.warning("IK switch keys of '%s' skipped: no such bone on '%s'", name, armature.name)
            continue
        on = 1.0 if getattr(ik_bone, "mmd_ik_toggle", True) else 0.0
        for constraint in constraints.get(ik_bone.name, ()):
            if not points:
                if constraint.influence != on:
                    constraint.influence = on
                continue
            path = constraint.path_from_id("influence")
            if path in driven:
                if points:
                    log.warning(
                        "IK switch keys of '%s' skipped: its constraints are driven (mmd_tools Use Property Driver)",
                        name,
                    )
                continue
            for frame, state in points:
                constraint.influence = float(state)
                armature.keyframe_insert(data_path=path, frame=frame)
            fcurve = find_fcurve(armature, path, 0)
            if fcurve is not None:
                set_constant(fcurve)
            keyed += 1
    if switches:
        log.info(
            "IK switch keys: %d IK bone(s) switch off in the PMM, %d constraint(s) keyed",
            len(switches),
            keyed,
        )
    return keyed


# After Clean removes the clips, a constraint keeps the influence of the frame it was
# on, so an IK that was off would stay off. Puts every switchable constraint back to
# its bone's mmd_ik_toggle. Returns how many were set.
def restore_ik_influence(armature):
    count = 0
    for name, constraints in ik_switch_table(armature).items():
        ik_bone = armature.pose.bones.get(name)
        if ik_bone is None:
            continue
        on = 1.0 if getattr(ik_bone, "mmd_ik_toggle", True) else 0.0
        for constraint in constraints:
            constraint.influence = on
            count += 1
    return count
