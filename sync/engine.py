import bpy
import math
import os
import time
import traceback
import types

from .. import hooks
from ..common.addon import addon_version
from ..common.constants import PMM_FPS
from ..common.logger import log
from ..common.scene_utils import model_key, redraw_all, scene_fps
from ..common.timing import begin_timings, finish_timings, phase
from ..pmm import PmmError, parse_pmm
from .analysis import clear_analysis, content_digest, parsed_copy
from .audio import describe_audio, import_audio
from .model_import import sync_target
from .plan import BLOCKED, PART_LABELS, SKIP, build_plan, parse_checked
from .progress import SyncProgress
from .retime import retime_factor, retime_project
from .scale import current_scale
from .scene_elements import import_scene_elements
from .sync_state import upgrade_scene_settings, upgrade_settings
from .targets import refresh_targets
from .tempfiles import sweep_stale_temp


_last_sync = {"digest": None, "failed": 0}
_active = {"driver": None}


# Raised at a work-unit boundary after the user cancels. Never raised inside a model's
# import, so a cancelled run leaves each model either fully synced or untouched.
class SyncCancelled(Exception):
    pass


# Options and tallies of one sync. Generator steps report through here.
class SyncRun:
    def __init__(self, force=False, force_keys=()):
        self.force = force
        self.force_keys = tuple(force_keys)
        self.cancelled = False
        self.imported = 0
        self.total = 0
        self.failed = 0
        self.synced = 0
        self.skipped = 0
        self.blocked = 0
        self.keys = 0

    # Hands control back to the caller, then honours a cancel made meanwhile.
    def tick(self, fraction, text):
        yield fraction, text
        if self.cancelled:
            raise SyncCancelled()


# Logs size, mtime and a content hash of the PMM, and warns when the content equals
# the previous sync's. That is the usual 'forgot to save in MMD' cause. Returns the hash.
def log_file_state(path, data):
    digest = content_digest(data)
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
    return digest


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


# Puts a row that ran nothing into its state. A previous warning stays on show.
def mark_unchanged(row, entry):
    if row.state != "WARN":
        row.state = "OK"
        row.status_text = next(iter(entry.parts.values())).reason if entry.parts else ""


def log_parts(entry):
    for name, part in entry.parts.items():
        level = log.warning if part.operation == BLOCKED else log.info
        level(
            "plan '%s' %s: %s -> %s (%s)",
            entry.pmm_name,
            PART_LABELS[name],
            part.operation,
            part.destination,
            part.reason,
        )
        for warning in part.warnings:
            log.warning("plan '%s' %s: %s", entry.pmm_name, PART_LABELS[name], warning)
    for warning in entry.warnings:
        log.warning("plan '%s': %s", entry.pmm_name, warning)


# Executes one plan entry; the plan already decided what runs.
def apply_entry(run, entry, row, model, settings):
    if not entry.selected:
        log.info("skip '%s': disabled", entry.pmm_name)
        return
    log_parts(entry)
    upgrade_settings(row, entry)
    if entry.reason:
        log.warning("skip '%s': %s", entry.pmm_name, entry.reason)
        run.blocked += 1
        row.state = "ERROR"
        row.status_text = entry.reason
        return
    if not entry.run_parts:
        blocked = [p for p in entry.parts.values() if p.operation == BLOCKED]
        if blocked:
            run.blocked += 1
            row.state = "WARN"
            row.status_text = blocked[0].reason[:120]
        else:
            run.skipped += 1
            mark_unchanged(row, entry)
        return
    if sync_target(row, model, settings, entry):
        run.imported += 1
        run.synced += 1
        run.keys += sum(entry.parts[name].keys for name in entry.run_parts)
        blocked = [p for p in entry.parts.values() if p.operation == BLOCKED]
        if blocked and row.state == "OK":
            row.state = "WARN"
            row.status_text = blocked[0].reason[:120]
    else:
        run.failed += 1


