from ..common.constants import (
    UV_ATTRIBUTE_PREFIX,
    UV_KEY_PREFIX,
    UV_NODE_PREFIX,
    UV_SLIDER_LIMIT,
)
from ..common.logger import log
from .morphs import expand_group_morph, group_morph_table, uv_morph_table


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


# Per-vertex UV offsets of a morph, read from its stored offsets or from vertex
# group weights depending on how mmd_tools stored it.
def uv_offset_map(mesh_obj, morph):
    offsets = {}
    if morph.data_type == "VERTEX_GROUP":
        scale = morph.vertex_group_scale
        axes = {}
        for group in mesh_obj.vertex_groups:
            found = uv_group_axis(group.name, morph.name)
            if found is not None:
                axes[group.index] = found
        if axes:
            for vertex in mesh_obj.data.vertices:
                for member in vertex.groups:
                    entry = axes.get(member.group)
                    if entry is None or member.weight <= 0:
                        continue
                    sign, axis = entry
                    slot = offsets.setdefault(vertex.index, [0.0, 0.0, 0.0, 0.0])
                    slot[axis] += (
                        -member.weight * scale if sign == "-" else member.weight * scale
                    )
    else:
        for item in morph.data:
            slot = offsets.setdefault(item.index, [0.0, 0.0, 0.0, 0.0])
            for axis in range(4):
                slot[axis] += item.offset[axis]
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


# Removes the bridge's UV attributes and shader nodes from a mesh and its materials.
def clear_uv_channels(mesh_obj):
    mesh = mesh_obj.data
    for attribute in [
        a for a in mesh.attributes if a.name.startswith(UV_ATTRIBUTE_PREFIX)
    ]:
        mesh.attributes.remove(attribute)
    for material in mesh.materials:
        if material is not None:
            strip_uv_nodes(material)


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


# Material slots with faces on the given vertices; only those need shader nodes.
def touched_material_indices(mesh, vertex_ids):
    found = set()
    for polygon in mesh.polygons:
        if polygon.material_index in found:
            continue
        for vertex in polygon.vertices:
            if vertex in vertex_ids:
                found.add(polygon.material_index)
                break
    return found


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


# Rebuilds UV morph support for a model: clears old data, then for each animated UV
# morph writes the offsets to a mesh attribute, adds an MMDUV_ shape key to carry
# the weight and patches the materials' nodes. Returns the channel specs.
def prepare_uv_channels(root, meshes, model, resolved, enabled):
    for mesh_obj in meshes:
        if mesh_obj.type == "MESH" and mesh_obj.mode != "EDIT":
            clear_uv_channels(mesh_obj)
    if not enabled:
        return []
    uv_morphs = uv_morph_table(root)
    if not uv_morphs:
        return []
    leaves = uv_leaf_names(model, resolved, group_morph_table(root), uv_morphs)
    channels = []
    for leaf in leaves:
        morph = uv_morphs[leaf]
        if morph.uv_index != 0:
            log.warning(
                "UV morph '%s' targets additional UV %d; only the base UV is supported",
                leaf,
                morph.uv_index,
            )
            continue
        for mesh_obj in meshes:
            if mesh_obj.type != "MESH" or mesh_obj.mode == "EDIT":
                continue
            if uv_natively_bound(mesh_obj, leaf):
                log.info(
                    "UV morph '%s' on '%s' is bound by mmd_tools (UV Warp modifiers); leaving it to mmd_tools",
                    leaf,
                    mesh_obj.name,
                )
                continue
            offsets = uv_offset_map(mesh_obj, morph)
            usable = {
                i: (o[0], -o[1])
                for i, o in offsets.items()
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
        indices = set()
        for channel in group:
            channel["materials"] = touched_material_indices(
                mesh, set(channel["offsets"])
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
    for material, specs in pending.values():
        ordered = [specs[leaf] for leaf in leaves if leaf in specs]
        if build_uv_nodes(material, ordered):
            applied += 1
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
    log.info(
        "UV morph channels: %d on %d mesh(es), %d material(s) patched",
        len(channels),
        len(by_mesh),
        applied,
    )
    return channels
