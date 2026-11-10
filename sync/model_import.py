import logging
import traceback

from ..common.animation import (
    clear_bridge_nla,
    collect_owners,
    describe_action,
    frame_span,
    purge_actions,
    reset_animation,
    restore_points,
    snapshot_outside,
    tag_actions,
)
from ..common.constants import ACTION_PREFIX, TEMP_PREFIX
from ..common.logger import log
from ..common.scene_utils import find_armature, short_text
from ..common.timing import phase
from ..pmm import bone_track_moves, build_vmd
from .clips import (
    clip_track_names,
    find_bridge_clip_any,
    model_label,
    place_clip,
    primary_key_owner,
    release_stash,
)
from .mmd_tools_link import mmd_tools_modules, run_importer
from .morph_keys import ensure_morph_keys
from .morphs import report_morphs, resolve_morph_names
from .ik import apply_ik_keys
from .motion import ik_switches, vmd_bone_name
from .physics import import_physics_clip
from .rigify import clear_remap, is_rigify_armature, retarget
from .plan import vmd_bone_table
from .scale import current_scale
from .sync_state import forget_parts, rig_layout, write_parts
from .targets import target_usable
from .tempfiles import remove_file, write_temp_vmd
from .uv_morphs import prepare_uv_channels
from .visibility import apply_visibility, visibility_track_name


ALL_PARTS = ("motion", "shape")


# Logs how many PMM bones match the armature's original bone names. The first thing
# to check when a model does not move.
def diagnose_bones(root, model):
    armature = find_armature(root)
    if armature is None:
        log.error(
            "root '%s' has no armature child; bone animation cannot be applied",
            root.name,
        )
        return
    table = vmd_bone_table(armature)
    matched = [n for n in model.bone_names if vmd_bone_name(n) in table]
    unmatched = [n for n in model.bone_names if vmd_bone_name(n) not in table]
    log.info(
        "armature '%s': %d/%d PMM bones reach a bone through the VMD",
        armature.name,
        len(matched),
        len(model.bone_names),
    )
    tracks = dict(zip(model.bone_names, model.bones))
    losing = [n for n in unmatched if bone_track_moves(tracks[n])]
    if losing:
        log.warning("unmatched PMM bones with motion (first 15): %s", losing[:15])
    if len(unmatched) > len(losing):
        log.info(
            "unmatched PMM bones holding only the rest pose, nothing lost (first 15): %s",
            [n for n in unmatched if n not in losing][:15],
        )
    animated = [n for n, keys in zip(model.bone_names, model.bones) if len(keys) > 1]
    log.info("bones with more than one keyframe: %d (%s)", len(animated), animated[:10])


# Every key frame of a model, bones, morphs and IK switches, to find its PMM frame span.
def model_frames(model):
    for keys in model.bones:
        for key in keys:
            yield key.frame
    for keys in model.morphs:
        for key in keys:
            yield key.frame
    for points in ik_switches(model).values():
        for frame, _state in points:
            yield frame


