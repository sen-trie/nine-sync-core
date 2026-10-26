import bpy
import hashlib
import math
import os
import time
import traceback
import types

from .. import hooks
from ..common.addon import addon_version
from ..common.constants import PMM_FPS
from ..common.logger import log
from ..common.scene_utils import fps_differs, model_key, redraw_all, scene_fps
from ..pmm import PmmError, parse_pmm
from .audio import describe_audio, import_audio
from .model_import import sync_target
from .retime import retime_factor, retime_project
from .scale import current_scale
from .scene_elements import import_scene_elements
from .targets import refresh_targets
from .tempfiles import sweep_stale_temp


_last_sync = {"digest": None, "failed": 0}


# Logs size, mtime and a content hash of the PMM, and warns when the content equals
# the previous sync's. That is the usual 'forgot to save in MMD' cause.
def log_file_state(path, data):
    digest = hashlib.md5(data).hexdigest()[:12]
    stat = os.stat(path)
    log.info(
        "PMM file: size=%d bytes, modified=%s, content hash=%s",
        stat.st_size,
        time.strftime("%H:%M:%S", time.localtime(stat.st_mtime)),
        digest,
    )
    if digest == _last_sync["digest"]:
        log.warning(
            "PMM content is unchanged since the previous sync attempt (save in MMD if you expected new data)"
        )
    _last_sync["digest"] = digest


# Logs the first camera and light keys, to check them against MMD without a
# debugger.
def log_scene_keys(project):
    for key in project.camera_keys[:40]:
        log.info(
            "PMM camera key f=%d position=(%.2f, %.2f, %.2f) angle_deg=(%.2f, %.2f, %.2f) distance=%.2f fov=%d follow=(model %d, bone %d)",
            key.frame,
            *key.look_at,
            *(math.degrees(v) for v in key.angle),
            key.distance,
            key.fov,
            key.parent_model,
            key.parent_bone,
        )
    for key in project.light_keys[:40]:
        log.info(
            "PMM light key f=%d rgb=(%d, %d, %d) direction=(%.2f, %.2f, %.2f)",
            key.frame,
            *(round(v * 255) for v in key.color),
            *key.direction,
        )


# Builds the ctx handed to post_sync hooks. Built after retiming; its fields are
# part of hooks.API_VERSION.
def hook_context(scene, project, path, settings):
    targets = {}
    for item in settings.targets:
        if item.target is not None:
            targets[item.key] = item.target
    return types.SimpleNamespace(
        scene=scene,
        project=project,
        pmm_path=path,
        targets=targets,
        unit=current_scale(settings),
        retimed=True,
        integrated=True,
        core_version=addon_version(),
    )


# Full sync. Order matters: models, camera and light, FX hooks (accessories), then
# audio. Returns (items imported, models in the PMM).
def sync(scene):
    settings = scene.mmd_bridge
    path = bpy.path.abspath(settings.filepath)
    log.info("sync start: %s", path)
    sweep_stale_temp()
    with open(path, "rb") as handle:
        data = handle.read()
    log_file_state(path, data)
    project = parse_pmm(data)
    settings.pmm_accessory_count = len(project.accessories)
    factor = retime_factor(scene, settings)
    if factor != 1.0:
        retime_project(project, factor)
        log.info(
            "retimed PMM keys by x%.4f for a %.3f fps scene (PMM is %d fps)",
            factor,
            scene_fps(scene),
            PMM_FPS,
        )
    elif settings.retime_fps and settings.update_scene_settings and fps_differs(scene):
        log.info(
            "retiming skipped because Update Frame Range will switch the scene to 30 fps"
        )
    log_scene_keys(project)
    refresh_targets(settings, project)
    settings.audio_note = describe_audio(project, settings)
    log.info(
        "using scale %.4f (%s)",
        current_scale(settings),
        "auto from model" if settings.auto_scale else "manual",
    )
    by_key = {model_key(m): m for m in project.models}
    imported = 0
    failed = 0
    for target in settings.targets:
        if not target.enabled:
            log.info("skip '%s': disabled", target.pmm_name)
            continue
        if target.target is None:
            log.warning("skip '%s': no target object assigned", target.pmm_name)
            continue
        model = by_key.get(target.key)
        if model is None:
            log.warning("skip '%s': not present in PMM", target.pmm_name)
            target.state = "ERROR"
            target.status_text = "Not present in the PMM"
            continue
        if sync_target(target, model, settings):
            imported += 1
        else:
            failed += 1
    _last_sync["failed"] = failed
    imported += import_scene_elements(scene, project, settings)
    if project.accessories and not hooks.fx_installed():
        log.info(
            "PMM has %d accessory(ies); install NineSync: FX to import them",
            len(project.accessories),
        )
    context = hook_context(scene, project, path, settings)
    for result in hooks.run("post_sync", context):
        if isinstance(result, int):
            imported += result
    try:
        if import_audio(scene, project, settings):
            imported += 1
    except Exception as error:
        log.error("audio failed: %s\n%s", error, traceback.format_exc())
    scene.frame_set(scene.frame_current)
    log.info(
        "sync done: %d item(s) imported, %d model(s) in PMM",
        imported,
        len(project.models),
    )
    return imported, len(project.models)


# Runs sync and turns any failure into a status message instead of an exception, so
# the operator can report it.
def run_sync(scene):
    settings = scene.mmd_bridge
    try:
        imported, total = sync(scene)
    except (PmmError, OSError) as error:
        log.error("PMM read failed: %s", error)
        settings.status = "PMM read failed: %s" % error
        redraw_all()
        return False
    except Exception as error:
        log.error("sync failed: %s\n%s", error, traceback.format_exc())
        settings.status = "Sync failed: %s" % error
        redraw_all()
        return False
    if _last_sync["failed"]:
        settings.status = "Synced %d item(s), %d model(s) failed at %s" % (
            imported,
            _last_sync["failed"],
            time.strftime("%H:%M:%S"),
        )
    else:
        settings.status = "Synced %d item(s) at %s" % (
            imported,
            time.strftime("%H:%M:%S"),
        )
    redraw_all()
    return True
