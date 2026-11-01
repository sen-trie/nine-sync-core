from ..common.animation import iter_fcurves, rewrite_fcurve
from ..common.logger import log
from .morphs import expand_group_morph, group_morph_table, uv_morph_table
from .uv_morphs import uv_key_name


# Linear weight at a frame, clamped to the first and last key.
def sample_morph(keys, frame):
    if frame <= keys[0].frame:
        return keys[0].weight
    if frame >= keys[-1].frame:
        return keys[-1].weight
    for a, b in zip(keys, keys[1:]):
        if a.frame <= frame <= b.frame:
            span = b.frame - a.frame
            if span == 0:
                return a.weight
            return a.weight + (b.weight - a.weight) * (frame - a.frame) / span
    return keys[-1].weight


# Groups morph tracks by the shape key they finally drive, expanding group morphs.
# Also returns the keys driven through groups and group offsets that are not shape
# keys (material, bone), which are skipped.
def collect_contributions(model, resolved, groups, uv_morphs):
    contributions = {}
    group_driven = set()
    skipped = []
    for name, mkeys in zip(model.morph_names, model.morphs):
        if not any(abs(k.weight) > 1e-6 for k in mkeys):
            continue
        target = resolved.get(name)
        if target is None:
            continue
        if target in groups:
            for kind, leaf, factor in expand_group_morph(groups, target, 1.0):
                if kind in ("vertex_morphs", "uv_morphs"):
                    contributions.setdefault((kind, leaf), []).append((mkeys, factor))
                    group_driven.add((kind, leaf))
                else:
                    skipped.append((name, kind, leaf, round(factor, 3)))
        elif target in uv_morphs:
            contributions.setdefault(("uv_morphs", target), []).append((mkeys, 1.0))
        else:
            contributions.setdefault(("vertex_morphs", target), []).append((mkeys, 1.0))
    return contributions, group_driven, skipped


# Keys shape keys directly where mmd_tools' VMD import does not: group morph
# expansion and UV channels. Curves the importer already made are kept unless a
# group drives them, and keys driven by mmd_tools' own bindings are left to their
# drivers.
def ensure_morph_keys(hosts, model, resolved, root):
    groups = group_morph_table(root)
    contributions, group_driven, skipped = collect_contributions(
        model, resolved, groups, uv_morph_table(root)
    )
    written = []
    driven_skipped = []
    for host in hosts:
        keys = getattr(getattr(host, "data", None), "shape_keys", None)
        if keys is None:
            continue
        anim = keys.animation_data
        action = anim.action if anim else None
        existing = (
            {fc.data_path for fc in iter_fcurves(action, anim.action_slot)}
            if action is not None
            else set()
        )
        driven = {fc.data_path for fc in anim.drivers} if anim is not None else set()
        blocks = {block.name: block for block in keys.key_blocks}
        for (kind, leaf), items in contributions.items():
            block = blocks.get(leaf if kind == "vertex_morphs" else uv_key_name(leaf))
            if block is None:
                continue
            path = block.path_from_id("value")
            if path in driven:
                driven_skipped.append(leaf)
                continue
            if (kind, leaf) not in group_driven and path in existing:
                continue
            frames = sorted({k.frame for mkeys, _ in items for k in mkeys})
            values = []
            for frame in frames:
                total = sum(
                    factor * sample_morph(mkeys, frame) for mkeys, factor in items
                )
                values.append(min(max(total, block.slider_min), block.slider_max))
            block.value = values[0]
            try:
                keys.keyframe_insert(data_path=path, frame=frames[0])
            except (RuntimeError, TypeError, ValueError) as error:
                log.warning("could not key shape key '%s': %s", block.name, error)
                continue
            anim = keys.animation_data
            action = anim.action if anim else None
            fcurve = (
                next(
                    (
                        fc
                        for fc in iter_fcurves(action, anim.action_slot)
                        if fc.data_path == path
                    ),
                    None,
                )
                if action
                else None
            )
            if fcurve is None:
                log.warning("could not create an fcurve for shape key '%s'", block.name)
                continue
            rewrite_fcurve(fcurve, frames, values)
            written.append(block.name)
    if written:
        log.info(
            "keyed %d shape key(s) directly (group morph expansion, UV channels, or morphs mmd_tools skipped): %s",
            len(written),
            written[:20],
        )
    if driven_skipped:
        log.info(
            "shape keys driven by mmd_tools morph binding were left to their drivers: %s",
            sorted(set(driven_skipped))[:20],
        )
    if skipped:
        log.warning(
            "group morph offsets that are not shape keys or UV morphs were not applied (material/bone): %s",
            skipped[:20],
        )
    return written
