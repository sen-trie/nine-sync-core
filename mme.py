import bpy
import hashlib
import json
import math
import numpy as np
import os
import tempfile
import time
import traceback

from .common import (
    ACCESSORY_COLLECTION,
    ACCESSORY_UNIT,
    LIGHT_COLLECTION,
    MME_ATTR_CULL,
    MME_ATTR_INST,
    MME_ATTR_NRM,
    MME_ATTR_POS,
    MME_ATTR_SRC,
    MME_ATTR_UV,
    MME_BACKEND_PROP,
    MME_BAKED_PROP,
    MME_CONTROL_PREFIX,
    MME_CORE_FILE,
    MME_CORE_VERSION,
    MME_DRAW_PROP,
    MME_DRIVEN_PROP,
    MME_GN_MODIFIER,
    MME_GROUP_PROP,
    MME_LOOP_PREFIX,
    MME_OWNER_PROP,
    MME_PARAMS_PROP,
    MME_PARAM_BUILT_PROP,
    MME_PARAM_PREFIX,
    MME_PRUNED_PROP,
    MME_PRUNE_TR,
    MME_SELF_NAMES,
    MME_SWAP,
    MME_SWAP3,
    MME_UNIFORMS_PROP,
    MME_VARYINGS_PROP,
    MME_VARYING_PREFIX,
    PMM_FPS,
    _mme_core,
    _mme_runtimes,
    _mme_state,
    accessory_key,
    child_of_type,
    find_accessory_root,
    find_armature,
    find_rig_root,
    iter_fcurves,
    log,
    mme_effect_roots,
    mme_load_controls,
    mme_param_meta,
    model_key,
    pose_bone_name,
    redraw_all,
    scene_fps,
    short_name,
    short_text,
)
from .xfile import (
    XMaterial,
    parse_x_bytes,
    resolve_x_texture,
    x_effect_geometry,
)
from .fx import (
    FX_FAR,
    FX_NEAR,
    FxError,
    FxExec,
    FxNumpyBackend,
    FxNumpyEnv,
    FxSceneState,
    FxTextureStore,
    FxUnsupported,
    FxValue,
    fx_classify_global,
    fx_clip_to_world,
    fx_entry,
    fx_find_file,
    fx_is_numeric,
    fx_lh_look,
    fx_lh_ortho,
    fx_lh_perspective,
    fx_parse_file,
    fx_plan_draws,
    fx_read_text,
    fx_resolve_type,
    fx_run_entry,
    fx_script_values,
    fx_structural_params,
    fx_vertex_inputs,
    fx_vs_globals,
)
from .mme_nodes import (
    mme_add_driver,
    mme_bake_only_tree,
    mme_build_geo_tree,
    mme_compile_material,
    mme_fallback_material,
    mme_load_pixels,
    mme_prop_name,
    mme_save_core_library,
    mme_set_drivers,
    mme_varying_name,
)


def remove_accessory_children(root):
    for child in list(root.children):
        remove_accessory_children_single(child)


def mme_pass_groups(obj):
    groups = []
    for modifier in getattr(obj, "modifiers", []):
        if modifier.type != "NODES" or modifier.node_group is None:
            continue
        top = modifier.node_group
        if top.get(MME_OWNER_PROP) is None:
            continue
        groups.append(top)
        for node in top.nodes:
            if (
                node.bl_idname == "GeometryNodeGroup"
                and node.node_tree is not None
                and node.node_tree.get(MME_OWNER_PROP) is not None
            ):
                groups.append(node.node_tree)
    return groups


def mme_free_groups(groups):
    for group in groups:
        try:
            if group.users == 0:
                bpy.data.node_groups.remove(group)
        except ReferenceError:
            pass


def mme_purge_owned(prefix):
    removed = 0
    for material in [
        m
        for m in bpy.data.materials
        if str(m.get(MME_OWNER_PROP, "")).startswith(prefix)
    ]:
        if material.users:
            continue
        group = bpy.data.node_groups.get(material.get(MME_GROUP_PROP, ""))
        bpy.data.materials.remove(material)
        if group is not None and group.users == 0:
            bpy.data.node_groups.remove(group)
        removed += 1
    for group in [
        g
        for g in bpy.data.node_groups
        if str(g.get(MME_OWNER_PROP, "")).startswith(prefix)
    ]:
        if group.users == 0:
            bpy.data.node_groups.remove(group)
    return removed


def mme_geometry_cache_file(x_path):
    st = os.stat(x_path)
    text = "%s|%d|%d|%d" % (
        os.path.abspath(x_path),
        st.st_size,
        int(st.st_mtime * 1000),
        MME_CORE_VERSION,
    )
    folder = os.path.join(tempfile.gettempdir(), "ninesync_cache")
    return folder, os.path.join(
        folder, hashlib.md5(text.encode("utf-8")).hexdigest() + ".npz"
    )


def mme_x_effect_geometry(x_path):
    folder, cache = mme_geometry_cache_file(x_path)
    if os.path.isfile(cache):
        try:
            with np.load(cache, allow_pickle=False) as data:
                sizes = data["face_sizes"]
                flat = data["face_flat"].tolist()
                faces = []
                at = 0
                for size in sizes.tolist():
                    faces.append(tuple(flat[at : at + size]))
                    at += size
                materials = [XMaterial(**m) for m in json.loads(str(data["materials"]))]
                return "txt", (
                    data["positions"].copy(),
                    data["normals"].copy(),
                    data["uvs"].copy(),
                    faces,
                    data["face_subsets"].copy(),
                    materials,
                )
        except (OSError, ValueError, KeyError, TypeError) as error:
            log.debug("geometry cache %s unreadable: %s", cache, error)
    with open(x_path, "rb") as handle:
        xscene = parse_x_bytes(handle.read())
    if xscene.mode != "txt":
        return xscene.mode, None
    geometry = x_effect_geometry(xscene)
    positions, normals, uvs, faces, face_subsets, materials = geometry
    try:
        os.makedirs(folder, exist_ok=True)
        np.savez(
            cache,
            positions=positions,
            normals=normals,
            uvs=uvs,
            face_sizes=np.array([len(f) for f in faces], dtype=np.int64),
            face_flat=np.array([v for f in faces for v in f], dtype=np.int64),
            face_subsets=np.asarray(face_subsets, dtype=np.int64),
            materials=np.array(json.dumps([dict(m._asdict()) for m in materials])),
        )
    except OSError as error:
        log.debug("geometry cache %s not written: %s", cache, error)
    return "txt", geometry


class MmePassData:
    __slots__ = (
        "draw",
        "src",
        "inst",
        "loop_values",
        "positions",
        "normals",
        "uvs",
        "count",
        "stamp",
    )

    def __init__(self, draw, src, inst, loop_values, runtime):
        self.draw = draw
        self.src = src
        self.inst = inst
        self.loop_values = loop_values
        self.positions = runtime.positions[src]
        self.normals = runtime.normals[src]
        self.uvs = runtime.uvs[src]
        self.count = len(src)
        self.stamp = None