# Full sync as a series of steps. Each yield is a (fraction, stage) pair and a point
# where the caller may redraw or cancel. Order matters: models, camera and light, FX
# hooks (accessories), then audio.
def sync_steps(scene, run):
    settings = scene.mmd_bridge
    path = bpy.path.abspath(settings.filepath)
    log.info("sync start: %s", path)
    sweep_stale_temp()
    yield from run.tick(0.02, "Reading PMM")
    with phase("read"):
        with open(path, "rb") as handle:
            data = handle.read()
    digest = log_file_state(path, data)
    yield from run.tick(0.05, "Parsing PMM")
    with phase("parse"):
        project = parsed_copy(digest)
        if project is None:
            project = parse_checked(data, parse_pmm)
        else:
            log.info("PMM unchanged since the preview read it; its parse is reused")
    run.total = len(project.models)
    settings.pmm_accessory_count = len(project.accessories)
    factor = retime_factor(scene, settings)
    if factor != 1.0:
        with phase("retime"):
            retime_project(project, factor)
        log.info(
            "retimed PMM keys by x%.4f for a %.3f fps scene (PMM is %d fps)",
            factor,
            scene_fps(scene),
            PMM_FPS,
        )
    log_scene_keys(project)
    refresh_targets(settings, project)
    settings.audio_note = describe_audio(project, settings)
    log.info(
        "using scale %.4f (%s)",
        current_scale(settings),
        "auto from model" if settings.auto_scale else "manual",
    )
    yield from run.tick(0.08, "Checking for changes")
    with phase("plan"):
        plan = build_plan(scene, project, run.force, run.force_keys, check_edits=False)
    rows = {row.key: row for row in settings.targets}
    by_key = {model_key(m): m for m in project.models}
    entries = plan.items
    for position, entry in enumerate(entries):
        verb = "Importing" if entry.run_parts else "Checking"
        yield from run.tick(
            0.1 + 0.7 * position / len(entries),
            "%s model %d of %d: %s" % (verb, position + 1, len(entries), entry.pmm_name),
        )
        row = rows.get(entry.key)
        model = by_key.get(entry.key)
        if row is not None and model is not None:
            apply_entry(run, entry, row, model, settings)
    _last_sync["failed"] = run.failed
    yield from run.tick(0.82, "Updating camera and light")
    with phase("camera_light"):
        upgrade_scene_settings(settings, plan.source, plan.scene)
        run.imported += import_scene_elements(scene, project, settings, plan)
    if project.accessories and not hooks.fx_installed():
        log.info(
            "PMM has %d accessory(ies); install NineSync: FX to import them",
            len(project.accessories),
        )
    yield from run.tick(0.88, "Running add-on hooks")
    context = hook_context(scene, project, path, settings)
    with phase("hooks"):
        for result in hooks.run("post_sync", context):
            if isinstance(result, int):
                run.imported += result
    yield from run.tick(0.94, "Updating audio")
    try:
        with phase("audio"):
            if import_audio(scene, project, settings):
                run.imported += 1
    except Exception as error:
        log.error("audio failed: %s\n%s", error, traceback.format_exc())
    yield from run.tick(0.98, "Finalizing")
    scene.frame_set(scene.frame_current)
    log.info(
        "sync done: %d item(s) imported, %d model(s) synced, %d unchanged, %d blocked, %d model(s) in PMM",
        run.imported,
        run.synced,
        run.skipped,
        run.blocked,
        run.total,
    )


# Runs every step in one go. Returns (items imported, models in the PMM).
def sync(scene, force=False, force_keys=()):
    run = SyncRun(force, force_keys)
    for _step in sync_steps(scene, run):
        pass
    return run.imported, run.total


def status_line(run):
    parts = ["Synced %d item(s)" % run.imported]
    if run.skipped:
        parts.append("%d unchanged" % run.skipped)
    if run.blocked:
        parts.append("%d blocked" % run.blocked)
    if run.failed:
        parts.append("%d model(s) failed" % run.failed)
    return "%s at %s" % (", ".join(parts), time.strftime("%H:%M:%S"))


# One sync, advanced a step at a time, so a modal operator and a plain call share every
# outcome path. Any failure becomes a status message instead of an exception.
class SyncDriver:
    def __init__(self, scene, force=False, force_keys=()):
        self.scene = scene
        self.run = SyncRun(force, force_keys)
        self.steps = sync_steps(scene, self.run)
        self.progress = SyncProgress()
        self.ok = False
        self.closed = False
        self.outcome = "error"
        _active["driver"] = self
        begin_timings()
        self.progress.begin()

    # Advances one step. False once the sync is over, however it ended.
    def step(self):
        settings = self.scene.mmd_bridge
        try:
            fraction, text = next(self.steps)
        except StopIteration:
            self.ok = True
            self.outcome = "ok"
            settings.status = status_line(self.run)
        except SyncCancelled:
            self.outcome = "cancelled"
            done = self.run.synced + self.run.skipped
            settings.status = "Cancelled after %d of %d model(s) at %s" % (
                done,
                self.run.total,
                time.strftime("%H:%M:%S"),
            )
            log.info("sync cancelled after %d model(s)", done)
            self.scene.frame_set(self.scene.frame_current)
        except (PmmError, OSError) as error:
            log.error("PMM read failed: %s", error)
            settings.status = "PMM read failed: %s" % error
        except Exception as error:
            log.error("sync failed: %s\n%s", error, traceback.format_exc())
            settings.status = "Sync failed: %s" % error
        else:
            self.progress.update(fraction, text)
            settings.status = text
            redraw_all()
            return True
        self.close()
        return False

    def cancel(self):
        self.run.cancelled = True

    # Releases everything the sync holds. Safe to repeat, and it runs on every exit
    # path, including Blender dropping the modal handler.
    def close(self):
        if self.closed:
            return
        self.closed = True
        self.steps.close()
        if self.outcome == "ok":
            self.progress.update(1.0, "Done")
        self.progress.end()
        _active["driver"] = None
        run = self.run
        finish_timings(
            self.outcome,
            models=run.total,
            synced=run.synced,
            unchanged=run.skipped,
            blocked=run.blocked,
            failed=run.failed,
            source_keys=run.keys,
        )
        clear_analysis()
        redraw_all()


def sync_running():
    return _active["driver"] is not None


# Sync to completion in one call, for scripts and background mode.
def run_sync(scene, force=False, force_keys=()):
    driver = SyncDriver(scene, force, force_keys)
    try:
        while driver.step():
            pass
    finally:
        driver.close()
    return driver.ok


# After a file load the old driver's data is gone; only its bookkeeping is released.
def abandon_sync():
    driver = _active["driver"]
    if driver is not None:
        driver.outcome = "abandoned"
        driver.close()
