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
from .rigs import ensure_camera_rig, ensure_light_rig


# Camera and light sync. Like models, it saves the user's keys outside the PMM span
# first and restores them afterwards. Returns how many elements were imported.
def import_scene_elements(scene, project, settings):
    if project.scene_error:
        log.warning("camera/light section could not be read: %s", project.scene_error)
        return 0
    want_camera = settings.sync_camera and len(project.camera_keys) > 0
    want_light = settings.sync_light and len(project.light_keys) > 0
    log.info(
        "scene elements: camera keys=%d (sync=%s), light keys=%d (sync=%s)",
        len(project.camera_keys),
        settings.sync_camera,
        len(project.light_keys),
        settings.sync_light,
    )
    if want_camera:
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
    if want_light:
        first = project.light_keys[0]
        log.info(
            "first light key: frame=%d color=%s direction=%s",
            first.frame,
            first.color,
            first.direction,
        )
    done = 0
    resettable = []
    camera_owners = []
    light_owners = []
    kept = []
    if want_camera:
        root, camera = ensure_camera_rig(scene, settings)
        resettable += [root, camera]
        camera_owners = collect_owners([root, camera])
        low, high = frame_span(k.frame for k in project.camera_keys)
        kept += snapshot_outside(camera_owners, low, high)
    if want_light:
        root, light = ensure_light_rig(scene, settings)
        resettable += [root, light]
        light_owners = collect_owners([root, light])
        low, high = frame_span(k.frame for k in project.light_keys)
        kept += snapshot_outside(light_owners, low, high)
    log.info("keeping keys outside the PMM span on %d camera/light curve(s)", len(kept))
    clear_bridge_nla(camera_owners + light_owners)
    reset_animation(resettable)
    purge_actions(ACTION_PREFIX + "Scene")
    purge_actions(ACTION_PREFIX + "Light")
    if want_camera:
        import_camera(scene, project, settings)
        done += 1
    if want_light:
        import_light(scene, project, settings)
        done += 1
    restored = restore_points(kept)
    tag_actions(camera_owners, "Scene")
    tag_actions(light_owners, "Light")
    log.info("restored %d preserved camera/light key(s)", restored)
    return done
