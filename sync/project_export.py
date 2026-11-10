import bpy
import os
import re

from ..common.constants import (
    PMM_FPS,
    PMM_TEMPLATE_PATH,
    PMX_EXTENSIONS,
    TEMP_PREFIX,
)
from ..common.logger import log
from ..common.scene_utils import fps_differs, model_key, scene_fps
from ..pmm import (
    PmmError,
    PmmModel,
    encode_name,
    interpolation_from_vmd,
    VisibilityKey,
    parse_pmm_file,
    read_vmd,
)
from ..common.animation import iter_fcurves
from ..pmm_writer import build_pmm
from .accessory_export import collect_accessories, marker_accessory
from .mmd_tools_link import mmd_module
from .pmx_labels import write_labelled_pmx
from .retime import retime_keys
from .root_motion import has_root_motion
from .scale import export_scale
from .sync_state import rig_layout
from .targets import refresh_targets, target_usable
from .tempfiles import remove_file, temp_vmd_path


def template_path():
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), PMM_TEMPLATE_PATH)


# The original PMX of an mmd_tools model. mmd_tools only stores the import folder
# and names the root after the file, so the path is rebuilt from those; a path an
# earlier export remembered wins.
def find_pmx_path(root):
    remembered = root.get("ninesync_pmx_path", "")
    if remembered and os.path.isfile(remembered):
        return remembered
    folder = root.get("import_folder", "")
    if not folder or not os.path.isdir(folder):
        return ""
    stem = re.sub(r"\.\d{3}$", "", root.name)
    for name in (stem, root.mmd_root.name, root.mmd_root.name_e):
        for extension in PMX_EXTENSIONS:
            candidate = os.path.join(folder, name + extension)
            if name and os.path.isfile(candidate):
                return candidate
    found = [f for f in os.listdir(folder) if f.lower().endswith(PMX_EXTENSIONS)]
    if len(found) == 1:
        return os.path.join(folder, found[0])
    return ""


# Keeps the export checklist in step with the mmd_tools models in the file: new
# models are added enabled, vanished ones dropped, and a missing PMX path re-found.
def refresh_export_items(settings):
    roots = [
        o
        for o in bpy.data.objects
        if getattr(o, "mmd_type", "") == "ROOT" and target_usable(o)
    ]
    for index in reversed(range(len(settings.export_items))):
        if settings.export_items[index].target not in roots:
            settings.export_items.remove(index)
    present = {i.target for i in settings.export_items}
    for root in roots:
        if root not in present:
            item = settings.export_items.add()
            item.target = root
            item.enabled = True
    for item in settings.export_items:
        if not item.pmx_path or not os.path.isfile(bpy.path.abspath(item.pmx_path)):
            item.pmx_path = find_pmx_path(item.target)
    log.info("export list: %d model(s)", len(settings.export_items))


# Bone names, morph names and IK bone indices in the order MMD loads them from the
# PMX itself, since a PMM refers to both by index. Every morph is listed, system
# ones included: leaving some out makes MMD report a different model structure.
# A PMD goes through mmd_tools' own PMD-to-PMX conversion, so its bone list is
# whatever that conversion produces; unverified against MMD's PMD handling.
# Timeline rows and morph panels follow what MMD saves for the same PMX: the two fixed
# rows, one per own display frame, one for facial when there are morphs; and the first
# morph of each panel (brow, eye, mouth, other), -1 for an empty one.
def read_pmx_layout(path):
    if path.lower().endswith(".pmd"):
        pmx = mmd_module("core.pmd.importer").import_pmd_to_pmx(path)
    else:
        pmx = mmd_module("core.pmx").load(path)
    bone_names = [b.name for b in pmx.bones]
    morphs = pmx.morphs
    constraints = [i for i, b in enumerate(pmx.bones) if b.isIK]
    own_frames = sum(1 for frame in pmx.display if not frame.isSpecial)
    frame_rows = 2 + own_frames + (1 if morphs else 0)
    categories = [m.category for m in morphs]
    panels = tuple(
        categories.index(panel) if panel in categories else -1 for panel in (1, 2, 3, 4)
    )
    hidden = {i for i, b in enumerate(pmx.bones) if not b.visible}
    return bone_names, [m.name for m in morphs], constraints, frame_rows, panels, hidden, outside_parents(pmx)


