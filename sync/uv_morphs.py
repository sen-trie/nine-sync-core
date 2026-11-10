import bmesh
import hashlib
import json
import numpy

from ..common.constants import (
    UV_ATTRIBUTE_PREFIX,
    UV_KEY_PREFIX,
    UV_NODE_PREFIX,
    UV_SLIDER_LIMIT,
)
from ..common.logger import log
from .morphs import expand_group_morph, group_morph_table, uv_morph_table


# Mesh property recording what the last UV morph build made, and from what.
UV_STATE_PROPERTY = "mmd_bridge_uv"


# Shape key that carries a UV morph's weight, cut to 63 bytes, Blender's name limit.
def uv_key_name(leaf):
    raw = (UV_KEY_PREFIX + leaf).encode("utf-8")[:63]
    return raw.decode("utf-8", errors="ignore")


# Mesh attribute holding one channel's per-vertex UV offsets.
def uv_attribute_name(index):
    return "%s%d" % (UV_ATTRIBUTE_PREFIX, index)


# Parses mmd_tools' vertex group names (UV_<morph>+X) into (sign, axis index).
def uv_group_axis(name, morph_name):
    if not name.startswith("UV_") or len(name) < 6:
        return None
    if name[3:-2] != morph_name or name[-2] not in "+-" or name[-1] not in "XYZW":
        return None
    return name[-2], "XYZW".index(name[-1])


# Per-vertex UV offsets of each of a mesh's UV morphs, {leaf: {vertex: [x, y, z, w]}}.
# Vertex group morphs are read in one pass over the deform weights (bmesh, read only)
# for all of them, instead of one RNA walk over every vertex per morph; each slot still
# sums its groups in the mesh's own group order, so the values are unchanged.
def uv_offset_maps(mesh_obj, morphs):
    offsets = {leaf: {} for leaf in morphs}
    axes = {}
    for leaf, morph in morphs.items():
        if morph.data_type == "VERTEX_GROUP":
            scale = morph.vertex_group_scale
            for group in mesh_obj.vertex_groups:
                found = uv_group_axis(group.name, morph.name)
                if found is not None:
                    axes.setdefault(group.index, []).append((leaf, found[0], found[1], scale))
        else:
            slots = offsets[leaf]
            for item in morph.data:
                slot = slots.setdefault(item.index, [0.0, 0.0, 0.0, 0.0])
                for axis in range(4):
                    slot[axis] += item.offset[axis]
    if not axes:
        return offsets
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh_obj.data)
        layer = bm.verts.layers.deform.active
        if layer is None:
            return offsets
        wanted = set(axes)
        for vert in bm.verts:
            weights = vert[layer]
            if wanted.isdisjoint(weights.keys()):
                continue
            for group, weight in weights.items():
                entries = axes.get(group)
                if entries is None or weight <= 0:
                    continue
                for leaf, sign, axis, scale in entries:
                    slot = offsets[leaf].setdefault(vert.index, [0.0, 0.0, 0.0, 0.0])
                    slot[axis] += -weight * scale if sign == "-" else weight * scale
    finally:
        bm.free()
    return offsets


# True when mmd_tools already drives this morph with UV Warp modifiers, so the
# bridge leaves it alone.
def uv_natively_bound(mesh_obj, morph_name):
    for modifier in mesh_obj.modifiers:
        if (
            modifier.type == "UV_WARP"
            and modifier.name.startswith("mmd_bind")
            and uv_group_axis(modifier.vertex_group, morph_name)
        ):
            return True
    return False


# Removes the bridge's shader nodes and drivers and reconnects the base texture to
# its original vector input.
def strip_uv_nodes(material):
    tree = getattr(material, "node_tree", None)
    if tree is None:
        return
    nodes = tree.nodes
    for head in [n for n in nodes if n.name.startswith(UV_NODE_PREFIX + "_Add0")]:
        tex = nodes.get(head.get("mmd_bridge_tex", ""))
        if tex is not None and head.inputs[0].is_linked:
            tree.links.new(head.inputs[0].links[0].from_socket, tex.inputs["Vector"])
    for node in [n for n in nodes if n.name.startswith(UV_NODE_PREFIX)]:
        nodes.remove(node)
    anim = tree.animation_data
    if anim is not None:
        for fcurve in [fc for fc in anim.drivers if UV_NODE_PREFIX in fc.data_path]:
            anim.drivers.remove(fcurve)


# Removes the bridge's UV attributes and shader nodes from a mesh and its materials,
# and the record of them, so the next sync builds them again.
def clear_uv_channels(mesh_obj):
    mesh = mesh_obj.data
    for attribute in [
        a for a in mesh.attributes if a.name.startswith(UV_ATTRIBUTE_PREFIX)
    ]:
        mesh.attributes.remove(attribute)
    for material in mesh.materials:
        if material is not None:
            strip_uv_nodes(material)
    if UV_STATE_PROPERTY in mesh:
        del mesh[UV_STATE_PROPERTY]