class MmeRuntime:
    def __init__(self, fx_path, x_path, values=None):
        self.fx_path = fx_path
        self.x_path = x_path
        self.x_folder = os.path.dirname(x_path)
        self.program = fx_parse_file(fx_path)
        mode, geometry = mme_x_effect_geometry(x_path)
        if mode != "txt":
            raise FxUnsupported("binary .x files cannot drive effects")
        (
            self.positions,
            self.normals,
            self.uvs,
            self.faces,
            self.face_subsets,
            self.materials,
        ) = geometry
        self.textures = FxTextureStore(self.program, mme_load_pixels)
        self.values = dict(values or {})
        self.draws = fx_plan_draws(
            self.program, max(1, len(self.materials)), self.values
        )
        self.passes = {}
        self.controls_runtime = set()
        self.stamp = mme_effect_stamp(fx_path, x_path, self.program)

    def loop_key(self):
        names = set(fx_script_values(self.program, "loopbycount"))
        return json.dumps({k: v for k, v in sorted(self.values.items()) if k in names})

    def draw_by_order(self, order):
        for draw in self.draws:
            if draw.order == order:
                return draw
        return None

    def loop_overrides_nodes(self, draw, B):
        out = {}
        for var, _count, _name in draw.loops:
            if var:
                sock = B.attribute(MME_LOOP_PREFIX + var)
                B.tag(sock, "LoopGetIndex %s" % var)
                out[var] = FxValue(("i", ()), [sock])
        return out

    def material_state(self, state, subset):
        xmat = self.materials[subset] if subset < len(self.materials) else None
        if xmat is None:
            return
        texture = ""
        if xmat.texture:
            first = xmat.texture.split("*")[0].strip()
            if first and not first.lower().endswith((".sph", ".spa")):
                texture = resolve_x_texture(first, self.x_folder) or ""
        diffuse = tuple(float(v) for v in xmat.diffuse)
        state.material = {
            "diffuse": diffuse,
            "ambient": diffuse[:3],
            "specular": tuple(float(v) for v in xmat.specular),
            "power": float(xmat.power),
            "emissive": tuple(float(v) for v in xmat.emissive),
            "texture": texture,
        }


def mme_effect_stamp(fx_path, x_path, program=None):
    files = list(program.files) if program is not None else [fx_path]
    parts = [x_path, str(os.path.getmtime(x_path))]
    for path in files:
        try:
            parts.append("%s=%s" % (os.path.basename(path), os.path.getmtime(path)))
        except OSError:
            parts.append(path)
    return "|".join(parts)


def mme_runtime_for(root):
    fx_path = root.get("mme_fx", "")
    x_path = root.get("mme_x", "")
    if not fx_path or not x_path:
        return None
    key = root.name
    runtime = _mme_runtimes.get(key)
    if runtime is not None and runtime.fx_path == fx_path and runtime.x_path == x_path:
        return runtime
    if not (os.path.isfile(fx_path) and os.path.isfile(x_path)):
        return None
    runtime = MmeRuntime(fx_path, x_path, mme_built_values(root))
    controls = mme_load_controls(root)
    runtime.controls_runtime = {
        name for name, desc in controls.items() if desc.get("kind") != "const"
    }
    _mme_runtimes[key] = runtime
    return runtime


def mme_np_matrix(matrix):
    return np.array([list(row) for row in matrix], dtype=np.float64)


def mme_world_row(blender_world, unit):
    inv_unit = np.diag([1.0 / unit, 1.0 / unit, 1.0 / unit, 1.0])
    col = inv_unit @ MME_SWAP @ blender_world @ MME_SWAP
    return col.T


def mme_camera_state(state, scene, depsgraph, unit):
    camera = scene.camera
    if camera is None:
        return
    evaluated = camera.evaluated_get(depsgraph) if depsgraph is not None else camera
    world = mme_np_matrix(evaluated.matrix_world)
    eye = MME_SWAP3 @ world[:3, 3] / unit
    forward = MME_SWAP3 @ (-world[:3, 2])
    up = MME_SWAP3 @ world[:3, 1]
    state.view = fx_lh_look(eye, forward, up)
    state.camera_pos = eye
    state.camera_dir = forward / (np.linalg.norm(forward) or 1.0)
    render = scene.render
    width = render.resolution_x * render.pixel_aspect_x
    height = render.resolution_y * render.pixel_aspect_y
    aspect = width / height if height else 1.0
    data = evaluated.data
    if getattr(data, "type", "PERSP") == "ORTHO":
        scale = data.ortho_scale / unit
        if aspect >= 1.0:
            state.proj = fx_lh_ortho(scale, scale / aspect, FX_NEAR, FX_FAR)
        else:
            state.proj = fx_lh_ortho(scale * aspect, scale, FX_NEAR, FX_FAR)
        state.ortho = True
        return
    fit = getattr(data, "sensor_fit", "AUTO")
    angle = data.angle
    if fit == "VERTICAL":
        fov_y = data.angle_y
    elif fit == "HORIZONTAL" or aspect >= 1.0:
        fov_x = data.angle_x if fit == "HORIZONTAL" else angle
        fov_y = 2.0 * math.atan(math.tan(fov_x * 0.5) / aspect)
    else:
        fov_y = angle
    state.proj = fx_lh_perspective(max(fov_y, 1e-4), aspect, FX_NEAR, FX_FAR)


def mme_light_state(state, depsgraph):
    collection = bpy.data.collections.get(LIGHT_COLLECTION)
    root = find_rig_root(collection, "LIGHT") if collection else None
    light = child_of_type(root, "LIGHT") if root else None
    if light is None:
        return
    evaluated = light.evaluated_get(depsgraph) if depsgraph is not None else light
    loc = evaluated.location
    direction = np.array([-loc.x, -loc.z, -loc.y], dtype=np.float64)
    if np.linalg.norm(direction) > 1e-9:
        state.light_dir = direction
    state.light_color = np.array(list(evaluated.data.color), dtype=np.float64)


def mme_eval_prop(obj, depsgraph, name, default=0.0):
    source = obj.evaluated_get(depsgraph) if depsgraph is not None else obj
    try:
        return float(source.get(name, obj.get(name, default)))
    except (TypeError, ValueError):
        return default


def mme_object_items(obj, depsgraph, unit, item, t):
    evaluated = obj.evaluated_get(depsgraph) if depsgraph is not None else obj
    world = mme_np_matrix(evaluated.matrix_world)
    pos = MME_SWAP3 @ world[:3, 3] / unit
    euler = evaluated.rotation_euler
    rotation = (-euler.x, -euler.z, -euler.y)
    lower = item.lower()
    if lower == "x":
        return [pos[0]]
    if lower == "y":
        return [pos[1]]
    if lower == "z":
        return [pos[2]]
    if lower == "xyz":
        return list(pos)
    if lower == "rx":
        return [rotation[0]]
    if lower == "ry":
        return [rotation[1]]
    if lower == "rz":
        return [rotation[2]]
    if lower == "rxyz":
        return list(rotation)
    if lower == "si":
        return [mme_eval_prop(obj, depsgraph, "mme_si", 1.0) * ACCESSORY_UNIT]
    if lower == "tr":
        return [mme_eval_prop(obj, depsgraph, "mme_tr", 1.0)]
    if lower:
        return [0.0]
    if fx_is_numeric(t) and len(t[1]) == 2:
        child_scale = np.diag([ACCESSORY_UNIT * unit] * 3 + [1.0])
        return list(mme_world_row(world @ child_scale, unit).reshape(-1))
    if fx_is_numeric(t) and t[0] == "b":
        return [1.0 if obj.visible_get() or not obj.hide_render else 0.0]
    return list(pos) + [1.0]