# Imports one PMM model onto its mmd_tools rig. The PMM keys go through a temp VMD
# that mmd_tools applies, then the parts it skips (shape keys, UV morphs) are filled
# in, the user's keys outside the PMM span are restored and, with clips on, results
# are moved into reusable NLA clips. parts limits the import to "motion" or "shape":
# the other part's IDs are left out of the targets, so its actions stay as they are.
# rebuild (Force Resync) also rebuilds a UV morph setup that looks unchanged.
def import_model(root, model, settings, item, parts=ALL_PARTS, rebuild=False):
    if not target_usable(root):
        raise RuntimeError(
            "target '%s' has no armature or is not in the scene; reassign the model"
            % root.name
        )
    _importer_mod, model_mod, _utils_mod = mmd_tools_modules()
    rig = model_mod.Model(root)
    armature = rig.armature()
    placeholder = rig.morph_slider.placeholder()
    meshes = list(rig.meshes())
    motion = "motion" in parts
    shape = "shape" in parts
    targets = [root] if motion else []
    for extra in ([armature] if motion else []) + ([placeholder] + meshes if shape else []):
        if extra is not None and extra not in targets:
            targets.append(extra)
    log.info(
        "importing '%s' into root '%s', targets: %s",
        model.name,
        root.name,
        [(o.name, o.type) for o in targets],
    )
    label = model_label(model)
    if motion:
        diagnose_bones(root, model)
    morph_map, morph_available = resolve_morph_names(root, meshes, model)
    if shape:
        report_morphs(model, morph_map, morph_available)
    with phase("vmd_build", label):
        data = build_vmd(
            model, morph_map, bones=motion, morphs=shape
        )
    if shape:
        try:
            with phase("uv_morphs", label):
                prepare_uv_channels(
                    root, meshes, model, morph_map, settings.uv_morphs, rebuild
                )
        except Exception as error:
            log.error("UV morph setup failed: %s\n%s", error, traceback.format_exc())
    owners = collect_owners(targets)
    use_clips = armature is not None
    arm_track, shape_track, legacy_shape_track, _legacy_arm = clip_track_names(label)
    key_owners = [o for o in owners if hasattr(o, "key_blocks")] if use_clips else []
    primary_keys = primary_key_owner(key_owners, placeholder)
    arm_clip = item.clip if use_clips and motion else None
    stash = {}
    sources = {}
    if use_clips:
        for owner in ([armature] if motion else []) + key_owners:
            anim = owner.animation_data or owner.animation_data_create()
            action = anim.action
            stash[owner.as_pointer()] = (
                owner,
                action,
                action.use_fake_user if action else False,
            )
            if action is not None:
                action.use_fake_user = True
        if motion:
            sources[armature.as_pointer()] = arm_clip
        for owner in key_owners:
            found = find_bridge_clip_any(owner, (shape_track, legacy_shape_track))
            if (
                primary_keys is not None
                and owner == primary_keys
                and item.shape_clip is not None
            ):
                found = item.shape_clip
            sources[owner.as_pointer()] = found
    low, high = frame_span(model_frames(model))
    kept = snapshot_outside(owners, low, high, sources if use_clips else None)
    log.info(
        "PMM frame span for '%s': %s-%s; keeping keys outside it on %d curve(s); motion clip=%s; shapekey clip=%s",
        model.name,
        low,
        high,
        len(kept),
        arm_clip.name if arm_clip else None,
        item.shape_clip.name if item.shape_clip else None,
    )
    plain_owners = [
        o for o in owners if not (use_clips and (o == armature or o in key_owners))
    ]
    clear_bridge_nla(plain_owners, keep=(visibility_track_name(label),))
    reset_animation(targets, keep_keys=use_clips)
    for owner in key_owners:
        owner.animation_data.action = None
    purge_actions(ACTION_PREFIX)
    with phase("vmd_write", label):
        tmp_path = write_temp_vmd(data, TEMP_PREFIX)
    placed = False
    try:
        try:
            with phase("importer", label):
                run_importer(
                    tmp_path,
                    current_scale(settings),
                    targets,
                    ACTION_PREFIX + label,
                    False,
                    vmd_bone_table,
                )
        finally:
            remove_file(tmp_path)
        if shape:
            with phase("morph_keys", label):
                ensure_morph_keys(meshes, model, morph_map, root)
        if motion and armature is not None:
            with phase("ik_keys", label):
                apply_ik_keys(armature, model, vmd_bone_table(armature))
        restored = restore_points(kept)
        tag_actions(plain_owners, label)
        log.info("restored %d preserved key(s) for '%s'", restored, model.name)
        if motion:
            describe_action("armature", armature)
        if use_clips:
            if motion:
                arm_ptr = armature.as_pointer()
                active_arm = stash[arm_ptr][1]
                keep_active = (
                    arm_clip is not None
                    and active_arm is not None
                    and active_arm == arm_clip
                )
                item.clip = place_clip(
                    armature,
                    arm_clip,
                    arm_track,
                    ACTION_PREFIX + label + "_motion_clip",
                    active_arm,
                    keep_active,
                )
            for owner in key_owners:
                ptr = owner.as_pointer()
                is_primary = primary_keys is not None and owner == primary_keys
                clip_name = (
                    ACTION_PREFIX
                    + label
                    + ("_shapekey_clip" if is_primary else "_shapekey_" + owner.name)
                )
                shape_result = place_clip(
                    owner, sources[ptr], shape_track, clip_name, stash[ptr][1]
                )
                if is_primary and shape_result is not None:
                    item.shape_clip = shape_result
            placed = True
    finally:
        release_stash(stash, restore=not placed)
    if motion and settings.use_physics_vmd and armature is not None:
        try:
            import_physics_clip(item, armature, settings, label)
        except Exception as error:
            log.error("physics clip failed: %s\n%s", error, traceback.format_exc())
    if motion and is_rigify_armature(armature):
        remap_rigify(armature, model, item, label)