# The outside parent slots MMD keeps for a model: -1 for the whole model, then every IK
# bone and every bone the user can move (movable, visible, operable). MMD rebuilds this
# list from the PMX on load and reports "model structure is different" when the PMM's
# list does not match. Checked against 15 models saved by MMD 9.32.
def outside_parents(pmx):
    return [-1] + [
        i
        for i, b in enumerate(pmx.bones)
        if b.isIK or (b.isMovable and b.visible and b.isControllable)
    ]


# Runs mmd_tools' own VMD exporter into a temp file and reads it back. Its scale is
# the inverse of the importer's, so an export lands where a sync would put it.
def export_vmd(settings, **params):
    exporter = mmd_module("core.vmd.exporter")
    path = temp_vmd_path(TEMP_PREFIX + "export_")
    try:
        exporter.VMDExporter().export(
            filepath=path, scale=1.0 / export_scale(settings), **params
        )
        with open(path, "rb") as stream:
            return read_vmd(stream.read())
    finally:
        remove_file(path)


# Blender's frame rate is the reference: a PMM is always 30 fps, so frames are
# converted whatever the Retime option says. Sync then undoes it with the same factor.
def export_factor(scene):
    return scene_fps(scene) / PMM_FPS


# A first key after frame 0 would leave MMD's initial state to a default, so a track
# that starts late holds its first value from frame 0.
def anchored(keys):
    if keys and keys[0].frame != 0:
        return [keys[0]._replace(frame=0)] + keys
    return keys


# Maps the exporter's frame numbers to PMM frames: it subtracts min(1, frame_start),
# and sync multiplies PMM frames by the retime factor.
def to_pmm_frames(keys, scene, settings, hold_cuts=False):
    offset = min(1, scene.frame_start)
    shifted = [k._replace(frame=k.frame + offset) for k in keys]
    factor = export_factor(scene)
    if abs(factor - 1.0) < 1e-6:
        return shifted
    return retime_keys(shifted, 1.0 / factor, hold_cuts)


VISIBILITY_PATHS = ("hide_viewport", "hide_render")


# Scene frames where a mesh's hide_viewport or hide_render is keyed, from the active
# action and from unmuted NLA strips (Sync Model Visibility plays from one). Boolean
# curves only change value on a key, so these are the only frames to look at.
def visibility_key_frames(meshes):
    frames = set()
    for mesh in meshes:
        anim = mesh.animation_data
        if anim is None:
            continue
        sources = []
        if anim.action is not None:
            sources.append((anim.action, anim.action_slot, 0.0, 1.0))
        for track in anim.nla_tracks:
            if track.mute:
                continue
            for strip in track.strips:
                if strip.action is not None and not strip.mute:
                    offset = strip.frame_start - strip.action_frame_start * strip.scale
                    sources.append((strip.action, strip.action_slot, offset, strip.scale))
        for action, slot, offset, scale in sources:
            for fcurve in iter_fcurves(action, slot):
                if fcurve.data_path in VISIBILITY_PATHS:
                    for point in fcurve.keyframe_points:
                        frames.add(int(round(offset + point.co.x * scale)))
    return frames