def mme_control_values(desc, root, depsgraph, unit, t):
    kind = desc.get("kind")
    if kind == "const":
        return list(desc.get("values", [0.0]))
    if kind == "self":
        return mme_object_items(root, depsgraph, unit, desc.get("item", ""), t)
    if kind == "prop":
        return [mme_eval_prop(root, depsgraph, desc.get("prop", ""), 0.0)]
    if kind == "object":
        other = bpy.data.objects.get(desc.get("object", ""))
        if other is None:
            return [0.0]
        return mme_object_items(other, depsgraph, unit, desc.get("item", ""), t)
    if kind == "bone":
        armature = bpy.data.objects.get(desc.get("object", ""))
        if armature is None or armature.type != "ARMATURE":
            return [0.0]
        evaluated = (
            armature.evaluated_get(depsgraph) if depsgraph is not None else armature
        )
        bone = evaluated.pose.bones.get(desc.get("bone", ""))
        if bone is None:
            return [0.0]
        world = mme_np_matrix(evaluated.matrix_world) @ mme_np_matrix(bone.matrix)
        if fx_is_numeric(t) and len(t[1]) == 2:
            return list(mme_world_row(world, unit).reshape(-1))
        pos = MME_SWAP3 @ world[:3, 3] / unit
        return list(pos) + [1.0]
    if kind == "model":
        if fx_is_numeric(t) and t[0] == "b":
            return [1.0]
        target = bpy.data.objects.get(desc.get("object", ""))
        if target is None:
            return [0.0]
        return mme_object_items(target, depsgraph, unit, "", t)
    return [0.0]


def mme_frame_state(scene, depsgraph, root, runtime, unit):
    state = FxSceneState()
    state.ortho = False
    render = scene.render
    pct = render.resolution_percentage / 100.0
    state.viewport = (render.resolution_x * pct, render.resolution_y * pct)
    mme_camera_state(state, scene, depsgraph, unit)
    mme_light_state(state, depsgraph)
    fps = scene_fps(scene) or PMM_FPS
    state.time = scene.frame_current / fps
    state.elapsed = 1.0 / fps
    controls = mme_load_controls(root)
    program = runtime.program
    for name, desc in controls.items():
        g = program.globals.get(name)
        if g is None:
            continue
        t = fx_resolve_type(g.type, program)
        try:
            state.controls[name] = [
                float(v) for v in mme_control_values(desc, root, depsgraph, unit, t)
            ]
        except Exception as error:
            log.debug("control %s failed: %s", name, error)
            state.controls[name] = [0.0]
    state.params = mme_param_values(root, program, depsgraph)
    return state


def mme_param_values(root, program, depsgraph=None):
    params = getattr(program, "params", {})
    source = (
        root.evaluated_get(depsgraph)
        if (root is not None and depsgraph is not None)
        else root
    )
    out = {}
    for name, info in params.items():
        prop = mme_prop_name(MME_PARAM_PREFIX, name)
        value = None
        if source is not None:
            value = source.get(prop, root.get(prop) if root is not None else None)
        if value is None:
            out[name] = list(info["default"])
        elif hasattr(value, "__len__"):
            out[name] = [float(v) for v in value]
        else:
            out[name] = [float(value)]
    return out


def mme_built_values(root):
    try:
        return json.loads(root.get(MME_PARAM_BUILT_PROP, "{}"))
    except (TypeError, ValueError):
        return {}


def mme_param_signature(root):
    try:
        names = json.loads(root.get("mme_structural", "[]"))
    except (TypeError, ValueError):
        names = []
    meta = mme_param_meta(root)
    out = {}
    for name in names:
        info = meta.get(name)
        if info is None:
            continue
        value = root.get(info["prop"])
        if value is None:
            out[name] = list(info["default"])
        elif hasattr(value, "__len__"):
            out[name] = [float(v) for v in value]
        else:
            out[name] = [float(value)]
    return out


def mme_sync_params(root, program):
    params = getattr(program, "params", {})
    meta = {}
    for name, info in params.items():
        prop = mme_prop_name(MME_PARAM_PREFIX, name)
        info = dict(info)
        info["prop"] = prop
        meta[name] = info
        size = info["size"]
        kind = info["type"]
        default = info["default"]
        if kind == "i":
            cast = int
        elif kind == "b":
            cast = bool
        else:
            cast = float
        typed = [cast(v) for v in default]
        current = root.get(prop)
        fits = current is not None and (
            (
                size == 1
                and not hasattr(current, "__len__")
                and type(current) is type(typed[0])
            )
            or (size > 1 and hasattr(current, "__len__") and len(current) == size)
        )
        if not fits:
            root[prop] = typed[0] if size == 1 else typed
        ui = root.id_properties_ui(prop)
        options = {"description": info["help"] or "MME effect parameter %s" % name}
        if kind != "b":
            options["default"] = typed[0] if size == 1 else typed
            if "min" in info:
                options["soft_min"] = cast(info["min"])
            if "max" in info:
                options["soft_max"] = cast(info["max"])
            if info.get("color"):
                options["subtype"] = "COLOR_GAMMA"
                options.setdefault("soft_min", 0.0)
        ui.update(**options)
    wanted = {m["prop"] for m in meta.values()}
    for key in [
        k for k in root.keys() if k.startswith(MME_PARAM_PREFIX) and k not in wanted
    ]:
        del root[key]
    root[MME_PARAMS_PROP] = json.dumps(meta)
    return meta


def mme_reset_params(root):
    for name, info in mme_param_meta(root).items():
        prop = info.get("prop") or mme_prop_name(MME_PARAM_PREFIX, name)
        cast = {"i": int, "b": bool}.get(info["type"], float)
        typed = [cast(v) for v in info["default"]]
        if prop in root and info["size"] > 1:
            for k, value in enumerate(typed):
                root[prop][k] = value
        else:
            root[prop] = typed[0] if info["size"] == 1 else typed


def mme_self_tr_globals(runtime, root):
    found = []
    for name, desc in mme_load_controls(root).items():
        if desc.get("kind") == "self" and desc.get("item", "").lower() == "tr":
            found.append(name)
    return found


def mme_loop_values(draw, inst, density):
    values = {}
    counts = [mme_loop_count(count, density) for _var, count, _name in draw.loops]
    remaining = inst.copy()
    for (var, _count, _name), count in reversed(list(zip(draw.loops, counts))):
        if var:
            values[var] = (remaining % count).astype(np.float64)
        remaining = remaining // count
    return values


def mme_loop_count(count, density):
    return max(1, int(round(count * max(0.0, min(1.0, density)))))


def mme_run_vs(runtime, data, state):
    program = runtime.program
    function, args = fx_entry(program, data.draw.fx_pass.vs)
    if function is None:
        raise FxUnsupported("pass %s has no vertex shader" % data.draw.fx_pass.name)
    runtime.material_state(state, data.draw.subset)
    ex = FxExec(program, FxNumpyBackend(), FxNumpyEnv(program, state, runtime.textures))
    for var, values in data.loop_values.items():
        ex.overrides[var] = FxValue(("i", ()), [values])
    provide = fx_vertex_inputs(data.positions, data.normals, data.uvs, data.src)
    with np.errstate(all="ignore"):
        outputs = fx_run_entry(ex, function, args, provide(ex))
    return outputs, ex


def mme_clip_world(outputs, state, count):
    position = outputs.get("POSITION0")
    if position is None:
        return np.zeros((count, 3)), np.ones(count, dtype=bool)
    comps = [
        np.broadcast_to(np.asarray(c, dtype=np.float64), (count,))
        for c in (list(position.c) + [np.float64(1.0)] * 4)[:4]
    ]
    view_proj = state.view @ state.proj
    try:
        inverse = np.linalg.inv(view_proj)
    except np.linalg.LinAlgError:
        inverse = np.identity(4)
    if getattr(state, "ortho", False):
        homo = np.stack(comps, axis=-1)
        world = homo @ inverse
        w = np.where(np.abs(world[:, 3:4]) < 1e-20, 1.0, world[:, 3:4])
        culled = (np.abs(comps[3]) < 1e-12) | (comps[2] < 0) | (comps[2] > comps[3])
        return world[:, :3] / w, culled
    with np.errstate(all="ignore"):
        return fx_clip_to_world(comps, inverse, state.proj)


