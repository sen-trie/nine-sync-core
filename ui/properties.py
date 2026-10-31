import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import PropertyGroup

from ..common.constants import ACTION_PREFIX
from ..common.logger import log


# Poll for the target picker: only mmd_tools model roots.
def is_mmd_root(self, obj):
    return getattr(obj, "mmd_type", "") == "ROOT"


# Hides scene and light actions from the per-model clip pickers.
def clip_poll(self, action):
    return not (
        action.name.startswith(ACTION_PREFIX + "Scene")
        or action.name.startswith(ACTION_PREFIX + "Light")
    )


# A new target makes the old clips and state meaningless, so they are cleared.
def on_target_changed(self, context):
    name = self.target.name if self.target else None
    log.info("target for PMM model '%s' (%s) set to %s", self.pmm_name, self.key, name)
    if self.clip is not None:
        self.clip = None
    if self.shape_clip is not None:
        self.shape_clip = None
    self.state = "NEW"
    self.status_text = ""


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


# One row of the Create MMD Project checklist: an mmd_tools model and the PMX it
# was loaded from.
class MMDBridgeExportItem(PropertyGroup):
    enabled: BoolProperty(name="Include", default=True)
    target: PointerProperty(name="Model", type=bpy.types.Object, poll=is_mmd_root)
    pmx_path: StringProperty(
        name="PMX File",
        description="Original PMX the model was loaded from. MMD loads the model from this path when it opens the project",
        subtype="FILE_PATH",
    )


# One row of the Create MMD Project prop list: a mesh object that is written as an
# .x accessory.
class MMDBridgeExportProp(PropertyGroup):
    enabled: BoolProperty(name="Include", default=True)
    target: PointerProperty(
        name="Mesh",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == "MESH",
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
    export_items: CollectionProperty(type=MMDBridgeExportItem)
    export_props: CollectionProperty(type=MMDBridgeExportProp)
    export_path: StringProperty(
        name="PMM File",
        description="Where Create PMM saves the project",
        subtype="FILE_PATH",
    )
    export_camera: BoolProperty(
        name="Include Camera",
        description="Put the MMD camera rig's camera in the project",
        default=True,
    )
    export_animation: BoolProperty(
        name="Export Animation",
        description="Write the animation Blender already has into the project. Off creates the models and camera at their initial pose",
        default=True,
    )
    export_bones: BoolProperty(name="Bones", default=True)
    export_morphs: BoolProperty(name="Morphs", default=True)
    export_camera_motion: BoolProperty(name="Camera", default=True)
