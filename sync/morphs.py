from ..common.logger import log


# mmd_tools' per-type morph lists on a root.
def morph_collections(root):
    mmd_root = getattr(root, "mmd_root", None)
    if mmd_root is None:
        return []
    names = (
        "vertex_morphs",
        "bone_morphs",
        "material_morphs",
        "uv_morphs",
        "group_morphs",
    )
    return [getattr(mmd_root, name) for name in names if hasattr(mmd_root, name)]


# Maps PMM morph names to Blender names, which can differ: exact name first, then
# English name, then a unique CP932 prefix match for names PMM stored truncated.
# Returns the mapping and every name Blender has.
def resolve_morph_names(root, meshes, model):
    entries = []
    seen = set()
    for collection in morph_collections(root):
        for morph in collection:
            if morph.name not in seen:
                seen.add(morph.name)
                entries.append((morph.name, getattr(morph, "name_e", "")))
    for mesh in meshes:
        keys = getattr(getattr(mesh, "data", None), "shape_keys", None)
        if keys is None:
            continue
        for block in keys.key_blocks:
            if block.name not in seen:
                seen.add(block.name)
                entries.append((block.name, ""))
    by_english = {}
    for name, name_e in entries:
        if name_e and name_e not in by_english:
            by_english[name_e] = name
    resolved = {}
    for pmm_name in model.morph_names:
        if pmm_name in seen:
            resolved[pmm_name] = pmm_name
        elif pmm_name in by_english:
            resolved[pmm_name] = by_english[pmm_name]
        else:
            prefix = pmm_name.encode("cp932", errors="ignore")
            if prefix:
                candidates = [
                    n
                    for n, _ in entries
                    if n.encode("cp932", errors="ignore").startswith(prefix)
                ]
                if len(candidates) == 1:
                    resolved[pmm_name] = candidates[0]
    return resolved, [name for name, _ in entries]


# Logs unmatched animated morphs with the names Blender has. The usual cause of
# 'morph does nothing' reports.
def report_morphs(model, resolved, available):
    animated = [
        n
        for n, keys in zip(model.morph_names, model.morphs)
        if any(abs(k.weight) > 1e-6 for k in keys)
    ]
    log.info("morphs with non-zero keys: %d %s", len(animated), animated[:20])
    renamed = [(n, resolved[n]) for n in animated if n in resolved and resolved[n] != n]
    if renamed:
        log.info("morph name remap: %s", renamed)
    missing = [n for n in animated if n not in resolved]
    if missing:
        log.warning("PMM morphs with no Blender match: %s", missing)
        log.warning("Blender morph names available (first 80): %s", available[:80])


# Group morphs of a root by name.
def group_morph_table(root):
    mmd_root = getattr(root, "mmd_root", None)
    groups = getattr(mmd_root, "group_morphs", None)
    if groups is None:
        return {}
    return {group.name: group for group in groups}


# UV morphs of a root by name.
def uv_morph_table(root):
    mmd_root = getattr(root, "mmd_root", None)
    morphs = getattr(mmd_root, "uv_morphs", None)
    if morphs is None:
        return {}
    return {morph.name: morph for morph in morphs}


# Flattens nested group morphs into (type, name, factor) leaves. The depth cap
# guards against cyclic groups.
def expand_group_morph(groups, name, factor, depth=0):
    group = groups.get(name)
    if group is None or depth > 6:
        return []
    leaves = []
    for offset in group.data:
        scaled = factor * offset.factor
        if offset.morph_type == "group_morphs":
            leaves.extend(expand_group_morph(groups, offset.name, scaled, depth + 1))
        else:
            leaves.append((offset.morph_type, offset.name, scaled))
    return leaves