def mme_prune_state(state):
    probe = FxSceneState()
    probe.__dict__.update(state.__dict__)
    probe.controls = dict(state.controls)
    eye = np.array([0.0, 0.0, -1.0e5])
    probe.view = fx_lh_look(eye, (0.0, 0.0, 1.0), (0.0, 1.0, 0.0))
    probe.proj = fx_lh_perspective(math.radians(30.0), 1.0, FX_NEAR, FX_FAR)
    probe.camera_pos = eye
    probe.camera_dir = np.array([0.0, 0.0, 1.0])
    probe.ortho = False
    return probe


def mme_prune_mask(runtime, data, state, tr_names):
    if not tr_names:
        samples = [None]
    else:
        samples = MME_PRUNE_TR
    state = mme_prune_state(state)
    keep = np.zeros(data.count, dtype=bool)
    for value in samples:
        if value is not None:
            for name in tr_names:
                state.controls[name] = [value]
        outputs, _ex = mme_run_vs(runtime, data, state)
        _world, culled = mme_clip_world(outputs, state, data.count)
        keep |= ~culled
    return keep


def mme_build_mesh(name, positions, faces):
    mesh = bpy.data.meshes.new(name)
    count = len(positions)
    mesh.vertices.add(count)
    mesh.vertices.foreach_set("co", positions.astype(np.float32).reshape(-1))
    sizes = np.array([len(f) for f in faces], dtype=np.int64)
    flat = (
        np.concatenate([np.asarray(f, dtype=np.int64) for f in faces])
        if faces
        else np.zeros(0, dtype=np.int64)
    )
    starts = np.zeros(len(faces), dtype=np.int64)
    if len(faces):
        starts[1:] = np.cumsum(sizes)[:-1]
    mesh.loops.add(len(flat))
    mesh.loops.foreach_set("vertex_index", flat.astype(np.int32))
    mesh.polygons.add(len(faces))
    mesh.polygons.foreach_set("loop_start", starts.astype(np.int32))
    mesh.update(calc_edges=True)
    return mesh


def mme_set_attribute(mesh, name, values, kind="FLOAT"):
    attribute = mesh.attributes.get(name)
    if attribute is None or attribute.data_type != kind or attribute.domain != "POINT":
        if attribute is not None:
            mesh.attributes.remove(attribute)
        attribute = mesh.attributes.new(name=name, type=kind, domain="POINT")
    if kind == "INT":
        attribute.data.foreach_set("value", np.asarray(values, dtype=np.int32))
    elif kind == "FLOAT_VECTOR":
        attribute.data.foreach_set(
            "vector", np.asarray(values, dtype=np.float32).reshape(-1)
        )
    else:
        attribute.data.foreach_set("value", np.asarray(values, dtype=np.float32))


def mme_read_attribute(mesh, name, kind="INT"):
    attribute = mesh.attributes.get(name)
    if attribute is None:
        return None
    out = np.empty(len(attribute.data), dtype=np.int32 if kind == "INT" else np.float32)
    attribute.data.foreach_get("value", out)
    return out


def mme_child_matrix(root, unit):
    return mme_np_matrix(root.matrix_world) @ np.diag(
        [ACCESSORY_UNIT * unit] * 3 + [1.0]
    )


def mme_pass_geometry(runtime, draw, density):
    subset_faces = [
        f for f, s in zip(runtime.faces, runtime.face_subsets) if s == draw.subset
    ]
    if not subset_faces:
        return None
    used = np.unique(
        np.concatenate([np.asarray(f, dtype=np.int64) for f in subset_faces])
    )
    remap = {int(v): i for i, v in enumerate(used)}
    local_faces = [tuple(remap[int(v)] for v in reversed(f)) for f in subset_faces]
    counts = [mme_loop_count(count, density) for _var, count, _name in draw.loops]
    instances = int(np.prod(counts)) if counts else 1
    return used, local_faces, counts, instances


def mme_keep_faces(local_faces, keep, offset=0):
    return [f for f in local_faces if any(keep[v + offset] for v in f)]


def mme_compact(src, faces):
    alive = np.zeros(len(src), dtype=bool)
    for face in faces:
        alive[list(face)] = True
    new_index = -np.ones(len(src), dtype=np.int64)
    new_index[alive] = np.arange(int(alive.sum()))
    return alive, [tuple(int(new_index[v]) for v in face) for face in faces]


def mme_pass_object(label, mesh, collection, root):
    obj = bpy.data.objects.new(label, mesh)
    collection.objects.link(obj)
    obj.parent = root
    obj.visible_shadow = False
    for name in (
        "visible_diffuse",
        "visible_glossy",
        "visible_transmission",
        "visible_volume_scatter",
    ):
        setattr(obj, name, False)
    return obj


def mme_uniform_driver(program, gname, index, root, controls, scene):
    g = program.globals.get(gname)
    if g is None:
        return None
    source = fx_classify_global(g, program)
    if source is None:
        return None
    kind = source[0]

    def scene_var(name, path):
        return {
            "name": name,
            "type": "SINGLE_PROP",
            "id_type": "SCENE",
            "id": scene.name,
            "path": path,
        }

    def prop_var(obj, path):
        return {
            "name": "var",
            "type": "SINGLE_PROP",
            "id_type": "OBJECT",
            "id": obj.name,
            "path": path,
        }

    if kind == "time":
        return {
            "expr": "frame * base / fps",
            "vars": [
                scene_var("fps", "render.fps"),
                scene_var("base", "render.fps_base"),
            ],
        }
    if kind == "elapsed":
        return {
            "expr": "base / fps",
            "vars": [
                scene_var("fps", "render.fps"),
                scene_var("base", "render.fps_base"),
            ],
        }
    if kind == "viewport":
        axis = "render.resolution_x" if index == 0 else "render.resolution_y"
        return {
            "expr": "res * pct / 100",
            "vars": [
                scene_var("res", axis),
                scene_var("pct", "render.resolution_percentage"),
            ],
        }
    if kind == "param":
        meta = mme_param_meta(root).get(gname)
        if meta is None:
            return None
        path = '["%s"]' % meta["prop"]
        if meta["size"] > 1:
            path = "%s[%d]" % (path, index)
        return {"expr": "var", "vars": [prop_var(root, path)]}
    if kind == "control":
        desc = controls.get(gname, {})
        if desc.get("kind") == "prop":
            return {
                "expr": "var",
                "vars": [prop_var(root, '["%s"]' % desc.get("prop", ""))],
            }
        if desc.get("kind") in ("self", "object"):
            obj = (
                root
                if desc.get("kind") == "self"
                else bpy.data.objects.get(desc.get("object", ""))
            )
            item = desc.get("item", "").lower()
            if obj is None:
                return None
            if item == "tr":
                return {"expr": "var", "vars": [prop_var(obj, '["mme_tr"]')]}
            if item == "si":
                return {
                    "expr": "var * %r" % ACCESSORY_UNIT,
                    "vars": [prop_var(obj, '["mme_si"]')],
                }
    return None


