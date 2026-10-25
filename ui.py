import bpy
import importlib
import json
import math
import os
import textwrap
import time
import traceback
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, PointerProperty, StringProperty
from bpy.types import Operator, Panel, PropertyGroup

from .common import (
    ACCESSORY_COLLECTION,
    CAMERA_COLLECTION,
    LIGHT_COLLECTION,
    LOG_TEXT_NAME,
    MME_BACKEND_PROP,
    MME_BAKED_PROP,
    MME_CORE_FILE,
    MME_DRAW_PROP,
    MME_DRIVEN_PROP,
    MME_GN_MODIFIER,
    MME_GROUP_PROP,
    MME_KEY_PROP,
    MME_PRUNED_PROP,
    MME_SELF_NAMES,
    MME_UNIFORMS_PROP,
    MME_VARYINGS_PROP,
    NINESYNC_CATEGORY,
    NINESYNC_FX_LABEL,
    NINESYNC_MAIN_LABEL,
    PMM_FPS,
    REPORT_TEXT_NAME,
    UNIT_SCALE,
    _mme_runtimes,
    _mme_state,
    child_of_type,
    find_accessory_root,
    find_rig_root,
    fps_differs,
    log,
    mme_control_source,
    mme_control_use_prop,
    mme_effect_passes,
    mme_load_controls,
    mme_param_meta,
    redraw_all,
    scene_fps,
    short_text,
)
from .pmm import (
    PmmError,
    parse_pmm_file,
)
from .fx import (
    FxError,
)
from .mme_nodes import (
    mme_build_standard_groups,
    mme_core_names,
    mme_core_path,
    mme_save_core_library,
)
from .mme import (
    mme_bake_pass,
    mme_clear_bake,
    mme_progress_begin,
    mme_progress_end,
    mme_progress_step,
    mme_rebuild_effect,
    mme_reset_params,
    mme_target_roots,
    mme_update_scene,
)
from .bridge import (
    build_report,
    clip_poll,
    current_scale,
    describe_audio,
    first_armature,
    is_mmd_root,
    item_state,
    item_status_text,
    missing_dependencies,
    mmd_tools_available,
    on_target_changed,
    refresh_accessories,
    refresh_targets,
    run_clean,
    run_sync,
    scale_warning_active,
    status_icon,
)


def draw_wrapped(layout, text, width=38):
    column = layout.column(align=True)
    column.scale_y = 0.85
    for line in textwrap.wrap(text, width):
        column.label(text=line)


def draw_dependency_notices(layout):
    for label, message, url in missing_dependencies():
        box = layout.box()
        box.alert = True
        box.label(text=message, icon="ERROR")
        box.operator("wm.url_open", text="Get %s" % label, icon="URL").url = url


class MMDBridgeTarget(PropertyGroup):
    key: StringProperty()
    pmm_name: StringProperty()
    enabled: BoolProperty(name="Enabled", default=True)
    target: PointerProperty(
        name="Target", type=bpy.types.Object, poll=is_mmd_root, update=on_target_changed
    )
    clip: PointerProperty(
        name="Motion Clip",
        description="Action that receives this model's armature (bone) animation on every sync. Leave empty to create a new clip on its own NLA track",
        type=bpy.types.Action,
        poll=clip_poll,
    )
    shape_clip: PointerProperty(
        name="Shapekey Clip",
        description="Action that receives this model's shape key (morph) animation on every sync, for the main body mesh. Leave empty to create a new clip on its own NLA track. Other shape key holders such as the morph placeholder keep automatic clips",
        type=bpy.types.Action,
        poll=clip_poll,
    )
    state: StringProperty(default="NEW")
    status_text: StringProperty()
    physics_vmd: StringProperty(
        name="Baked Physics VMD",
        description="Experimental: VMD baked from MMD with MMDBridge. Applied as a clip above the PMM clip",
        subtype="FILE_PATH",
    )


class MMDBridgeAccessory(PropertyGroup):
    key: StringProperty()
    display: StringProperty()
    enabled: BoolProperty(name="Enabled", default=True)
    use_effect: BoolProperty(
        name="Use Effect",
        description="Run this accessory's MME effect (.fx). Turn off to import the plain mesh",
        default=True,
    )
    effect_path: StringProperty(
        name="Effect File",
        description="Effect (.fx) to use instead of the one found next to the .x or in the PMM's .emm file",
        subtype="FILE_PATH",
    )
    effect_note: StringProperty()
    show_controls: BoolProperty(name="Effect Controls", default=False)
    parent_bone: StringProperty(
        name="Parent Bone",
        description="Overrides the parent bone stored in the PMM. Leave empty to use the PMM value",
    )