# A Rigify rig made by MMD Tools Append cannot take the PMM motion on its MMD bones, as
# the Rigify controls drive them. The motion is carried over onto the controls from the
# MMD bones the plain import just keyed. Failing here leaves the plain import in place.
def remap_rigify(armature, model, item, label):
    if not item.rigify_remap:
        log.info("Rigify remap is off for '%s'", model.name)
        clear_remap(armature, label)
        return
    try:
        with phase("rigify", label):
            retarget(armature, model, label, item)
    except Exception as error:
        log.error("Rigify remap failed: %s\n%s", error, traceback.format_exc())


# Collects warnings logged during one model's sync so its row can show the WARN
# state.
class WarningCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.messages = []

    def emit(self, record):
        text = record.getMessage()
        if not text.startswith("mmd_tools reported"):
            self.messages.append(text)


# Show/hide keys for one model. Kept apart from the import so a failure here costs only
# the visibility record, and the motion that already imported keeps its own.
def sync_visibility(target, model, settings, entry):
    label = model_label(model)
    try:
        with phase("visibility", label):
            layout = rig_layout(target.target)
            apply_visibility(layout.meshes, model, label)
        write_parts(target, entry, layout, settings, label, ("visible",))
    except Exception as error:
        log.error(
            "visibility for '%s' failed: %s\n%s", target.pmm_name, error, traceback.format_exc()
        )
        forget_parts(target, ("visible",))


# Syncs one model row and records OK, WARN or ERROR on it. A failure is contained so
# the other models still sync. With a plan entry only its running parts import, and the
# row's record moves forward only once they all succeeded.
def sync_target(target, model, settings, entry=None):
    capture = WarningCapture()
    log.addHandler(capture)
    parts = entry.run_parts if entry is not None else ALL_PARTS
    imported = tuple(p for p in parts if p in ALL_PARTS)
    rebuild = entry is None or any(entry.parts[p].forced for p in imported)
    try:
        if imported:
            with phase("model", model_label(model)):
                import_model(target.target, model, settings, target, imported, rebuild)
            if entry is not None:
                layout = rig_layout(target.target)
                write_parts(target, entry, layout, settings, model_label(model), imported)
        if "visible" in parts:
            sync_visibility(target, model, settings, entry)
    except Exception as error:
        log.error(
            "model '%s' failed: %s\n%s", target.pmm_name, error, traceback.format_exc()
        )
        forget_parts(target, imported)
        target.state = "ERROR"
        target.status_text = short_text(error)
        return False
    finally:
        log.removeHandler(capture)
    if capture.messages:
        target.state = "WARN"
        target.status_text = "%d warning(s): %s" % (
            len(capture.messages),
            short_text(capture.messages[0], 70),
        )
    else:
        target.state = "OK"
        target.status_text = ""
    return True
