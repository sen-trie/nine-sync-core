from mathutils import Matrix

from ..common.animation import describe_action, iter_fcurves
from ..common.constants import ACTION_PREFIX, FOLLOW_PREFIX, TEMP_PREFIX
from ..common.logger import log
from ..common.scene_utils import find_armature, model_key, pose_bone_name
from ..pmm import build_scene_vmd
from .mmd_tools_link import run_importer
from .rigs import ensure_camera_rig
from .scale import current_scale
from .tempfiles import remove_file, write_temp_vmd


# Maps each (model, bone) that camera keys follow to an armature and pose bone, with
# a message per pair that cannot be resolved (mapped to None, so the camera still
# imports). Read-only, so the plan can use it too.
def follow_table(project, settings):
    table = {}
    problems = []
    if not settings.follow_camera:
        return table, problems
    items = {t.key: t for t in settings.targets}
    by_index = {m.index: m for m in project.models}
    for key in project.camera_keys:
        if key.parent_model < 0 or key.parent_bone < 0:
            continue
        pair = (key.parent_model, key.parent_bone)
        if pair in table:
            continue
        table[pair] = None
        model = by_index.get(key.parent_model)
        if model is None:
            problems.append("camera follows model %d which is not in the PMM" % key.parent_model)
            continue
        item = items.get(model_key(model))
        if item is None or item.target is None:
            problems.append(
                "camera follows '%s' but that model has no Blender target assigned" % model.name
            )
            continue
        armature = find_armature(item.target)
        if armature is None:
            problems.append("camera follow: root '%s' has no armature" % item.target.name)
            continue
        if key.parent_bone >= len(model.bone_names):
            problems.append(
                "camera follow: bone index %d out of range for '%s'" % (key.parent_bone, model.name)
            )
            continue
        mmd_name = model.bone_names[key.parent_bone]
        bone = pose_bone_name(armature, mmd_name)
        if bone is None:
            problems.append(
                "camera follow: bone '%s' not found on armature '%s'" % (mmd_name, armature.name)
            )
            continue
        table[pair] = (armature, bone)
    return table, problems


# follow_table with its outcome logged, for the import.
def resolve_follow(project, settings):
    table, problems = follow_table(project, settings)
    for problem in problems:
        log.warning("%s", problem)
    for pair, resolved in table.items():
        if resolved is not None:
            log.info(
                "camera follow resolved: model %d bone %d -> %s / %s",
                pair[0],
                pair[1],
                resolved[0].name,
                resolved[1],
            )
    return table


# Constant interpolation, so a follow constraint switches on or off instantly
# instead of blending.
def make_constant(obj, path):
    anim = obj.animation_data
    action = anim.action if anim else None
    if action is None:
        log.warning("no action on '%s' to set constant interpolation", obj.name)
        return
    for fcurve in iter_fcurves(action, anim.action_slot):
        if fcurve.data_path == path:
            for point in fcurve.keyframe_points:
                point.interpolation = "CONSTANT"


# One Child Of constraint per followed bone. Its influence is keyed 1 on the camera
# keys that use it and 0 on the rest, which reproduces MMD's per-key parent.
def apply_follow(root, project, plan, settings):
    for constraint in [c for c in root.constraints if c.name.startswith(FOLLOW_PREFIX)]:
        root.constraints.remove(constraint)
    keys = sorted(project.camera_keys, key=lambda k: k.frame)
    for pair, resolved in plan.items():
        if resolved is None:
            continue
        armature, bone = resolved
        constraint = root.constraints.new(type="CHILD_OF")
        constraint.name = FOLLOW_PREFIX + "_%d_%d" % pair
        constraint.target = armature
        constraint.subtarget = bone
        constraint.use_rotation_x = settings.follow_rotation
        constraint.use_rotation_y = settings.follow_rotation
        constraint.use_rotation_z = settings.follow_rotation
        constraint.use_scale_x = False
        constraint.use_scale_y = False
        constraint.use_scale_z = False
        constraint.set_inverse_pending = False
        constraint.inverse_matrix = Matrix.Identity(4)
        constraint.influence = 1.0
        path = 'constraints["%s"].influence' % constraint.name
        for key in keys:
            active = (key.parent_model, key.parent_bone) == pair
            constraint.influence = 1.0 if active else 0.0
            constraint.keyframe_insert("influence", frame=key.frame)
        make_constant(root, path)
        log.info(
            "follow constraint '%s' -> %s / %s (rotation=%s)",
            constraint.name,
            armature.name,
            bone,
            settings.follow_rotation,
        )


# Camera keys go through a temp VMD that mmd_tools turns into rig animation, then
# the follow constraints are applied.
def import_camera(scene, project, settings):
    root, camera = ensure_camera_rig(scene, settings)
    data = build_scene_vmd(project.camera_keys)
    tmp_path = write_temp_vmd(data, TEMP_PREFIX + "camera_")
    try:
        run_importer(
            tmp_path,
            current_scale(settings),
            [camera],
            ACTION_PREFIX + "Scene",
            True,
        )
    finally:
        remove_file(tmp_path)
    describe_action("camera root", root)
    describe_action("camera distance", camera)
    apply_follow(root, project, resolve_follow(project, settings), settings)
    if settings.set_active_camera and scene.camera != camera:
        scene.camera = camera
        log.info("scene camera set to '%s'", camera.name)