# MMD shows or hides a whole model, so the model counts as hidden at a frame only when
# every mesh is hidden there, in the viewport or in renders. The timeline is stepped to
# each keyed frame so NLA and the active action combine as Blender plays them, then put
# back. Unkeyed meshes give one key: their current state.
def collect_visibility(scene, settings, root):
    layout = rig_layout(root)
    meshes = list(layout.meshes) if layout is not None else []
    if not meshes:
        return []
    frames = sorted(visibility_key_frames(meshes) | {scene.frame_start})
    current, subframe = scene.frame_current, scene.frame_subframe
    keys = []
    try:
        for frame in frames:
            scene.frame_set(frame)
            hidden = all(any(getattr(m, p) for p in VISIBILITY_PATHS) for m in meshes)
            keys.append(VisibilityKey(frame, 0 if hidden else 1))
    finally:
        scene.frame_set(current, subframe=subframe)
    factor = export_factor(scene)
    if abs(factor - 1.0) > 1e-6:
        keys = retime_keys(keys, 1.0 / factor, True)
    start = [k for k in keys if k.frame <= 0]
    folded = [(start[-1] if start else keys[0])._replace(frame=0)]
    for key in keys:
        if key.frame > 0 and folded[-1].visible != key.visible:
            folded.append(key)
    if len(folded) == 1 and folded[0].visible:
        return []
    return folded


def collect_model(scene, settings, index, root, path):
    bone_names, morph_names, constraints, frame_rows, panels, hidden, outside = read_pmx_layout(path)
    model = PmmModel()
    model.index = index
    model.name = root.mmd_root.name or root.name
    model.name_en = root.mmd_root.name_e or ""
    model.path = os.path.normpath(path)
    model.bone_names = bone_names
    model.morph_names = morph_names
    model.constraints = constraints
    model.outside_parents = outside
    model.bones = [[] for _ in bone_names]
    model.morphs = [[] for _ in morph_names]
    model.frame_rows = frame_rows
    model.morph_panels = panels
    want_bones = settings.export_animation and settings.export_bones
    want_morphs = settings.export_animation and settings.export_morphs
    if want_bones or want_morphs:
        rig = mmd_module("core.model").Model(root)
        armature = rig.armature() if want_bones else None
        mesh = None
        if want_morphs:
            mesh = rig.morph_slider.placeholder(binded=True) or rig.firstMesh()
        if armature is not None or mesh is not None:
            bones, morphs, _ = export_vmd(
                settings, armature=armature, mesh=mesh, model_name=model.name
            )
            by_bone = {encode_name(n): i for i, n in enumerate(bone_names)}
            by_morph = {encode_name(n): i for i, n in enumerate(morph_names)}
            for name, keys in bones.items():
                slot = by_bone.get(name)
                if slot is not None:
                    keys = [
                        k._replace(interpolation=interpolation_from_vmd(k.interpolation))
                        for k in keys
                    ]
                    model.bones[slot] = anchored(to_pmm_frames(keys, scene, settings))
            for name, keys in morphs.items():
                slot = by_morph.get(name)
                if slot is not None:
                    model.morphs[slot] = anchored(to_pmm_frames(keys, scene, settings))
    if settings.export_animation and settings.export_visibility:
        model.visibility = collect_visibility(scene, settings, root)
    log.info(
        "export model %d '%s': %d bones, %d morphs, %d bone keys, %d morph keys, visibility %s",
        index,
        model.name,
        len(bone_names),
        len(morph_names),
        sum(len(k) for k in model.bones),
        sum(len(k) for k in model.morphs),
        [(k.frame, k.visible) for k in model.visibility][:10] or "always shown",
    )
    return model, {slot for slot in hidden if len(model.bones[slot]) > 1}


# MMD shows a bone flagged not visible in the PMX at its frame 0 state and never
# evaluates its later keys, though it keeps them. A labelled copy unhides them; with
# the original PMX only the user can.
def hidden_bone_warning(model, root, stuck):
    return "'%s': %d keyed bone(s) are hidden in its PMX (%s...), so MMD will not play them. Turn on Clean MMD Labels, or unhide them and re-export the PMX" % (
        root.name,
        len(stuck),
        model.bone_names[min(stuck)],
    )


