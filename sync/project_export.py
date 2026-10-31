import bpy
import os
import re

from ..common.constants import PMM_FPS, PMM_TEMPLATE_PATH, PMX_EXTENSIONS, TEMP_PREFIX
from ..common.logger import log
from ..common.scene_utils import fps_differs, model_key, scene_fps
from ..pmm import (
    PmmError,
    PmmModel,
    encode_name,
    interpolation_from_vmd,
    parse_pmm_file,
    read_vmd,
)
from ..pmm_writer import build_pmm
from .accessory_export import collect_accessories
from .mmd_tools_link import mmd_module
from .retime import retime_keys
from .rigs import ensure_camera_rig
from .scale import current_scale
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
def read_pmx_layout(path):
    if path.lower().endswith(".pmd"):
        pmx = mmd_module("core.pmd.importer").import_pmd_to_pmx(path)
    else:
        pmx = mmd_module("core.pmx").load(path)
    bone_names = [b.name for b in pmx.bones]
    morphs = pmx.morphs
    constraints = [i for i, b in enumerate(pmx.bones) if b.isIK]
    return bone_names, [m.name for m in morphs], constraints


# Runs mmd_tools' own VMD exporter into a temp file and reads it back. Its scale is
# the inverse of the importer's, so an export lands where a sync would put it.
def export_vmd(settings, **params):
    exporter = mmd_module("core.vmd.exporter")
    path = temp_vmd_path(TEMP_PREFIX + "export_")
    try:
        exporter.VMDExporter().export(
            filepath=path, scale=1.0 / current_scale(settings), **params
        )
        with open(path, "rb") as stream:
            return read_vmd(stream.read())
    finally:
        remove_file(path)


# Blender's frame rate is the reference: a PMM is always 30 fps, so frames are
# converted whatever the Retime option says. Sync then undoes it with the same factor.
def export_factor(scene):
    return scene_fps(scene) / PMM_FPS


# Maps the exporter's frame numbers to PMM frames: it subtracts min(1, frame_start),
# and sync multiplies PMM frames by the retime factor.
def to_pmm_frames(keys, scene, settings, hold_cuts=False):
    offset = min(1, scene.frame_start)
    shifted = [k._replace(frame=k.frame + offset) for k in keys]
    factor = export_factor(scene)
    if abs(factor - 1.0) < 1e-6:
        return shifted
    return retime_keys(shifted, 1.0 / factor, hold_cuts)


def collect_model(scene, settings, index, root, path):
    bone_names, morph_names, constraints = read_pmx_layout(path)
    model = PmmModel()
    model.index = index
    model.name = root.mmd_root.name or root.name
    model.name_en = root.mmd_root.name_e or ""
    model.path = os.path.normpath(path)
    model.bone_names = bone_names
    model.morph_names = morph_names
    model.constraints = constraints
    model.bones = [[] for _ in bone_names]
    model.morphs = [[] for _ in morph_names]
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
                    model.bones[slot] = to_pmm_frames(keys, scene, settings)
            for name, keys in morphs.items():
                slot = by_morph.get(name)
                if slot is not None:
                    model.morphs[slot] = to_pmm_frames(keys, scene, settings)
    log.info(
        "export model %d '%s': %d bones, %d morphs, %d bone keys, %d morph keys",
        index,
        model.name,
        len(bone_names),
        len(morph_names),
        sum(len(k) for k in model.bones),
        sum(len(k) for k in model.morphs),
    )
    return model


# Writes the PMM (playing the scene frame range) and links it to the Syncer: the file becomes the Syncer's PMM, and
# each exported model is assigned to its Blender root explicitly, so the first sync
# back after editing in MMD touches the same objects. The camera is the rig the
# Syncer already uses, so its mapping carries over as well.
def create_pmm(scene, settings, out_path):
    if fps_differs(scene) and not settings.retime_fps:
        settings.retime_fps = True
        log.info("scene is %g fps; Retime to Scene FPS turned on so sync matches", scene_fps(scene))
    items = [i for i in settings.export_items if i.enabled and i.target is not None]
    if not items:
        raise PmmError("no models are checked")
    template = template_path()
    if not os.path.isfile(template):
        raise PmmError("PMM template missing: %s" % template)
    with open(template, "rb") as stream:
        template_data = stream.read()
    models = []
    roots = []
    for item in items:
        path = bpy.path.abspath(item.pmx_path)
        if not os.path.isfile(path):
            raise PmmError("PMX file not found for '%s': %s" % (item.target.name, path))
        models.append(collect_model(scene, settings, len(models), item.target, path))
        roots.append((item.target, path))
    camera_keys = []
    if settings.export_camera:
        rig_root, _ = ensure_camera_rig(scene, settings)
        if settings.export_animation and settings.export_camera_motion:
            _, _, cameras = export_vmd(settings, camera=rig_root)
            camera_keys = to_pmm_frames(cameras, scene, settings, True)
    factor = export_factor(scene)
    play_range = (
        int(round(scene.frame_start / factor)),
        int(round(scene.frame_end / factor)),
    )
    props = [i.target for i in settings.export_props if i.enabled and i.target is not None]
    accessories = collect_accessories(scene, settings, props, out_path) if props else []
    data = build_pmm(template_data, models, camera_keys, play_range, accessories)
    with open(out_path, "wb") as stream:
        stream.write(data)
    log.info("wrote %s (%d bytes)", out_path, len(data))
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
    return len(models), len(camera_keys), len(accessories)