# Drives a weight node from the MMDUV_ shape key value, so the normal morph
# animation moves the UV.
def add_uv_driver(tree, weight_node, key, block_name):
    path = 'nodes["%s"].outputs[0].default_value' % weight_node.name
    fcurve = tree.driver_add(path)
    driver = fcurve.driver
    driver.type = "SUM"
    variable = driver.variables.new()
    variable.name = "w"
    variable.type = "SINGLE_PROP"
    target = variable.targets[0]
    target.id_type = "KEY"
    target.id = key
    escaped = block_name.replace("\\", "\\\\").replace('"', '\\"')
    target.data_path = 'key_blocks["%s"].value' % escaped


# The material's main colour texture, skipping toon, sphere and our own nodes.
def find_base_texture(nodes):
    tex = nodes.get("mmd_base_tex")
    if tex is not None and tex.bl_idname == "ShaderNodeTexImage":
        return tex
    for node in nodes:
        if node.bl_idname != "ShaderNodeTexImage" or node.image is None:
            continue
        if node.name.startswith(("mmd_toon_tex", "mmd_sphere_tex", UV_NODE_PREFIX)):
            continue
        return node
    return None


# Chains attribute * weight additions in front of the base texture's vector input,
# which makes UV morphs (the > < eyes) work in shaders. Returns False when there is
# no base texture.
def build_uv_nodes(material, specs):
    tree = getattr(material, "node_tree", None)
    if tree is None:
        return False
    nodes, links = tree.nodes, tree.links
    tex = find_base_texture(nodes)
    if tex is None:
        return False
    vector = tex.inputs["Vector"]
    if vector.is_linked:
        previous = vector.links[0].from_socket
    else:
        coord = nodes.new("ShaderNodeTexCoord")
        coord.name = UV_NODE_PREFIX + "_Coord"
        previous = coord.outputs["UV"]
    for number, spec in enumerate(specs):
        attribute = nodes.new("ShaderNodeAttribute")
        attribute.name = "%s_Attr%d" % (UV_NODE_PREFIX, number)
        attribute.attribute_type = "GEOMETRY"
        attribute.attribute_name = spec["attribute"]
        weight = nodes.new("ShaderNodeValue")
        weight.name = "%s_Weight%d" % (UV_NODE_PREFIX, number)
        scale = nodes.new("ShaderNodeVectorMath")
        scale.name = "%s_Scale%d" % (UV_NODE_PREFIX, number)
        scale.operation = "SCALE"
        add = nodes.new("ShaderNodeVectorMath")
        add.name = "%s_Add%d" % (UV_NODE_PREFIX, number)
        add.operation = "ADD"
        if number == 0:
            add["mmd_bridge_tex"] = tex.name
        links.new(attribute.outputs["Vector"], scale.inputs[0])
        links.new(weight.outputs[0], scale.inputs[3])
        links.new(previous, add.inputs[0])
        links.new(scale.outputs[0], add.inputs[1])
        previous = add.outputs[0]
        add_uv_driver(tree, weight, spec["key"], spec["block"])
    links.new(previous, vector)
    return True


# (material index per face, first corner per face, vertex per corner) of a mesh, read
# once for every channel's material test. Material indices come from the attribute:
# polygons.foreach_get("material_index") goes through a per-face getter, 80 times
# slower on a 45k-face mesh. A mesh without the attribute has every face on slot 0.
def face_table(mesh):
    faces = len(mesh.polygons)
    material = numpy.zeros(faces, dtype=numpy.int32)
    start = numpy.zeros(faces, dtype=numpy.int32)
    corners = numpy.zeros(len(mesh.loops), dtype=numpy.int32)
    if faces:
        indices = mesh.attributes.get("material_index")
        if indices is not None and indices.domain == "FACE" and indices.data_type == "INT":
            indices.data.foreach_get("value", material)
        mesh.polygons.foreach_get("loop_start", start)
        mesh.loops.foreach_get("vertex_index", corners)
    return material, start, corners


# Material slots with faces on the given vertices; only those need shader nodes.
def touched_material_indices(table, vertex_ids, vertex_count):
    material, start, corners = table
    if not len(material) or not vertex_ids:
        return set()
    marked = numpy.zeros(vertex_count, dtype=bool)
    ids = numpy.fromiter(vertex_ids, dtype=numpy.int64)
    marked[ids[(ids >= 0) & (ids < vertex_count)]] = True
    hit = numpy.logical_or.reduceat(marked[corners], start)
    return {int(i) for i in numpy.unique(material[hit])}


