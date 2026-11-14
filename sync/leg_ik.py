import bisect
import math

from mathutils import Matrix, Quaternion, Vector

from ..common.animation import create_fcurve, find_fcurve, rewrite_fcurve
from ..common.logger import log
from .motion import ik_switches, vmd_bone_name


# Blender and MMD differ by swapping the Y and Z axes, so a rotation crosses over by
# conjugating its matrix with this one.
SWAP = Matrix(((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, 1.0, 0.0)))
KNEE_AXIS = Vector((1.0, 0.0, 0.0))
# MMD's IK loop: its usual pass count, and the widest turn one step may make (the PMX
# limit most models carry, 2.0 radians).
IK_ITERATIONS = 40
IK_STEP_LIMIT = 2.0


def swapped(vector):
    return Vector((vector.x, vector.z, vector.y))


# MMD's bezier of one interpolation curve: the eased share of the way (0-1) at time t,
# found by bisection on the curve's x.
def bezier_ease(x1, y1, x2, y2, t):
    x1, y1, x2, y2 = x1 / 127.0, y1 / 127.0, x2 / 127.0, y2 / 127.0
    low, high = 0.0, 1.0
    for _ in range(24):
        s = (low + high) / 2.0
        x = 3 * (1 - s) ** 2 * s * x1 + 3 * (1 - s) * s * s * x2 + s ** 3
        if x < t:
            low = s
        else:
            high = s
    s = (low + high) / 2.0
    return 3 * (1 - s) ** 2 * s * y1 + 3 * (1 - s) * s * s * y2 + s ** 3


# One PMM bone track read the way MMD plays it: location and rotation at any frame, the
# keys' own bezier curves easing each channel and the rotation taking the shortest arc.
class KeyTrack:
    def __init__(self, keys, scale):
        self.keys = keys
        self.frames = [k.frame for k in keys]
        self.scale = scale

    def at(self, frame):
        keys = self.keys
        if not keys:
            return Vector((0.0, 0.0, 0.0)), Quaternion()
        index = bisect.bisect_right(self.frames, frame) - 1
        if index < 0:
            index = 0
        prev = keys[index]
        qa = Quaternion((prev.rotation[3], prev.rotation[0], prev.rotation[1], prev.rotation[2]))
        if index + 1 >= len(keys) or frame <= prev.frame:
            return Vector(prev.location) * self.scale, qa
        nxt = keys[index + 1]
        t = (frame - prev.frame) / (nxt.frame - prev.frame)
        curve = list(nxt.interpolation)
        loc = Vector(
            prev.location[i] + (nxt.location[i] - prev.location[i]) * bezier_ease(*curve[i * 4 : i * 4 + 4], t)
            for i in range(3)
        )
        qb = Quaternion((nxt.rotation[3], nxt.rotation[0], nxt.rotation[1], nxt.rotation[2]))
        return loc * self.scale, qa.slerp(qb, bezier_ease(*curve[12:16], t))


def bone_path(pose_bone):
    path = []
    while pose_bone is not None:
        path.append(pose_bone)
        pose_bone = pose_bone.parent
    return path[::-1]


# World position and rotation of the last bone of a chain, in MMD's axes, from the PMM
# tracks (bones the PMM has no track for stay at rest).
def chain_pose(path, tracks, frame):
    pos = Vector()
    rot = Quaternion()
    parent_rest = None
    before = rot
    for bone in path:
        rest = swapped(bone.bone.head_local)
        track = tracks.get(bone.name)
        loc, quat = track.at(frame) if track is not None else (Vector(), Quaternion())
        if parent_rest is None:
            pos = rest + loc
        else:
            pos = pos + rot @ (rest - parent_rest) + rot @ loc
        before = rot
        rot = rot @ quat
        parent_rest = rest
    return pos, rot, before