def mme_drive_uniforms(obj, program, uniforms, root, scene):
    controls = mme_load_controls(root)
    driven = []
    for prop, gname, index in uniforms:
        spec = mme_uniform_driver(program, gname, index, root, controls, scene)
        if spec is None:
            continue
        mme_add_driver(obj, '["%s"]' % prop, spec)
        driven.append(prop)
    obj[MME_DRIVEN_PROP] = json.dumps(driven)


def mme_pass_material(runtime, draw, state, label, owner, vs_outputs):
    runtime.material_state(state, draw.subset)
    try:
        mat, varyings, uniforms, mode, loop_attrs, node_count = mme_compile_material(
            label, runtime, draw, state, vs_outputs, owner
        )
        return (
            mat,
            varyings,
            uniforms,
            loop_attrs,
            "%s, %d shader nodes" % (mode.lower(), node_count),
        )
    except FxError as error:
        log.warning(
            "effect pass %s: pixel shader not converted (%s); using a plain material",
            label,
            error,
        )
        mat = mme_fallback_material(
            label, runtime, draw.subset, reason=str(error), owner=owner
        )
        varyings = [mme_varying_name("TEXCOORD0", 0), mme_varying_name("TEXCOORD0", 1)]
        return mat, varyings, [], [], "fallback material"


def mme_finish_pass(
    obj,
    runtime,
    draw,
    root,
    scene,
    counts,
    density,
    mat,
    varyings,
    uniforms,
    backend,
    pruned,
):
    obj.data.materials.clear()
    obj.data.materials.append(mat)
    obj[MME_DRAW_PROP] = json.dumps(
        {
            "order": draw.order,
            "technique": draw.technique.name,
            "pass": draw.fx_pass.name,
            "subset": draw.subset,
            "loops": [
                [var, count] for var, count in zip([l[0] for l in draw.loops], counts)
            ],
            "density": density,
        }
    )
    obj[MME_VARYINGS_PROP] = json.dumps(varyings)
    obj[MME_UNIFORMS_PROP] = json.dumps(uniforms)
    obj[MME_BACKEND_PROP] = backend
    obj[MME_PRUNED_PROP] = bool(pruned)
    params = getattr(runtime.program, "params", {})
    used = fx_vs_globals(runtime.program, [draw.fx_pass]) | {
        name for _var, _count, name in draw.loops
    }
    obj["mme_vs_params"] = json.dumps(sorted(name for name in params if name in used))
    for prop, gname, index in uniforms:
        obj[prop] = 0.0
    mme_drive_uniforms(obj, runtime.program, uniforms, root, scene)
    obj["mme_handler"] = mme_needs_handler(obj)


def mme_handler_uniforms(obj):
    try:
        uniforms = json.loads(obj.get(MME_UNIFORMS_PROP, "[]"))
        driven = set(json.loads(obj.get(MME_DRIVEN_PROP, "[]")))
    except (TypeError, ValueError):
        return []
    return [u for u in uniforms if u[0] not in driven]


def mme_needs_handler(obj):
    if obj.get(MME_BACKEND_PROP, "PYTHON") == "PYTHON" and not obj.get(MME_BAKED_PROP):
        return True
    return bool(mme_handler_uniforms(obj))


def mme_build_pass(
    runtime,
    draw,
    state,
    root,
    collection,
    unit,
    density,
    label,
    backend="GEONODES",
    scene=None,
):
    scene = scene or bpy.context.scene
    geometry = mme_pass_geometry(runtime, draw, density)
    if geometry is None:
        return None
    used, local_faces, counts, instances = geometry
    per = len(used)
    src = np.tile(used, instances)
    inst = np.repeat(np.arange(instances, dtype=np.int64), per)
    data = MmePassData(draw, src, inst, mme_loop_values(draw, inst, density), runtime)
    state.world = mme_world_row(mme_child_matrix(root, unit), unit)
    tr_names = mme_self_tr_globals(runtime, root)
    keep = mme_prune_mask(runtime, data, state, tr_names)
    owner = "%s|%s" % (root.get("mmd_bridge_accessory"), label)
    if backend == "GEONODES":
        keep_src = keep.reshape(instances, per).any(axis=0)
        faces = mme_keep_faces(local_faces, keep_src)
        if not faces:
            log.info("effect pass %s draws nothing visible; skipped", label)
            return None
        alive, faces = mme_compact(used, faces)
        src_one = used[alive]
        pruned = len(src_one) < per
        one = MmePassData(
            draw,
            src_one,
            np.zeros(len(src_one), dtype=np.int64),
            mme_loop_values(draw, np.zeros(len(src_one), dtype=np.int64), density),
            runtime,
        )
        outputs, _ex = mme_run_vs(runtime, one, state)
        vs_outputs = {
            k: len(v.c)
            for k, v in outputs.items()
            if k != "POSITION0" and fx_is_numeric(v.t)
        }
        mat, varyings, uniforms, loop_attrs, note = mme_pass_material(
            runtime, draw, state, label, owner, vs_outputs
        )
        try:
            started = time.time()
            tree, drivers, defaults, node_count = mme_build_geo_tree(
                label, runtime, draw, state, root, unit, varyings, loop_attrs, scene
            )
        except FxError as error:
            log.warning(
                "effect pass %s: vertex shader cannot run in Geometry Nodes (%s); using the Python backend",
                label,
                error,
            )
            return mme_build_pass(
                runtime,
                draw,
                state,
                root,
                collection,
                unit,
                density,
                label,
                "PYTHON",
                scene,
            )
        positions = runtime.positions[src_one]
        mesh = mme_build_mesh(label, positions @ MME_SWAP3.T, faces)
        mme_set_attribute(mesh, MME_ATTR_SRC, src_one, "INT")
        mme_set_attribute(mesh, MME_ATTR_POS, positions, "FLOAT_VECTOR")
        mme_set_attribute(mesh, MME_ATTR_NRM, runtime.normals[src_one], "FLOAT_VECTOR")
        uv3 = np.zeros((len(src_one), 3))
        uv3[:, :2] = runtime.uvs[src_one]
        mme_set_attribute(mesh, MME_ATTR_UV, uv3, "FLOAT_VECTOR")
        obj = mme_pass_object(label, mesh, collection, root)
        modifier = obj.modifiers.new(MME_GN_MODIFIER, "NODES")
        modifier.node_group = tree
        mme_set_drivers(obj, modifier, drivers, defaults)
        mme_finish_pass(
            obj,
            runtime,
            draw,
            root,
            scene,
            counts,
            density,
            mat,
            varyings,
            uniforms,
            "GEO",
            pruned,
        )
        log.info(
            "effect pass %s: Geometry Nodes, %d source vertices x %d loop instance(s), %d faces each, %d nodes (%.2fs), %s",
            label,
            len(src_one),
            instances,
            len(faces),
            node_count,
            time.time() - started,
            note,
        )
        return obj
    faces = []
    for k in range(instances):
        offset = k * per
        for face in local_faces:
            faces.append(tuple(v + offset for v in face))
    faces = [f for f in faces if any(keep[v] for v in f)]
    if not faces:
        log.info("effect pass %s draws nothing visible; skipped", label)
        return None
    alive, faces = mme_compact(src, faces)
    pruned = not alive.all()
    src = src[alive]
    inst = inst[alive]
    data = MmePassData(draw, src, inst, mme_loop_values(draw, inst, density), runtime)
    outputs, _ex = mme_run_vs(runtime, data, state)
    vs_outputs = {
        k: len(v.c)
        for k, v in outputs.items()
        if k != "POSITION0" and fx_is_numeric(v.t)
    }
    world, culled = mme_clip_world(outputs, state, data.count)
    local = mme_world_to_local(world, culled, mme_child_matrix(root, unit), unit)
    mesh = mme_build_mesh(label, local, faces)
    mme_set_attribute(mesh, MME_ATTR_SRC, src, "INT")
    mme_set_attribute(mesh, MME_ATTR_INST, inst, "INT")
    mme_set_attribute(mesh, MME_ATTR_CULL, culled.astype(np.float32))
    for var, values in data.loop_values.items():
        mme_set_attribute(mesh, MME_LOOP_PREFIX + var, values)
    mat, varyings, uniforms, _loop_attrs, note = mme_pass_material(
        runtime, draw, state, label, owner, vs_outputs
    )
    obj = mme_pass_object(label, mesh, collection, root)
    modifier = obj.modifiers.new(MME_GN_MODIFIER, "NODES")
    modifier.node_group = mme_bake_only_tree(label, root)
    mme_finish_pass(
        obj,
        runtime,
        draw,
        root,
        scene,
        counts,
        density,
        mat,
        varyings,
        uniforms,
        "PYTHON",
        pruned,
    )
    mme_write_outputs(obj, mesh, outputs, data.count, varyings)
    log.info(
        "effect pass %s: Python, %d vertices, %d faces, %d instance(s), %s",
        label,
        len(src),
        len(faces),
        instances,
        note,
    )
    return obj