class MMDBridgeSettings(PropertyGroup):
    filepath: StringProperty(name="PMM File", subtype="FILE_PATH")
    status: StringProperty(default="Idle")
    auto_scale: BoolProperty(
        name="Auto Scale From Model",
        description="Use the scale the first enabled model was imported with in mmd_tools",
        default=True,
    )
    scale: FloatProperty(
        name="Scale",
        description="Motion scale passed to the mmd_tools VMD importer when Auto Scale is off",
        default=0.08,
        min=0.001,
    )
    scale_warning_dismissed_for: FloatProperty(default=-1.0)
    sync_accessories: BoolProperty(
        name="Sync Accessories",
        description="Import and animate .x accessories stored in the PMM. Static meshes only",
        default=True,
    )
    sphere_maps: BoolProperty(
        name="Sphere Maps (Experimental)",
        description="Rebuild .sph/.spa sphere maps from accessory textures with a camera-space normal lookup",
        default=True,
    )
    accessory_dir: StringProperty(
        name="Accessory Folder",
        description="Extra folder searched for .x files when the path stored in the PMM does not exist",
        subtype="DIR_PATH",
    )
    accessories: CollectionProperty(type=MMDBridgeAccessory)
    mme_effects: BoolProperty(
        name="MME Effects (.fx)",
        description="Run the MikuMikuEffect file of each accessory: the same-name .fx next to the .x, or the one assigned in the PMM's .emm file. Vertex shaders run every frame, pixel shaders become material nodes",
        default=True,
    )
    mme_density: FloatProperty(
        name="Effect Density",
        description="Share of each LoopByCount loop to draw. 1 matches MMD. Lower values make heavy effects such as Nuke2 faster to scrub. Geometry Nodes passes update live; Python passes on the next sync or Refresh Effects",
        default=1.0,
        min=0.02,
        max=1.0,
        subtype="FACTOR",
        update=lambda self, context: context.scene.update_tag(),
    )
    mme_tolerance: FloatProperty(
        name="Geometry Tolerance",
        description="How far simplified effect strips may drift from the full mesh, as a share of the effect's size. Rows of strip meshes (such as Thunder's 10,000-segment rings and bolts) that the shader output can rebuild by interpolation are merged. 0 keeps every row. Faces that the shader always makes fully transparent are removed regardless. Applies on the next Rebuild",
        default=0.0005,
        min=0.0,
        max=0.05,
        precision=4,
        step=0.01,
    )
    mme_live: BoolProperty(
        name="Live Effect Preview",
        description="Recompute Python-backend effect passes whenever the frame changes in the viewport. Geometry Nodes passes always update. Renders always recompute",
        default=True,
    )
    mme_backend: EnumProperty(
        name="Effect Backend",
        description="How effect vertex shaders run. Applied on the next sync or Refresh Effects",
        items=[
            ("GEONODES", "Geometry Nodes", "Compile vertex shaders to Geometry Nodes: no Python per frame, renders and bakes natively. Passes that cannot compile fall back to Python"),
            ("PYTHON", "Python", "Run vertex shaders in Python (numpy) from a frame-change handler"),
        ],
        default="GEONODES",
    )
    fx_info_tab: EnumProperty(
        name="Effect Info",
        items=[
            ("LINKS", "Linked Objects", "Objects and morphs the effect reads (CONTROLOBJECT)", "LINKED", 0),
            ("PASSES", "Passes", "The effect's draw passes", "NODETREE", 1),
            ("PASS", "Selected Pass", "Details of the selected pass object", "OBJECT_DATA", 2),
        ],
        default="PASSES",
    )
    mme_core_autosave: BoolProperty(
        name="Grow Library Automatically",
        description="After an effect import builds new shared node groups, add them to mme_core.blend so later imports append them instead of building them again",
        default=True,
    )
    mme_bake_target: EnumProperty(
        name="Bake To",
        items=[
            ("PACKED", "Packed in .blend", "Store baked effect geometry inside the .blend file"),
            ("DISK", "Disk", "Store baked effect geometry in the folder below"),
        ],
        default="PACKED",
    )
    mme_bake_dir: StringProperty(
        name="Bake Folder",
        description="Folder for effect bakes stored on disk",
        default="//mme_bake/",
        subtype="DIR_PATH",
    )
    mme_standard_view: BoolProperty(
        name="MMD Colors (Standard View)",
        description="Switch the scene view transform to Standard when effects are synced so effect colors match MMD",
        default=True,
    )
    use_physics_vmd: BoolProperty(
        name="MMDBridge Physics (Experimental)",
        description="Layer a VMD baked with MMDBridge above the PMM clip to reproduce MMD's own rigid body results",
        default=False,
    )
    physics_disable_rigid_body: BoolProperty(
        name="Disable Rigid Body World",
        description="Turn off Blender's rigid body world while a baked physics clip is applied",
        default=True,
    )
    use_clips: BoolProperty(
        name="NLA Clips",
        description="Keep each model's motion and shapekey animation in reusable action clips on NLA tracks instead of as the active action",
        default=True,
    )
    retime_fps: BoolProperty(
        name="Retime to Scene FPS",
        description="PMM keyframes are 30 fps. Scale every key frame by scene fps / 30 so motion keeps its real speed in a scene with a different frame rate",
        default=True,
    )
    sync_audio: BoolProperty(
        name="Sync Audio",
        description="Add the audio file stored in the PMM to the video sequencer, starting at frame 0",
        default=True,
    )
    audio_path: StringProperty(
        name="Audio File",
        description="Audio file to use instead of the one stored in the PMM. Leave empty to use the PMM path, then the PMM folder",
        subtype="FILE_PATH",
    )
    audio_note: StringProperty()
    uv_morphs: BoolProperty(
        name="UV Morphs (Eye Shapes)",
        description="Apply UV morphs, including the ones inside group morphs, by offsetting the base texture in the material nodes. Needed for effects such as the > < eyes",
        default=True,
    )
    update_scene_settings: BoolProperty(
        name="Update Frame Range",
        description="Let mmd_tools set the scene frame range and FPS on every sync; the range is only ever widened",
        default=False,
    )
    interpolation_layout: EnumProperty(
        name="Interpolation Layout",
        description="How the 16 interpolation bytes in the PMM are ordered",
        items=[
            ("CURVE", "Per Curve", "x1 y1 x2 y2 repeated for X, Y, Z, Rotation"),
            ("VMD_ROW", "VMD Row", "x1 for each curve, then y1, then x2, then y2"),
        ],
        default="CURVE",
    )
    sync_camera: BoolProperty(name="Sync Camera", default=True)
    sync_light: BoolProperty(name="Sync Light", default=True)
    set_active_camera: BoolProperty(name="Make Scene Camera", default=True)
    follow_camera: BoolProperty(
        name="Follow Bone",
        description="Make the camera follow the model and bone stored in each PMM camera keyframe",
        default=True,
    )
    follow_rotation: BoolProperty(
        name="Follow Bone Rotation",
        description="Also inherit the bone rotation, not only its position",
        default=False,
    )
    targets: CollectionProperty(type=MMDBridgeTarget)


