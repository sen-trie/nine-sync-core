import bpy
from mathutils import Vector

from ..common.animation import iter_fcurves
from ..common.constants import ACTION_PREFIX
from ..common.logger import log
from .rigs import ensure_light_rig


# Keys the light directly instead of through a VMD. Keys one frame apart are held
# constant so a cut does not blend; other segments are linear.
def import_light(scene, project, settings):
    root, light = ensure_light_rig(scene, settings)
    ordered = sorted(project.light_keys, key=lambda k: k.frame)
    data = light.data
    # Keying both IDs with no action makes Blender share one between them. Separate
    # actions match what mmd_tools builds for a light.
    for owner, label in ((data, "Light_color"), (light, "Light_loc")):
        anim = owner.animation_data or owner.animation_data_create()
        anim.action = bpy.data.actions.new(ACTION_PREFIX + label)
    for key in ordered:
        data.color = key.color
        data.keyframe_insert("color", frame=key.frame)
        light.location = Vector(key.direction).xzy * -1
        light.keyframe_insert("location", frame=key.frame)
    for owner, label in ((data, "Light_color"), (light, "Light_loc")):
        anim = owner.animation_data
        action = anim.action if anim else None
        if action is None:
            log.warning("light: '%s' has no action after keying", owner.name)
            continue
        action.name = ACTION_PREFIX + label
        for fcurve in iter_fcurves(action, anim.action_slot):
            points = sorted(fcurve.keyframe_points, key=lambda p: p.co[0])
            for index, point in enumerate(points):
                point.interpolation = "LINEAR"
                if (
                    index + 1 < len(points)
                    and points[index + 1].co[0] - point.co[0] <= 1.0
                ):
                    point.interpolation = "CONSTANT"
        log.info(
            "light: '%s' action '%s' fcurves=%d",
            owner.name,
            action.name,
            len(iter_fcurves(action)),
        )
