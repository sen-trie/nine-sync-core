import os

import bpy

from ..common.logger import log
from ..common.scene_utils import model_key
from .targets import refresh_targets, target_usable


SEARCH_DEPTH = 4
SEARCH_LIMIT = 4000


# True once any PMM model is linked to a Blender model; One-Click Setup is offered
# until then.
def pmm_linked(settings):
    return any(item.target is not None for item in settings.targets)


# The PMX a PMM model was saved with, or None. The stored path first, then by file name
# in the PMM's folder and a few levels below it, because projects move between machines
# and MMD often stores paths relative to its own folder.
def find_model_file(model, pmm_path):
    stored = model.path.replace("\\", os.sep)
    base = os.path.basename(stored).lower()
    pmm_dir = os.path.dirname(pmm_path)
    for candidate in (stored, os.path.join(pmm_dir, stored), os.path.join(pmm_dir, base)):
        if candidate and os.path.isfile(candidate):
            return os.path.normpath(candidate)
    if not base:
        return None
    seen = 0
    for folder, dirs, files in os.walk(pmm_dir):
        depth = os.path.relpath(folder, pmm_dir).count(os.sep)
        if depth >= SEARCH_DEPTH:
            dirs[:] = []
        seen += 1
        if seen > SEARCH_LIMIT:
            break
        for name in files:
            if name.lower() == base:
                return os.path.normpath(os.path.join(folder, name))
    return None


def mmd_roots():
    return {o for o in bpy.data.objects if getattr(o, "mmd_type", "") == "ROOT"}


# Imports one PMX with mmd_tools' own importer and returns the root it made, or None.
# The importer reports its errors and still returns FINISHED, so the new root is what
# tells success apart.
def import_pmx(path, scale):
    before = mmd_roots()
    try:
        bpy.ops.mmd_tools.import_model(filepath=path, scale=scale)
    except RuntimeError as error:
        log.error("mmd_tools could not import %s: %s", path, error)
        return None
    made = [o for o in mmd_roots() - before if o.parent is None]
    return made[0] if made else None


# Reads the PMM's models into the target list, then imports each one no Blender model
# matched and links it to its row. A PMM that uses one PMX twice gets two imports, one
# per row. Returns (imported names, PMM names whose PMX was not found, failed names).
def setup_models(settings, project, pmm_path, scale):
    refresh_targets(settings, project)
    by_key = {model_key(m): m for m in project.models}
    imported, missing, failed = [], [], []
    for item in settings.targets:
        if item.target is not None and target_usable(item.target):
            continue
        model = by_key.get(item.key)
        if model is None:
            continue
        path = find_model_file(model, pmm_path)
        if path is None:
            log.warning("one-click setup: PMX for '%s' not found (%s)", item.pmm_name, model.path)
            missing.append(item.pmm_name)
            continue
        root = import_pmx(path, scale)
        if root is None:
            failed.append(item.pmm_name)
            continue
        root["ninesync_pmx_path"] = path
        item.target = root
        item.enabled = True
        imported.append(item.pmm_name)
        log.info("one-click setup: imported '%s' from %s at scale %g as '%s'", item.pmm_name, path, scale, root.name)
    return imported, missing, failed
