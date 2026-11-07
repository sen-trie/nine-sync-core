import bpy
import os
import time
from bpy.props import BoolProperty, FloatProperty, IntProperty, StringProperty
from bpy.types import Operator

from .. import hooks
from ..common.constants import REPORT_TEXT_NAME
from ..common.logger import log
from ..common.scene_utils import redraw_all
from ..pmm import PmmError, parse_pmm_file
from ..sync.accessory_export import refresh_prop_colors
from ..sync.audio import describe_audio
from ..sync.clean import run_clean
from ..sync.analysis import analyze, clear_analysis
from ..sync.engine import SyncDriver, run_sync, sync_running
from ..sync.mmd_tools_link import mmd_tools_available, mmd_tools_status
from ..sync.project_export import create_pmm, refresh_export_items
from ..sync.rigs import ensure_camera_rig
from ..sync.report import build_report
from ..sync.root_motion import convert_root_motion
from ..sync.scale import current_scale
from ..sync.targets import refresh_targets
from .helpers import draw_wrapped
from .panels.preview import draw_preview
from .preferences import addon_preferences, apply_import_scale
from .relink import refuse_if_outdated


class MMDBRIDGE_OT_sync_now(Operator):
    bl_idname = "ninesync.sync_now"
    bl_label = "Sync Now"
    bl_description = "Sync the selected models from the PMM. Unchanged models are skipped. Esc cancels between models"

    force: BoolProperty(
        name="Force Resync",
        description="Import every selected model even when nothing changed",
        default=False,
        options={"SKIP_SAVE"},
    )
    force_key: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def description(cls, context, properties):
        if properties.force or properties.force_key:
            return "Import again even though nothing changed. Inside the PMM frame span this replaces any edits made in Blender; keys outside it are kept"
        return cls.bl_description

    def ready(self, context):
        if refuse_if_outdated(self, context):
            return False
        if sync_running():
            self.report({"WARNING"}, "A sync is already running")
            return False
        path = bpy.path.abspath(context.scene.mmd_bridge.filepath)
        log.info("Sync Now pressed")
        if not mmd_tools_available():
            detail = mmd_tools_status()[1]
            self.report({"ERROR"}, detail)
            log.error("mmd_tools unusable: %s", detail)
            return False
        if not os.path.isfile(path):
            self.report({"ERROR"}, "PMM file not found")
            log.error("PMM file not found: %s", path)
            return False
        return True

    def force_keys(self):
        return (self.force_key,) if self.force_key else ()

    def conclude(self, context, ok):
        status = context.scene.mmd_bridge.status
        self.report({"INFO"} if ok else {"ERROR"}, status)
        return {"FINISHED"} if ok else {"CANCELLED"}

    # Blocking path for scripts and background mode, where no event loop can tick.
    def execute(self, context):
        if not self.ready(context):
            return {"CANCELLED"}
        return self.conclude(context, run_sync(context.scene, self.force, self.force_keys()))

    # One work unit per timer tick, so Blender redraws the progress between models and
    # Esc is heard. Everything stays on the main thread.
    def invoke(self, context, event):
        if not self.ready(context):
            return {"CANCELLED"}
        self._driver = SyncDriver(context.scene, self.force, self.force_keys())
        manager = context.window_manager
        self._timer = manager.event_timer_add(0.01, window=context.window)
        manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type == "ESC" and event.value == "PRESS":
            self._driver.cancel()
        elif event.type == "TIMER":
            if not self._driver.step():
                self.release(context)
                if self._driver.outcome == "cancelled":
                    self.report({"WARNING"}, context.scene.mmd_bridge.status)
                    return {"CANCELLED"}
                return self.conclude(context, self._driver.ok)
        return {"RUNNING_MODAL"}

    def cancel(self, context):
        self.release(context)
        self._driver.close()

    def release(self, context):
        context.window_manager.event_timer_remove(self._timer)