# UV morphs actually animated, directly or through group morphs, in first-use order.
def uv_leaf_names(model, resolved, groups, uv_morphs):
    leaves = []
    for name, mkeys in zip(model.morph_names, model.morphs):
        if not any(abs(k.weight) > 1e-6 for k in mkeys):
            continue
        target = resolved.get(name)
        if target is None:
            continue
        if target in groups:
            for kind, leaf, factor in expand_group_morph(groups, target, 1.0):
                if kind == "uv_morphs" and abs(factor) > 1e-9 and leaf not in leaves:
                    leaves.append(leaf)
        elif target in uv_morphs and target not in leaves:
            leaves.append(target)
    return leaves


# What a model's UV morph setup is built from, cheap to read: the animated leaves and
# how each is stored, and per mesh its size, UV groups, mmd_tools bindings and materials.
# Vertex group weights are not read here (that is the slow part); editing them needs
# Force Resync, which always rebuilds.
def uv_signature(meshes, leaves, uv_morphs):
    parts = ["uv-1"]
    for leaf in leaves:
        morph = uv_morphs[leaf]
        entry = [leaf, morph.data_type, morph.uv_index, round(getattr(morph, "vertex_group_scale", 1.0), 7)]
        if morph.data_type != "VERTEX_GROUP":
            entry.append([(item.index, tuple(round(v, 7) for v in item.offset)) for item in morph.data])
        parts.append(entry)
    for mesh_obj in meshes:
        mesh = mesh_obj.data
        tree_info = []
        for material in mesh.materials:
            tree = getattr(material, "node_tree", None) if material is not None else None
            tex = find_base_texture(tree.nodes) if tree is not None else None
            tree_info.append((material.name if material else "", tex.name if tex else ""))
        parts.append(
            (
                mesh_obj.name,
                mesh.name,
                len(mesh.vertices),
                len(mesh.polygons),
                len(mesh.loops),
                [(g.index, g.name) for g in mesh_obj.vertex_groups if g.name.startswith("UV_")],
                [leaf for leaf in leaves if uv_natively_bound(mesh_obj, leaf)],
                tree_info,
            )
        )
    return hashlib.sha256(repr(parts).encode("utf-8", "replace")).hexdigest()[:24]


def read_uv_state(mesh_obj):
    try:
        return json.loads(mesh_obj.data.get(UV_STATE_PROPERTY, ""))
    except (TypeError, ValueError):
        return None


# True when a material still has the chain build_uv_nodes made with `count` links: the
# last Add node feeding the base texture, and one driver per weight node.
def uv_chain_intact(material, count):
    tree = getattr(material, "node_tree", None)
    if tree is None:
        return False
    head = tree.nodes.get("%s_Add0" % UV_NODE_PREFIX)
    last = tree.nodes.get("%s_Add%d" % (UV_NODE_PREFIX, count - 1))
    tex = tree.nodes.get(head.get("mmd_bridge_tex", "")) if head is not None else None
    if last is None or tex is None or not tex.inputs["Vector"].is_linked:
        return False
    if tex.inputs["Vector"].links[0].from_node != last:
        return False
    anim = tree.animation_data
    drivers = [fc for fc in anim.drivers if UV_NODE_PREFIX in fc.data_path] if anim else []
    return len(drivers) == count


# True when every mesh still holds what the last build with this signature made: its
# attributes and MMDUV_ keys, and the node chains and drivers in the patched materials.
def uv_setup_current(meshes, signature):
    materials = {}
    for mesh_obj in meshes:
        for material in mesh_obj.data.materials:
            if material is not None:
                materials[material.name] = material
    for mesh_obj in meshes:
        state = read_uv_state(mesh_obj)
        if not isinstance(state, dict) or state.get("signature") != signature:
            return False
        mesh = mesh_obj.data
        if any(mesh.attributes.get(name) is None for name in state.get("attributes", ())):
            return False
        blocks = mesh.shape_keys.key_blocks if mesh.shape_keys is not None else {}
        if any(blocks.get(name) is None for name in state.get("blocks", ())):
            return False
        for name, count in state.get("materials", {}).items():
            if not uv_chain_intact(materials.get(name), count):
                return False
    return True


