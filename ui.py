import bpy
import importlib
import math
import os
import textwrap
import time
from . import hooks
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty
from bpy.types import Operator, Panel, PropertyGroup

from .common import (
    ADDON_PACKAGE,
    CAMERA_COLLECTION,
    FX_URL,
    LIGHT_COLLECTION,
    LOG_TEXT_NAME,
    NINESYNC_CATEGORY,
    NINESYNC_MAIN_LABEL,
    PMM_FPS,
    REPORT_TEXT_NAME,
    UNIT_SCALE,
    WRAP_CHAR,
    WRAP_MARGIN,
    child_of_type,
    find_rig_root,
    fps_differs,
    log,
    redraw_all,
    scene_fps,
)
from .pmm import (
    PmmError,
    parse_pmm_file,
)
from .bridge import (
    build_report,
    clip_poll,
    current_scale,
    describe_audio,
    is_mmd_root,
    item_state,
    item_status_text,
    missing_dependencies,
    mmd_tools_available,
    on_target_changed,
    refresh_targets,
    run_clean,
    run_sync,
    scale_warning_active,
    status_icon,
)


def draw_wrapped(layout, text, width=None):
    if width is None:
        width = 38
        region = getattr(bpy.context, "region", None)
        if region is not None and region.width > 1:
            scale = bpy.context.preferences.system.ui_scale or 1.0
            width = max(16, int((region.width / scale - WRAP_MARGIN) / WRAP_CHAR))
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
    pmm_accessory_count: IntProperty(default=0, options={"HIDDEN"})
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
    return settings is None or not hasattr(settings, "sync_audio")


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


IMPORT_SCALE_OPERATORS = ("mmd_tools.import_model", "mmd_tools.import_vmd")


def addon_preferences():
    addon = bpy.context.preferences.addons.get(ADDON_PACKAGE)
    return addon.preferences if addon is not None else None


def apply_import_scale(scale):
    applied = []
    hooks.registry()["shared"]["import_scale"] = scale
    wm = bpy.context.window_manager
    for idname in IMPORT_SCALE_OPERATORS:
        try:
            props = wm.operator_properties_last(idname)
        except (RuntimeError, KeyError, TypeError, AttributeError):
            props = None
        if props is not None and hasattr(props, "scale"):
            props.scale = scale
            applied.append(idname)
    return applied


class NineSyncPreferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_PACKAGE

    use_import_scale: BoolProperty(
        name="Set Import Scale",
        description="Preset the scale of mmd_tools' model and motion importers (and NineSync: FX's accessory importer) every time Blender starts",
        default=False,
    )
    import_scale: FloatProperty(
        name="Import Scale",
        description="Scale preset for mmd_tools and NineSync imports",
        default=1.0,
        min=0.0001,
        soft_max=10.0,
    )

    def draw(self, context):
        row = self.layout.row()
        row.prop(self, "use_import_scale")
        sub = row.row()
        sub.active = self.use_import_scale
        sub.prop(self, "import_scale")


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
            fix = box.operator("ninesync.set_import_scale", text="Import at Scale 1.0 From Now On", icon="IMPORT")
            fix.scale = UNIT_SCALE
        header, box = layout.panel("ninesync_models", default_closed=False)
        header.label(text="Models (%d)" % len(settings.targets))
        header.operator("ninesync.scan", text="", icon="FILE_REFRESH", emboss=False)
        if box is not None and not len(settings.targets):
            box.label(text="Press the refresh icon to read the PMM")
        for item in (settings.targets if box is not None else ()):
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
                note_row.label(text=note)
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
        draw_fx_notice(layout, settings)
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


def draw_fx_notice(layout, settings):
    if settings.pmm_accessory_count <= 0 or hooks.fx_installed():
        return
    box = layout.box()
    box.label(text="This PMM has %d accessory(ies)" % settings.pmm_accessory_count, icon="INFO")
    draw_wrapped(box, "Install NineSync: FX to import them and their MME effects.")
    if FX_URL:
        box.operator("wm.url_open", text="Get NineSync: FX", icon="URL").url = FX_URL


class NINESYNC_PT_legacy(Panel):
    bl_label = NINESYNC_MAIN_LABEL
    bl_idname = "NINESYNC_PT_legacy"
    bl_order = 0
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = NINESYNC_CATEGORY

    def draw(self, context):
        box = self.layout.box()
        box.alert = True
        box.label(text="Old combined NineSync is enabled", icon="ERROR")
        draw_wrapped(box, "Disable the old combined NineSync in Preferences > Add-ons, then restart Blender.")


CLASSES = (
    MMDBridgeTarget,
    MMDBridgeSettings,
    MMDBRIDGE_OT_sync_now,
    MMDBRIDGE_OT_scan,
    MMDBRIDGE_OT_clean,
    MMDBRIDGE_OT_copy_report,
    NineSyncPreferences,
    NINESYNC_OT_set_import_scale,
    MMDBRIDGE_OT_dismiss_scale_warning,
    MMDBRIDGE_PT_main,
    MMDBRIDGE_PT_audio,
    MMDBRIDGE_PT_camera,
    MMDBRIDGE_PT_readout,
    MMDBRIDGE_PT_options,
)


LEGACY_CLASSES = (NINESYNC_PT_legacy,)