STALE_SETTINGS_MESSAGE = "An older copy of NineSync (MMD PMM Bridge) replaced this add-on's settings. They were re-linked; press the button again, and remove the old copy in Preferences > Add-ons"


_relink = {"pending": False}


def settings_outdated(scene):
    settings = getattr(scene, "mmd_bridge", None)
    return settings is None or not hasattr(settings, "mme_backend")


def relink_settings():
    _relink["pending"] = False
    try:
        scene = bpy.context.scene
    except AttributeError:
        scene = None
    if scene is not None and not settings_outdated(scene):
        return None
    importlib.import_module(__package__).reload_in_place()
    log.warning(
        "settings were registered by an older copy of the add-on; re-linked to this version. Remove the old copy in Preferences > Add-ons"
    )
    redraw_all()
    return None


def schedule_relink():
    if _relink["pending"]:
        return
    _relink["pending"] = True
    bpy.app.timers.register(relink_settings, first_interval=0.01)


def refuse_if_outdated(operator, context):
    if not settings_outdated(context.scene):
        return False
    schedule_relink()
    operator.report({"WARNING"}, STALE_SETTINGS_MESSAGE)
    return True


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
        refresh_accessories(settings, project)
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
    accessories: BoolProperty(
        name="Accessories",
        description="Delete imported accessory objects and their animation",
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
        layout.prop(self, "accessories")
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
            "accessories": self.accessories,
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


class MMDBRIDGE_OT_refresh_effects(Operator):
    bl_idname = "ninesync.refresh_effects"
    bl_label = "Refresh Effects"
    bl_description = "Reload effect files and rebuild every effect pass with the current backend, density and parameters. Bakes are cleared"

    root_name: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        if refuse_if_outdated(self, context):
            return {"CANCELLED"}
        _mme_runtimes.clear()
        _mme_state["errors"].clear()
        built = 0
        for root in mme_target_roots(context.scene, self.root_name):
            try:
                built += mme_rebuild_effect(context.scene, root)
            except Exception as error:
                log.error("effect '%s' rebuild failed: %s\n%s", root.name, error, traceback.format_exc())
                self.report({"WARNING"}, "%s: %s" % (root.name, short_text(error, 80)))
        mme_update_scene(context.scene, context.evaluated_depsgraph_get(), force=True)
        self.report({"INFO"}, "Rebuilt %d effect pass(es)" % built)
        redraw_all()
        return {"FINISHED"}


class MMDBRIDGE_OT_bake_effects(Operator):
    bl_idname = "ninesync.bake_effects"
    bl_label = "Bake Effects"
    bl_description = "Bake effect geometry over the scene frame range so playback and rendering read stored geometry instead of running shaders"

    root_name: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    clear: BoolProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        if refuse_if_outdated(self, context):
            return {"CANCELLED"}
        scene = context.scene
        settings = scene.mmd_bridge
        roots = mme_target_roots(scene, self.root_name)
        passes = [p for root in roots for p in mme_effect_passes(root)]
        if not passes:
            self.report({"WARNING"}, "No effect passes to %s" % ("clear" if self.clear else "bake"))
            return {"CANCELLED"}
        if settings.mme_bake_target == "DISK" and not bpy.data.filepath and settings.mme_bake_dir.startswith("//"):
            self.report({"ERROR"}, "Save the .blend first, or pick an absolute Bake Folder")
            return {"CANCELLED"}
        frame = scene.frame_current
        done = 0
        started = time.time()
        if not self.clear:
            mme_progress_begin(context, "NineSync baking", len(passes), scene.frame_start, scene.frame_end)
        try:
            for index, obj in enumerate(passes):
                mme_progress_step(index, obj.name)
                try:
                    if self.clear:
                        done += mme_clear_bake(context, obj)
                    else:
                        done += mme_bake_pass(context, obj, scene.frame_start, scene.frame_end, settings)
                except Exception as error:
                    log.error("bake of '%s' failed: %s\n%s", obj.name, error, traceback.format_exc())
        finally:
            mme_progress_end()
        scene.frame_set(frame)
        if self.clear:
            self.report({"INFO"}, "Cleared %d effect bake(s)" % done)
        else:
            self.report(
                {"INFO"},
                "Baked %d of %d effect pass(es), frames %d-%d, in %.1fs"
                % (done, len(passes), scene.frame_start, scene.frame_end, time.time() - started),
            )
        redraw_all()
        return {"FINISHED"}


class MMDBRIDGE_OT_reset_params(Operator):
    bl_idname = "ninesync.reset_effect_params"
    bl_label = "Reset Parameters"
    bl_description = "Set this effect's parameters back to the values written in the .fx file"

    root_name: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        root = bpy.data.objects.get(self.root_name)
        if root is None:
            return {"CANCELLED"}
        mme_reset_params(root)
        root.update_tag()
        redraw_all()
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


class MMDBRIDGE_PT_main(Panel):
    bl_label = NINESYNC_MAIN_LABEL
    bl_idname = "NINESYNC_PT_main"
    bl_order = 0
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = NINESYNC_CATEGORY

    def draw(self, context):
        layout = self.layout
        if settings_outdated(context.scene):
            box = layout.box()
            box.alert = True
            draw_wrapped(box, STALE_SETTINGS_MESSAGE)
            schedule_relink()
            return
        settings = context.scene.mmd_bridge
        draw_dependency_notices(layout)
        if not mmd_tools_available():
            return
        layout.prop(settings, "filepath", text="")
        row = layout.row()
        row.scale_y = 1.5
        row.operator("ninesync.sync_now", icon="FILE_REFRESH")
        layout.label(text=settings.status, icon="INFO")
        if fps_differs(context.scene):
            fps_box = layout.box()
            fps_box.label(
                text="Scene is %g fps, PMM is %d fps"
                % (round(scene_fps(context.scene), 3), PMM_FPS),
                icon="TIME",
            )
            fps_box.prop(settings, "retime_fps")
            if settings.retime_fps and settings.update_scene_settings:
                draw_wrapped(
                    fps_box,
                    "Update Frame Range also switches the scene to 30 fps, so retiming is skipped.",
                )
        if scale_warning_active(settings):
            box = layout.box()
            header = box.row()
            header.alert = True
            header.label(
                text="Scale is %.3f, not %.1f" % (current_scale(settings), UNIT_SCALE),
                icon="ERROR",
            )
            header.operator(
                "ninesync.dismiss_scale_warning", text="", icon="X", emboss=False
            )
            draw_wrapped(
                box,
                "Camera keyframe interpolation may not match MMD exactly because of the different distance scale.",
            )
        box = layout.box()
        header = box.row()
        header.label(text="Models")
        header.operator("ninesync.scan", text="", icon="FILE_REFRESH")
        if not len(settings.targets):
            box.label(text="Press the refresh icon to read the PMM")
        for item in settings.targets:
            state = item_state(item)
            row = box.row(align=True)
            row.prop(item, "enabled", text="")
            row.label(text="", icon=status_icon(state))
            row.label(text=item.pmm_name)
            row.prop(item, "target", text="")
            note = item_status_text(item)
            if note:
                note_row = box.row()
                note_row.alert = state == "ERROR"
                note_row.scale_y = 0.85
                note_row.label(text=short_text(note, 60))
            if settings.use_clips and item.target is not None:
                clip_row = box.row(align=True)
                clip_row.label(text="", icon="NLA")
                clip_row.prop(item, "clip", text="Motion")
                shape_row = box.row(align=True)
                shape_row.label(text="", icon="SHAPEKEY_DATA")
                shape_row.prop(item, "shape_clip", text="Shapekey")
                if settings.use_physics_vmd:
                    physics_row = box.row(align=True)
                    physics_row.label(text="", icon="PHYSICS")
                    physics_row.prop(item, "physics_vmd", text="")
        tools = layout.row(align=True)
        tools.operator("ninesync.clean", icon="TRASH")
        tools.operator("ninesync.copy_report", icon="COPYDOWN")
        layout.label(text="Log: Text Editor > %s" % LOG_TEXT_NAME)


class MMDBRIDGE_PT_audio(Panel):
    bl_label = "Audio"
    bl_idname = "NINESYNC_PT_audio"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = NINESYNC_CATEGORY
    bl_parent_id = "NINESYNC_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        settings = context.scene.mmd_bridge
        layout.prop(settings, "sync_audio")
        layout.prop(settings, "audio_path")
        draw_wrapped(
            layout,
            settings.audio_note or "Press the refresh icon in Models to read the PMM",
        )
        draw_wrapped(
            layout,
            "Audio starts at frame 0, the same as MMD. The PMM stores no audio offset.",
        )


class MMDBRIDGE_PT_camera(Panel):
    bl_label = "Camera & Light"
    bl_idname = "NINESYNC_PT_camera"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = NINESYNC_CATEGORY
    bl_parent_id = "NINESYNC_PT_main"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.mmd_bridge
        layout.prop(settings, "sync_camera")
        layout.prop(settings, "sync_light")
        layout.prop(settings, "set_active_camera")
        layout.separator()
        layout.prop(settings, "follow_camera")
        if settings.follow_camera:
            layout.prop(settings, "follow_rotation")


class MMDBRIDGE_PT_readout(Panel):
    bl_label = "MMD Values (current frame)"
    bl_idname = "NINESYNC_PT_readout"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = NINESYNC_CATEGORY
    bl_parent_id = "NINESYNC_PT_camera"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        scale = current_scale(context.scene.mmd_bridge)
        camera_collection = bpy.data.collections.get(CAMERA_COLLECTION)
        root = find_rig_root(camera_collection, "CAMERA") if camera_collection else None
        camera = child_of_type(root, "CAMERA") if root else None
        if root is not None and camera is not None:
            loc = root.location
            rot = root.rotation_euler
            layout.label(
                text="Camera position: %.2f, %.2f, %.2f"
                % (loc.x / scale, loc.z / scale, loc.y / scale)
            )
            layout.label(
                text="Camera angle: %.2f, %.2f, %.2f"
                % (-math.degrees(rot.x), math.degrees(rot.z), math.degrees(rot.y))
            )
            layout.label(text="Distance: %.2f" % (-camera.location.y / scale))
            mmd_camera = getattr(root, "mmd_camera", None)
            if mmd_camera is not None:
                layout.label(text="View angle: %.1f" % math.degrees(mmd_camera.angle))
        else:
            layout.label(text="No MMD camera yet")
        light_collection = bpy.data.collections.get(LIGHT_COLLECTION)
        light_root = (
            find_rig_root(light_collection, "LIGHT") if light_collection else None
        )
        light = child_of_type(light_root, "LIGHT") if light_root else None
        if light is not None:
            color = light.data.color
            pos = light.location
            layout.label(
                text="Light RGB: %d, %d, %d"
                % (round(color[0] * 255), round(color[1] * 255), round(color[2] * 255))
            )
            layout.label(
                text="Light position: %.2f, %.2f, %.2f" % (-pos.x, -pos.z, -pos.y)
            )


class MMDBRIDGE_PT_options(Panel):
    bl_label = "Options"
    bl_idname = "NINESYNC_PT_options"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = NINESYNC_CATEGORY
    bl_parent_id = "NINESYNC_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        settings = context.scene.mmd_bridge
        layout = self.layout
        layout.prop(settings, "auto_scale")
        row = layout.row()
        row.enabled = not settings.auto_scale
        row.prop(settings, "scale")
        layout.prop(settings, "use_clips")
        layout.prop(settings, "uv_morphs")
        if settings.uv_morphs:
            draw_wrapped(
                layout,
                "UV morphs are shader based: view them in Material Preview or Rendered, not Solid. The weights live on MMDUV_ shape keys inside the Shapekey Clip.",
            )
        layout.prop(settings, "update_scene_settings")
        layout.prop(settings, "interpolation_layout")
        layout.separator()
        layout.prop(settings, "use_physics_vmd")
        if settings.use_physics_vmd:
            layout.prop(settings, "physics_disable_rigid_body")
            draw_wrapped(
                layout,
                "Experimental. MMD's physics solver cannot run inside Blender. Bake the motion in MMD with MMDBridge, set the VMD on each model row, and it is layered above the PMM clip.",
            )


def draw_effect_controls(layout, entry):
    collection = bpy.data.collections.get(ACCESSORY_COLLECTION)
    root = find_accessory_root(collection, entry.key) if collection else None
    if root is None or not root.get("mme_fx"):
        return
    passes = mme_effect_passes(root)
    box = layout.box()
    head = box.row(align=True)
    head.prop(
        entry,
        "show_controls",
        text="Effect Controls",
        icon="TRIA_DOWN" if entry.show_controls else "TRIA_RIGHT",
        emboss=False,
    )
    geo = sum(1 for p in passes if p.get(MME_BACKEND_PROP) == "GEO")
    baked = sum(1 for p in passes if p.get(MME_BAKED_PROP))
    summary = "%d GN, %d Py" % (geo, len(passes) - geo)
    if baked:
        summary += ", %d baked" % baked
    head.label(text=summary)
    if not entry.show_controls:
        return
    col = box.column(align=True)
    col.prop(root, '["mme_tr"]', text="Tr (keyed from PMM)")
    col.prop(root, '["mme_si"]', text="Si (keyed from PMM)")
    meta = mme_param_meta(root)
    if meta:
        col.separator()
        for name, info in sorted(meta.items(), key=lambda item: item[1].get("order", 0)):
            if info.get("prop") in root:
                col.prop(root, '["%s"]' % info["prop"], text=info.get("label") or name)
    row = box.row(align=True)
    reset = row.operator("ninesync.reset_effect_params", text="Reset", icon="LOOP_BACK")
    reset.root_name = root.name
    rebuild = row.operator("ninesync.refresh_effects", text="Rebuild", icon="FILE_REFRESH")
    rebuild.root_name = root.name
    row = box.row(align=True)
    bake = row.operator("ninesync.bake_effects", text="Bake", icon="RENDER_ANIMATION")
    bake.root_name = root.name
    clear = row.operator("ninesync.bake_effects", text="Clear Bake", icon="TRASH")
    clear.root_name = root.name
    clear.clear = True


class MMDBRIDGE_PT_accessories(Panel):
    bl_label = "Accessories (.x)"
    bl_idname = "NINESYNC_PT_accessories"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = NINESYNC_CATEGORY
    bl_parent_id = "NINESYNC_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        settings = context.scene.mmd_bridge
        layout.prop(settings, "sync_accessories")
        layout.prop(settings, "sphere_maps")
        layout.prop(settings, "accessory_dir")
        armature = first_armature(settings)
        if not len(settings.accessories):
            layout.label(text="Press the refresh icon in Models to read the PMM")
        for entry in settings.accessories:
            row = layout.row(align=True)
            row.prop(entry, "enabled", text="")
            row.label(text=entry.display)
            if armature is not None:
                row.prop_search(entry, "parent_bone", armature.pose, "bones", text="")
        draw_wrapped(layout, "MME effects (.fx) for these accessories are set up in the %s panel." % NINESYNC_FX_LABEL)


def fx_effect_root(obj):
    if obj is None:
        return None
    if obj.get("mme_fx"):
        return obj
    if obj.get(MME_DRAW_PROP) and obj.parent is not None and obj.parent.get("mme_fx"):
        return obj.parent
    return None


def fx_entry_for(settings, root):
    key = root.get("mmd_bridge_accessory")
    for entry in settings.accessories:
        if entry.key == key:
            return entry
    return None


def fx_pass_summary(obj):
    try:
        info = json.loads(obj.get(MME_DRAW_PROP, "{}"))
    except (TypeError, ValueError):
        info = {}
    backend = "Geometry Nodes" if obj.get(MME_BACKEND_PROP) == "GEO" else "Python"
    loops = ", ".join("%s x%d" % (var or "loop", count) for var, count in info.get("loops", []))
    flags = []
    if obj.get(MME_BAKED_PROP):
        flags.append("baked")
    if obj.get("mme_handler", True):
        flags.append("frame handler")
    if obj.get(MME_PRUNED_PROP):
        flags.append("pruned")
    return info, backend, loops, flags


def fx_control_text(desc):
    kind = desc.get("kind")
    source = desc.get("from") or ""
    if kind == "const":
        return "missing: %s (uses %s)" % (source or "target", ", ".join("%g" % v for v in desc.get("values", [0.0])))
    if kind == "self":
        return "this accessory: %s" % (desc.get("item") or "transform")
    if kind == "object":
        return "%s: %s" % (desc.get("object", ""), desc.get("item") or "transform")
    if kind == "prop":
        return "morph %s (keyed)" % source
    if kind == "bone":
        return "bone %s on %s" % (desc.get("bone", ""), desc.get("object", ""))
    if kind == "model":
        return "model %s" % desc.get("object", "")
    return kind


def draw_fx_selected(layout, context, root, active):
    settings = context.scene.mmd_bridge
    row = layout.row(align=True)
    row.scale_y = 1.2
    rebuild = row.operator("ninesync.refresh_effects", text="Rebuild", icon="FILE_REFRESH")
    rebuild.root_name = root.name
    bake = row.operator("ninesync.bake_effects", text="Bake", icon="RENDER_ANIMATION")
    bake.root_name = root.name
    clear = row.operator("ninesync.bake_effects", text="Clear Bake", icon="TRASH")
    clear.root_name = root.name
    clear.clear = True
    col = layout.column(align=True)
    col.label(text="Accessory: %s" % root.get("mme_acc_name", root.name), icon="EMPTY_AXIS")
    col.label(text="Effect: %s" % os.path.basename(root.get("mme_fx", "")), icon="SHADERFX")
    col.label(text="Mesh: %s" % os.path.basename(root.get("mme_x", "")), icon="MESH_DATA")
    header, body = layout.panel("ninesync_fx_keyed", default_closed=False)
    header.label(text="Keyed from the PMM", icon="KEYINGSET")
    if body is not None:
        sub = body.column(align=True)
        sub.prop(root, '["mme_tr"]', text="Tr (opacity)")
        sub.prop(root, '["mme_si"]', text="Si (scale)")
    meta = mme_param_meta(root)
    header, body = layout.panel("ninesync_fx_params", default_closed=False)
    header.label(text="Effect Parameters", icon="PROPERTIES")
    reset = header.operator("ninesync.reset_effect_params", text="", icon="LOOP_BACK", emboss=False)
    reset.root_name = root.name
    if body is not None:
        try:
            structural = set(json.loads(root.get("mme_structural", "[]")))
        except (TypeError, ValueError):
            structural = set()
        if not meta:
            body.label(text="This effect exposes no parameters")
        for name, info in sorted(meta.items(), key=lambda item: item[1].get("order", 0)):
            if info.get("prop") not in root:
                continue
            row = body.row(align=True)
            row.prop(root, '["%s"]' % info["prop"], text=info.get("label") or name)
            row.label(text="", icon="MODIFIER" if name in structural else "BLANK1")
        if structural & set(meta):
            draw_wrapped(body, "Parameters with the wrench change the geometry; the effect rebuilds after you edit them.")
    layout.row().prop(settings, "fx_info_tab", expand=True)
    box = layout.box()
    tab = settings.fx_info_tab
    if tab == "LINKS":
        controls = mme_load_controls(root)
        if not controls:
            box.label(text="This effect reads no other objects")
        groups = {}
        for name, desc in controls.items():
            groups.setdefault(mme_control_source(desc), []).append((name, desc))
        for source, entries in groups.items():
            col = box.column(align=True)
            header = col.row(align=True)
            prop = next((mme_control_use_prop(d) for _n, d in entries if mme_control_use_prop(d)), None)
            if source.lower() in MME_SELF_NAMES:
                header.label(text="This accessory", icon="EMPTY_AXIS")
            elif prop is not None and prop in root:
                header.prop(root, '["%s"]' % prop, text=source, toggle=True)
            else:
                header.label(text=source, icon="ERROR")
            off = prop is not None and not bool(root.get(prop, True))
            for name, desc in entries:
                split = col.split(factor=0.35, align=True)
                missing = desc.get("kind") == "const"
                split.label(text=name, icon="ERROR" if missing else ("HIDE_ON" if off else "LINKED"))
                right = split.row()
                right.alert = missing
                right.enabled = not off
                right.label(text=short_text(fx_control_text(desc), 48))
            box.separator(factor=0.5)
    elif tab == "PASSES":
        for obj in mme_effect_passes(root):
            info, backend, loops, flags = fx_pass_summary(obj)
            row = box.row(align=True)
            select = row.operator(
                "ninesync.select_object",
                text="%s / %s" % (info.get("technique", "?"), info.get("pass", "?")),
                icon="RADIOBUT_ON" if obj == active else "RADIOBUT_OFF",
                emboss=False,
            )
            select.name = obj.name
            row.label(text=backend, icon="GEOMETRY_NODES" if backend == "Geometry Nodes" else "SCRIPT")
            detail = ([loops] if loops else []) + flags
            if detail:
                sub = box.row()
                sub.scale_y = 0.75
                sub.separator(factor=3.0)
                sub.label(text=", ".join(detail))
    else:
        if active is None or not active.get(MME_DRAW_PROP):
            box.label(text="Pick a pass in the Passes tab", icon="INFO")
        else:
            info, backend, loops, flags = fx_pass_summary(active)
            try:
                varyings = json.loads(active.get(MME_VARYINGS_PROP, "[]"))
                uniforms = json.loads(active.get(MME_UNIFORMS_PROP, "[]"))
                driven = json.loads(active.get(MME_DRIVEN_PROP, "[]"))
            except (TypeError, ValueError):
                varyings, uniforms, driven = [], [], []
            col = box.column(align=True)
            col.label(text="%s / %s" % (info.get("technique", "?"), info.get("pass", active.name)), icon="OBJECT_DATA")
            col.label(text="Subset %s, %s backend" % (info.get("subset", "?"), backend))
            if loops:
                col.label(text="Loops: %s" % loops)
            if flags:
                col.label(text="State: %s" % ", ".join(flags))
            col.label(text="%d vertex outputs to the material" % len(varyings))
            col.label(text="%d material inputs from the scene (%d by drivers)" % (len(uniforms), len(driven)))
            if active.data is not None and len(active.data.materials) and active.data.materials[0] is not None:
                mat = active.data.materials[0]
                col.label(text=mat.name, icon="MATERIAL")
                group = bpy.data.node_groups.get(mat.get(MME_GROUP_PROP, ""))
                if group is not None:
                    col.label(text=group.name, icon="NODE_MATERIAL")
            modifier = active.modifiers.get(MME_GN_MODIFIER)
            if modifier is not None and modifier.node_group is not None:
                col.label(text=modifier.node_group.name, icon="GEOMETRY_NODES")
    entry = fx_entry_for(settings, root)
    if entry is not None:
        layout.prop(entry, "effect_path", text="Effect File")


class NINESYNC_PT_fx(Panel):
    bl_label = NINESYNC_FX_LABEL
    bl_idname = "NINESYNC_PT_fx"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = NINESYNC_CATEGORY
    bl_order = 1

    def draw(self, context):
        layout = self.layout
        if settings_outdated(context.scene):
            draw_wrapped(layout, STALE_SETTINGS_MESSAGE)
            return
        settings = context.scene.mmd_bridge
        layout.prop(settings, "mme_effects")
        if not settings.mme_effects:
            draw_wrapped(layout, "Turn this on and sync to import accessory effects.")
            return
        col = layout.column()
        col.prop(settings, "mme_backend")
        col.prop(settings, "mme_density", slider=True)
        col.prop(settings, "mme_tolerance")
        if settings.mme_backend == "PYTHON":
            col.prop(settings, "mme_live")
        col.prop(settings, "mme_standard_view")
        row = layout.row()
        row.scale_y = 1.3
        row.operator("ninesync.refresh_effects", text="Rebuild All Effects", icon="SHADERFX")
        draw_wrapped(layout, "Effects show in Material Preview and Rendered mode. Offscreen blur and post passes are approximated.")


class NINESYNC_PT_fx_selected(Panel):
    bl_label = "Selected Effect"
    bl_idname = "NINESYNC_PT_fx_selected"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = NINESYNC_CATEGORY
    bl_parent_id = "NINESYNC_PT_fx"

    @classmethod
    def poll(cls, context):
        return not settings_outdated(context.scene) and context.scene.mmd_bridge.mme_effects

    def draw(self, context):
        active = context.active_object
        root = fx_effect_root(active)
        if root is None:
            draw_wrapped(self.layout, "Select an effect accessory (MMDAcc_...) or one of its passes to see its settings here.")
            return
        draw_fx_selected(self.layout, context, root, active)


class NINESYNC_PT_fx_accessories(Panel):
    bl_label = "Accessory Effects"
    bl_idname = "NINESYNC_PT_fx_accessories"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = NINESYNC_CATEGORY
    bl_parent_id = "NINESYNC_PT_fx"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return not settings_outdated(context.scene) and context.scene.mmd_bridge.mme_effects

    def draw(self, context):
        layout = self.layout
        settings = context.scene.mmd_bridge
        if not len(settings.accessories):
            draw_wrapped(layout, "Read a PMM in %s first." % NINESYNC_MAIN_LABEL)
        for entry in settings.accessories:
            row = layout.row(align=True)
            row.prop(entry, "use_effect", text="", icon="SHADERFX")
            row.label(text=entry.display)
            if entry.use_effect:
                layout.prop(entry, "effect_path", text="Effect")
            if entry.effect_note:
                note_row = layout.row()
                note_row.scale_y = 0.85
                note_row.alert = entry.effect_note.startswith(("Effect failed", "Effect error"))
                note_row.label(text=short_text(entry.effect_note, 60), icon="INFO")
            if entry.use_effect:
                draw_effect_controls(layout, entry)


class NINESYNC_PT_fx_bake(Panel):
    bl_label = "Bake"
    bl_idname = "NINESYNC_PT_fx_bake"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = NINESYNC_CATEGORY
    bl_parent_id = "NINESYNC_PT_fx"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return not settings_outdated(context.scene) and context.scene.mmd_bridge.mme_effects

    def draw(self, context):
        layout = self.layout
        settings = context.scene.mmd_bridge
        layout.label(text="Scene frames %d - %d" % (context.scene.frame_start, context.scene.frame_end))
        layout.prop(settings, "mme_bake_target", text="Store")
        if settings.mme_bake_target == "DISK":
            layout.prop(settings, "mme_bake_dir", text="")
        row = layout.row(align=True)
        row.operator("ninesync.bake_effects", text="Bake All", icon="RENDER_ANIMATION")
        clear = row.operator("ninesync.bake_effects", text="Clear All", icon="TRASH")
        clear.clear = True


class NINESYNC_PT_fx_library(Panel):
    bl_label = "Core Node Library"
    bl_idname = "NINESYNC_PT_fx_library"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = NINESYNC_CATEGORY
    bl_parent_id = "NINESYNC_PT_fx"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return not settings_outdated(context.scene) and context.scene.mmd_bridge.mme_effects

    def draw(self, context):
        layout = self.layout
        settings = context.scene.mmd_bridge
        path = mme_core_path()
        names = mme_core_names()
        col = layout.column(align=True)
        col.label(text=MME_CORE_FILE, icon="FILE_BLEND" if os.path.isfile(path) else "ERROR")
        col.label(text="%d shared groups in the library" % len(names))
        local = [g for g in bpy.data.node_groups if g.get(MME_KEY_PROP)]
        col.label(text="%d shared groups in this file (%d not in the library)" % (len(local), len([g for g in local if g.name not in names])))
        layout.prop(settings, "mme_core_autosave")
        row = layout.row(align=True)
        row.operator("ninesync.save_core_library", text="Save to Library", icon="FILE_TICK")
        row.operator("ninesync.rebuild_core_library", text="Rebuild Standard", icon="FILE_REFRESH")


class NINESYNC_OT_select_object(Operator):
    bl_idname = "ninesync.select_object"
    bl_label = "Select"
    bl_description = "Select this object and make it active"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    name: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        obj = bpy.data.objects.get(self.name)
        if obj is None:
            return {"CANCELLED"}
        for other in context.selected_objects:
            other.select_set(False)
        obj.hide_select = False
        obj.select_set(True)
        context.view_layer.objects.active = obj
        return {"FINISHED"}


class NINESYNC_OT_save_core_library(Operator):
    bl_idname = "ninesync.save_core_library"
    bl_label = "Save Shared Groups to Library"
    bl_description = "Write every shared effect node group in this file into mme_core.blend next to the add-on, so later imports append them instead of building them"

    def execute(self, context):
        try:
            count = mme_save_core_library()
        except (OSError, RuntimeError) as error:
            self.report({"ERROR"}, "Could not write %s: %s" % (MME_CORE_FILE, error))
            return {"CANCELLED"}
        self.report({"INFO"}, "%s now holds %d shared group(s)" % (MME_CORE_FILE, count))
        redraw_all()
        return {"FINISHED"}


class NINESYNC_OT_rebuild_core_library(Operator):
    bl_idname = "ninesync.rebuild_core_library"
    bl_label = "Rebuild Standard Groups"
    bl_description = "Rebuild the standard shared groups (camera, matrices, clip space, common HLSL intrinsics) with this Blender version and save them to the library. Function groups from imported effects are kept"

    def execute(self, context):
        try:
            built = mme_build_standard_groups(force=True)
            count = mme_save_core_library()
        except (OSError, RuntimeError, FxError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, "Rebuilt %d standard group(s); %s holds %d" % (built, MME_CORE_FILE, count))
        redraw_all()
        return {"FINISHED"}


CLASSES = (
    MMDBridgeTarget,
    MMDBridgeAccessory,
    MMDBridgeSettings,
    MMDBRIDGE_OT_sync_now,
    MMDBRIDGE_OT_scan,
    MMDBRIDGE_OT_clean,
    MMDBRIDGE_OT_copy_report,
    MMDBRIDGE_OT_refresh_effects,
    MMDBRIDGE_OT_bake_effects,
    MMDBRIDGE_OT_reset_params,
    MMDBRIDGE_OT_dismiss_scale_warning,
    NINESYNC_OT_select_object,
    NINESYNC_OT_save_core_library,
    NINESYNC_OT_rebuild_core_library,
    MMDBRIDGE_PT_main,
    MMDBRIDGE_PT_audio,
    MMDBRIDGE_PT_camera,
    MMDBRIDGE_PT_readout,
    MMDBRIDGE_PT_options,
    MMDBRIDGE_PT_accessories,
    NINESYNC_PT_fx,
    NINESYNC_PT_fx_selected,
    NINESYNC_PT_fx_accessories,
    NINESYNC_PT_fx_bake,
    NINESYNC_PT_fx_library,
)