# One leg: the thigh and knee that a two-bone IK constraint drives towards an IK bone,
# with the knee a hinge about its X axis. Blender's solver and MMD's reach the same ankle
# position but not always the same knee, and Blender's does not keep a pose it is handed
# (see silence_constraint). Running MMD's loop (see solve) and baking its result as FK,
# with the constraints off over those frames, matches MMD's legs where Blender's does not.
class LegChain:
    def __init__(self, thigh, knee, ik_bone, switch_index, knee_range, ankle=None, toe_bone=None):
        self.ankle = ankle
        self.toe_path = bone_path(toe_bone) if toe_bone is not None else None
        self.toe_vector = swapped(ankle.bone.tail_local - ankle.bone.head_local) if ankle is not None else None
        self.thigh = thigh
        self.knee = knee
        self.ik_bone = ik_bone
        self.switch_index = switch_index
        self.knee_range = knee_range
        self.thigh_path = bone_path(thigh)
        self.ik_path = bone_path(ik_bone)
        self.thigh_vector = swapped(knee.bone.head_local - thigh.bone.head_local)
        self.shin_vector = swapped(knee.bone.tail_local - knee.bone.head_local)

    def bones(self):
        return [self.thigh_path, self.ik_path]

    # MMD's own IK loop, run on the two leg bones: the knee turns about X only and takes
    # its angle from the plane, the thigh swings the shortest way, knee first, each
    # step capped, until the ankle stops getting closer to the IK bone. It starts from
    # the keyed FK pose, which is why the knee lands where it does. A leg stretched past
    # its reach ends with the knee on whichever side that path leaves it.
    def solve(self, tracks, frame):
        hip, thigh_keyed, parent_rot = chain_pose(self.thigh_path, tracks, frame)
        target = chain_pose(self.ik_path, tracks, frame)[0]
        thigh = parent_rot.inverted() @ thigh_keyed
        knee_track = tracks.get(self.knee.name)
        knee = knee_track.at(frame)[1] if knee_track is not None else Quaternion()
        low, high = self.knee_range
        angle = 0.0
        best = None
        saved = None

        def reach(thigh, knee):
            thigh_world = parent_rot @ thigh
            knee_world = thigh_world @ knee
            knee_pos = hip + thigh_world @ self.thigh_vector
            return knee_pos, thigh_world, knee_world, knee_pos + knee_world @ self.shin_vector

        for iteration in range(IK_ITERATIONS):
            for link in ("knee", "thigh"):
                knee_pos, thigh_world, knee_world, ankle = reach(thigh, knee)
                if link == "knee":
                    to_ankle = knee_world.inverted() @ (ankle - knee_pos)
                    to_target = knee_world.inverted() @ (target - knee_pos)
                else:
                    to_ankle = thigh_world.inverted() @ (ankle - hip)
                    to_target = thigh_world.inverted() @ (target - hip)
                if to_ankle.length < 1e-9 or to_target.length < 1e-9:
                    continue
                to_ankle.normalize()
                to_target.normalize()
                step = math.acos(max(-1.0, min(1.0, to_ankle.dot(to_target))))
                if step < 1e-5:
                    continue
                step = min(step, IK_STEP_LIMIT)
                if link == "thigh":
                    turn = to_ankle.cross(to_target)
                    if turn.length > 1e-12:
                        thigh = thigh @ Quaternion(turn.normalized(), step)
                    continue
                plus = (Quaternion(KNEE_AXIS, step) @ to_ankle).dot(to_target)
                minus = (Quaternion(KNEE_AXIS, -step) @ to_ankle).dot(to_target)
                wanted = angle + (step if plus > minus else -step)
                if iteration == 0 and not low <= wanted <= high:
                    # The first pass flips a bend that points the wrong way instead of
                    # clamping it.
                    if low < -wanted < high:
                        wanted = -wanted
                    else:
                        middle = (low + high) / 2.0
                        if abs(middle - wanted) > abs(middle + wanted):
                            wanted = -wanted
                angle = max(low, min(high, wanted))
                knee = Quaternion(KNEE_AXIS, angle)
            gap = (target - reach(thigh, knee)[3]).length
            if best is None or gap < best:
                best = gap
                saved = (thigh.copy(), angle)
            if gap < 1e-5:
                break
        # CCD does not close the gap every pass (a capped first step can overshoot), so all
        # passes run and the closest one is kept; stopping at the first pass that got no
        # closer left a leg folded 5 units from its target.
        thigh, angle = saved
        return thigh, angle

    # The ankle's rotation relative to the knee once the thigh and knee are solved: MMD's
    # one-link toe IK turns the keyed ankle the shortest way so its tip meets the toe IK
    # bone.
    def solve_ankle(self, tracks, frame, thigh_world, bend):
        hip, _rot, _parent = chain_pose(self.thigh_path, tracks, frame)
        knee_world = thigh_world @ Quaternion(KNEE_AXIS, bend)
        ankle_pos = hip + thigh_world @ self.thigh_vector + knee_world @ self.shin_vector
        track = tracks.get(self.ankle.name)
        keyed = track.at(frame)[1] if track is not None else Quaternion()
        fk_world = knee_world @ keyed
        target, _rot, _parent = chain_pose(self.toe_path, tracks, frame)
        want = target - ankle_pos
        if want.length < 1e-9:
            return keyed
        swing = (fk_world @ self.toe_vector).normalized().rotation_difference(want.normalized())
        return knee_world.inverted() @ (swing @ fk_world)