# Points the Syncer at the new file and assigns each exported model to its Blender
# root, so the first sync back after editing in MMD touches the same objects. Opt-in:
# this is the only part of an export that writes to the .blend.
def link_to_syncer(scene, settings, out_path, roots):
    if fps_differs(scene) and not settings.retime_fps:
        settings.retime_fps = True
        log.info("scene is %g fps; Retime to Scene FPS turned on so sync matches", scene_fps(scene))
    settings.filepath = out_path
    project = parse_pmm_file(out_path)
    refresh_targets(settings, project)
    by_key = {t.key: t for t in settings.targets}
    for model, (root, path) in zip(project.models, roots):
        item = by_key.get(model_key(model))
        if item is not None:
            item.enabled = True
            if item.target != root:
                item.target = root
        root["ninesync_pmx_path"] = path
    settings.pmm_accessory_count = len(project.accessories)


# Writes the PMM, playing the scene frame range. Nothing in the scene changes: the
# timeline is stepped to read motion and put back. See link_to_syncer for the exception.
# With no model checked it still writes a project holding the camera and props.
def create_pmm(scene, settings, out_path):
    items = [i for i in settings.export_items if i.enabled and i.target is not None]
    template = template_path()
    if not os.path.isfile(template):
        raise PmmError("PMM template missing: %s" % template)
    with open(template, "rb") as stream:
        template_data = stream.read()
    models = []
    roots = []
    warnings = []
    taken = set()
    for item in items:
        path = bpy.path.abspath(item.pmx_path)
        if not os.path.isfile(path):
            raise PmmError("PMX file not found for '%s': %s" % (item.target.name, path))
        root = item.target
        model, stuck = collect_model(scene, settings, len(models), root, path)
        if settings.export_mmd_labels and path.lower().endswith(".pmx"):
            model.path, model.name_en = write_labelled_pmx(
                root, model.index, path, out_path, stuck, taken
            )
            drive = os.path.splitdrive(path)[0].lower()
            if drive != os.path.splitdrive(os.path.abspath(out_path))[0].lower():
                warnings.append("'%s': its PMX is on another drive, so its copy cannot reach the textures. Save the PMM on %s" % (root.name, drive.upper()))
        elif stuck:
            warnings.append(hidden_bone_warning(model, root, stuck))
        if has_root_motion(root):
            warnings.append("'%s' has root motion, which a PMM cannot hold. Press Bake Root Motion on its row" % root.name)
        models.append(model)
        roots.append((root, path))
    for warning in warnings:
        log.warning(warning)
    camera_keys = []
    # Only the rig picked as MMD Camera is written; none picked keeps MMD's default
    # camera. A rig is never made or rebuilt on export, because rebuilding deletes the
    # rig the user's scene camera points at.
    rig_root = settings.export_camera_object if settings.export_animation else None
    if rig_root is not None:
        _, _, cameras = export_vmd(settings, camera=rig_root)
        camera_keys = anchored(to_pmm_frames(cameras, scene, settings, True))
        log.info("export camera '%s': %d key(s)", rig_root.name, len(camera_keys))
    factor = export_factor(scene)
    play_range = (
        int(round(scene.frame_start / factor)),
        int(round(scene.frame_end / factor)),
    )
    props = [i.target for i in settings.export_props if i.enabled and i.target is not None]
    accessories = collect_accessories(scene, settings, props, out_path) if props else []
    prop_count = len(accessories)
    if settings.export_animation and settings.export_markers:
        taken = {acc.name.lower() for acc in accessories}
        markers, merged, early = marker_accessory(scene, out_path, len(accessories), taken)
        if markers is not None:
            accessories.append(markers)
        for count, text in (
            (merged, "%d marker(s) land on the same MMD frame as another marker and share its key"),
            (early, "%d marker(s) before frame 0 were left out; MMD starts at frame 0"),
        ):
            if count:
                log.warning(text % count)
                warnings.append(text % count)
    data = build_pmm(template_data, models, camera_keys, play_range, accessories)
    with open(out_path, "wb") as stream:
        stream.write(data)
    log.info("wrote %s (%d bytes)", out_path, len(data))
    if settings.export_link_syncer:
        link_to_syncer(scene, settings, out_path, roots)
    return len(models), len(camera_keys), prop_count, warnings
