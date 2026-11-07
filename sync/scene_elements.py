import traceback

from ..common.animation import (
    clear_bridge_nla,
    collect_owners,
    frame_span,
    purge_actions,
    reset_animation,
    restore_points,
    snapshot_outside,
    tag_actions,
)
from ..common.constants import ACTION_PREFIX
from ..common.logger import log
from .camera import import_camera
from .light import import_light
from .plan import BLOCKED
from .rigs import ensure_camera_rig, ensure_light_rig
from .sync_state import find_scene_rig, forget_scene_part, write_scene_part


# (rig builder, key attribute, action base name, importer) per scene element.
ELEMENTS = {
    "camera": (ensure_camera_rig, "camera_keys", "Scene", import_camera),
    "light": (ensure_light_rig, "light_keys", "Light", import_light),
}


def log_first_keys(project, plan):
    camera = plan.get("camera")
    if camera is not None and camera.runs:
        first = project.camera_keys[0]
        log.info(
            "first camera key: frame=%d distance=%.3f look_at=%s angle=%s fov=%d parent=(%d,%d) perspective_raw=%d",
            first.frame,
            first.distance,
            tuple(round(v, 3) for v in first.look_at),
            tuple(round(v, 3) for v in first.angle),
            first.fov,
            first.parent_model,
            first.parent_bone,
            first.perspective,
        )
    light = plan.get("light")
    if light is not None and light.runs:
        first = project.light_keys[0]
        log.info(
            "first light key: frame=%d color=%s direction=%s",
            first.frame,
            first.color,
            first.direction,
        )


# Rebuilds one element. Like models, it saves the user's keys outside the PMM span
# first and restores them afterwards. Only this element's IDs are reset, and only its
# own unused actions are purged, so the other element's animation is never touched.
def rebuild_element(scene, project, settings, kind):
    ensure_rig, attribute, base, importer = ELEMENTS[kind]
    root, child = ensure_rig(scene, settings)
    owners = collect_owners([root, child])
    low, high = frame_span(k.frame for k in getattr(project, attribute))
    kept = snapshot_outside(owners, low, high)
    log.info("%s: keeping keys outside the PMM span on %d curve(s)", kind, len(kept))
    clear_bridge_nla(owners)
    reset_animation([root, child])
    purge_actions(ACTION_PREFIX + base)
    importer(scene, project, settings)
    restored = restore_points(kept)
    tag_actions(owners, base)
    log.info("%s: restored %d preserved key(s)", kind, restored)


# A skipped camera still becomes the scene camera when that option asks for it; that
# changes no animation.
def keep_scene_camera(scene, settings):
    rig = find_scene_rig("camera")
    if rig is not None and scene.camera != rig[1]:
        scene.camera = rig[1]
        log.info("scene camera set to '%s'", rig[1].name)


# Camera and light sync as the plan decided, each element on its own: an unchanged one
# is not touched, and a failure in one leaves the other free to import. A record moves
# forward only for an element that imported. Returns how many elements were imported.
def import_scene_elements(scene, project, settings, plan):
    if project.scene_error:
        log.warning("camera/light section could not be read: %s", project.scene_error)
        return 0
    log.info(
        "scene elements: camera keys=%d (sync=%s), light keys=%d (sync=%s)",
        len(project.camera_keys),
        settings.sync_camera,
        len(project.light_keys),
        settings.sync_light,
    )
    log_first_keys(project, plan.scene)
    done = 0
    for kind in ELEMENTS:
        part = plan.scene.get(kind)
        if part is None:
            continue
        level = log.warning if part.operation == BLOCKED else log.info
        level("plan %s: %s -> %s (%s)", kind, part.operation, part.destination, part.reason)
        for warning in part.warnings:
            log.warning("plan %s: %s", kind, warning)
        if not part.runs:
            continue
        try:
            rebuild_element(scene, project, settings, kind)
        except Exception as error:
            log.error("%s failed: %s\n%s", kind, error, traceback.format_exc())
            forget_scene_part(settings, kind)
            continue
        write_scene_part(settings, plan.source, kind, part)
        done += 1
    camera = plan.scene.get("camera")
    if settings.sync_camera and camera is not None and not camera.runs:
        keep_scene_camera(scene, settings)
    return done