def mme_world_to_local(world, culled, child_matrix, unit):
    blender = (world @ MME_SWAP3.T) * unit
    inverse = np.linalg.inv(child_matrix)
    local = blender @ inverse[:3, :3].T + inverse[:3, 3]
    local[culled] = 0.0
    return np.where(np.isfinite(local), local, 0.0)


def mme_write_outputs(obj, mesh, outputs, count, varyings):
    for name in varyings:
        body = name[len(MME_VARYING_PREFIX) :]
        semantic, _sep, index = body.rpartition("_")
        value = outputs.get(semantic)
        if value is None or not fx_is_numeric(value.t):
            arr = np.zeros(count)
        else:
            k = int(index)
            comp = value.c[k] if k < len(value.c) else np.float64(0.0)
            arr = np.broadcast_to(np.asarray(comp, dtype=np.float64), (count,))
        mme_set_attribute(
            mesh, name, np.nan_to_num(arr, nan=0.0, posinf=1e6, neginf=-1e6)
        )


def mme_pass_data(runtime, obj):
    try:
        info = json.loads(obj.get(MME_DRAW_PROP, "{}"))
    except (TypeError, ValueError):
        return None
    mesh = obj.data
    stamp = (mesh.name, len(mesh.vertices), obj.get(MME_DRAW_PROP))
    cached = runtime.passes.get(obj.name)
    if cached is not None and cached.stamp == stamp:
        return cached
    draw = runtime.draw_by_order(info.get("order", -1))
    if (
        draw is None
        or draw.fx_pass.name != info.get("pass")
        or draw.technique.name != info.get("technique")
    ):
        return None
    src = mme_read_attribute(mesh, MME_ATTR_SRC)
    inst = mme_read_attribute(mesh, MME_ATTR_INST)
    if src is None or inst is None or len(src) != len(mesh.vertices):
        return None
    src = src.astype(np.int64)
    inst = inst.astype(np.int64)
    if len(src) and src.max() >= len(runtime.positions):
        return None
    data = MmePassData(
        draw, src, inst, mme_loop_values(draw, inst, info.get("density", 1.0)), runtime
    )
    data.stamp = stamp
    runtime.passes[obj.name] = data
    return data


def mme_update_pass(runtime, obj, root, state, unit, depsgraph=None):
    evaluated = obj.evaluated_get(depsgraph) if depsgraph is not None else obj
    child = mme_np_matrix(evaluated.matrix_world)
    state.world = mme_world_row(child, unit)
    mesh = obj.data
    if obj.get(MME_BACKEND_PROP, "PYTHON") == "PYTHON" and not obj.get(MME_BAKED_PROP):
        data = mme_pass_data(runtime, obj)
        if data is None:
            return False
        outputs, _ex = mme_run_vs(runtime, data, state)
        world, culled = mme_clip_world(outputs, state, data.count)
        local = mme_world_to_local(world, culled, child, unit)
        mesh.vertices.foreach_set("co", local.astype(np.float32).reshape(-1))
        mme_set_attribute(mesh, MME_ATTR_CULL, culled.astype(np.float32))
        try:
            varyings = json.loads(obj.get(MME_VARYINGS_PROP, "[]"))
        except (TypeError, ValueError):
            varyings = []
        mme_write_outputs(obj, mesh, outputs, data.count, varyings)
    uniforms = mme_handler_uniforms(obj)
    program = runtime.program
    for prop, gname, index in uniforms:
        g = program.globals.get(gname)
        if g is None:
            continue
        source = fx_classify_global(g, program)
        if source is None:
            continue
        values = state.values(source, g)
        obj[prop] = float(values[index]) if index < len(values) else 0.0
    mesh.update()
    obj.update_tag()
    return True


def mme_update_scene(scene, depsgraph=None, force=False):
    settings = getattr(scene, "mmd_bridge", None)
    if settings is None or not getattr(settings, "mme_effects", False):
        return 0
    rendering = False
    try:
        rendering = bpy.app.is_job_running("RENDER")
    except (AttributeError, TypeError):
        pass
    if not (
        force or rendering or settings.mme_live or _mme_state.get("only") is not None
    ):
        return 0
    updated = 0
    only = _mme_state.get("only")
    for root in mme_effect_roots(scene):
        children = [
            c
            for c in root.children
            if c.get(MME_DRAW_PROP) and c.get("mme_handler", True)
        ]
        if only is not None:
            children = [c for c in children if c.name in only]
        if not children:
            continue
        if rendering:
            children = [c for c in children if not c.hide_render]
        else:
            children = [c for c in children if not c.hide_viewport and not c.hide_get()]
        if not children:
            continue
        try:
            runtime = mme_runtime_for(root)
        except Exception as error:
            key = (root.name, str(error))
            if key not in _mme_state["errors"]:
                _mme_state["errors"].add(key)
                log.error("effect for '%s' could not be loaded: %s", root.name, error)
            continue
        if runtime is None:
            continue
        unit = float(root.get("mme_unit", 1.0)) or 1.0
        state = mme_frame_state(scene, depsgraph, root, runtime, unit)
        for child in children:
            try:
                if mme_update_pass(runtime, child, root, state, unit, depsgraph):
                    updated += 1
            except Exception as error:
                key = (child.name, str(error))
                if key not in _mme_state["errors"]:
                    _mme_state["errors"].add(key)
                    log.error(
                        "effect pass '%s' failed: %s\n%s",
                        child.name,
                        error,
                        traceback.format_exc(),
                    )
    return updated


def mme_read_emm(pmm_path):
    path = os.path.splitext(pmm_path)[0] + ".emm"
    if not os.path.isfile(path):
        return {}, {}
    try:
        text = fx_read_text(path)
    except OSError:
        return {}, {}
    sections = {}
    current = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip().lower()
            sections.setdefault(current, {})
            continue
        if current and "=" in line:
            key, value = line.split("=", 1)
            sections[current][key.strip().lower()] = value.strip()
    return sections.get("object", {}), sections.get("effect", {})