class NINESYNC_OT_preview(Operator):
    bl_idname = "ninesync.preview"
    bl_label = "Preview"
    bl_description = "Read the PMM and show which PMX models changed since the last sync. Changes nothing in the scene"

    confirmed: BoolProperty(options={"HIDDEN", "SKIP_SAVE"})

    # Preview itself never syncs: only the dialog's own button sets confirmed. A plain
    # execute just reads the PMM, which is how the Inspector refreshes.
    def execute(self, context):
        if refuse_if_outdated(self, context):
            return {"CANCELLED"}
        if self.confirmed:
            bpy.ops.ninesync.sync_now("INVOKE_DEFAULT")
            return {"FINISHED"}
        result = analyze(context.scene)
        if result.error:
            self.report({"WARNING"}, result.error)
        redraw_all()
        return {"FINISHED"}

    def invoke(self, context, event):
        if refuse_if_outdated(self, context):
            return {"CANCELLED"}
        analyze(context.scene)
        self.confirmed = True
        return context.window_manager.invoke_props_dialog(
            self, width=640, title="Sync Preview", confirm_text="Sync Now"
        )

    def draw(self, context):
        draw_preview(self.layout, context.scene)


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
        clear_analysis()
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
    dismiss: BoolProperty(default=False, options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        if self.dismiss and context.scene is not None:
            settings = context.scene.mmd_bridge
            settings.scale_warning_dismissed_for = current_scale(settings)
            redraw_all()
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


class NINESYNC_OT_spawn_camera(Operator):
    bl_idname = "ninesync.spawn_camera"
    bl_label = "Spawn MMD Camera"
    bl_description = "Create the MMD camera rig that the Syncer uses, or select the existing one, and pick it as Create PMM's MMD Camera if none is picked"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return mmd_tools_available()

    def execute(self, context):
        if refuse_if_outdated(self, context):
            return {"CANCELLED"}
        settings = context.scene.mmd_bridge
        try:
            root, camera = ensure_camera_rig(context.scene, settings, rebuild=False)
        except Exception as error:
            self.report({"ERROR"}, "Could not create the MMD camera: %s" % error)
            log.error("spawn camera failed: %s", error)
            return {"CANCELLED"}
        context.scene.camera = camera
        if settings.export_camera_object is None:
            settings.export_camera_object = root
        for obj in context.selected_objects:
            obj.select_set(False)
        if root.name in context.view_layer.objects:
            root.select_set(True)
            context.view_layer.objects.active = root
        self.report({"INFO"}, "MMD camera rig: %s" % root.name)
        redraw_all()
        return {"FINISHED"}


class NINESYNC_OT_refresh_export(Operator):
    bl_idname = "ninesync.refresh_export"
    bl_label = "Find MMD Armatures"
    bl_description = "List the mmd_tools models in this file and find the PMX each was loaded from"

    def execute(self, context):
        if refuse_if_outdated(self, context):
            return {"CANCELLED"}
        refresh_export_items(context.scene.mmd_bridge)
        redraw_all()
        return {"FINISHED"}


# True for a mesh that belongs to an mmd_tools model; those travel with the PMX.
def inside_mmd_model(obj):
    while obj is not None:
        if getattr(obj, "mmd_type", "") == "ROOT":
            return True
        obj = obj.parent
    return False


class NINESYNC_OT_add_export_props(Operator):
    bl_idname = "ninesync.add_export_props"
    bl_label = "Add Selected Meshes"
    bl_description = "List the selected mesh objects to be written as .x accessories (blocking props). Meshes that belong to an mmd_tools model are skipped"

    def execute(self, context):
        if refuse_if_outdated(self, context):
            return {"CANCELLED"}
        settings = context.scene.mmd_bridge
        listed = {i.target for i in settings.export_props}
        added = 0
        for obj in context.selected_objects:
            if obj.type != "MESH" or obj in listed or inside_mmd_model(obj):
                continue
            item = settings.export_props.add()
            item.target = obj
            refresh_prop_colors(item)
            added += 1
        if not added:
            self.report({"WARNING"}, "No new mesh objects are selected")
        redraw_all()
        return {"FINISHED"}


class NINESYNC_OT_remove_export_prop(Operator):
    bl_idname = "ninesync.remove_export_prop"
    bl_label = "Remove Prop"
    bl_description = "Take this mesh off the prop list (the object itself is not touched)"

    index: IntProperty()

    def execute(self, context):
        settings = context.scene.mmd_bridge
        if 0 <= self.index < len(settings.export_props):
            settings.export_props.remove(self.index)
        redraw_all()
        return {"FINISHED"}


class NINESYNC_OT_refresh_prop_colors(Operator):
    bl_idname = "ninesync.refresh_prop_colors"
    bl_label = "Read Materials"
    bl_description = "List this mesh's materials. New ones start from their texture's average colour; colours already picked are kept"

    index: IntProperty()

    def execute(self, context):
        settings = context.scene.mmd_bridge
        if 0 <= self.index < len(settings.export_props):
            refresh_prop_colors(settings.export_props[self.index])
        redraw_all()
        return {"FINISHED"}


class NINESYNC_OT_convert_root_motion(Operator):
    bl_idname = "ninesync.convert_root_motion"
    bl_label = "Bake Root Motion"
    bl_description = "Move this model's root empty motion onto its parent bone and reset the root. Changes the .blend. Keys already on the bone are kept, and new keys are made only on existing keyframes"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty(options={"HIDDEN"})

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        settings = context.scene.mmd_bridge
        if not 0 <= self.index < len(settings.export_items):
            return {"CANCELLED"}
        root = settings.export_items[self.index].target
        if root is None:
            return {"CANCELLED"}
        try:
            keys, stretched = convert_root_motion(context.scene, root)
        except RuntimeError as error:
            self.report({"ERROR"}, str(error))
            log.error("convert root motion failed: %s", error)
            return {"CANCELLED"}
        self.report({"INFO"}, "'%s': root motion moved onto the parent bone, %d key(s)" % (root.name, keys))
        if stretched:
            self.report({"WARNING"}, "'%s' is scaled; the scale stays in Blender but MMD bones cannot hold it" % root.name)
        redraw_all()
        return {"FINISHED"}


class NINESYNC_OT_create_pmm(Operator):
    bl_idname = "ninesync.create_pmm"
    bl_label = "Create PMM"
    bl_description = "Write an MMD project with the checked models, the chosen camera and optionally the existing animation. Reads the scene only; Link to Syncer also sets it as the Syncer's PMM so MMD changes can be synced back"

    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.pmm", options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        return mmd_tools_available()

    def invoke(self, context, event):
        if refuse_if_outdated(self, context):
            return {"CANCELLED"}
        settings = context.scene.mmd_bridge
        self.filepath = bpy.path.abspath(settings.export_path) or (
            os.path.splitext(bpy.data.filepath)[0] + ".pmm"
            if bpy.data.filepath
            else "project.pmm"
        )
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        if refuse_if_outdated(self, context):
            return {"CANCELLED"}
        settings = context.scene.mmd_bridge
        path = bpy.path.abspath(self.filepath)
        if not path.lower().endswith(".pmm"):
            path += ".pmm"
        try:
            models, camera_keys, props, warnings = create_pmm(context.scene, settings, path)
        except (OSError, PmmError) as error:
            self.report({"ERROR"}, str(error))
            log.error("create PMM failed: %s", error)
            return {"CANCELLED"}
        settings.export_path = path
        settings.status = "Created %s at %s" % (os.path.basename(path), time.strftime("%H:%M:%S"))
        self.report(
            {"INFO"},
            "Created %s with %d model(s), %d camera key(s), %d prop(s)"
            % (os.path.basename(path), models, camera_keys, props),
        )
        for warning in warnings:
            self.report({"WARNING"}, warning)
        redraw_all()
        return {"FINISHED"}
