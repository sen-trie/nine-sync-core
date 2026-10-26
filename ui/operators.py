import bpy
import os
import time
from bpy.props import BoolProperty, FloatProperty
from bpy.types import Operator

from .. import hooks
from ..common.constants import REPORT_TEXT_NAME
from ..common.logger import log
from ..common.scene_utils import redraw_all
from ..pmm import PmmError, parse_pmm_file
from ..sync.audio import describe_audio
from ..sync.clean import run_clean
from ..sync.engine import run_sync
from ..sync.mmd_tools_link import mmd_tools_available
from ..sync.report import build_report
from ..sync.scale import current_scale
from ..sync.targets import refresh_targets
from .helpers import draw_wrapped
from .preferences import addon_preferences, apply_import_scale
from .relink import refuse_if_outdated


class MMDBRIDGE_OT_sync_now(Operator):
    bl_idname = "ninesync.sync_now"
    bl_label = "Sync Now"

    def execute(self, context):
        if refuse_if_outdated(self, context):
            return {"CANCELLED"}
        settings = context.scene.mmd_bridge
        path = bpy.path.abspath(settings.filepath)
        log.info("Sync Now pressed")
        if not mmd_tools_available():
            self.report({"ERROR"}, "mmd_tools is not enabled")
            log.error("mmd_tools operator MMD_TOOLS_OT_import_vmd not found")
            return {"CANCELLED"}
        if not os.path.isfile(path):
            self.report({"ERROR"}, "PMM file not found")
            log.error("PMM file not found: %s", path)
            return {"CANCELLED"}
        if not run_sync(context.scene):
            self.report({"ERROR"}, settings.status)
            return {"CANCELLED"}
        self.report({"INFO"}, settings.status)
        return {"FINISHED"}


class MMDBRIDGE_OT_scan(Operator):
    bl_idname = "ninesync.scan"
    bl_label = "Read Models"

    def execute(self, context):
        if refuse_if_outdated(self, context):
            return {"CANCELLED"}
        settings = context.scene.mmd_bridge
        try:
            project = parse_pmm_file(bpy.path.abspath(settings.filepath))
        except (OSError, PmmError) as error:
            self.report({"ERROR"}, str(error))
            log.error("scan failed: %s", error)
            return {"CANCELLED"}
        refresh_targets(settings, project)
        settings.pmm_accessory_count = len(project.accessories)
        settings.audio_note = describe_audio(project, settings)
        self.report(
            {"INFO"},
            "Found %d model(s), %d accessory(ies)"
            % (len(project.models), len(project.accessories)),
        )
        return {"FINISHED"}


class MMDBRIDGE_OT_clean(Operator):
    bl_idname = "ninesync.clean"
    bl_label = "Clean"
    bl_description = "Remove data created by this add-on. Your own actions, clips and shape keys are never touched"
    bl_options = {"REGISTER", "UNDO"}

    animation: BoolProperty(
        name="Model animation clips",
        description="Remove bridge-created motion, shapekey and physics clips and their NLA tracks",
        default=True,
    )
    pose: BoolProperty(
        name="Reset bones and shape keys the clips animated",
        description="Return only the bones and shape keys that the removed clips animated to their rest values",
        default=True,
    )
    uv: BoolProperty(
        name="UV morph data",
        description="Remove the MMDUV_ shape keys, offset attributes, shader nodes and drivers",
        default=True,
    )
    scene: BoolProperty(
        name="Camera and light rigs",
        description="Delete the MMD camera and sun rigs and their animation",
        default=False,
    )
    fx: BoolProperty(
        name="Accessories & effects (NineSync: FX)",
        description="Delete imported accessory objects, their effects and their animation",
        default=False,
    )
    audio: BoolProperty(
        name="Audio strip",
        description="Remove the sound strip added by the bridge",
        default=False,
    )

    @classmethod
    def poll(cls, context):
        return mmd_tools_available()

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=380)

    def draw(self, context):
        layout = self.layout
        draw_wrapped(
            layout,
            "Only data created by this add-on is removed. Actions, clips and shape keys you made or picked yourself are kept.",
            52,
        )
        layout.prop(self, "animation")
        row = layout.row()
        row.enabled = self.animation
        row.prop(self, "pose")
        layout.prop(self, "uv")
        layout.prop(self, "scene")
        if hooks.fx_installed():
            layout.prop(self, "fx")
        layout.prop(self, "audio")

    def execute(self, context):
        if refuse_if_outdated(self, context):
            return {"CANCELLED"}
        settings = context.scene.mmd_bridge
        options = {
            "animation": self.animation,
            "pose": self.pose,
            "uv": self.uv,
            "scene": self.scene,
            "fx": self.fx and hooks.fx_installed(),
            "audio": self.audio,
        }
        counts = run_clean(context.scene, settings, options)
        settings.status = "Cleaned at %s" % time.strftime("%H:%M:%S")
        self.report(
            {"INFO"},
            "Removed %d clip(s), %d UV key(s), %d rig(s), %d accessory(ies), %d audio strip(s); reset %d channel(s)"
            % (
                counts["clips"],
                counts["uv_keys"],
                counts["rigs"],
                counts["accessories"],
                counts["audio"],
                counts["reset"],
            ),
        )
        redraw_all()
        return {"FINISHED"}


class MMDBRIDGE_OT_copy_report(Operator):
    bl_idname = "ninesync.copy_report"
    bl_label = "Support Report"
    bl_description = "Copy a support report with versions, settings, model status and the recent log to the clipboard. User folders are redacted, but check it before posting"

    def execute(self, context):
        text = build_report(context)
        context.window_manager.clipboard = text
        block = bpy.data.texts.get(REPORT_TEXT_NAME) or bpy.data.texts.new(
            REPORT_TEXT_NAME
        )
        block.clear()
        block.write(text)
        self.report(
            {"INFO"},
            "Support report copied (%d lines) and saved to the %s text"
            % (len(text.splitlines()), REPORT_TEXT_NAME),
        )
        return {"FINISHED"}


class NINESYNC_OT_set_import_scale(Operator):
    bl_idname = "ninesync.set_import_scale"
    bl_label = "Set Import Scale"
    bl_description = "Import new MMD models and motions at this scale from now on (presets mmd_tools' importers, remembers it in the add-on preferences and shares it with NineSync: FX when installed). Models already in the scene keep their scale; re-import them to clear the camera warning"

    scale: FloatProperty(default=1.0, options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        prefs = addon_preferences()
        if prefs is not None:
            prefs.use_import_scale = True
            prefs.import_scale = self.scale
            context.preferences.is_dirty = True
        applied = apply_import_scale(self.scale)
        log.info("import scale preset to %g for %s", self.scale, applied)
        self.report({"INFO"}, "New imports will use scale %g (%s)" % (self.scale, ", ".join(applied) or "saved for next start"))
        return {"FINISHED"}


class MMDBRIDGE_OT_dismiss_scale_warning(Operator):
    bl_idname = "ninesync.dismiss_scale_warning"
    bl_label = "Dismiss"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        settings = context.scene.mmd_bridge
        settings.scale_warning_dismissed_for = current_scale(settings)
        redraw_all()
        return {"FINISHED"}
