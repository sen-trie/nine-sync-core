import bpy
import os
import platform
import re

from .. import hooks
from ..common.addon import addon_version
from ..common.constants import (
    LOG_TEXT_NAME,
    NINESYNC_NAME,
    PMM_FPS,
    REPORT_LOG_LINES,
)
from ..common.scene_utils import scene_fps
from .mmd_tools_link import MMD_TOOLS_MIN_VERSION, mmd_tools_status, mmd_tools_version
from .retime import retime_factor
from .scale import current_scale
from .targets import item_state, item_status_text


# Strips the user name out of home folders so a support report can be posted
# publicly.
def redact_paths(text):
    text = re.sub(r"([A-Za-z]:[\\/]+Users[\\/]+)[^\\/\"']+", r"\1<user>", text)
    text = re.sub(r"(/(?:home|Users)/)[^/\"'\s]+", r"\1<user>", text)
    return text


# Support report: versions, options, per-model status, FX report lines and the
# recent log.
def build_report(context):
    scene = context.scene
    settings = scene.mmd_bridge
    lines = [NINESYNC_NAME + " support report"]
    lines.append(
        "add-on %s | Blender %s | %s"
        % (
            ".".join(str(v) for v in addon_version()),
            bpy.app.version_string,
            platform.system(),
        )
    )
    lines.append(
        "mmd_tools %s (%s, supported from %s) | NineSync FX installed: %s"
        % (mmd_tools_version(), mmd_tools_status()[0], ".".join(map(str, MMD_TOOLS_MIN_VERSION)), hooks.fx_installed())
    )
    lines.append(
        "scene fps %.3f (PMM is %d) | retime factor %.4f"
        % (scene_fps(scene), PMM_FPS, retime_factor(scene, settings))
    )
    lines.append(
        "PMM file: %s"
        % (os.path.basename(bpy.path.abspath(settings.filepath)) or "none")
    )
    lines.append("status: %s" % settings.status)
    lines.append(
        "options: auto_scale=%s scale=%.4f clips=%s uv_morphs=%s retime=%s audio=%s physics=%s frame_range=%s layout=%s camera=%s light=%s accessories_in_pmm=%d"
        % (
            settings.auto_scale,
            current_scale(settings),
            settings.use_clips,
            settings.uv_morphs,
            settings.retime_fps,
            settings.sync_audio,
            settings.use_physics_vmd,
            settings.update_scene_settings,
            settings.interpolation_layout,
            settings.sync_camera,
            settings.sync_light,
            settings.pmm_accessory_count,
        )
    )
    lines.append("audio: %s" % (settings.audio_note or "not read"))
    for item in settings.targets:
        lines.append(
            "model '%s' -> %s | %s | motion=%s | shapekey=%s | %s"
            % (
                item.pmm_name,
                item.target.name if item.target else "none",
                item_state(item),
                item.clip.name if item.clip else "none",
                item.shape_clip.name if item.shape_clip else "none",
                item_status_text(item) or "no issues",
            )
        )
    for extra in hooks.run("report", context):
        lines.extend(extra or [])
    block = bpy.data.texts.get(LOG_TEXT_NAME)
    lines.append("")
    lines.append("---- last %d log lines ----" % REPORT_LOG_LINES)
    if block is not None:
        lines.extend(block.as_string().splitlines()[-REPORT_LOG_LINES:])
    else:
        lines.append("no log text block")
    return redact_paths("\n".join(lines))