# A rotation in MMD's axes as the rotation_quaternion of a pose bone: the same crossing
# mmd_tools makes when it imports a VMD, through the bone's rest orientation.
def basis_quaternion(bone, local):
    rest = bone.bone.matrix_local.to_3x3()
    return (rest.inverted() @ SWAP @ local.to_matrix() @ SWAP @ rest).to_quaternion()


def knee_limits(knee):
    if not (knee.use_ik_limit_x and knee.use_ik_limit_y and knee.use_ik_limit_z):
        return None
    if knee.ik_min_y != knee.ik_max_y or knee.ik_min_z != knee.ik_max_z:
        return None
    low, high = knee.ik_min_x, knee.ik_max_x
    # mmd_tools keeps MMD's smallest bend (0.5 degrees) on the rotation limit, not on the
    # IK limits.
    for constraint in knee.constraints:
        if constraint.type == "LIMIT_ROTATION" and constraint.use_limit_x and not constraint.mute:
            low, high = constraint.min_x, constraint.max_x
            break
    return -high, -low


# The leg chains of a model: each IK the PMM lists whose Blender constraint is a
# two-bone chain on a knee that bends about X only. Foot IKs of one bone and rigs that
# do not follow mmd_tools' setup are left to Blender's own solver.
def find_leg_chains(armature, model, table):
    chains = []
    for index, bone_index in enumerate(model.constraints):
        if not 0 <= bone_index < len(model.bone_names):
            continue
        name = model.bone_names[bone_index]
        ik_bone = table.get(vmd_bone_name(name)) or table.get(name)
        if ik_bone is None:
            continue
        for knee in armature.pose.bones:
            for constraint in knee.constraints:
                if constraint.type != "IK" or constraint.subtarget != ik_bone.name:
                    continue
                if constraint.chain_count != 2 or not constraint.use_tail or knee.parent is None:
                    continue
                limits = knee_limits(knee)
                if limits is None:
                    log.info("leg IK: '%s' is not a hinge knee, left to Blender", knee.name)
                    continue
                ankle = toe_bone = toe_ik = None
                for child in knee.children:
                    for sub in child.constraints:
                        if sub.type == "IK" and sub.chain_count == 1 and sub.use_tail and sub.subtarget:
                            toe_bone = armature.pose.bones.get(sub.subtarget)
                            ankle = child if toe_bone is not None else None
                            toe_ik = sub if toe_bone is not None else None
                chain = LegChain(knee.parent, knee, ik_bone, index, limits, ankle, toe_bone)
                chain.ik_constraints = [c for c in (constraint, toe_ik) if c is not None]
                chains.append(chain)
    return chains


# Turns a constraint's influence to 0 over the solved frames and leaves its other keys
# and its value before and after as they were. Blender's solver does not keep a pose it
# is handed: it re-solves the knee on its own and lands away from MMD's, even with the
# ankle already on the target (measured on a pose with the foot far behind: leg overlap
# with MMD 0.57-0.80 with the constraint live, 0.90-0.96 with it off). The baked FK keys
# carry the whole pose, so the IK bone stays where the PMM put it but no longer pulls.
def silence_constraint(armature, constraint, first, last):
    path = constraint.path_from_id("influence")
    curve = find_fcurve(armature, path, 0)
    before = constraint.influence
    kept = []
    if curve is not None:
        kept = [(p.co[0], p.co[1]) for p in curve.keyframe_points]
        before_value = curve.evaluate(first - 1)
        after_value = curve.evaluate(last + 1)
    else:
        before_value = after_value = before
    # The value around the solved span is restored with a key either side, but never one
    # before frame 0: that would stretch the action's range and shift its NLA strip.
    pre = [(first - 1, before_value)] if first - 1 >= 0 and before_value != 0.0 else []
    post = [(last + 1, after_value)] if after_value != 0.0 else []
    outside_before = [(f, v) for f, v in kept if f < first - 1]
    outside_after = [(f, v) for f, v in kept if f > last + 1]
    pairs = outside_before + pre + [(first, 0.0), (last, 0.0)] + post + outside_after
    frames = [f for f, _v in pairs]
    values = [v for _f, v in pairs]
    if curve is None:
        constraint.influence = 0.0
        armature.keyframe_insert(data_path=path, frame=first)
        curve = find_fcurve(armature, path, 0)
        if curve is None:
            return
    rewrite_fcurve(curve, frames, values, "BREAKDOWN")
    for point in curve.keyframe_points:
        point.interpolation = "CONSTANT"
    curve.update()
    constraint.influence = before