# Rebuilds UV morph support for a model: clears old data, then for each animated UV
# morph writes the offsets to a mesh attribute, adds an MMDUV_ shape key to carry
# the weight and patches the materials' nodes. When nothing it is built from changed
# and the result is still in place, it is kept as it is: a rebuild costs the vertex
# pass and makes EEVEE recompile every patched material. rebuild forces it.
# Returns the channel specs of a build, an empty list when kept.
def prepare_uv_channels(root, meshes, model, resolved, enabled, rebuild=False):
    meshes = [m for m in meshes if m.type == "MESH" and m.mode != "EDIT"]
    uv_morphs = uv_morph_table(root) if enabled else {}
    leaves = uv_leaf_names(model, resolved, group_morph_table(root), uv_morphs) if uv_morphs else []
    signature = uv_signature(meshes, leaves, uv_morphs) if enabled else ""
    if enabled and not rebuild and meshes and uv_setup_current(meshes, signature):
        log.info("UV morph setup unchanged on %d mesh(es); kept as is", len(meshes))
        return []
    for mesh_obj in meshes:
        clear_uv_channels(mesh_obj)
    if not enabled:
        return []
    wanted = {}
    for leaf in leaves:
        morph = uv_morphs[leaf]
        if morph.uv_index != 0:
            log.warning(
                "UV morph '%s' targets additional UV %d; only the base UV is supported",
                leaf,
                morph.uv_index,
            )
            continue
        wanted[leaf] = morph
    maps = {}
    for mesh_obj in meshes:
        unbound = {
            leaf: morph for leaf, morph in wanted.items() if not uv_natively_bound(mesh_obj, leaf)
        }
        maps[mesh_obj.as_pointer()] = uv_offset_maps(mesh_obj, unbound) if unbound else {}
    channels = []
    for leaf in wanted:
        for mesh_obj in meshes:
            found = maps[mesh_obj.as_pointer()]
            if leaf not in found:
                log.info(
                    "UV morph '%s' on '%s' is bound by mmd_tools (UV Warp modifiers); leaving it to mmd_tools",
                    leaf,
                    mesh_obj.name,
                )
                continue
            usable = {
                i: (o[0], -o[1])
                for i, o in found[leaf].items()
                if abs(o[0]) > 1e-9 or abs(o[1]) > 1e-9
            }
            if not usable:
                log.warning(
                    "UV morph '%s' has no X/Y offsets on '%s'", leaf, mesh_obj.name
                )
                continue
            channels.append(
                {
                    "leaf": leaf,
                    "mesh": mesh_obj,
                    "offsets": usable,
                    "number": leaves.index(leaf),
                }
            )
    by_mesh = {}
    for channel in channels:
        mesh_obj = channel["mesh"]
        mesh = mesh_obj.data
        count = len(mesh.vertices)
        attribute = uv_attribute_name(channel["number"])
        flat = [0.0] * (3 * count)
        for index, (dx, dy) in channel["offsets"].items():
            if 0 <= index < count:
                flat[3 * index] = dx
                flat[3 * index + 1] = dy
        created = mesh.attributes.new(
            name=attribute, type="FLOAT_VECTOR", domain="POINT"
        )
        created.data.foreach_set("vector", flat)
        if mesh.shape_keys is None:
            mesh_obj.shape_key_add(name="Basis", from_mix=False)
        block_name = uv_key_name(channel["leaf"])
        block = mesh.shape_keys.key_blocks.get(block_name)
        if block is None:
            block = mesh_obj.shape_key_add(name=block_name, from_mix=False)
            block.value = 0.0
        block.slider_min = -UV_SLIDER_LIMIT
        block.slider_max = UV_SLIDER_LIMIT
        channel["attribute"] = attribute
        channel["block"] = block.name
        channel["key"] = mesh.shape_keys
        by_mesh.setdefault(mesh_obj.as_pointer(), []).append(channel)
        mesh.update()
    pending = {}
    for group in by_mesh.values():
        mesh = group[0]["mesh"].data
        table = face_table(mesh)
        indices = set()
        for channel in group:
            channel["materials"] = touched_material_indices(
                table, channel["offsets"].keys(), len(mesh.vertices)
            )
            indices.update(channel["materials"])
        for index in sorted(indices):
            if index >= len(mesh.materials) or mesh.materials[index] is None:
                continue
            material = mesh.materials[index]
            specs = pending.setdefault(material.as_pointer(), (material, {}))[1]
            for channel in group:
                if index in channel["materials"]:
                    specs.setdefault(channel["leaf"], channel)
    applied = 0
    patched = {}
    for material, specs in pending.values():
        ordered = [specs[leaf] for leaf in leaves if leaf in specs]
        if build_uv_nodes(material, ordered):
            applied += 1
            patched[material.name] = len(ordered)
            log.info(
                "UV morph shader nodes added to material '%s': %s",
                material.name,
                [c["leaf"] for c in ordered],
            )
        else:
            log.warning(
                "material '%s' has no base texture node; UV morph skipped for %s",
                material.name,
                [c["leaf"] for c in ordered],
            )
    for mesh_obj in meshes:
        own = by_mesh.get(mesh_obj.as_pointer(), [])
        mesh_obj.data[UV_STATE_PROPERTY] = json.dumps(
            {
                "signature": signature,
                "attributes": [c["attribute"] for c in own],
                "blocks": [c["block"] for c in own],
                "materials": patched,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
    log.info(
        "UV morph channels: %d on %d mesh(es), %d material(s) patched",
        len(channels),
        len(by_mesh),
        applied,
    )
    return channels