def mme_find_effect(acc, x_path, settings, entry):
    if entry is not None and not entry.use_effect:
        return None
    if entry is not None and entry.effect_path:
        override = bpy.path.abspath(entry.effect_path)
        return override if os.path.isfile(override) else None
    pmm_path = bpy.path.abspath(settings.filepath)
    objects, effects = mme_read_emm(pmm_path)
    if effects:
        base = acc.path.replace("\\", "/").rsplit("/", 1)[-1].lower()
        slot = None
        for key, value in objects.items():
            if (
                key.startswith("acc")
                and value.replace("\\", "/").rsplit("/", 1)[-1].lower() == base
            ):
                slot = key
                break
        if slot is None:
            slot = "acc%d" % (acc.index + 1)
        value = effects.get(slot)
        if value is not None:
            if value.lower() in ("none", ""):
                return None
            for candidate in (value, os.path.join(os.path.dirname(pmm_path), value)):
                if os.path.isfile(candidate):
                    return candidate
            found = fx_find_file(
                os.path.dirname(x_path), value.replace("\\", "/").rsplit("/", 1)[-1]
            )
            if found:
                return found
    stem = os.path.splitext(os.path.basename(x_path))[0].lower()
    folder = os.path.dirname(x_path)
    try:
        for name in os.listdir(folder):
            lower = name.lower()
            if lower == stem + ".fx":
                return os.path.join(folder, name)
    except OSError:
        pass
    return None


def mme_resolve_controls(program, project, settings, acc, root):
    controls = {}
    by_path = {}
    for model in project.models:
        base = model.path.replace("\\", "/").rsplit("/", 1)[-1].lower()
        by_path[base] = model
        if model.name:
            by_path.setdefault(model.name.lower(), model)
    accessories = {}
    for other in project.accessories:
        accessories[other.name.lower()] = other
        accessories[other.path.replace("\\", "/").rsplit("/", 1)[-1].lower()] = other
    items = {t.key: t for t in settings.targets}
    collection = bpy.data.collections.get(ACCESSORY_COLLECTION)
    morph_tracks = {}
    for g in program.globals.values():
        source = fx_classify_global(g, program)
        if source is None or source[0] != "control":
            continue
        name, item = source[1], source[2]
        lower = name.lower()
        desc = {"kind": "const", "values": [0.0]}
        if lower in MME_SELF_NAMES:
            desc = {"kind": "self", "item": item}
        elif lower.endswith((".x", ".vac")):
            other = accessories.get(lower)
            if other is not None and collection is not None:
                other_root = find_accessory_root(collection, accessory_key(other))
                if other_root is not None and other_root != root:
                    desc = {"kind": "object", "object": other_root.name, "item": item}
                elif other_root is root:
                    desc = {"kind": "self", "item": item}
        else:
            model = by_path.get(lower)
            if model is None:
                stem = os.path.splitext(lower)[0]
                model = next(
                    (m for k, m in by_path.items() if os.path.splitext(k)[0] == stem),
                    None,
                )
            if model is not None:
                item_obj = items.get(model_key(model))
                target = item_obj.target if item_obj is not None else None
                if item and item in model.morph_names:
                    prop = mme_prop_name(MME_CONTROL_PREFIX, g.name)
                    morph_tracks[prop] = model.morphs[model.morph_names.index(item)]
                    desc = {"kind": "prop", "prop": prop}
                elif item and item in model.bone_names and target is not None:
                    armature = find_armature(target)
                    bone = (
                        pose_bone_name(armature, item) if armature is not None else None
                    )
                    if bone is not None:
                        desc = {"kind": "bone", "object": armature.name, "bone": bone}
                elif not item:
                    desc = {
                        "kind": "model",
                        "object": target.name if target is not None else "",
                    }
        desc["from"] = "%s%s" % (name, (" / %s" % item) if item else "")
        controls[g.name] = desc
    return controls, morph_tracks


def mme_key_morph_props(root, morph_tracks):
    anim = root.animation_data or root.animation_data_create()
    for prop, keys in morph_tracks.items():
        if not keys:
            root[prop] = 0.0
            continue
        for key in keys:
            root[prop] = float(key.weight)
            root.keyframe_insert('["%s"]' % prop, frame=key.frame)
    action = anim.action
    if action is not None:
        for fcurve in iter_fcurves(action):
            if fcurve.data_path.startswith('["%s' % MME_CONTROL_PREFIX):
                for point in fcurve.keyframe_points:
                    point.interpolation = "LINEAR"


def mme_build_effect(
    scene, collection, settings, acc, root, x_path, fx_path, unit, controls
):
    _mme_runtimes.pop(root.name, None)
    program = fx_parse_file(fx_path)
    mme_sync_params(root, program)
    values = mme_param_values(root, program)
    runtime = MmeRuntime(fx_path, x_path, values)
    root["mme_fx"] = fx_path
    root["mme_x"] = x_path
    root["mme_unit"] = unit
    root["mme_controls"] = json.dumps(controls)
    runtime.controls_runtime = {
        name for name, desc in controls.items() if desc.get("kind") != "const"
    }
    if acc is not None:
        root["mme_acc_name"] = acc.name
        root["mme_tr"] = float(acc.keys[0].opacity) if acc.keys else 1.0
        root["mme_si"] = float(acc.keys[0].scale) if acc.keys else 1.0
    else:
        for name in ("mme_tr", "mme_si"):
            if name not in root:
                root[name] = 1.0
    for name, desc in controls.items():
        if desc.get("kind") == "prop" and desc["prop"] not in root:
            root[desc["prop"]] = 0.0
    depsgraph = bpy.context.evaluated_depsgraph_get()
    state = mme_frame_state(scene, depsgraph, root, runtime, unit)
    density = settings.mme_density
    backend = settings.mme_backend
    built_before = _mme_core["stats"]["built"]
    library_before = _mme_core["stats"]["library"]
    file_before = _mme_core["stats"]["file"]
    children = []
    base = short_name(
        root.get("mme_acc_name", acc.name if acc is not None else root.name), 24
    )
    emitted = [d for d in runtime.draws if d.target == "" and d.kind == "geometry"]
    for draw in emitted:
        label = "%s_%s_%s" % (
            base,
            draw.technique.name or "tech",
            draw.fx_pass.name or str(draw.order),
        )
        if draw.fx_pass.vs is None:
            log.info("effect pass %s uses fixed-function drawing; skipped", label)
            continue
        try:
            obj = mme_build_pass(
                runtime,
                draw,
                state,
                root,
                collection,
                unit,
                density,
                label,
                backend,
                scene,
            )
        except FxError as error:
            log.warning("effect pass %s skipped: %s", label, error)
            continue
        if obj is not None:
            children.append(obj)
    root["mme_structural"] = json.dumps(fx_structural_params(runtime.program))
    root[MME_PARAM_BUILT_PROP] = json.dumps(mme_param_signature(root))
    stats = dict(_mme_core["stats"])
    log.info(
        "shared node groups: %d built, %d appended from %s, %d reused from this file",
        stats["built"] - built_before,
        stats["library"] - library_before,
        MME_CORE_FILE,
        stats["file"] - file_before,
    )
    if stats["built"] > built_before and settings.mme_core_autosave:
        try:
            count = mme_save_core_library()
            log.info("%s updated: %d shared group(s)", MME_CORE_FILE, count)
        except (OSError, RuntimeError) as error:
            log.warning("%s could not be updated: %s", MME_CORE_FILE, error)
    covered = {d.subset for d in runtime.draws}
    uncovered = [s for s in range(len(runtime.materials)) if s not in covered]
    offscreen = sorted({d.target for d in runtime.draws if d.target})
    skipped = [d.label for d in runtime.draws if d.kind == "buffer"]
    if offscreen:
        log.info(
            "effect '%s': offscreen targets %s are emulated by drawing their source shading directly",
            os.path.basename(fx_path),
            offscreen,
        )
    if skipped:
        log.info(
            "effect '%s': screen-space passes not emulated: %s",
            os.path.basename(fx_path),
            skipped,
        )
    _mme_runtimes[root.name] = runtime
    return children, uncovered, None, len(emitted)


