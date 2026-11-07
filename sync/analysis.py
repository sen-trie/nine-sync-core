import bpy
import os
import types

from ..common.scene_utils import model_key
from ..pmm import PmmError, parse_pmm
from .motion import summarize_model, track_range
from .plan import build_plan, parse_checked
from .retime import retime_factor, retime_project
from .scale import current_scale


_cache = {"result": None}


# Cheap fingerprint of every preview input, cheap enough to compare on each redraw. It
# cannot see a keyframe edit inside an action; a sync always replans, so that only
# means the preview can lag, never that a stale plan is executed.
def input_token(scene):
    settings = scene.mmd_bridge
    path = bpy.path.abspath(settings.filepath)
    try:
        stat = os.stat(path)
        file_state = (stat.st_size, stat.st_mtime_ns)
    except OSError:
        file_state = None
    rows = tuple(
        (
            row.key,
            row.enabled,
            row.target.name if row.target else "",
            row.clip.name if row.clip else "",
            row.shape_clip.name if row.shape_clip else "",
            row.physics_vmd,
            row.sync_record,
        )
        for row in settings.targets
    )
    return (
        path,
        file_state,
        rows,
        round(current_scale(settings), 6),
        round(retime_factor(scene, settings), 6),
        settings.uv_morphs,
        settings.update_scene_settings,
        settings.use_physics_vmd,
        settings.sync_camera,
        settings.sync_light,
        settings.follow_camera,
        settings.follow_rotation,
        settings.sync_visibility,
        settings.scene_record,
        len(bpy.data.actions),
    )


# Reads the PMM and builds the summaries and plan. Source numbers are taken before
# retiming, so the inspector matches the PMM; the plan sees the scene's frames, as a
# sync would. Nothing in the scene or bpy.data is written.
def analyze(scene):
    settings = scene.mmd_bridge
    result = types.SimpleNamespace(
        token=input_token(scene), error="", summaries=[], plan=None, scene_tracks=[]
    )
    _cache["result"] = result
    path = bpy.path.abspath(settings.filepath)
    if not settings.filepath:
        result.error = "No PMM selected"
        return result
    try:
        with open(path, "rb") as handle:
            project = parse_checked(handle.read(), parse_pmm)
    except (PmmError, OSError) as error:
        result.error = "Could not read the PMM: %s" % error
        return result
    for model in project.models:
        summary = summarize_model(model)
        summary.key = model_key(model)
        result.summaries.append(summary)
    if project.scene_error:
        result.scene_tracks.append("Camera and light could not be read")
    else:
        for name, keys in (("Camera", project.camera_keys), ("Light", project.light_keys)):
            result.scene_tracks.append(describe_track(name, keys))
    factor = retime_factor(scene, settings)
    if factor != 1.0:
        retime_project(project, factor)
    result.plan = build_plan(scene, project)
    return result


def describe_track(name, keys):
    span = track_range([keys])
    if span is None:
        return "%s: no keys" % name
    return "%s: %d key(s), frames %d-%d" % (name, len(keys), span[0], span[1])


# The cached analysis, with .stale set when an input changed since it was built.
def current_analysis(scene):
    result = _cache["result"]
    if result is not None:
        result.stale = result.token != input_token(scene)
    return result


def clear_analysis():
    _cache["result"] = None