def ik_is_on(points, frame):
    if not points:
        return True
    state = points[0][1]
    for at, value in points:
        if at > frame:
            break
        state = value
    return bool(state)


def channel_curves(armature, bone):
    path = 'pose.bones["%s"].rotation_quaternion' % bone.name
    curves = []
    for index in range(4):
        curve = find_fcurve(armature, path, index)
        if curve is None:
            curve = create_fcurve(armature, path, index, bone.name, 0)
        if curve is None:
            return None
        curves.append(curve)
    return curves


# Flips a quaternion to the side of the previous one, so keys that cross between two
# equal rotations do not blend through the long way round.
def keep_continuous(quats, start=None):
    out = []
    last = start
    for quat in quats:
        if last is not None and last.dot(quat) < 0.0:
            quat = Quaternion((-quat.w, -quat.x, -quat.y, -quat.z))
        out.append(quat)
        last = quat
    return out


# Solves every leg IK of the PMM the way MMD does and writes the thigh and knee as FK
# keys on the armature's active action, one per frame, so Blender's IK solver finds
# the pose already there. Frames where the PMM switches the IK off keep the keyed FK
# pose. Returns how many chains were written.
def solve_leg_ik(armature, model, table, scale):
    chains = find_leg_chains(armature, model, table)
    if not chains:
        log.info("leg IK: no two-bone knee chain found on '%s'", armature.name)
        return 0
    tracks = {}
    for index, name in enumerate(model.bone_names):
        bone = table.get(vmd_bone_name(name)) or table.get(name)
        if bone is not None and bone.name not in tracks:
            tracks[bone.name] = KeyTrack(model.bones[index], scale)
    switches = ik_switches(model)
    written = 0
    for chain in chains:
        frames = [f for path in chain.bones() for bone in path if bone.name in tracks for f in tracks[bone.name].frames]
        points = switches.get(chain.switch_index, [])
        frames += [f for f, _state in points]
        if not frames:
            continue
        first, last = int(math.floor(min(frames))), int(math.ceil(max(frames)))
        thigh_curves = channel_curves(armature, chain.thigh)
        knee_curves = channel_curves(armature, chain.knee)
        if thigh_curves is None or knee_curves is None:
            log.warning("leg IK: could not reach the curves of '%s'", chain.thigh.name)
            continue
        ankle_curves = channel_curves(armature, chain.ankle) if chain.ankle is not None else None
        pairs = [(chain.thigh, thigh_curves), (chain.knee, knee_curves)]
        if ankle_curves is not None:
            pairs.append((chain.ankle, ankle_curves))
        keyed = {}
        for bone, curves in pairs:
            keyed[bone.name] = [Quaternion([c.evaluate(f) for c in curves]) for f in range(first, last + 1)]
        solved_thigh = []
        solved_knee = []
        solved_ankle = []
        for offset, frame in enumerate(range(first, last + 1)):
            if not ik_is_on(points, frame):
                solved_thigh.append(keyed[chain.thigh.name][offset])
                solved_knee.append(keyed[chain.knee.name][offset])
                if ankle_curves is not None:
                    solved_ankle.append(keyed[chain.ankle.name][offset])
                continue
            local, bend = chain.solve(tracks, frame)
            solved_thigh.append(basis_quaternion(chain.thigh, local))
            solved_knee.append(basis_quaternion(chain.knee, Quaternion((1.0, 0.0, 0.0), bend)))
            if ankle_curves is not None:
                thigh_world = chain_pose(chain.thigh_path, tracks, frame)[2] @ local
                solved_ankle.append(basis_quaternion(chain.ankle, chain.solve_ankle(tracks, frame, thigh_world, bend)))
        outputs = [(thigh_curves, solved_thigh), (knee_curves, solved_knee)]
        if ankle_curves is not None:
            outputs.append((ankle_curves, solved_ankle))
        for curves, quats in outputs:
            quats = keep_continuous(quats)
            for component, curve in enumerate(curves):
                rewrite_fcurve(curve, list(range(first, last + 1)), [q[component] for q in quats], "BREAKDOWN")
        for constraint in getattr(chain, "ik_constraints", ()):
            silence_constraint(armature, constraint, first, last)
        written += 1
        log.info(
            "leg IK: '%s' and '%s' solved for frames %d-%d",
            chain.thigh.name,
            chain.knee.name,
            first,
            last,
        )
    return written