def mme_rebuild_effect(scene, root):
    settings = scene.mmd_bridge
    collection = bpy.data.collections.get(ACCESSORY_COLLECTION)
    fx_path, x_path = root.get("mme_fx", ""), root.get("mme_x", "")
    if collection is None or not (os.path.isfile(fx_path) and os.path.isfile(x_path)):
        return 0
    unit = float(root.get("mme_unit", 1.0)) or 1.0
    passes = [c for c in root.children if c.get(MME_DRAW_PROP)]
    visibility = None
    for child in passes:
        if child.animation_data is not None and child.animation_data.action is not None:
            visibility = child.animation_data.action
            break
    hidden = (
        (passes[0].hide_viewport, passes[0].hide_render) if passes else (False, False)
    )
    for child in passes:
        remove_accessory_children_single(child)
    started = time.time()
    children, _uncovered, _tracks, total = mme_build_effect(
        scene,
        collection,
        settings,
        None,
        root,
        x_path,
        fx_path,
        unit,
        mme_load_controls(root),
    )
    for child in children:
        child.scale = (ACCESSORY_UNIT * unit,) * 3
        child.hide_viewport, child.hide_render = hidden
        if visibility is not None:
            anim = child.animation_data_create()
            anim.action = visibility
            if anim.action_suitable_slots:
                anim.action_slot = anim.action_suitable_slots[0]
    log.info(
        "effect '%s' rebuilt in %.1fs: %d of %d pass(es)",
        root.name,
        time.time() - started,
        len(children),
        total,
    )
    return len(children)


def mme_rebuild_needed(root):
    current = mme_param_signature(root)
    built = mme_built_values(root)
    if current == built:
        return False
    changed = {k for k in set(current) | set(built) if current.get(k) != built.get(k)}
    for child in root.children:
        if not child.get(MME_DRAW_PROP):
            continue
        if not (
            child.get(MME_PRUNED_PROP)
            or child.get(MME_BACKEND_PROP, "PYTHON") == "PYTHON"
        ):
            continue
        try:
            reads = set(json.loads(child.get("mme_vs_params", "null")) or current)
        except (TypeError, ValueError):
            reads = set(current)
        if changed & reads:
            return True
    root[MME_PARAM_BUILT_PROP] = json.dumps(current)
    return False


def mme_run_rebuilds():
    names = list(_mme_state["rebuild"])
    _mme_state["rebuild"].clear()
    try:
        scene = bpy.context.scene
    except AttributeError:
        return None
    for name in names:
        root = bpy.data.objects.get(name)
        if root is None or not root.get("mme_fx"):
            continue
        try:
            if mme_rebuild_needed(root):
                mme_rebuild_effect(scene, root)
        except Exception as error:
            log.error(
                "effect '%s' rebuild failed: %s\n%s",
                name,
                error,
                traceback.format_exc(),
            )
    redraw_all()
    return None


def mme_clear_root_props(root):
    for name in [
        k
        for k in root.keys()
        if k in ("mme_fx", "mme_x", "mme_unit", "mme_controls")
        or k.startswith(MME_CONTROL_PREFIX)
    ]:
        del root[name]
    _mme_runtimes.pop(root.name, None)


def remove_subset_faces(obj, subsets):
    mesh = obj.data
    if not subsets or not len(mesh.polygons):
        return
    indices = np.empty(len(mesh.polygons), dtype=np.int32)
    mesh.polygons.foreach_get("material_index", indices)
    import bmesh

    bm = bmesh.new()
    bm.from_mesh(mesh)
    doomed = [f for f in bm.faces if f.material_index in subsets]
    if doomed:
        bmesh.ops.delete(bm, geom=doomed, context="FACES")
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()


def effect_setup(project, settings, acc, entry, path, root):
    if not settings.mme_effects:
        return None, None, {}, {}
    fx_path = mme_find_effect(acc, path, settings, entry)
    if not fx_path:
        return None, None, {}, {}
    try:
        program = fx_parse_file(fx_path)
        controls, tracks = mme_resolve_controls(program, project, settings, acc, root)
    except (FxError, OSError, UnicodeError) as error:
        log.error(
            "accessory '%s': effect %s could not be read: %s", acc.name, fx_path, error
        )
        if entry is not None:
            entry.effect_note = "Effect error: %s" % short_text(error, 60)
        return None, None, {}, {}
    return fx_path, program, controls, tracks


def remove_accessory_children_single(child):
    data = child.data
    groups = mme_pass_groups(child)
    bpy.data.objects.remove(child, do_unlink=True)
    mme_free_groups(groups)
    if (
        data is not None
        and getattr(data, "users", 1) == 0
        and isinstance(data, bpy.types.Mesh)
    ):
        try:
            for material in list(data.materials):
                if (
                    material is not None
                    and material.users <= 1
                    and material.get(MME_OWNER_PROP) is None
                ):
                    bpy.data.materials.remove(material)
            bpy.data.meshes.remove(data)
        except (ReferenceError, RuntimeError):
            pass


def mme_target_roots(scene, root_name):
    roots = mme_effect_roots(scene)
    if root_name:
        roots = [r for r in roots if r.name == root_name]
    return roots


def mme_bake_pass(context, obj, start, end, settings):
    modifier = obj.modifiers.get(MME_GN_MODIFIER)
    if modifier is None or not len(modifier.bakes):
        return False
    bake = modifier.bakes[0]
    bake.bake_mode = "ANIMATION"
    bake.use_custom_simulation_frame_range = True
    bake.frame_start = start
    bake.frame_end = end
    if settings.mme_bake_target == "DISK":
        modifier.bake_target = "DISK"
        modifier.bake_directory = settings.mme_bake_dir
        bake.bake_target = "DISK"
    else:
        modifier.bake_target = "PACKED"
        bake.bake_target = "PACKED"
    _mme_state["only"] = {obj.name}
    try:
        with context.temp_override(
            object=obj, active_object=obj, selected_objects=[obj]
        ):
            result = bpy.ops.object.geometry_node_bake_single(
                session_uid=obj.session_uid,
                modifier_name=modifier.name,
                bake_id=bake.bake_id,
            )
    finally:
        _mme_state["only"] = None
    if "FINISHED" not in result:
        return False
    obj[MME_BAKED_PROP] = True
    obj["mme_handler"] = mme_needs_handler(obj)
    return True


def mme_clear_bake(context, obj):
    modifier = obj.modifiers.get(MME_GN_MODIFIER)
    if modifier is None or not len(modifier.bakes):
        return False
    bake = modifier.bakes[0]
    with context.temp_override(object=obj, active_object=obj, selected_objects=[obj]):
        bpy.ops.object.geometry_node_bake_delete_single(
            session_uid=obj.session_uid,
            modifier_name=modifier.name,
            bake_id=bake.bake_id,
        )
    if MME_BAKED_PROP in obj:
        del obj[MME_BAKED_PROP]
    obj["mme_handler"] = mme_needs_handler(obj)
    return True
