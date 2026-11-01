import bpy

from ..common.logger import log
from ..common.scene_utils import find_armature, model_key


# A target is usable when it is still in the scene and has an armature.
def target_usable(obj):
    return (
        obj is not None
        and len(obj.users_collection) > 0
        and find_armature(obj) is not None
    )


# Candidate mmd_tools roots for a PMM model, in the first matching tier, most reliable
# tier first: exact name, English name, object name, then partial name. More than one
# is an ambiguous match. Writes nothing, not even a log line, so previews can use it.
def match_roots(model):
    roots = [
        o
        for o in bpy.data.objects
        if getattr(o, "mmd_type", "") == "ROOT" and target_usable(o)
    ]
    tiers = (
        lambda o: model.name and o.mmd_root.name == model.name,
        lambda o: model.name_en and o.mmd_root.name_e == model.name_en,
        lambda o: o.name in (model.name, model.name_en),
        lambda o: any(
            name and model.name and (name in model.name or model.name in name)
            for name in (o.mmd_root.name, o.mmd_root.name_e, o.name)
        ),
    )
    for tier in tiers:
        found = [o for o in roots if tier(o)]
        if found:
            return found
    return []


# Auto-matches a PMM model to an mmd_tools root.
def find_root_for(model):
    found = match_roots(model)
    log.debug("auto-match for PMM model '%s': %s", model.name, [o.name for o in found])
    return found[0] if found else None


# Syncs the target list with the PMM's models: drops slots for models that are gone,
# adds new ones, and re-matches targets that were deleted from the scene.
def refresh_targets(settings, project):
    wanted = [model_key(m) for m in project.models]
    log.info("PMM contains %d model(s): %s", len(wanted), wanted)
    for index in reversed(range(len(settings.targets))):
        if settings.targets[index].key not in wanted:
            log.info("removing stale target %s", settings.targets[index].key)
            settings.targets.remove(index)
    present = {t.key: t for t in settings.targets}
    for model in project.models:
        key = model_key(model)
        item = present.get(key)
        if item is None:
            item = settings.targets.add()
            item.key = key
            item.pmm_name = model.name or model.name_en or key
            item.enabled = True
            present[key] = item
            log.info("added target slot %s", key)
        if item.target is not None and not target_usable(item.target):
            log.warning(
                "target '%s' for '%s' is stale (removed from the scene or has no armature); re-matching",
                item.target.name,
                item.pmm_name,
            )
            item.target = None
        if item.target is None:
            found = find_root_for(model)
            if found is not None:
                log.info("auto-matched '%s' -> %s", item.pmm_name, found.name)
                item.target = found
            else:
                log.warning(
                    "no mmd_tools root matched PMM model '%s'; assign one manually",
                    item.pmm_name,
                )


# Row state for the panel and report: OFF, ERROR (no target), or the last sync
# result.
def item_state(item):
    if not item.enabled:
        return "OFF"
    if item.target is None:
        return "ERROR"
    return item.state or "NEW"


# Note shown under a model row.
def item_status_text(item):
    if not item.enabled:
        return ""
    if item.target is None:
        return "No target assigned"
    return item.status_text
