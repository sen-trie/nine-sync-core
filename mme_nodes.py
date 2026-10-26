import bpy
import hashlib
import json
import math
import numpy as np
import os

from .common import (
    ACCESSORY_UNIT,
    LIGHT_COLLECTION,
    MME_ATTR_CULL,
    MME_ATTR_INST,
    MME_ATTR_NRM,
    MME_ATTR_POS,
    MME_ATTR_SRC,
    MME_ATTR_UV,
    MME_CORE_FILE,
    MME_CORE_VERSION,
    MME_GAMMA,
    MME_GROUP_PROP,
    MME_IMAGE_PREFIX,
    MME_KEY_PROP,
    MME_LOOP_PREFIX,
    MME_OWNER_PROP,
    MME_PARAM_PREFIX,
    MME_RET_PROP,
    MME_SIG_PROP,
    MME_SWAP,
    MME_UNIFORM_PREFIX,
    MME_VARYING_PREFIX,
    PMM_FPS,
    _mme_core,
    _mme_groups,
    child_of_type,
    find_rig_root,
    log,
    mme_control_source,
    mme_control_use_prop,
    mme_load_controls,
    mme_param_meta,
    node_socket,
    scene_fps,
)
from .xfile import (
    resolve_x_texture,
)
from .fx import (
    FX_FAR,
    FX_GROUP_INTRINSICS,
    FX_NEAR,
    FxError,
    FxExec,
    FxImage,
    FxNumpyBackend,
    FxProgram,
    FxSceneState,
    FxUnsupported,
    FxValue,
    MME_LABEL_LIMIT,
    fx_classify_global,
    fx_comp_label,
    fx_count,
    fx_entry,
    fx_function_info,
    fx_intrinsic_values,
    fx_is_numeric,
    fx_is_render_target,
    fx_resolve_type,
    fx_run_entry,
    fx_sampler_texture_global,
    fx_source_is_runtime,
    fx_type_name,
    fx_value_from_list,
)


MME_SIMPLIFY_TR = (1.0, 0.9, 0.75, 0.6, 0.45, 0.3, 0.15, 0.0)


MME_GATE_TR = tuple(k / 32.0 for k in range(32, -1, -1))


MME_GATE_MARGIN = 2


MME_GATE_EPS = 1e-5


MME_GATE_ALPHA = 1.0 / 512.0


MME_GATE_PROBES = 96


MME_TRANSPARENT_BOUNCES = 128


MME_VARIANT_FRAMES = 24


MME_VARIANT_MAX = 48


MME_SIMPLIFY_VARIANTS = 8


MME_PROGRESS_WIDTH = 24


MME_WRAP_MARGIN = 40


MME_WRAP_CHAR = 6.2


MME_PROGRESS_REDRAW = 0.1


MME_RUNTIME_VALUES_PROP = "mme_runtime_values"


def mme_group_name(label, tag, key):
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()[:6]
    text = "MME %s [%s] %s" % (label, tag, digest)
    if len(text.encode("utf-8")) > 63:
        text = "MME %s [%s] %s" % (label[:30], tag, digest)
    return text


def mme_core_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), MME_CORE_FILE)


def mme_core_names():
    path = mme_core_path()
    if _mme_core["names"] is None or _mme_core["path"] != path:
        names = set()
        if os.path.isfile(path):
            try:
                with bpy.data.libraries.load(path, link=False) as (source, _target):
                    names = set(source.node_groups)
            except (OSError, RuntimeError) as error:
                log.warning("core library %s could not be read: %s", path, error)
        _mme_core["names"] = names
        _mme_core["path"] = path
    return _mme_core["names"]


def mme_core_append(name, key):
    if name not in mme_core_names():
        return None
    blocking = bpy.data.node_groups.get(name)
    if blocking is not None:
        blocking.name = name + " (stale)"
    try:
        with bpy.data.libraries.load(mme_core_path(), link=False) as (_source, target):
            target.node_groups = [name]
    except (OSError, RuntimeError) as error:
        log.warning("core library group %s could not be appended: %s", name, error)
        return None
    loaded = target.node_groups[0] if target.node_groups else None
    if loaded is None:
        return None
    if loaded.get(MME_KEY_PROP) != key:
        bpy.data.node_groups.remove(loaded)
        return None
    for group in bpy.data.node_groups:
        if group.get(MME_KEY_PROP) and group.library is None:
            _mme_groups.setdefault(group[MME_KEY_PROP], group.name)
    return loaded


def mme_core_group(name, tree_type, key, build):
    found = _mme_groups.get(key)
    group = bpy.data.node_groups.get(found) if found else None
    if group is not None and group.get(MME_KEY_PROP) == key and group.bl_idname == tree_type:
        return group
    for group in bpy.data.node_groups:
        if group.get(MME_KEY_PROP) == key and group.bl_idname == tree_type and group.library is None:
            _mme_groups[key] = group.name
            _mme_core["stats"]["file"] += 1
            return group
    group = None if _mme_core.get("skip_library") else mme_core_append(name, key)
    if group is not None and group.bl_idname == tree_type:
        _mme_groups[key] = group.name
        _mme_core["stats"]["library"] += 1
        return group
    group = bpy.data.node_groups.new(name, tree_type)
    group[MME_KEY_PROP] = key
    try:
        build(group)
    except Exception:
        bpy.data.node_groups.remove(group)
        raise
    _mme_groups[key] = group.name
    _mme_core["stats"]["built"] += 1
    return group


MME_STANDARD_INTRINSICS = [
    ("mul", [("f", (4,)), ("f", (4, 4))]),
    ("mul", [("f", (3,)), ("f", (4, 4))]),
    ("mul", [("f", (3,)), ("f", (3, 3))]),
    ("mul", [("f", (4, 4)), ("f", (4,))]),
    ("mul", [("f", (4, 4)), ("f", (4, 4))]),
    ("mul", [("f", (3, 3)), ("f", (3, 3))]),
]


def mme_build_standard_groups(force=False):
    if force:
        for group in list(bpy.data.node_groups):
            key = str(group.get(MME_KEY_PROP, ""))
            if key.startswith(("core:", "in:")):
                del group[MME_KEY_PROP]
                if group.users == 0:
                    bpy.data.node_groups.remove(group)
                else:
                    group.name = group.name + " (old)"
        _mme_groups.clear()
    before = _mme_core["stats"]["built"]
    _mme_core["skip_library"] = force
    try:
        program = FxProgram()
        program.params = {}
        for backend, idname in ((MmeGeoBackend, "GeometryNodeTree"), (MmePsBackend, "ShaderNodeTree")):
            scratch = bpy.data.node_groups.new("MME scratch", idname)
            try:
                B = backend(scratch)
                ex = FxExec(program, B, MmeGroupEnv(program))
                for name, types in MME_STANDARD_INTRINSICS:
                    B.intrinsic_group_tree(ex, name, types)
            finally:
                bpy.data.node_groups.remove(scratch)
        mme_geo_matrix_group()
        mme_geo_camera_group()
        mme_geo_light_group()
        mme_geo_clip_group()
    finally:
        _mme_core["skip_library"] = False
    return _mme_core["stats"]["built"] - before


def mme_group_stats_reset():
    _mme_core["stats"] = {"built": 0, "file": 0, "library": 0}


def mme_save_core_library(path=None):
    path = path or mme_core_path()
    keep = {}
    for group in bpy.data.node_groups:
        key = group.get(MME_KEY_PROP)
        if key and group.library is None and str(key).endswith(":v%d" % MME_CORE_VERSION):
            keep.setdefault(key, group)
    appended = []
    missing = [n for n in mme_core_names() if bpy.data.node_groups.get(n) is None]
    if missing and os.path.isfile(path):
        with bpy.data.libraries.load(path, link=False) as (_source, target):
            target.node_groups = missing
        appended = [g for g in target.node_groups if g is not None]
        for group in appended:
            key = group.get(MME_KEY_PROP)
            if key:
                keep.setdefault(key, group)
    blocks = set(keep.values())
    for group in list(blocks):
        for node in group.nodes:
            if node.bl_idname in ("GeometryNodeGroup", "ShaderNodeGroup") and node.node_tree is not None:
                blocks.add(node.node_tree)
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    bpy.data.libraries.write(path, blocks, fake_user=True, compress=True)
    for group in appended:
        if group.users == 0:
            bpy.data.node_groups.remove(group)
    _mme_core["names"] = None
    return len(keep)


class MmeGroupEnv:
    def __init__(self, program):
        self.program = program
        self.rt_lookup = None

    def global_value(self, ex, g):
        return None

    def sample(self, ex, info, u, v, lod):
        g = fx_sampler_texture_global(self.program, info)
        slot = g.name if g is not None else (info.name if info is not None else "texture")
        point = info.point() if info else False
        address = info.address() if info else "WRAP"
        extension = {"CLAMP": "EXTEND", "MIRROR": "MIRROR"}.get(address, "REPEAT")
        return ex.B.image_slot(slot, "Closest" if point else "Linear", extension, u, v)


def mme_slot_image(env, slot):
    program = getattr(env, "program", None)
    g = program.globals.get(slot) if program is not None else None
    if g is None or fx_is_render_target(g) or getattr(env, "textures", None) is None:
        return None
    path = env.textures.resource_path(g, env.state.material.get("texture", ""))
    if not path:
        return None
    try:
        return mme_effect_image(path)
    except RuntimeError:
        return None


class MmeGroupCalls:
    group_mode = False

    def iface_kind(self, t):
        if t == ("f", (3,)):
            return "VECTOR"
        if t == ("f", (4, 4)) and self.tree_tag == "GN":
            return "MATRIX"
        return "FLOATS"

    def vector_of(self, comps, force=False):
        comps = list(comps)
        if not force and all(not isinstance(c, MmeSock) for c in comps):
            return tuple(float(c) for c in comps)
        if all(isinstance(c, MmeSock) for c in comps):
            node = comps[0].socket.node
            if (
                node.bl_idname == "ShaderNodeSeparateXYZ"
                and all(c.socket.node == node for c in comps)
                and [c.socket.identifier for c in comps] == [o.identifier for o in node.outputs[:3]]
                and node.inputs[0].is_linked
            ):
                return MmeSock(node.inputs[0].links[0].from_socket)
        key = ("vec3",) + tuple(self.key(c) for c in comps)
        found = self.cache.get(key)
        if found is not None:
            return found
        node = self.new("ShaderNodeCombineXYZ")
        for k, comp in enumerate(comps):
            self.feed(node.inputs[k], comp)
        result = MmeSock(node.outputs[0])
        self.cache[key] = result
        return result

    def split_vector(self, value):
        if not isinstance(value, MmeSock):
            return [float(v) for v in value]
        key = ("split3", self.key(value))
        found = self.cache.get(key)
        if found is not None:
            return list(found)
        node = self.new("ShaderNodeSeparateXYZ")
        self.links.new(value.socket, node.inputs[0])
        comps = [MmeSock(node.outputs[k]) for k in range(3)]
        self.cache[key] = comps
        return list(comps)

    def matrix_of(self, comps):
        comps = list(comps)
        if all(isinstance(c, MmeSock) for c in comps):
            node = comps[0].socket.node
            if (
                node.bl_idname == "FunctionNodeSeparateMatrix"
                and all(c.socket.node == node for c in comps)
                and [c.socket.identifier for c in comps] == [o.identifier for o in node.outputs[:16]]
                and node.inputs[0].is_linked
            ):
                return MmeSock(node.inputs[0].links[0].from_socket)
        key = ("mat4",) + tuple(self.key(c) for c in comps)
        found = self.cache.get(key)
        if found is not None:
            return found
        node = self.new("FunctionNodeCombineMatrix")
        for k, comp in enumerate(comps):
            self.feed(node.inputs[k], comp)
        result = MmeSock(node.outputs[0])
        self.cache[key] = result
        return result

    def split_matrix(self, value):
        key = ("split16", self.key(value))
        found = self.cache.get(key)
        if found is not None:
            return list(found)
        node = self.new("FunctionNodeSeparateMatrix")
        self.links.new(value.socket, node.inputs[0])
        comps = [MmeSock(node.outputs[k]) for k in range(16)]
        self.cache[key] = comps
        return list(comps)

    def feed_value(self, socket, value):
        if isinstance(value, MmeSock):
            self.links.new(value.socket, socket)
        elif isinstance(value, tuple):
            socket.default_value = value
        else:
            self.feed(socket, value)

    def group_inputs(self, label, t):
        kind = self.iface_kind(t)
        if kind == "VECTOR":
            return FxValue(t, self.split_vector(self.new_input(label, "NodeSocketVector", label)))
        if kind == "MATRIX":
            return FxValue(t, self.split_matrix(self.new_input(label, "NodeSocketMatrix", label)))
        return FxValue(t, [self.new_input(fx_comp_label(label, t, k), "NodeSocketFloat", fx_comp_label(label, t, k)) for k in range(fx_count(t[1]))])

    def group_outputs(self, label, value):
        t = ("f", value.t[1])
        kind = self.iface_kind(t)
        if kind == "VECTOR":
            return [(label, self.vector_of(value.c, force=True), "NodeSocketVector")]
        if kind == "MATRIX":
            return [(label, self.matrix_of(value.c), "NodeSocketMatrix")]
        return [(fx_comp_label(label, t, k), c, "NodeSocketFloat") for k, c in enumerate(value.c)]

    def function_group(self, ex, function, ptypes, rt, info):
        key = "fn:%s:%s:%s:v%d" % (self.tree_tag, function.name, info["hash"], MME_CORE_VERSION)
        name = mme_group_name("fn %s" % function.name, self.tree_tag, key)
        program = ex.program
        backend = type(self)

        def build(group):
            sub = backend(group)
            sub.group_mode = True
            sub_ex = FxExec(program, sub, MmeGroupEnv(program))
            values = [sub.group_inputs(p.name, pt) for p, pt in zip(function.params, ptypes)]
            result, _outs = sub_ex.run_function(function, values)
            result = sub_ex.convert(result, rt)
            sub.statement("return")
            sub.finish(sub.group_outputs("return", result))
            group[MME_RET_PROP] = json.dumps([rt[0], list(rt[1])])
            group.description = "HLSL function %s from an MME effect" % function.name

        return mme_core_group(name, self.tree.bl_idname, key, build)

    def intrinsic_group_tree(self, ex, name, types):
        signature = "%s(%s)" % (name, ",".join(fx_type_name(t) for t in types))
        key = "in:%s:%s:v%d" % (self.tree_tag, signature, MME_CORE_VERSION)
        program = ex.program
        backend = type(self)

        def build(group):
            sub = backend(group)
            sub.group_mode = True
            sub_ex = FxExec(program, sub, MmeGroupEnv(program))
            labels = ["abc"[i] if i < 3 else "arg%d" % i for i in range(len(types))]
            sub.statement(signature)
            result = sub.matrix_intrinsic(name, types, labels)
            if result is None and name == "mul":
                result = sub.vector_mul(types, labels)
            if result is None:
                values = [sub.group_inputs(label, t) for label, t in zip(labels, types)]
                result = fx_intrinsic_values(sub_ex, name, values)
            rt = ("f", result.t[1])
            group[MME_RET_PROP] = json.dumps([rt[0], list(rt[1])])
            sub.finish(sub.group_outputs("result", FxValue(rt, result.c)))
            group.description = "HLSL intrinsic %s" % signature

        return mme_core_group(mme_group_name(signature, self.tree_tag, key), self.tree.bl_idname, key, build)

    def matrix_intrinsic(self, name, types, labels):
        if self.tree_tag != "GN" or name != "mul":
            return None
        m4, v4 = ("f", (4, 4)), ("f", (4,))
        if types == [v4, m4] or types == [m4, v4]:
            vector_first = types[0] == v4
            v = self.group_inputs(labels[0] if vector_first else labels[1], v4)
            m = self.new_input(labels[1] if vector_first else labels[0], "NodeSocketMatrix", labels[1] if vector_first else labels[0])
            column = self.new("FunctionNodeCombineMatrix")
            column.label = "vector as a matrix column"
            for k in range(16):
                self.feed(column.inputs[k], v.c[k] if k < 4 else 0.0)
            source = m.socket
            if not vector_first:
                transpose = self.new("FunctionNodeTransposeMatrix")
                self.links.new(m.socket, transpose.inputs[0])
                source = transpose.outputs[0]
            multiply = self.new("FunctionNodeMatrixMultiply")
            self.links.new(source, multiply.inputs[0])
            self.links.new(column.outputs[0], multiply.inputs[1])
            out = self.split_matrix(MmeSock(multiply.outputs[0]))
            return FxValue(v4, out[:4])
        if types == [m4, m4]:
            a = self.new_input(labels[0], "NodeSocketMatrix", labels[0])
            b = self.new_input(labels[1], "NodeSocketMatrix", labels[1])
            multiply = self.new("FunctionNodeMatrixMultiply")
            self.links.new(b.socket, multiply.inputs[0])
            self.links.new(a.socket, multiply.inputs[1])
            return FxValue(m4, self.split_matrix(MmeSock(multiply.outputs[0])))
        return None

    def vm(self, op, label, a, b=None, scale=None, output="Vector"):
        inputs = [("Vector", a)]
        if b is not None:
            inputs.append(("Vector_001", b))
        if scale is not None:
            inputs.append(("Scale", scale))
        return self.native_node("ShaderNodeVectorMath", label, {"operation": op}, inputs, output)

    def vec_mat(self, v, m, dims):
        rows, cols = dims
        k = min(len(v), rows)
        acc = None
        for i in range(k):
            row = self.vector_of(m[i * cols:i * cols + 3])
            term = self.vm("SCALE", "v[%d] * row %d" % (i, i), row, scale=v[i])
            acc = term if acc is None else self.vm("ADD", "sum of rows", acc, term)
        out = self.split_vector(acc)
        if cols == 4:
            column = self.vector_of([m[i * cols + 3] for i in range(3)])
            w = self.vm("DOT_PRODUCT", "w = dot(v.xyz, column w)", self.vector_of(v[:3]), column, output="Value")
            if k == 4:
                w = self.add(w, self.mul(v[3], m[3 * cols + 3]))
            out.append(w)
        return out

    def mat_vec(self, m, dims, v):
        rows, cols = dims
        k = min(cols, len(v))
        vector = self.vector_of(v[:3])
        out = []
        for i in range(rows):
            value = self.vm("DOT_PRODUCT", "dot(row %d, v)" % i, self.vector_of(m[i * cols:i * cols + 3]), vector, output="Value")
            if k == 4:
                value = self.add(value, self.mul(m[i * cols + 3], v[3]))
            out.append(value)
        return out

    def vector_mul(self, types, labels):
        (da, db) = (types[0][1], types[1][1])
        ok = False
        if len(da) == 1 and len(db) == 2:
            ok = db[1] in (3, 4) and min(da[0], db[0]) in (3, 4)
        elif len(da) == 2 and len(db) == 1:
            ok = min(da[1], db[0]) in (3, 4)
        elif len(da) == 2 and len(db) == 2:
            ok = db[1] in (3, 4) and min(da[1], db[0]) in (3, 4)
        if not ok:
            return None
        a, b = [self.group_inputs(label, t) for label, t in zip(labels, types)]
        if len(da) == 1:
            return FxValue(("f", (db[1],)), self.vec_mat(a.c, b.c, db))
        if len(db) == 1:
            return FxValue(("f", (da[0],)), self.mat_vec(a.c, da, b.c))
        out = []
        for i in range(da[0]):
            out.extend(self.vec_mat(a.c[i * da[1]:(i + 1) * da[1]], b.c, db))
        return FxValue(("f", (da[0], db[1])), out)

    def place_group(self, group, names, values, out_name, label, slots=(), env=None):
        try:
            ret = json.loads(group.get(MME_RET_PROP, '["f", []]'))
        except (TypeError, ValueError):
            ret = ["f", []]
        rt = (ret[0], tuple(ret[1]))
        cache_key = ("call", group.name) + tuple(self.key(c) for v in values for c in v.c)
        found = self.cache.get(cache_key)
        if found is not None:
            return FxValue(rt, list(found))
        node = self.new(self.group_idname)
        node.node_tree = group
        node.label = label[:MME_LABEL_LIMIT]
        inputs = {s.name: s for s in node.inputs}
        for name, value in zip(names, values):
            whole = inputs.get(name)
            if whole is not None and whole.type == "VECTOR":
                self.feed_value(whole, self.vector_of(value.c))
                continue
            if whole is not None and whole.type == "MATRIX":
                self.feed_value(whole, self.matrix_of(value.c))
                continue
            for k, comp in enumerate(value.c):
                target = inputs.get(fx_comp_label(name, value.t, k))
                if target is not None:
                    self.feed(target, comp)
        for slot in slots:
            target = inputs.get("tex:" + slot)
            if target is not None:
                target.default_value = mme_slot_image(env, slot)
        outputs = {s.name: s for s in node.outputs}
        whole = outputs.get(out_name)
        if whole is not None and whole.type == "VECTOR":
            comps = self.split_vector(MmeSock(whole))
        elif whole is not None and whole.type == "MATRIX":
            comps = self.split_matrix(MmeSock(whole))
        else:
            comps = []
            for k in range(fx_count(rt[1])):
                socket = outputs.get(fx_comp_label(out_name, rt, k))
                comps.append(MmeSock(socket) if socket is not None else 0.0)
        self.cache[cache_key] = comps
        return FxValue(rt, comps)

    def call_group(self, ex, function, args):
        info = fx_function_info(ex.program, function)
        if not info["pure"] or (info["slots"] and not self.image_inputs):
            return None
        params = function.params
        if len(args) != len(params):
            return None
        if any("out" in p.quals or "inout" in p.quals for p in params):
            return None
        try:
            rt = fx_resolve_type(function.ret, ex.program)
            ptypes = [fx_resolve_type(p.type, ex.program) for p in params]
        except FxError:
            return None
        if not fx_is_numeric(rt):
            return None
        for p, pt, a in zip(params, ptypes, args):
            if p.array is not None or not fx_is_numeric(pt) or not fx_is_numeric(a.t):
                return None
        values = [ex.convert(a, pt) for a, pt in zip(args, ptypes)]
        if all(self.known(c) is not None for v in values for c in v.c):
            return None
        group = self.function_group(ex, function, ptypes, rt, info)
        label = "%s(%s)" % (function.name, ", ".join(p.name for p in params))
        result = self.place_group(group, [p.name for p in params], values, "return", label, info["slots"], ex.env)
        return ex.convert(result, rt)

    def native_node(self, idname, label, props, inputs, output):
        key = ("native", idname, tuple(sorted(props.items()))) + tuple((n, self.key(v) if not isinstance(v, tuple) else v) for n, v in inputs)
        found = self.cache.get(key)
        if found is not None:
            return found
        node = self.new(idname)
        node.label = label
        for prop, value in props.items():
            setattr(node, prop, value)
        for identifier, value in inputs:
            self.feed_value(node_socket(node, identifier), value)
        result = MmeSock(node_socket(node, output, outputs=True))
        self.cache[key] = result
        return result

    def native_intrinsic(self, ex, name, args):
        if not args or not all(fx_is_numeric(a.t) for a in args):
            return None
        if all(self.known(c) is not None for a in args for c in a.c):
            return None
        v3 = ("f", (3,))
        v4 = ("f", (4,))
        types = [("f", a.t[1]) for a in args]
        if name in ("dot", "length", "normalize") and all(t == v4 for t in types):
            a = args[0]
            b = args[1] if name == "dot" else args[0]
            d = self.vm("DOT_PRODUCT", "dot(xyz)", self.vector_of(a.c[:3]), self.vector_of(b.c[:3]), output="Value")
            d = self.add(d, self.mul(a.c[3], b.c[3]))
            if name == "dot":
                return FxValue(("f", ()), [d])
            length = self.unary("sqrt", d)
            if name == "length":
                return FxValue(("f", ()), [length])
            inv = self.select(self.cmp(">", length, 1e-20), self.div(1.0, length), 0.0)
            xyz = self.split_vector(self.vm("SCALE", "normalize xyz", self.vector_of(a.c[:3]), scale=inv))
            return FxValue(v4, xyz + [self.mul(a.c[3], inv)])
        if name in ("cross", "normalize", "length", "dot", "distance", "reflect") and all(t == v3 for t in types):
            vectors = [self.vector_of(a.c) for a in args]
            if name == "normalize":
                out = self.native_node("ShaderNodeVectorMath", "normalize", {"operation": "NORMALIZE"}, [("Vector", vectors[0])], "Vector")
                return FxValue(v3, self.split_vector(out))
            if name == "length":
                out = self.native_node("ShaderNodeVectorMath", "length", {"operation": "LENGTH"}, [("Vector", vectors[0])], "Value")
                return FxValue(("f", ()), [out])
            if name == "cross":
                out = self.native_node("ShaderNodeVectorMath", "cross", {"operation": "CROSS_PRODUCT"}, [("Vector", vectors[0]), ("Vector_001", vectors[1])], "Vector")
                return FxValue(v3, self.split_vector(out))
            if name == "dot":
                out = self.native_node("ShaderNodeVectorMath", "dot", {"operation": "DOT_PRODUCT"}, [("Vector", vectors[0]), ("Vector_001", vectors[1])], "Value")
                return FxValue(("f", ()), [out])
            if name == "distance":
                out = self.native_node("ShaderNodeVectorMath", "distance", {"operation": "DISTANCE"}, [("Vector", vectors[0]), ("Vector_001", vectors[1])], "Value")
                return FxValue(("f", ()), [out])
            d = self.native_node("ShaderNodeVectorMath", "dot(n, i)", {"operation": "DOT_PRODUCT"}, [("Vector", vectors[1]), ("Vector_001", vectors[0])], "Value")
            scaled = self.native_node("ShaderNodeVectorMath", "2 * dot(n, i) * n", {"operation": "SCALE"}, [("Vector", vectors[1]), ("Scale", self.mul(2.0, d))], "Vector")
            out = self.native_node("ShaderNodeVectorMath", "reflect", {"operation": "SUBTRACT"}, [("Vector", vectors[0]), ("Vector_001", scaled)], "Vector")
            return FxValue(v3, self.split_vector(out))
        if name == "lerp" and len(args) == 3:
            a, b, t = args
            if types[0] == v3 and types[1] == v3 and types[2] in (v3, ("f", ())):
                uniform = types[2] == ("f", ())
                factor = t.c[0] if uniform else self.vector_of(t.c)
                out = self.native_node(
                    "ShaderNodeMix",
                    "lerp",
                    {"data_type": "VECTOR", "factor_mode": "UNIFORM" if uniform else "NON_UNIFORM", "clamp_factor": False},
                    [("Factor_Float" if uniform else "Factor_Vector", factor), ("A_Vector", self.vector_of(a.c)), ("B_Vector", self.vector_of(b.c))],
                    "Result_Vector",
                )
                return FxValue(v3, self.split_vector(out))

            def mix(x, y, s):
                folded = self.fold(lambda p, q, r: p + (q - p) * r, x, y, s)
                if folded is not None:
                    return folded
                return self.native_node("ShaderNodeMix", "lerp", {"data_type": "FLOAT", "clamp_factor": False}, [("Factor_Float", s), ("A_Float", x), ("B_Float", y)], "Result_Float")

            return ex.elementwise(mix, FxValue(("f", a.t[1]), a.c), b, t)
        if name == "smoothstep" and len(args) == 3:

            def step(lo, hi, x):
                if self.known(lo) is not None and self.known(hi) is not None and self.known(x) is not None:
                    return self.np_smoothstep(lo, hi, x)
                return self.native_node(
                    "ShaderNodeMapRange",
                    "smoothstep",
                    {"data_type": "FLOAT", "interpolation_type": "SMOOTHSTEP", "clamp": True},
                    [("From Min", lo), ("From Max", hi), ("Value", x), ("To Min", 0.0), ("To Max", 1.0)],
                    "Result",
                )

            return ex.elementwise(step, FxValue(("f", args[0].t[1]), args[0].c), args[1], args[2])
        if name in ("saturate", "clamp"):
            lo_hi = [0.0, 1.0] if name == "saturate" else None

            def clamp(x, lo, hi):
                folded = self.fold(lambda p, q, r: min(max(p, q), r), x, lo, hi)
                if folded is not None:
                    return folded
                return self.native_node("ShaderNodeClamp", name, {"clamp_type": "MINMAX"}, [("Value", x), ("Min", lo), ("Max", hi)], "Result")

            if lo_hi is not None:
                a = args[0]
                return FxValue(("f", a.t[1]), [clamp(c, 0.0, 1.0) for c in a.c])
            if len(args) != 3:
                return None
            return ex.elementwise(clamp, args[0], args[1], args[2])
        return None

    def np_smoothstep(self, lo, hi, x):
        t = min(max((float(x) - float(lo)) / (float(hi) - float(lo)), 0.0), 1.0) if float(hi) != float(lo) else 0.0
        return t * t * (3.0 - 2.0 * t)

    def intrinsic_group(self, ex, name, args):
        if name not in FX_GROUP_INTRINSICS or not args:
            return None
        if not all(fx_is_numeric(a.t) for a in args):
            return None
        if any(all(self.known(c) is not None for c in a.c) for a in args):
            return None
        if name == "mul" and not any(len(a.t[1]) == 2 for a in args):
            return None
        if name in ("dot", "length", "normalize", "distance") and max(fx_count(a.t[1]) for a in args) < 3:
            return None
        types = [("f", a.t[1]) for a in args]
        values = [FxValue(t, list(a.c)) for t, a in zip(types, args)]
        group = self.intrinsic_group_tree(ex, name, types)
        names = ["abc"[i] if i < 3 else "arg%d" % i for i in range(len(args))]
        return self.place_group(group, names, values, "result", name, (), ex.env)


class MmeSock:
    __slots__ = ("socket",)

    def __init__(self, socket):
        self.socket = socket


MME_NODE_UNARY = {
    "abs": "ABSOLUTE",
    "sign": "SIGN",
    "floor": "FLOOR",
    "ceil": "CEIL",
    "trunc": "TRUNC",
    "round": "ROUND",
    "frac": "FRACT",
    "sqrt": "SQRT",
    "rsqrt": "INVERSE_SQRT",
    "exp": "EXPONENT",
    "sin": "SINE",
    "cos": "COSINE",
    "tan": "TANGENT",
    "asin": "ARCSINE",
    "acos": "ARCCOSINE",
    "atan": "ARCTANGENT",
    "sinh": "SINH",
    "cosh": "COSH",
    "tanh": "TANH",
}


MME_MATH_LABELS = {
    "ADD": "+",
    "SUBTRACT": "-",
    "MULTIPLY": "*",
    "DIVIDE": "/",
}


class MmeTreeBackend(MmeGroupCalls):
    vectorized = False
    wants_text = True
    separate_color = "ShaderNodeSeparateColor"
    combine_color = "ShaderNodeCombineColor"

    def __init__(self, tree):
        self.tree = tree
        self.nodes = tree.nodes
        self.links = tree.links
        self.np = FxNumpyBackend()
        self.cache = {}
        self.keep = []
        self.block = None
        self.blocks = []
        self.block_of = {}
        self.scopes = []
        self.block_stack = []
        self.inputs = {}
        self.input_items = {}
        self.auto_named = set()
        self.boolish = set()
        self.input_nodes = []
        self.input_owner = {}

    def statement(self, text):
        if text is None:
            return
        if len(self.scopes) > 1:
            text = "%s: %s" % (self.scopes[-1], text)
        self.block = len(self.blocks)
        self.blocks.append(text)

    def enter(self, name):
        self.scopes.append(name)
        self.block_stack.append(self.block)

    def leave(self):
        if self.scopes:
            self.scopes.pop()
        if self.block_stack:
            self.block = self.block_stack.pop()

    def new(self, idname):
        node = self.nodes.new(idname)
        self.block_of[node.name] = self.block
        return node

    def const(self, x):
        return float(x)

    def known(self, x):
        if isinstance(x, MmeSock):
            return None
        return float(x)

    def any_true(self, x):
        raise FxUnsupported("data-dependent loop")

    def key(self, x):
        if isinstance(x, MmeSock):
            return ("s", x.socket.node.name, x.socket.identifier)
        value = float(x)
        return ("c", "nan" if value != value else value)

    def tag(self, x, label):
        if not isinstance(x, MmeSock):
            return
        node = x.socket.node
        if node.bl_idname == "NodeGroupInput":
            item = self.input_items.get(x.socket.identifier)
            if item is not None and item.identifier in self.auto_named:
                self.auto_named.discard(item.identifier)
                item.name = label[:MME_LABEL_LIMIT]
            return
        if not node.label:
            node.label = label[:MME_LABEL_LIMIT]

    def feed(self, socket, value):
        if isinstance(value, MmeSock):
            self.links.new(value.socket, socket)
            return
        value = float(value)
        if socket.type == "BOOLEAN":
            socket.default_value = value != 0.0
        elif socket.type == "INT":
            socket.default_value = int(value)
        elif socket.type in ("VECTOR", "RGBA"):
            socket.default_value = [value] * len(socket.default_value)
        else:
            socket.default_value = value

    def socket_of(self, x):
        if isinstance(x, MmeSock):
            return x.socket
        key = ("value", self.key(x))
        found = self.cache.get(key)
        if found is not None:
            return found.socket
        node = self.new("ShaderNodeValue")
        node.outputs[0].default_value = float(x)
        node.label = "%g" % float(x)
        result = MmeSock(node.outputs[0])
        self.cache[key] = result
        return result.socket

    def fold(self, fn, *args):
        if any(isinstance(a, MmeSock) for a in args):
            return None
        with np.errstate(all="ignore"):
            return float(fn(*[np.float64(a) for a in args]))

    def remember(self, key, socket, boolish=False):
        result = MmeSock(socket)
        self.cache[key] = result
        self.keep.append(result)
        if boolish:
            self.boolish.add(self.key(result))
        return result

    def math(self, op, *args, boolish=False):
        key = ("math", op) + tuple(self.key(a) for a in args)
        found = self.cache.get(key)
        if found is not None:
            return found
        node = self.new("ShaderNodeMath")
        node.operation = op
        if op == "COMPARE":
            node.inputs[2].default_value = 0.0
        for index, arg in enumerate(args):
            self.feed(node.inputs[index], arg)
        return self.remember(key, node.outputs[0], boolish)

    def int_math(self, op, a, b):
        key = ("imath", op, self.key(a), self.key(b))
        found = self.cache.get(key)
        if found is not None:
            return found
        node = self.new("FunctionNodeIntegerMath")
        node.operation = op
        self.feed(node.inputs[0], a)
        self.feed(node.inputs[1], b)
        return self.remember(key, node.outputs[0])

    def bool_math(self, op, *args):
        key = ("bmath", op) + tuple(self.key(a) for a in args)
        found = self.cache.get(key)
        if found is not None:
            return found
        node = self.new("FunctionNodeBooleanMath")
        node.operation = op
        for index, arg in enumerate(args):
            self.feed(node.inputs[index], arg)
        return self.remember(key, node.outputs[0], True)

    def ordered(self, a, b):
        return (a, b) if repr(self.key(a)) <= repr(self.key(b)) else (b, a)

    def add(self, a, b):
        folded = self.fold(self.np.add, a, b)
        if folded is not None:
            return folded
        if self.known(a) == 0.0:
            return b
        if self.known(b) == 0.0:
            return a
        return self.math("ADD", *self.ordered(a, b))

    def sub(self, a, b):
        folded = self.fold(self.np.sub, a, b)
        if folded is not None:
            return folded
        if self.known(b) == 0.0:
            return a
        if self.key(a) == self.key(b):
            return 0.0
        return self.math("SUBTRACT", a, b, boolish=self.known(a) == 1.0 and self.key(b) in self.boolish)

    def mul(self, a, b):
        folded = self.fold(self.np.mul, a, b)
        if folded is not None:
            return folded
        if self.known(a) == 1.0:
            return b
        if self.known(b) == 1.0:
            return a
        if self.known(a) == 0.0 or self.known(b) == 0.0:
            return 0.0
        both = self.key(a) in self.boolish and self.key(b) in self.boolish
        return self.math("MULTIPLY", *self.ordered(a, b), boolish=both)

    def div(self, a, b):
        folded = self.fold(self.np.div, a, b)
        if folded is not None:
            return folded
        if self.known(b) == 1.0:
            return a
        if self.known(a) == 0.0:
            return 0.0
        return self.math("DIVIDE", a, b)

    def neg(self, a):
        if not isinstance(a, MmeSock):
            return -float(a)
        return self.math("MULTIPLY", a, -1.0)

    def mod(self, a, b):
        folded = self.fold(self.np.mod, a, b)
        if folded is not None:
            return folded
        return self.math("MODULO", a, b)

    def imod(self, a, b):
        folded = self.fold(self.np.imod, a, b)
        if folded is not None:
            return folded
        return self.int_math("MODULO", a, b)

    def idiv(self, a, b):
        folded = self.fold(self.np.idiv, a, b)
        if folded is not None:
            return folded
        return self.int_math("DIVIDE", a, b)

    def pow(self, a, b):
        folded = self.fold(self.np.pow, a, b)
        if folded is not None:
            return folded
        if self.known(b) == 1.0:
            return a
        if self.known(b) == 0.0:
            return 1.0
        return self.math("POWER", a, b)

    def unary(self, name, a):
        folded = self.fold(lambda x: self.np.unary(name, x), a)
        if folded is not None:
            return folded
        if name == "exp2":
            return self.math("POWER", 2.0, a)
        if name == "log":
            return self.math("LOGARITHM", a, math.e)
        if name == "log2":
            return self.math("LOGARITHM", a, 2.0)
        if name == "log10":
            return self.math("LOGARITHM", a, 10.0)
        op = MME_NODE_UNARY.get(name)
        if op is None:
            raise FxUnsupported("function %s" % name)
        return self.math(op, a)

    def atan2(self, a, b):
        folded = self.fold(self.np.atan2, a, b)
        if folded is not None:
            return folded
        return self.math("ARCTAN2", a, b)

    def min(self, a, b):
        folded = self.fold(self.np.min, a, b)
        if folded is not None:
            return folded
        if self.key(a) == self.key(b):
            return a
        return self.math("MINIMUM", *self.ordered(a, b))

    def max(self, a, b):
        folded = self.fold(self.np.max, a, b)
        if folded is not None:
            return folded
        if self.key(a) == self.key(b):
            return a
        both = self.key(a) in self.boolish and self.key(b) in self.boolish
        return self.math("MAXIMUM", *self.ordered(a, b), boolish=both)

    def cmp(self, op, a, b):
        folded = self.fold(lambda x, y: self.np.cmp(op, x, y), a, b)
        if folded is not None:
            return folded
        if op == "<":
            return self.math("LESS_THAN", a, b, boolish=True)
        if op == ">":
            return self.math("GREATER_THAN", a, b, boolish=True)
        if op == "<=":
            return self.lnot(self.math("GREATER_THAN", a, b, boolish=True))
        if op == ">=":
            return self.lnot(self.math("LESS_THAN", a, b, boolish=True))
        equal = self.math("COMPARE", a, b, boolish=True)
        if op == "==":
            return equal
        return self.lnot(equal)

    def bitop(self, op, a, b):
        folded = self.fold(lambda x, y: self.np.bitop(op, x, y), a, b)
        if folded is not None:
            return folded
        raise FxUnsupported("bit operation in a pixel shader")

    def bitnot(self, a):
        folded = self.fold(self.np.bitnot, a)
        if folded is not None:
            return folded
        raise FxUnsupported("bit operation in a pixel shader")

    def truth(self, a):
        known = self.known(a)
        if known is not None:
            return 1.0 if known != 0.0 else 0.0
        if self.key(a) in self.boolish:
            return a
        return self.lnot(self.math("COMPARE", a, 0.0, boolish=True))

    def land(self, a, b):
        ta, tb = self.truth(a), self.truth(b)
        ka, kb = self.known(ta), self.known(tb)
        if ka is not None:
            return tb if ka else 0.0
        if kb is not None:
            return ta if kb else 0.0
        return self.bool_math("AND", *self.ordered(ta, tb))

    def lor(self, a, b):
        ta, tb = self.truth(a), self.truth(b)
        ka, kb = self.known(ta), self.known(tb)
        if ka is not None:
            return 1.0 if ka else tb
        if kb is not None:
            return 1.0 if kb else ta
        return self.bool_math("OR", *self.ordered(ta, tb))

    def lnot(self, a):
        known = self.known(a)
        if known is not None:
            return 0.0 if known != 0.0 else 1.0
        return self.bool_math("NOT", self.truth(a))

    def select(self, c, a, b):
        known = self.known(c)
        if known is not None:
            return a if known != 0.0 else b
        if self.key(a) == self.key(b):
            return a
        key = ("select", self.key(c), self.key(a), self.key(b))
        found = self.cache.get(key)
        if found is not None:
            return found
        node = self.new("GeometryNodeSwitch")
        node.input_type = "FLOAT"
        self.feed(node.inputs["Switch"], c)
        self.feed(node.inputs["False"], b)
        self.feed(node.inputs["True"], a)
        both = self.key(a) in self.boolish and self.key(b) in self.boolish
        return self.remember(key, node.outputs[0], both)

    def new_input(self, key, socket_type="NodeSocketFloat", label=None):
        found = self.inputs.get(key)
        if found is not None:
            return found
        item = self.tree.interface.new_socket(label or key, in_out="INPUT", socket_type=socket_type)
        if label is None:
            self.auto_named.add(item.identifier)
        node = self.new("NodeGroupInput")
        self.input_nodes.append(node)
        socket = next(s for s in node.outputs if s.identifier == item.identifier)
        result = MmeSock(socket)
        self.inputs[key] = result
        self.input_items[item.identifier] = item
        self.input_owner[key] = (node.name, item.identifier)
        return result

    def attribute(self, name, kind="GEOMETRY"):
        return self.new_input(name)

    def input_identifier(self, key):
        found = self.inputs.get(key)
        return found.socket.identifier if found is not None else None

    def finish(self, outputs):
        output = self.new("NodeGroupOutput")
        self.block_of[output.name] = "out"
        existing = {}
        for item in self.tree.interface.items_tree:
            if item.item_type == "SOCKET" and item.in_out == "OUTPUT":
                existing.setdefault((item.name, item.socket_type), item)
        for name, value, socket_type in outputs:
            item = existing.pop((name, socket_type), None)
            if item is None:
                item = self.tree.interface.new_socket(name, in_out="OUTPUT", socket_type=socket_type)
            target = next(s for s in output.inputs if s.identifier == item.identifier)
            if isinstance(value, (list, tuple)):
                combine = self.new(self.combine_color if socket_type == "NodeSocketColor" else "ShaderNodeCombineXYZ")
                self.block_of[combine.name] = "out"
                for k, comp in enumerate(value):
                    self.feed(combine.inputs[k], comp)
                self.links.new(combine.outputs[0], target)
            elif isinstance(value, MmeSock):
                self.links.new(value.socket, target)
            else:
                target.default_value = float(value)
                item.default_value = float(value)
        mme_prune_tree(self.tree, output)
        alive = {n.name for n in self.tree.nodes}
        for key in list(self.inputs):
            node_name, identifier = self.input_owner[key]
            if node_name not in alive:
                item = self.input_items.pop(identifier, None)
                if item is not None:
                    self.tree.interface.remove(item)
                del self.inputs[key]
        for node in self.tree.nodes:
            if node.bl_idname == "NodeGroupInput":
                for socket in node.outputs:
                    socket.hide = not socket.is_linked
        mme_layout(self.tree, self.block_of, self.blocks)
        return output


class MmePsBackend(MmeTreeBackend):
    separate_color = "ShaderNodeSeparateColor"
    tree_tag = "SH"
    group_idname = "ShaderNodeGroup"
    image_inputs = False

    def image_slot(self, slot, interpolation, extension, u, v):
        raise FxUnsupported("texture inside a shared shader function")

    def attribute(self, name, kind="GEOMETRY"):
        found = self.inputs.get(name)
        if found is None:
            found = self.new_input(name)
            self.kinds[name] = kind
        return found

    def __init__(self, tree):
        super().__init__(tree)
        self.kinds = {}

    def image_sample(self, image, interpolation, extension, u, v):
        key = ("img", image.name, interpolation, extension, self.key(u), self.key(v))
        found = self.cache.get(key)
        if found is not None:
            return found
        flipped = self.sub(1.0, v)
        combine = self.new("ShaderNodeCombineXYZ")
        self.feed(combine.inputs[0], u)
        self.feed(combine.inputs[1], flipped)
        combine.inputs[2].default_value = 0.0
        texture = self.new("ShaderNodeTexImage")
        texture.image = image
        texture.interpolation = interpolation
        texture.extension = extension
        texture.label = os.path.basename(image.filepath or image.name)
        self.links.new(combine.outputs[0], texture.inputs["Vector"])
        separate = self.new(self.separate_color)
        self.links.new(texture.outputs["Color"], separate.inputs[0])
        result = [MmeSock(separate.outputs[k]) for k in range(3)]
        result.append(MmeSock(texture.outputs["Alpha"]))
        self.cache[key] = result
        self.keep.extend(result)
        return result


class MmeGeoBackend(MmeTreeBackend):
    separate_color = "FunctionNodeSeparateColor"
    combine_color = "FunctionNodeCombineColor"
    tree_tag = "GN"
    group_idname = "GeometryNodeGroup"
    image_inputs = True

    def image_slot(self, slot, interpolation, extension, u, v):
        key = ("imgslot", slot, interpolation, extension, self.key(u), self.key(v))
        found = self.cache.get(key)
        if found is not None:
            return found
        source = self.new_input("tex:" + slot, "NodeSocketImage", "tex:" + slot)
        flipped = self.sub(1.0, v)
        combine = self.new("ShaderNodeCombineXYZ")
        self.feed(combine.inputs[0], u)
        self.feed(combine.inputs[1], flipped)
        combine.inputs[2].default_value = 0.0
        texture = self.new("GeometryNodeImageTexture")
        texture.interpolation = interpolation
        texture.extension = extension
        texture.label = slot
        self.links.new(source.socket, texture.inputs["Image"])
        self.links.new(combine.outputs[0], texture.inputs["Vector"])
        separate = self.new(self.separate_color)
        self.links.new(texture.outputs["Color"], separate.inputs[0])
        result = [MmeSock(separate.outputs[k]) for k in range(3)]
        result.append(MmeSock(texture.outputs["Alpha"]))
        self.cache[key] = result
        return result

    def image_sample(self, image, interpolation, extension, u, v):
        key = ("img", image.name, interpolation, extension, self.key(u), self.key(v))
        found = self.cache.get(key)
        if found is not None:
            return found
        flipped = self.sub(1.0, v)
        combine = self.new("ShaderNodeCombineXYZ")
        self.feed(combine.inputs[0], u)
        self.feed(combine.inputs[1], flipped)
        combine.inputs[2].default_value = 0.0
        texture = self.new("GeometryNodeImageTexture")
        texture.inputs["Image"].default_value = image
        texture.interpolation = interpolation
        texture.extension = extension
        texture.label = os.path.basename(image.filepath or image.name)
        self.links.new(combine.outputs[0], texture.inputs["Vector"])
        separate = self.new(self.separate_color)
        self.links.new(texture.outputs["Color"], separate.inputs[0])
        result = [MmeSock(separate.outputs[k]) for k in range(3)]
        result.append(MmeSock(texture.outputs["Alpha"]))
        self.cache[key] = result
        self.keep.extend(result)
        return result

    def bit_math(self, op, a, b=None):
        key = ("bit", op, self.key(a), self.key(b) if b is not None else None)
        found = self.cache.get(key)
        if found is not None:
            return found
        node = self.new("FunctionNodeBitMath")
        node.operation = op
        self.feed(node.inputs["A"], a)
        if b is not None:
            self.feed(node.inputs["Shift" if op in ("SHIFT", "ROTATE") else "B"], b)
        return self.remember(key, node.outputs[0])

    def bitop(self, op, a, b):
        folded = self.fold(lambda x, y: self.np.bitop(op, x, y), a, b)
        if folded is not None:
            return folded
        if op == "&":
            return self.bit_math("AND", a, b)
        if op == "|":
            return self.bit_math("OR", a, b)
        if op == "^":
            return self.bit_math("XOR", a, b)
        if op == "<<":
            return self.bit_math("SHIFT", a, b)
        return self.bit_math("SHIFT", a, self.neg(b))

    def bitnot(self, a):
        folded = self.fold(self.np.bitnot, a)
        if folded is not None:
            return folded
        return self.bit_math("NOT", a)

    def matrix_input(self, key, label):
        found = self.cache.get(("matrix", key))
        if found is not None:
            return found
        source = self.new_input(key, "NodeSocketMatrix", label)
        separate = self.new("FunctionNodeSeparateMatrix")
        separate.label = label
        self.links.new(source.socket, separate.inputs[0])
        comps = [MmeSock(separate.outputs[k]) for k in range(16)]
        self.cache[("matrix", key)] = comps
        return comps


def mme_prune_tree(tree, output=None):
    if output is None:
        output = next((n for n in tree.nodes if n.bl_idname in ("ShaderNodeOutputMaterial", "NodeGroupOutput")), None)
    if output is None:
        return
    upstream = {}
    for link in tree.links:
        upstream.setdefault(link.to_node.name, set()).add(link.from_node.name)
    keep = set()
    stack = [output.name]
    while stack:
        name = stack.pop()
        if name in keep:
            continue
        keep.add(name)
        stack.extend(upstream.get(name, ()))
    for node in [n for n in tree.nodes if n.name not in keep and n.bl_idname != "NodeFrame"]:
        tree.nodes.remove(node)


MME_NODE_HEIGHTS = {
    "ShaderNodeMath": 150.0,
    "FunctionNodeIntegerMath": 150.0,
    "FunctionNodeBooleanMath": 130.0,
    "FunctionNodeBitMath": 150.0,
    "GeometryNodeSwitch": 150.0,
    "ShaderNodeValue": 90.0,
    "ShaderNodeCombineXYZ": 130.0,
    "ShaderNodeSeparateXYZ": 130.0,
    "ShaderNodeSeparateColor": 150.0,
    "FunctionNodeSeparateColor": 150.0,
    "ShaderNodeTexImage": 280.0,
    "GeometryNodeImageTexture": 260.0,
    "NodeGroupInput": 70.0,
}


def mme_node_size(node):
    height = MME_NODE_HEIGHTS.get(node.bl_idname)
    if height is None:
        rows = sum(1 for s in node.inputs if s.enabled and not s.hide)
        rows += sum(1 for s in node.outputs if s.enabled and not s.hide)
        height = 36.0 + 22.0 * rows
    return max(node.width, 140.0), height


def mme_layout(tree, block_of, labels):
    nodes = [n for n in tree.nodes if n.bl_idname != "NodeFrame"]
    if not nodes:
        return
    names = {n.name: n for n in nodes}
    feeds = {n.name: set() for n in nodes}
    for link in tree.links:
        if link.from_node.name in names and link.to_node.name in names:
            feeds[link.to_node.name].add(link.from_node.name)
    group_of = {}
    for node in nodes:
        block = block_of.get(node.name)
        group_of[node.name] = "pre" if block is None else block
    block_feeds = {}
    for name, sources in feeds.items():
        mine = group_of[name]
        block_feeds.setdefault(mine, set())
        for source in sources:
            if group_of[source] != mine:
                block_feeds[mine].add(group_of[source])
    level = {}

    def block_level(block, trail=()):
        if block in level:
            return level[block]
        if block in trail:
            return 0
        found = 0
        for source in block_feeds.get(block, ()):
            found = max(found, block_level(source, trail + (block,)) + 1)
        level[block] = found
        return found

    for block in block_feeds:
        block_level(block)
    if "out" in level:
        level["out"] = max(level.values()) + (0 if len(level) == 1 else 1)
    local = {}

    def local_depth(name, trail=()):
        if name in local:
            return local[name]
        found = 0
        for source in feeds[name]:
            if group_of[source] == group_of[name] and source not in trail:
                found = max(found, local_depth(source, trail + (name,)) + 1)
        local[name] = found
        return found

    members = {}
    for node in nodes:
        local_depth(node.name)
        members.setdefault(group_of[node.name], []).append(node)
    sizes = {n.name: mme_node_size(n) for n in nodes}
    shapes = {}
    for block, block_nodes in members.items():
        columns = {}
        for node in block_nodes:
            columns.setdefault(local[node.name], []).append(node)
        widths = []
        heights = []
        for depth in sorted(columns):
            widths.append(max(sizes[n.name][0] for n in columns[depth]))
            heights.append(sum(sizes[n.name][1] + MME_NODE_GAP_Y for n in columns[depth]))
        shapes[block] = (columns, widths, sum(widths) + MME_NODE_GAP_X * (len(widths) - 1), max(heights))
    by_level = {}
    for block in members:
        by_level.setdefault(level.get(block, 0), []).append(block)

    def order(block):
        if block == "pre":
            return -1
        if block == "out":
            return 1 << 30
        return block

    x = 0.0
    frames = []
    where = {}
    for lvl in sorted(by_level):
        blocks = sorted(by_level[lvl], key=order)
        column_width = max(shapes[b][2] for b in blocks)
        y = 0.0
        for block in blocks:
            columns, widths, width, height = shapes[block]
            label = labels[block] if isinstance(block, int) and block < len(labels) else None
            framed = label is not None and len(members[block]) > 1
            top = y - (40.0 if framed else 0.0)
            cx = x
            for depth, column_w in zip(sorted(columns), widths):
                cy = top
                for node in columns[depth]:
                    where[node.name] = (cx, cy)
                    cy -= sizes[node.name][1] + MME_NODE_GAP_Y
                cx += column_w + MME_NODE_GAP_X
            if framed:
                frames.append((label, members[block]))
            elif label is not None and members[block] and not members[block][0].label and members[block][0].bl_idname != "NodeGroupInput":
                members[block][0].label = label
            y = top - height - MME_BLOCK_GAP_Y
        x += column_width + MME_BLOCK_GAP_X
    for label, block_nodes in frames:
        frame = tree.nodes.new("NodeFrame")
        frame.label = label
        frame.label_size = 14
        frame.shrink = True
        for node in block_nodes:
            node.parent = frame
    flat = []
    for node in tree.nodes:
        flat.extend(where.get(node.name, (0.0, 0.0)))
    tree.nodes.foreach_set("location", flat)


MME_NODE_GAP_X = 40.0


MME_NODE_GAP_Y = 16.0


MME_BLOCK_GAP_X = 120.0


MME_BLOCK_GAP_Y = 70.0


def mme_prop_name(prefix, name, index=None):
    text = prefix + name if index is None else "%s%s_%d" % (prefix, name, index)
    if len(text.encode("utf-8")) > 63:
        digest = hashlib.md5(text.encode("utf-8")).hexdigest()[:10]
        text = (prefix + digest) if index is None else "%s%s_%d" % (prefix, digest, index)
    return text


def mme_varying_name(semantic, index):
    return "%s%s_%d" % (MME_VARYING_PREFIX, semantic, index)


def mme_effect_image(path):
    norm = os.path.normcase(os.path.abspath(path))
    for image in bpy.data.images:
        if image.get("mme_path") == norm:
            return image
    image = bpy.data.images.load(path, check_existing=False)
    image.name = MME_IMAGE_PREFIX + os.path.basename(path)
    image["mme_path"] = norm
    image.colorspace_settings.name = "Non-Color"
    image.alpha_mode = "CHANNEL_PACKED"
    return image


def mme_load_pixels(path):
    image = mme_effect_image(path)
    width, height = image.size
    if width == 0 or height == 0:
        raise FxError("image has no pixels: %s" % path)
    buffer = np.empty(width * height * 4, dtype=np.float32)
    image.pixels.foreach_get(buffer)
    data = buffer.reshape(height, width, 4)[::-1].astype(np.float64)
    return FxImage(data, path)


def mme_texture_path(env, info):
    g = fx_sampler_texture_global(env.program, info)
    if fx_is_render_target(g):
        return None, True
    material_texture = env.state.material.get("texture", "")
    if g is None and info is not None and info.texture is None:
        path = material_texture if (info.register or "").lower().replace(" ", "") in ("s0", "") else None
    else:
        path = env.textures.resource_path(g, material_texture)
    return path, False


def mme_node_sample(env, ex, info, u, v):
    path, target = mme_texture_path(env, info)
    if target:
        if env.rt_lookup is not None:
            g = fx_sampler_texture_global(env.program, info)
            return env.rt_lookup(ex, g.name)
        return [0.0, 0.0, 0.0, 0.0]
    if not path:
        return [0.0, 0.0, 0.0, 0.0]
    try:
        image = mme_effect_image(path)
    except RuntimeError:
        return [0.0, 0.0, 0.0, 0.0]
    point = info.point() if info else False
    address = info.address() if info else "WRAP"
    extension = {"CLAMP": "EXTEND", "MIRROR": "MIRROR"}.get(address, "REPEAT")
    return ex.B.image_sample(image, "Closest" if point else "Linear", extension, u, v)


class MmeNodeEnv:
    def __init__(self, program, state, textures, controls_runtime):
        self.program = program
        self.state = state
        self.textures = textures
        self.controls_runtime = controls_runtime
        self.uniforms = {}
        self.rt_lookup = None

    def global_value(self, ex, g):
        source = fx_classify_global(g, self.program)
        if source is None:
            return None
        t = fx_resolve_type(g.type, self.program)
        if not fx_is_numeric(t):
            return None
        values = self.state.values(source, g)
        runtime = fx_source_is_runtime(source)
        if source[0] == "control" and g.name not in self.controls_runtime:
            runtime = False
        if not runtime:
            return fx_value_from_list(ex, values, t)
        comps = []
        for index in range(len(values)):
            prop = mme_prop_name(MME_UNIFORM_PREFIX, g.name, index)
            self.uniforms[prop] = (g.name, index)
            comps.append(ex.B.attribute(prop, "OBJECT"))
        dims = (4, 4) if len(comps) == 16 else (() if len(comps) == 1 else (len(comps),))
        return ex.convert(FxValue(("f", dims), comps), t)

    def sample(self, ex, info, u, v, lod):
        return mme_node_sample(self, ex, info, u, v)


def mme_ps_inputs(vs_outputs):
    def provide(ex):
        def get(semantic, t):
            if semantic in vs_outputs:
                count = vs_outputs[semantic]
                comps = [ex.B.attribute(mme_varying_name(semantic, k)) for k in range(count)]
            elif semantic == "VFACE":
                comps = [1.0]
            else:
                comps = [0.0, 0.0, 0.0, 0.0]
            dims = () if len(comps) == 1 else (len(comps),)
            return FxValue(("f", dims), comps)

        return get

    return provide


def mme_blend_mode(states):
    def state(name, default):
        value = states.get(name)
        if value is None:
            return default
        return str(value).replace(" ", "").upper()

    if state("alphablendenable", "TRUE") in ("FALSE", "0"):
        return "OPAQUE"
    src = state("srcblend", "SRCALPHA")
    dst = state("destblend", "INVSRCALPHA")
    op = state("blendop", "ADD")
    if op not in ("ADD", "1"):
        return "ALPHA"
    if dst == "ONE":
        return "ADD_ALPHA" if src == "SRCALPHA" else "ADD"
    if src == "ZERO" and dst == "SRCCOLOR" or src == "DESTCOLOR" and dst == "ZERO":
        return "MULTIPLY"
    if src == "ONE" and dst == "INVSRCALPHA":
        return "PREMULTIPLIED"
    if src == "ONE" and dst == "ZERO":
        return "OPAQUE"
    return "ALPHA"


MME_MODE_LABELS = {
    "OPAQUE": "Opaque",
    "ALPHA": "Alpha blend (SRCALPHA, INVSRCALPHA)",
    "ADD": "Additive (ONE, ONE)",
    "ADD_ALPHA": "Additive (SRCALPHA, ONE)",
    "PREMULTIPLIED": "Premultiplied (ONE, INVSRCALPHA)",
    "MULTIPLY": "Multiply",
}


def mme_math_node(nodes, op, label, a=None, b=None, clamp=False):
    node = nodes.new("ShaderNodeMath")
    node.operation = op
    node.label = label
    node.use_clamp = clamp
    for index, value in enumerate((a, b)):
        if value is None:
            continue
        if isinstance(value, (int, float)):
            node.inputs[index].default_value = float(value)
        else:
            node.id_data.links.new(value, node.inputs[index])
    return node


def mme_shader_post(tree, group_node, mode, x0):
    nodes, links = tree.nodes, tree.links
    color = group_node.outputs["Color"]
    alpha = group_node.outputs["Alpha"]
    frame = nodes.new("NodeFrame")
    frame.label = "MMD blending: %s" % MME_MODE_LABELS.get(mode, mode)
    frame.label_size = 14
    placed = []

    def put(node, x, y):
        node.location = (x0 + x, y)
        placed.append(node)
        return node

    cull = put(nodes.new("ShaderNodeAttribute"), 0, -260)
    cull.attribute_name = MME_ATTR_CULL
    cull.label = "Culled vertex (mme_cull)"
    visible = put(mme_math_node(nodes, "SUBTRACT", "Visible = 1 - culled", 1.0, cull.outputs["Fac"]), 220, -260)
    final_alpha = put(mme_math_node(nodes, "MULTIPLY", "Alpha", alpha, visible.outputs[0], clamp=True), 420, -120)
    if mode == "ADD_ALPHA":
        scaled = put(nodes.new("ShaderNodeMix"), 420, 80)
        scaled.data_type = "RGBA"
        scaled.blend_type = "MULTIPLY"
        scaled.label = "Color * Alpha"
        scaled.inputs["Factor"].default_value = 1.0
        links.new(color, scaled.inputs["A"])
        links.new(final_alpha.outputs[0], scaled.inputs["B"])
        color = scaled.outputs["Result"]
    elif mode == "ADD":
        scaled = put(nodes.new("ShaderNodeMix"), 420, 80)
        scaled.data_type = "RGBA"
        scaled.blend_type = "MULTIPLY"
        scaled.label = "Color * Visible"
        scaled.inputs["Factor"].default_value = 1.0
        links.new(color, scaled.inputs["A"])
        links.new(visible.outputs[0], scaled.inputs["B"])
        color = scaled.outputs["Result"]
    gamma = put(nodes.new("ShaderNodeGamma"), 640, 80)
    gamma.label = "MMD gamma"
    gamma.inputs["Gamma"].default_value = MME_GAMMA
    links.new(color, gamma.inputs["Color"])
    output = nodes.new("ShaderNodeOutputMaterial")
    output.location = (x0 + 1300, 0)
    if mode == "MULTIPLY":
        transparent = put(nodes.new("ShaderNodeBsdfTransparent"), 860, 0)
        links.new(gamma.outputs[0], transparent.inputs["Color"])
        links.new(transparent.outputs[0], output.inputs["Surface"])
    else:
        emission = put(nodes.new("ShaderNodeEmission"), 860, 80)
        emission.inputs["Strength"].default_value = 1.0
        links.new(gamma.outputs[0], emission.inputs["Color"])
        if mode == "OPAQUE":
            links.new(emission.outputs[0], output.inputs["Surface"])
        else:
            transparent = put(nodes.new("ShaderNodeBsdfTransparent"), 860, -80)
            if mode in ("ADD", "ADD_ALPHA"):
                shader = put(nodes.new("ShaderNodeAddShader"), 1080, 0)
                links.new(transparent.outputs[0], shader.inputs[0])
                links.new(emission.outputs[0], shader.inputs[1])
            elif mode == "PREMULTIPLIED":
                mix = put(nodes.new("ShaderNodeMixShader"), 1080, -60)
                links.new(final_alpha.outputs[0], mix.inputs[0])
                black = put(nodes.new("ShaderNodeEmission"), 860, -200)
                black.inputs["Color"].default_value = (0.0, 0.0, 0.0, 1.0)
                links.new(transparent.outputs[0], mix.inputs[1])
                links.new(black.outputs[0], mix.inputs[2])
                shader = put(nodes.new("ShaderNodeAddShader"), 1200, 0)
                links.new(mix.outputs[0], shader.inputs[0])
                links.new(emission.outputs[0], shader.inputs[1])
                output.location = (x0 + 1420, 0)
            else:
                shader = put(nodes.new("ShaderNodeMixShader"), 1080, 0)
                links.new(final_alpha.outputs[0], shader.inputs[0])
                links.new(transparent.outputs[0], shader.inputs[1])
                links.new(emission.outputs[0], shader.inputs[2])
            links.new(shader.outputs[0], output.inputs["Surface"])
    for node in placed:
        where = tuple(node.location)
        node.parent = frame
        node.location_absolute = where


def mme_material_setup(mat, mode, states):
    blended = mode not in ("OPAQUE",)
    mat.surface_render_method = "BLENDED" if blended else "DITHERED"
    cull = str(states.get("cullmode", "")).replace(" ", "").upper()
    mat.use_backface_culling = cull in ("CCW", "3")
    mat.use_transparency_overlap = True
    mat.show_transparent_back = True
    mat.diffuse_color = (1.0, 0.55, 0.2, 0.3)


def mme_vs_signature(fx_pass):
    return repr(fx_pass.vs)


def mme_tree_signature(tree):
    parts = []
    for node in sorted(tree.nodes, key=lambda n: n.name):
        parts.append((node.bl_idname, node.name, node.label))
        for socket in node.inputs:
            if socket.is_linked or not hasattr(socket, "default_value"):
                continue
            value = socket.default_value
            try:
                value = tuple(round(float(v), 5) for v in value)
            except TypeError:
                try:
                    value = round(float(value), 5)
                except (TypeError, ValueError):
                    value = str(value)
            parts.append((socket.identifier, value))
    for link in tree.links:
        parts.append((link.from_node.name, link.from_socket.identifier, link.to_node.name, link.to_socket.identifier))
    return hashlib.md5(repr(sorted(parts, key=repr)).encode("utf-8")).hexdigest()


def mme_owned(collection, owner):
    for item in collection:
        if item.get(MME_OWNER_PROP) == owner:
            return item
    return None


def mme_reset_group(group, keep_outputs=True):
    for node in list(group.nodes):
        group.nodes.remove(node)
    for item in list(group.interface.items_tree):
        if item.item_type != "SOCKET":
            continue
        if keep_outputs and item.in_out == "OUTPUT":
            continue
        group.interface.remove(item)


def mme_group_outputs(group, wanted):
    for item in list(group.interface.items_tree):
        if item.item_type == "SOCKET" and item.in_out == "OUTPUT":
            if (item.name, item.socket_type) not in wanted:
                group.interface.remove(item)


MME_INPUT_NODE_PREFIX = "mmeIn_"


def mme_friendly_input(key, item_name):
    if key.startswith(MME_VARYING_PREFIX):
        return "Vertex output %s" % item_name
    if key.startswith(MME_LOOP_PREFIX):
        return "Loop index %s" % key[len(MME_LOOP_PREFIX):]
    return item_name


def mme_wire_inputs(tree, group_node, B):
    nodes, links = tree.nodes, tree.links
    for node in [n for n in nodes if n.name.startswith(MME_INPUT_NODE_PREFIX)]:
        nodes.remove(node)
    x, y = group_node.location_absolute
    row = 0
    for key, sock in B.inputs.items():
        identifier = sock.socket.identifier
        target = next((s for s in group_node.inputs if s.identifier == identifier), None)
        if target is None:
            continue
        attr = nodes.new("ShaderNodeAttribute")
        attr.name = MME_INPUT_NODE_PREFIX + key
        attr.attribute_type = B.kinds.get(key, "GEOMETRY")
        attr.attribute_name = key
        attr.label = mme_friendly_input(key, target.name)
        attr.hide = True
        attr.location = (x - 320, y - row * 40)
        links.new(attr.outputs["Fac"], target)
        row += 1


def mme_compile_material(name, runtime, draw, state, vs_outputs, owner=None):
    program = runtime.program
    mat = mme_owned(bpy.data.materials, owner) if owner else None
    edited = mat is not None and mat.get(MME_SIG_PROP) != mme_tree_signature(mat.node_tree)
    if mat is None:
        mat = bpy.data.materials.new(name)
        if owner:
            mat[MME_OWNER_PROP] = owner
    group = bpy.data.node_groups.get(mat.get(MME_GROUP_PROP, ""))
    if group is None or group.bl_idname != "ShaderNodeTree":
        group = bpy.data.node_groups.new("MME PS " + name, "ShaderNodeTree")
        mat[MME_GROUP_PROP] = group.name
    group[MME_OWNER_PROP] = owner or name
    mme_reset_group(group)
    mme_group_outputs(group, {("Color", "NodeSocketColor"), ("Alpha", "NodeSocketFloat")})
    B = MmePsBackend(group)
    env = MmeNodeEnv(program, state, runtime.textures, runtime.controls_runtime)
    provider = mme_ps_inputs(vs_outputs)
    loops = runtime.loop_overrides_nodes(draw, B)
    visiting = set()

    def run_ps(fx_pass):
        function, args = fx_entry(program, fx_pass.ps)
        if function is None:
            raise FxUnsupported("pass %s has no pixel shader" % fx_pass.name)
        ex = FxExec(program, B, env)
        ex.overrides.update(loops)
        outputs = fx_run_entry(ex, function, args, provider(ex))
        color = outputs.get("COLOR0")
        if color is None:
            return [0.0, 0.0, 0.0, 1.0], ex.discard
        comps = ex.convert(color, ("f", (4,))).c
        return comps, ex.discard

    def rt_lookup(ex, texture_name):
        writers = [
            d
            for d in runtime.draws
            if d.target == texture_name
            and d.kind == "geometry"
            and d.subset == draw.subset
            and d.order < draw.order
            and mme_vs_signature(d.fx_pass) == mme_vs_signature(draw.fx_pass)
            and [(l[0], l[1]) for l in d.loops] == [(l[0], l[1]) for l in draw.loops]
        ]
        if not writers or texture_name in visiting:
            return [0.0, 0.0, 0.0, 0.0]
        visiting.add(texture_name)
        try:
            comps, _discard = run_ps(writers[-1].fx_pass)
        finally:
            visiting.discard(texture_name)
        return list(comps)

    env.rt_lookup = rt_lookup
    try:
        color, discard = run_ps(draw.fx_pass)
        r, g, b, a = (list(color) + [0.0, 0.0, 0.0, 1.0])[:4]
        alpha = B.min(B.max(a, 0.0), 1.0)
        if B.known(discard) is None or B.known(discard) != 0.0:
            alpha = B.mul(alpha, B.sub(1.0, discard))
        B.statement("output color and alpha")
        B.finish([("Color", [r, g, b], "NodeSocketColor"), ("Alpha", alpha, "NodeSocketFloat")])
    except Exception:
        if not edited and mat.users == 0:
            bpy.data.materials.remove(mat)
            bpy.data.node_groups.remove(group)
        raise
    mode = mme_blend_mode(draw.fx_pass.states)
    tree = mat.node_tree
    group_node = next((n for n in tree.nodes if n.bl_idname == "ShaderNodeGroup" and n.node_tree == group), None)
    if not edited or group_node is None:
        for node in list(tree.nodes):
            tree.nodes.remove(node)
        group_node = tree.nodes.new("ShaderNodeGroup")
        group_node.node_tree = group
        group_node.name = "MME Pixel Shader"
        group_node.label = "%s (%s)" % (draw.fx_pass.name or "pass", fx_entry(program, draw.fx_pass.ps)[0].name)
        group_node.width = 240
        group_node.location = (0, 0)
        mme_shader_post(tree, group_node, mode, 340)
        mme_material_setup(mat, mode, draw.fx_pass.states)
    else:
        log.info("material %s was edited; only its pixel shader group was regenerated", mat.name)
    mme_wire_inputs(tree, group_node, B)
    mat[MME_SIG_PROP] = mme_tree_signature(tree)
    varyings = sorted(k for k in B.inputs if k.startswith(MME_VARYING_PREFIX) and B.kinds.get(k) == "GEOMETRY")
    uniforms = [[k, env.uniforms[k][0], env.uniforms[k][1]] for k in B.inputs if k in env.uniforms]
    loop_attrs = sorted(k for k in B.inputs if k.startswith(MME_LOOP_PREFIX))
    return mat, varyings, uniforms, mode, loop_attrs, len(group.nodes)


def mme_fallback_material(name, runtime, subset, mode="ALPHA", reason="", owner=None):
    xmat = runtime.materials[subset] if subset < len(runtime.materials) else None
    mat = mme_owned(bpy.data.materials, owner) if owner else None
    if mat is None:
        mat = bpy.data.materials.new(name)
        if owner:
            mat[MME_OWNER_PROP] = owner
    group = bpy.data.node_groups.get(mat.get(MME_GROUP_PROP, ""))
    if group is None or group.bl_idname != "ShaderNodeTree":
        group = bpy.data.node_groups.new("MME PS " + name, "ShaderNodeTree")
        mat[MME_GROUP_PROP] = group.name
    group[MME_OWNER_PROP] = owner or name
    mme_reset_group(group)
    mme_group_outputs(group, {("Color", "NodeSocketColor"), ("Alpha", "NodeSocketFloat")})
    B = MmePsBackend(group)
    B.statement("fallback: material diffuse x texture")
    color = [1.0, 1.0, 1.0, 1.0]
    if xmat is not None:
        color = list(xmat.diffuse)
        texture = xmat.texture.split("*")[0].strip() if xmat.texture else ""
        found = resolve_x_texture(texture, runtime.x_folder) if texture else None
        if found:
            sampled = B.image_sample(mme_effect_image(found), "Linear", "REPEAT", B.attribute(mme_varying_name("TEXCOORD0", 0)), B.attribute(mme_varying_name("TEXCOORD0", 1)))
            color = [B.mul(c, s) for c, s in zip(color, sampled)]
    B.finish([("Color", color[:3], "NodeSocketColor"), ("Alpha", color[3], "NodeSocketFloat")])
    if reason:
        warning = group.nodes.new("GeometryNodeWarning")
        warning.warning_type = "WARNING"
        warning.inputs["Message"].default_value = ("Pixel shader not converted: %s" % reason)[:200]
        warning.location = (0, 200)
    tree = mat.node_tree
    for node in list(tree.nodes):
        tree.nodes.remove(node)
    group_node = tree.nodes.new("ShaderNodeGroup")
    group_node.node_tree = group
    group_node.name = "MME Pixel Shader"
    group_node.label = "Fallback (pixel shader not converted)"
    group_node.width = 240
    mme_shader_post(tree, group_node, mode, 340)
    mme_wire_inputs(tree, group_node, B)
    mme_material_setup(mat, mode, {})
    mat[MME_SIG_PROP] = mme_tree_signature(tree)
    return mat


class MmeGeoEnv:
    def __init__(self, program, state, textures, controls, needs):
        self.program = program
        self.state = state
        self.textures = textures
        self.controls = controls
        self.needs = needs
        self.rt_lookup = None

    def global_value(self, ex, g):
        source = fx_classify_global(g, self.program)
        if source is None:
            return None
        t = fx_resolve_type(g.type, self.program)
        if not fx_is_numeric(t):
            return None
        values = self.state.values(source, g)
        runtime = fx_source_is_runtime(source)
        if source[0] == "control" and self.controls.get(g.name, {}).get("kind", "const") == "const":
            runtime = False
        if not runtime:
            return fx_value_from_list(ex, values, t)
        B = ex.B
        if source[0] == "matrix" or len(values) == 16:
            key = "m:" + g.name
            self.needs[key] = ("matrix", source, g.name)
            comps = B.matrix_input(key, g.name)
            return ex.convert(FxValue(("f", (4, 4)), comps), t)
        comps = []
        for k in range(len(values)):
            key = "u:%s:%d" % (g.name, k)
            self.needs[key] = ("scalar", source, g.name, k, len(values))
            comps.append(B.new_input(key))
        dims = () if len(comps) == 1 else (len(comps),)
        return ex.convert(FxValue(("f", dims), comps), t)

    def sample(self, ex, info, u, v, lod):
        return mme_node_sample(self, ex, info, u, v)


def mme_geo_vertex_inputs(needs):
    def provide(ex):
        B = ex.B

        def attr(name, k):
            key = "v:%s:%d" % (name, k)
            needs[key] = ("vertex", name, k)
            return B.new_input(key)

        def get(semantic, t):
            if semantic == "POSITION0":
                comps = [attr(MME_ATTR_POS, k) for k in range(3)] + [1.0]
            elif semantic == "NORMAL0":
                comps = [attr(MME_ATTR_NRM, k) for k in range(3)] + [0.0]
            elif semantic == "TEXCOORD0":
                comps = [attr(MME_ATTR_UV, 0), attr(MME_ATTR_UV, 1), 0.0, 1.0]
            elif semantic in ("_INDEX", "INDEX"):
                comps = [attr(MME_ATTR_SRC, 0)]
            elif semantic in ("COLOR0", "COLOR1"):
                comps = [1.0, 1.0, 1.0, 1.0]
            else:
                comps = [0.0, 0.0, 0.0, 1.0]
            if not fx_is_numeric(t):
                return ex.zero(t)
            count = fx_count(t[1])
            comps = (comps + [0.0] * 16)[:count]
            return FxValue(("f", t[1]), comps)

        return get

    return provide


def mme_np_comps(matrix):
    return [float(x) for x in np.asarray(matrix, dtype=np.float64).reshape(-1)]


class MmeGeoScene:
    def __init__(self, tree, root, unit, scene, state, controls, program):
        self.tree = tree
        self.B = MmeGeoBackend(tree)
        self.root = root
        self.unit = unit
        self.scene = scene
        self.state = state
        self.controls = controls
        self.program = program
        self.cache = {}
        self.drivers = []
        self.defaults = {}

    def node(self, idname, label=None, **props):
        node = self.B.new(idname)
        for key, value in props.items():
            setattr(node, key, value)
        if label:
            node.label = label
        return node

    def link(self, a, b):
        self.tree.links.new(a.socket if isinstance(a, MmeSock) else a, b)

    def sock(self, socket):
        return MmeSock(socket)

    def driven(self, key, label, spec, default=0.0):
        found = self.B.inputs.get(key)
        if found is not None:
            return found
        sock = self.B.new_input(key, "NodeSocketFloat", label)
        item = self.B.input_items[sock.socket.identifier]
        item.default_value = float(default)
        self.defaults[sock.socket.identifier] = float(default)
        self.drivers.append((sock.socket.identifier, spec))
        return sock

    def vec_node(self, op, a, b=None, label=None):
        node = self.node("ShaderNodeVectorMath", label, operation=op)
        for index, value in enumerate((a, b)):
            if value is None:
                continue
            if isinstance(value, (tuple, list)):
                node.inputs[index].default_value = value
            else:
                self.link(value, node.inputs[index])
        out = node.outputs["Value"] if op in ("DOT_PRODUCT", "LENGTH", "DISTANCE") else node.outputs["Vector"]
        return out

    def scale(self, vec, factor):
        node = self.node("ShaderNodeVectorMath", operation="SCALE")
        self.link(vec, node.inputs[0])
        self.B.feed(node.inputs["Scale"], factor)
        return node.outputs["Vector"]

    def split(self, vec):
        node = self.node("ShaderNodeSeparateXYZ")
        self.link(vec, node.inputs[0])
        return [MmeSock(node.outputs[k]) for k in range(3)]

    def combine(self, comps, label=None):
        node = self.node("ShaderNodeCombineXYZ", label)
        for k, comp in enumerate(comps):
            self.B.feed(node.inputs[k], comp)
        return node.outputs[0]

    def swap(self, vec):
        x, y, z = self.split(vec)
        return self.combine([x, z, y], "Blender to MMD axes")

    def const_matrix(self, matrix, label):
        key = ("cmat", tuple(round(v, 9) for v in np.asarray(matrix).reshape(-1)))
        if key in self.cache:
            return self.cache[key]
        node = self.node("FunctionNodeCombineMatrix", label)
        m = np.asarray(matrix, dtype=np.float64)
        for c in range(4):
            for r in range(4):
                node.inputs[c * 4 + r].default_value = float(m[r][c])
        self.cache[key] = node.outputs[0]
        return node.outputs[0]

    def combine_matrix(self, row_comps, label):
        node = self.node("FunctionNodeCombineMatrix", label)
        for k, comp in enumerate(row_comps):
            self.B.feed(node.inputs[k], comp)
        return node.outputs[0]

    def mat_mul(self, *matrices):
        result = matrices[0]
        for other in matrices[1:]:
            node = self.node("FunctionNodeMatrixMultiply")
            self.link(result, node.inputs[0])
            self.link(other, node.inputs[1])
            result = node.outputs[0]
        return result

    def invert(self, matrix):
        node = self.node("FunctionNodeInvertMatrix")
        self.link(matrix, node.inputs[0])
        return node.outputs[0]

    def transpose(self, matrix):
        node = self.node("FunctionNodeTransposeMatrix")
        self.link(matrix, node.inputs[0])
        return node.outputs[0]

    def object_info(self, obj):
        key = ("info", obj.name if obj is not None else "")
        if key in self.cache:
            return self.cache[key]
        node = self.node("GeometryNodeObjectInfo", "Object: %s" % (obj.name if obj else "-"), transform_space="ORIGINAL")
        node.inputs["Object"].default_value = obj
        self.cache[key] = node
        return node

    def self_info(self):
        if "self" in self.cache:
            return self.cache["self"]
        self_node = self.node("GeometryNodeSelfObject")
        info = self.node("GeometryNodeObjectInfo", "This pass object", transform_space="ORIGINAL")
        self.link(self_node.outputs[0], info.inputs["Object"])
        self.cache["self"] = info
        return info

    def inv_unit(self):
        return self.B.div(1.0, self.unit)

    def group_node(self, group, label, inputs):
        node = self.node(self.B.group_idname, label)
        node.node_tree = group
        for name, value in inputs.items():
            target = node.inputs.get(name)
            if target is None:
                continue
            if isinstance(value, (MmeSock, bpy.types.NodeSocket)):
                self.link(value, target)
            else:
                self.B.feed(target, value)
        return node

    def mmd_matrix_impl(self, transform, scale):
        iu = self.inv_unit()
        pre = self.combine_matrix([iu, 0.0, 0.0, 0.0, 0.0, 0.0, iu, 0.0, 0.0, iu, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0], "MMD units and axes")
        scaled = self.combine_matrix([scale, 0.0, 0.0, 0.0, 0.0, scale, 0.0, 0.0, 0.0, 0.0, scale, 0.0, 0.0, 0.0, 0.0, 1.0], "accessory scale")
        return self.mat_mul(pre, transform, scaled, self.const_matrix(MME_SWAP, "swap Y/Z"))

    def mmd_world(self, transform, scale=1.0):
        node = self.group_node(mme_geo_matrix_group(), "Blender transform to MMD", {"Transform": transform, "Unit": self.unit, "Scale": scale})
        return node.outputs["Matrix"]

    def world(self):
        if "world" not in self.cache:
            self.B.statement("WorldMatrix: this pass object in MMD space")
            self.cache["world"] = self.mmd_world(self.self_info().outputs["Transform"])
        return self.cache["world"]

    def look(self, eye, f, up, label):
        r = self.vec_node("NORMALIZE", self.vec_node("CROSS_PRODUCT", up, f))
        u = self.vec_node("CROSS_PRODUCT", f, r)
        rs, us, fs = self.split(r), self.split(u), self.split(f)
        dots = [self.B.neg(MmeSock(self.vec_node("DOT_PRODUCT", v, eye))) for v in (r, u, f)]
        comps = []
        for k in range(3):
            comps.extend([rs[k], us[k], fs[k], 0.0])
        comps.extend(dots + [1.0])
        return {"r": rs, "u": us, "f": fs, "rv": r, "uv": u, "matrix": self.combine_matrix(comps, label)}

    def camera_impl(self):
        B = self.B
        B.statement("Camera: view matrix in MMD space")
        active = self.node("GeometryNodeInputActiveCamera")
        info = self.node("GeometryNodeObjectInfo", "Scene camera", transform_space="ORIGINAL")
        self.link(active.outputs[0], info.inputs["Object"])
        transform = info.outputs["Transform"]
        eye = self.scale(self.swap(info.outputs["Location"]), self.inv_unit())
        forward = self.node("FunctionNodeTransformDirection")
        forward.inputs["Direction"].default_value = (0.0, 0.0, -1.0)
        self.link(transform, forward.inputs["Transform"])
        up = self.node("FunctionNodeTransformDirection")
        up.inputs["Direction"].default_value = (0.0, 1.0, 0.0)
        self.link(transform, up.inputs["Transform"])
        f = self.vec_node("NORMALIZE", self.swap(forward.outputs[0]))
        view = self.look(eye, f, self.swap(up.outputs[0]), "ViewMatrix")
        B.statement("Camera: projection matrix (MMD, near %g, far %g)" % (FX_NEAR, FX_FAR))
        cam_info = self.node("GeometryNodeCameraInfo")
        self.link(active.outputs[0], cam_info.inputs["Camera"])
        proj = self.node("FunctionNodeSeparateMatrix", "Blender projection")
        self.link(cam_info.outputs["Projection Matrix"], proj.inputs[0])
        ortho = MmeSock(cam_info.outputs["Is Orthographic"])
        B.boolish.add(B.key(ortho))
        p00, p11 = MmeSock(proj.outputs[0]), MmeSock(proj.outputs[5])
        xs = B.select(ortho, B.mul(p00, self.unit), p00)
        ys = B.select(ortho, B.mul(p11, self.unit), p11)
        q = FX_FAR / (FX_FAR - FX_NEAR)
        c22 = B.select(ortho, 1.0 / (FX_FAR - FX_NEAR), q)
        c23 = B.select(ortho, 0.0, 1.0)
        c32 = B.select(ortho, -FX_NEAR / (FX_FAR - FX_NEAR), -FX_NEAR * q)
        c33 = B.select(ortho, 1.0, 0.0)
        comps = [xs, 0.0, 0.0, 0.0, 0.0, ys, 0.0, 0.0, 0.0, 0.0, c22, c23, 0.0, 0.0, c32, c33]
        return {
            "eye": eye,
            "f": f,
            "r": view["rv"],
            "u": view["uv"],
            "view": view["matrix"],
            "proj": self.combine_matrix(comps, "ProjectionMatrix"),
            "ortho": ortho,
            "xs": xs,
            "ys": ys,
            "c22": c22,
            "c32": c32,
        }

    def camera(self):
        if "camera" in self.cache:
            return self.cache["camera"]
        self.B.statement("Camera in MMD space")
        node = self.group_node(mme_geo_camera_group(), "MMD camera", {"Unit": self.unit})
        out = node.outputs
        ortho = MmeSock(out["Orthographic"])
        self.B.boolish.add(self.B.key(ortho))
        found = {
            "eye": self.split(out["Eye"]),
            "r": self.split(out["Right"]),
            "u": self.split(out["Up"]),
            "f": self.split(out["Forward"]),
            "view": out["View"],
            "proj": out["Projection"],
            "ortho": ortho,
            "xs": MmeSock(out["XS"]),
            "ys": MmeSock(out["YS"]),
            "c22": MmeSock(out["C22"]),
            "c32": MmeSock(out["C32"]),
            "q": FX_FAR / (FX_FAR - FX_NEAR),
        }
        self.cache["camera"] = found
        return found

    def light_object(self):
        collection = bpy.data.collections.get(LIGHT_COLLECTION)
        rig = find_rig_root(collection, "LIGHT") if collection else None
        return child_of_type(rig, "LIGHT") if rig else None

    def light_impl(self, direction):
        d = self.vec_node("NORMALIZE", direction)
        eye = self.scale(d, -1000.0)
        view = self.look(eye, d, (0.0, 1.0, 0.0), "Light ViewMatrix")
        _view, proj = FxSceneState().light_matrices()
        return {"d": d, "eye": eye, "view": view["matrix"], "proj": self.const_matrix(np.asarray(proj).T, "Light ProjectionMatrix")}

    def light(self):
        if "light" in self.cache:
            return self.cache["light"]
        B = self.B
        B.statement("Light: MMD direction and color")
        light = self.light_object()
        if light is not None:
            loc = [
                self.driven("light:%d" % k, "Light location %s" % "xyz"[k], {"expr": "var", "vars": [{"name": "var", "type": "SINGLE_PROP", "id_type": "OBJECT", "id": light.name, "path": "location", "index": k}]}, light.location[k])
                for k in range(3)
            ]
            direction = self.combine([B.neg(loc[0]), B.neg(loc[2]), B.neg(loc[1])], "MMD light direction")
            color = [
                self.driven("lightcolor:%d" % k, "Light color %s" % "rgb"[k], {"expr": "var", "vars": [{"name": "var", "type": "SINGLE_PROP", "id_type": "LIGHT", "id": light.data.name, "path": "color", "index": k}]}, light.data.color[k])
                for k in range(3)
            ]
        else:
            direction = self.combine([float(v) for v in self.state.light_dir], "MMD light direction")
            color = [float(v) for v in self.state.light_color]
        node = self.group_node(mme_geo_light_group(), "MMD light", {"Direction": direction})
        out = node.outputs
        found = {
            "d": self.split(out["Direction"]),
            "eye": self.split(out["Eye"]),
            "color": color,
            "view": out["View"],
            "proj": out["Projection"],
        }
        self.cache["light"] = found
        return found

    def transform_matrix(self, source):
        base, suffix, target = source[1], source[2], source[3]
        if target == "light":
            parts = self.light()
        else:
            parts = self.camera()
        view, proj = parts["view"], parts["proj"]
        if base == "WORLD":
            m = self.world()
        elif base == "VIEW":
            m = view
        elif base == "PROJECTION":
            m = proj
        elif base == "WORLDVIEW":
            m = self.mat_mul(view, self.world())
        elif base == "VIEWPROJECTION":
            m = self.mat_mul(proj, view)
        else:
            m = self.mat_mul(proj, view, self.world())
        if "INVERSE" in suffix:
            m = self.invert(m)
        if "TRANSPOSE" in suffix:
            m = self.transpose(m)
        return m

    def prop_spec(self, obj, path, index=None, expr="var"):
        var = {"name": "var", "type": "SINGLE_PROP", "id_type": "OBJECT", "id": obj.name, "path": path}
        if index is not None:
            var["index"] = index
        return {"expr": expr, "vars": [var]}

    def object_items(self, obj, item, gname, n, is_bool):
        lower = (item or "").lower()
        if obj is None:
            return [0.0] * n
        suffix = "" if obj == self.root else " (%s)" % obj.name
        if lower == "tr":
            return [self.driven("tr:" + obj.name, "Tr" + suffix, self.prop_spec(obj, '["mme_tr"]'), obj.get("mme_tr", 1.0))]
        if lower == "si":
            return [self.driven("si:" + obj.name, "Si" + suffix, self.prop_spec(obj, '["mme_si"]', expr="var * %r" % ACCESSORY_UNIT), float(obj.get("mme_si", 1.0)) * ACCESSORY_UNIT)]
        if lower in ("x", "y", "z", "xyz", ""):
            if lower == "" and is_bool:
                return [1.0]
            pos = self.split(self.scale(self.swap(self.object_info(obj).outputs["Location"]), 1.0 / self.unit))
            if lower == "xyz":
                return pos
            if lower == "":
                return pos + [1.0]
            return [pos["xyz".index(lower)]]
        if lower in ("rx", "ry", "rz", "rxyz"):
            axes = {"rx": [0], "ry": [2], "rz": [1], "rxyz": [0, 2, 1]}[lower]
            return [
                self.driven("rot:%s:%d" % (obj.name, k), "Rotation %s (%s)" % ("xyz"[k], obj.name), self.prop_spec(obj, "rotation_euler", k, "-var"), -obj.rotation_euler[k])
                for k in axes
            ]
        return [0.0]

    def bone_spec(self, armature, bone, kind):
        return {
            "expr": "var",
            "vars": [
                {
                    "name": "var",
                    "type": "TRANSFORMS",
                    "id": armature.name,
                    "bone": bone,
                    "transform_type": kind,
                    "space": "WORLD_SPACE",
                    "rotation_mode": "QUATERNION",
                }
            ],
        }

    def bone_location(self, armature, bone):
        key = ("bone", armature.name, bone)
        if key in self.cache:
            return self.cache[key]
        loc = [self.driven("bone:%s:%s:%s" % (armature.name, bone, t), "%s %s" % (bone, t), self.bone_spec(armature, bone, t)) for t in ("LOC_X", "LOC_Y", "LOC_Z")]
        self.cache[key] = loc
        return loc

    def bone_matrix(self, armature, bone):
        loc = self.combine(self.bone_location(armature, bone), "bone location")
        quat = self.node("FunctionNodeQuaternionToRotation")
        for name, kind in zip(("W", "X", "Y", "Z"), ("ROT_W", "ROT_X", "ROT_Y", "ROT_Z")):
            self.B.feed(quat.inputs[name], self.driven("bone:%s:%s:%s" % (armature.name, bone, kind), "%s %s" % (bone, kind), self.bone_spec(armature, bone, kind), 1.0 if kind == "ROT_W" else 0.0))
        scale = self.combine([self.driven("bone:%s:%s:%s" % (armature.name, bone, kind), "%s %s" % (bone, kind), self.bone_spec(armature, bone, kind), 1.0) for kind in ("SCALE_X", "SCALE_Y", "SCALE_Z")], "bone scale")
        combine = self.node("FunctionNodeCombineTransform")
        self.link(loc, combine.inputs["Translation"])
        self.link(quat.outputs[0], combine.inputs["Rotation"])
        self.link(scale, combine.inputs["Scale"])
        return combine.outputs[0]

    def control_matrix(self, gname):
        desc = self.controls.get(gname, {})
        kind = desc.get("kind")
        scale = ACCESSORY_UNIT * self.unit
        if kind == "self":
            return self.mmd_world(self.object_info(self.root).outputs["Transform"], scale)
        if kind in ("object", "model"):
            obj = bpy.data.objects.get(desc.get("object", ""))
            return self.mmd_world(self.object_info(obj).outputs["Transform"], scale)
        if kind == "bone":
            armature = bpy.data.objects.get(desc.get("object", ""))
            if armature is not None:
                return self.mmd_world(self.bone_matrix(armature, desc.get("bone", "")))
        return self.const_matrix(np.identity(4), "identity")

    def control_values(self, gname, n):
        values = self.control_values_raw(gname, n)
        desc = self.controls.get(gname, {})
        prop = mme_control_use_prop(desc)
        if prop is None:
            return values
        source = mme_control_source(desc)
        use = self.driven("use:" + prop, "Use " + source, self.prop_spec(self.root, '["%s"]' % prop), float(bool(self.root.get(prop, True))))
        return [self.B.mul(v, use) for v in values]

    def control_values_raw(self, gname, n):
        desc = self.controls.get(gname, {})
        kind = desc.get("kind")
        g = self.program.globals.get(gname)
        t = fx_resolve_type(g.type, self.program) if g is not None else ("f", ())
        is_bool = fx_is_numeric(t) and t[0] == "b"
        if kind == "self":
            return self.object_items(self.root, desc.get("item", ""), gname, n, is_bool)
        if kind == "object":
            return self.object_items(bpy.data.objects.get(desc.get("object", "")), desc.get("item", ""), gname, n, is_bool)
        if kind == "prop":
            prop = desc.get("prop", "")
            return [self.driven("prop:" + prop, prop, self.prop_spec(self.root, '["%s"]' % prop), self.root.get(prop, 0.0))]
        if kind == "bone":
            armature = bpy.data.objects.get(desc.get("object", ""))
            if armature is None:
                return [0.0]
            x, y, z = self.bone_location(armature, desc.get("bone", ""))
            B = self.B
            return [B.div(x, self.unit), B.div(z, self.unit), B.div(y, self.unit), 1.0]
        if kind == "model":
            if is_bool:
                return [1.0]
            return self.object_items(bpy.data.objects.get(desc.get("object", "")), "", gname, n, is_bool)
        return [0.0]

    def param_values(self, gname, n):
        meta = mme_param_meta(self.root).get(gname, {})
        prop = meta.get("prop") or mme_prop_name(MME_PARAM_PREFIX, gname)
        size = int(meta.get("size", 1))
        default = list(self.root.get(prop, meta.get("default", [0.0]))) if size > 1 else [self.root.get(prop, (meta.get("default") or [0.0])[0])]
        out = []
        for k in range(size):
            path = '["%s"]' % prop
            label = meta.get("label") or gname
            if size > 1:
                label = "%s.%s" % (label, "xyzw"[k] if k < 4 else str(k))
            out.append(self.driven("param:%s:%d" % (gname, k), label, self.prop_spec(self.root, path, k if size > 1 else None), float(default[k]) if k < len(default) else 0.0))
        return out

    def scene_spec(self, expr, names):
        paths = {"fps": "render.fps", "base": "render.fps_base", "rx": "render.resolution_x", "ry": "render.resolution_y", "pct": "render.resolution_percentage"}
        return {"expr": expr, "vars": [{"name": n, "type": "SINGLE_PROP", "id_type": "SCENE", "id": self.scene.name, "path": paths[n]} for n in names]}

    def scalar_values(self, source, gname, n):
        kind = source[0]
        key = ("scalar", gname)
        if key in self.cache:
            return self.cache[key]
        if kind == "position":
            if source[1] == "light":
                values = self.light()["eye"] + [1.0]
            else:
                values = self.camera()["eye"] + [1.0]
        elif kind == "direction":
            values = (self.light()["d"] if source[1] == "light" else self.camera()["f"]) + [0.0]
        elif kind == "light_color":
            values = [0.0, 0.0, 0.0, 1.0] if source[1] == "DIFFUSE" else list(self.light()["color"]) + [1.0]
        elif kind == "time":
            time_node = self.node("GeometryNodeInputSceneTime")
            values = [MmeSock(time_node.outputs["Seconds"])]
        elif kind == "elapsed":
            fps = scene_fps(self.scene) or PMM_FPS
            values = [self.driven("elapsed", "ElapsedTime", self.scene_spec("base / fps", ("fps", "base")), 1.0 / fps)]
        elif kind == "viewport":
            r = self.scene.render
            values = [
                self.driven("viewport:0", "Viewport width", self.scene_spec("rx * pct / 100", ("rx", "pct")), r.resolution_x * r.resolution_percentage / 100.0),
                self.driven("viewport:1", "Viewport height", self.scene_spec("ry * pct / 100", ("ry", "pct")), r.resolution_y * r.resolution_percentage / 100.0),
            ]
        elif kind == "control":
            values = self.control_values(gname, n)
        elif kind == "param":
            values = self.param_values(gname, n)
        else:
            values = [0.0] * n
        values = (list(values) + [0.0] * n)[:n]
        self.cache[key] = values
        return values

    def provide(self, need):
        kind = need[0]
        if kind == "matrix":
            source, gname = need[1], need[2]
            self.B.statement("%s" % gname)
            if source[0] == "matrix":
                return self.transform_matrix(source)
            return self.control_matrix(gname)
        if kind == "scalar":
            source, gname, k, n = need[1], need[2], need[3], need[4]
            self.B.statement("%s" % gname)
            return self.scalar_values(source, gname, n)[k]
        if kind == "vertex":
            name, k = need[1], need[2]
            key = ("vattr", name)
            if key not in self.cache:
                self.B.statement("Vertex data from the .x file")
                if name == MME_ATTR_SRC:
                    node = self.node("GeometryNodeInputNamedAttribute", "source vertex index", data_type="INT")
                    node.inputs["Name"].default_value = name
                    self.cache[key] = [MmeSock(node.outputs["Attribute"])]
                else:
                    node = self.node("GeometryNodeInputNamedAttribute", name, data_type="FLOAT_VECTOR")
                    node.inputs["Name"].default_value = name
                    self.cache[key] = self.split(node.outputs["Attribute"])
            return self.cache[key][k]
        if kind == "loop":
            return self.cache.get(("loopvar", need[1]), 0.0)
        return 0.0


def mme_set_drivers(obj, mod, drivers, defaults):
    inputs = mod.properties.inputs
    for identifier, value in defaults.items():
        item = getattr(inputs, identifier, None)
        if item is not None:
            item.value = value
    for identifier, spec in drivers:
        mme_add_driver(obj, 'modifiers["%s"].properties.inputs.%s.value' % (mod.name, identifier), spec)


def mme_add_driver(owner, path, spec, index=-1):
    try:
        owner.driver_remove(path, index)
    except TypeError:
        pass
    fcurve = owner.driver_add(path, index) if index >= 0 else owner.driver_add(path)
    driver = fcurve.driver
    driver.type = "SCRIPTED"
    for var_spec in spec["vars"]:
        var = driver.variables.new()
        var.name = var_spec["name"]
        var.type = var_spec["type"]
        target = var.targets[0]
        if var_spec["type"] == "SINGLE_PROP":
            target.id_type = var_spec["id_type"]
            if var_spec["id_type"] == "SCENE":
                target.id = bpy.data.scenes.get(var_spec["id"])
            elif var_spec["id_type"] == "LIGHT":
                target.id = bpy.data.lights.get(var_spec["id"])
            else:
                target.id = bpy.data.objects.get(var_spec["id"])
                if target.id is not None:
                    target.id.update_tag()
            path_text = var_spec["path"]
            if "index" in var_spec:
                path_text = "%s[%d]" % (path_text, var_spec["index"])
            target.data_path = path_text
        else:
            target.id = bpy.data.objects.get(var_spec["id"])
            target.bone_target = var_spec.get("bone", "")
            target.transform_type = var_spec["transform_type"]
            target.transform_space = var_spec.get("space", "WORLD_SPACE")
            target.rotation_mode = var_spec.get("rotation_mode", "AUTO")
    driver.expression = spec["expr"]
    return fcurve


def mme_bake_node(S, geometry):
    bake = S.node("GeometryNodeBake", "Bake (Effects > Bake)")
    bake.bake_items.new("GEOMETRY", "Geometry")
    S.link(geometry, bake.inputs[0])
    return bake.outputs[0]


def mme_build_geo_tree(label, runtime, draw, state, root, unit, varyings, loop_attrs, scene):
    program = runtime.program
    function, args = fx_entry(program, draw.fx_pass.vs)
    if function is None:
        raise FxUnsupported("pass %s has no vertex shader" % draw.fx_pass.name)
    owner = "%s|%s" % (root.get("mmd_bridge_accessory"), label)
    mme_drop_orphan_groups(owner)
    controls = mme_load_controls(root)
    inner = bpy.data.node_groups.new("MME VS " + label, "GeometryNodeTree")
    inner[MME_OWNER_PROP] = owner
    top = None
    try:
        needs = {}
        B = MmeGeoBackend(inner)
        runtime.material_state(state, draw.subset)
        env = MmeGeoEnv(program, state, runtime.textures, controls, needs)
        ex = FxExec(program, B, env)
        for var, _count, _name in draw.loops:
            if var:
                key = "l:" + var
                needs[key] = ("loop", var)
                ex.overrides[var] = FxValue(("i", ()), [B.new_input(key, label="LoopGetIndex %s" % var)])
        outputs = fx_run_entry(ex, function, args, mme_geo_vertex_inputs(needs)(ex))
        position = outputs.get("POSITION0")
        if position is not None and fx_is_numeric(position.t):
            clip = list(ex.convert(position, ("f", (4,))).c)
        else:
            clip = [0.0, 0.0, 0.0, 0.0]
        outs = [("Clip %s" % c, clip[k], "NodeSocketFloat") for k, c in enumerate("xyzw")]
        for name in varyings:
            semantic, _sep, index = name[len(MME_VARYING_PREFIX):].rpartition("_")
            value = outputs.get(semantic)
            comp = 0.0
            if value is not None and fx_is_numeric(value.t):
                k = int(index)
                comp = value.c[k] if k < len(value.c) else 0.0
            outs.append((name, comp, "NodeSocketFloat"))
        B.statement("vertex shader outputs")
        B.finish(outs)
        inner_inputs = {key: B.input_identifier(key) for key in list(B.inputs)}
        top = bpy.data.node_groups.new("MME " + label, "GeometryNodeTree")
        top[MME_OWNER_PROP] = owner
        top.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
        top.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
        S = MmeGeoScene(top, root, unit, scene, state, controls, program)
        T = S.B
        group_in = T.new("NodeGroupInput")
        T.block_of[group_in.name] = None
        geometry = group_in.outputs[0]
        if draw.loops:
            T.statement("LoopByCount: one copy of the mesh per loop index")
            density = S.driven(
                "density",
                "Effect Density",
                {"expr": "var", "vars": [{"name": "var", "type": "SINGLE_PROP", "id_type": "SCENE", "id": scene.name, "path": "mmd_bridge.mme_density"}]},
                scene.mmd_bridge.mme_density,
            )
            params = getattr(program, "params", {})
            effective = []
            for var, count, name in draw.loops:
                if name in params:
                    source = S.param_values(name, 1)[0]
                else:
                    source = float(count)
                T.statement("LoopByCount=%s" % name)
                effective.append(T.max(1.0, T.unary("round", T.mul(source, density))))
            total = 1.0
            for value in effective:
                total = T.mul(total, value)
            points = S.node("GeometryNodePoints", "loop instances")
            T.feed(points.inputs["Count"], total)
            index = S.node("GeometryNodeInputIndex")
            store = S.node("GeometryNodeStoreNamedAttribute", "store loop instance", data_type="INT", domain="POINT")
            store.inputs["Name"].default_value = MME_ATTR_INST
            S.link(points.outputs[0], store.inputs["Geometry"])
            S.link(index.outputs[0], store.inputs["Value"])
            instance = S.node("GeometryNodeInstanceOnPoints")
            S.link(store.outputs[0], instance.inputs["Points"])
            S.link(geometry, instance.inputs["Instance"])
            realize = S.node("GeometryNodeRealizeInstances")
            S.link(instance.outputs[0], realize.inputs[0])
            geometry = realize.outputs[0]
            inst_attr = S.node("GeometryNodeInputNamedAttribute", "loop instance", data_type="INT")
            inst_attr.inputs["Name"].default_value = MME_ATTR_INST
            remaining = MmeSock(inst_attr.outputs["Attribute"])
            T.statement("LoopGetIndex values")
            for (var, _count, _name), value in reversed(list(zip(draw.loops, effective))):
                if var:
                    S.cache[("loopvar", var)] = T.imod(remaining, value)
                remaining = T.idiv(remaining, value)
        T.statement("Vertex shader: %s" % function.name)
        vs = S.node("GeometryNodeGroup", "Vertex shader: %s (%s)" % (function.name, draw.fx_pass.name or "pass"))
        vs.node_tree = inner
        vs.width = 260
        for key, identifier in inner_inputs.items():
            target = next((s for s in vs.inputs if s.identifier == identifier), None)
            if target is None:
                continue
            value = S.provide(needs.get(key, ("none",)))
            if isinstance(value, (MmeSock, bpy.types.NodeSocket)):
                S.link(value, target)
            else:
                T.feed(target, value)
        T.statement("Clip space to object space")
        clip_node = S.group_node(
            mme_geo_clip_group(),
            "Clip space to object space",
            {"Clip X": vs.outputs["Clip x"], "Clip Y": vs.outputs["Clip y"], "Clip Z": vs.outputs["Clip z"], "Clip W": vs.outputs["Clip w"], "Unit": unit},
        )
        culled = MmeSock(clip_node.outputs["Culled"])
        T.boolish.add(T.key(culled))
        T.statement("Set positions; culled vertices collapse to the origin")
        set_pos = S.node("GeometryNodeSetPosition")
        S.link(geometry, set_pos.inputs["Geometry"])
        S.link(clip_node.outputs["Position"], set_pos.inputs["Position"])
        geometry = set_pos.outputs[0]
        T.statement("Store vertex shader outputs for the material")

        def store(name, value, data_type="FLOAT"):
            nonlocal geometry
            node = S.node("GeometryNodeStoreNamedAttribute", name, data_type=data_type, domain="POINT")
            node.inputs["Name"].default_value = name
            S.link(geometry, node.inputs["Geometry"])
            T.feed(node.inputs["Value"], value)
            geometry = node.outputs[0]

        store(MME_ATTR_CULL, culled)
        for name in varyings:
            value = MmeSock(vs.outputs[name])
            store(name, T.select(T.cmp("==", value, value), value, 0.0))
        for name in loop_attrs:
            var = name[len(MME_LOOP_PREFIX):]
            store(name, S.cache.get(("loopvar", var), 0.0))
        T.statement("Output")
        geometry = mme_bake_node(S, geometry)
        group_out = T.new("NodeGroupOutput")
        T.block_of[group_out.name] = "out"
        S.link(geometry, group_out.inputs[0])
        mme_prune_tree(top, group_out)
        for node in top.nodes:
            if node.bl_idname == "NodeGroupInput":
                for socket in node.outputs:
                    socket.hide = not socket.is_linked
        mme_layout(top, T.block_of, T.blocks)
        return top, S.drivers, S.defaults, len(inner.nodes) + len(top.nodes)
    except Exception:
        if top is not None:
            bpy.data.node_groups.remove(top)
        bpy.data.node_groups.remove(inner)
        raise


def mme_geo_group_scene(group):
    S = MmeGeoScene(group, None, 1.0, None, None, {}, None)
    S.unit = S.B.new_input("Unit", "NodeSocketFloat", "Unit")
    S.B.input_items[S.unit.socket.identifier].default_value = 1.0
    return S


def mme_geo_matrix_group():
    key = "core:mmd_matrix:GN:v%d" % MME_CORE_VERSION

    def build(group):
        S = mme_geo_group_scene(group)
        transform = S.B.new_input("Transform", "NodeSocketMatrix", "Transform")
        scale = S.B.new_input("Scale", "NodeSocketFloat", "Scale")
        S.B.input_items[scale.socket.identifier].default_value = 1.0
        S.B.statement("unit scale, swap Y/Z (MMD is left-handed, Y-up)")
        matrix = S.mmd_matrix_impl(transform, scale)
        S.B.finish([("Matrix", MmeSock(matrix), "NodeSocketMatrix")])
        group.description = "Blender object transform to an MMD row-vector world matrix"

    return mme_core_group(mme_group_name("Blender to MMD matrix", "GN", key), "GeometryNodeTree", key, build)


def mme_geo_camera_group():
    key = "core:camera:GN:v%d" % MME_CORE_VERSION

    def build(group):
        S = mme_geo_group_scene(group)
        cam = S.camera_impl()
        S.B.statement("camera outputs")
        S.B.finish(
            [
                ("View", MmeSock(cam["view"]), "NodeSocketMatrix"),
                ("Projection", MmeSock(cam["proj"]), "NodeSocketMatrix"),
                ("Eye", MmeSock(cam["eye"]), "NodeSocketVector"),
                ("Right", MmeSock(cam["r"]), "NodeSocketVector"),
                ("Up", MmeSock(cam["u"]), "NodeSocketVector"),
                ("Forward", MmeSock(cam["f"]), "NodeSocketVector"),
                ("XS", cam["xs"], "NodeSocketFloat"),
                ("YS", cam["ys"], "NodeSocketFloat"),
                ("C22", cam["c22"], "NodeSocketFloat"),
                ("C32", cam["c32"], "NodeSocketFloat"),
                ("Orthographic", cam["ortho"], "NodeSocketBool"),
            ]
        )
        group.description = "Scene camera as MMD view and projection matrices"

    return mme_core_group(mme_group_name("Camera (MMD)", "GN", key), "GeometryNodeTree", key, build)


def mme_geo_light_group():
    key = "core:light:GN:v%d" % MME_CORE_VERSION

    def build(group):
        S = MmeGeoScene(group, None, 1.0, None, None, {}, None)
        direction = S.B.new_input("Direction", "NodeSocketVector", "Direction")
        S.B.statement("light view and projection")
        light = S.light_impl(direction)
        S.B.finish(
            [
                ("View", MmeSock(light["view"]), "NodeSocketMatrix"),
                ("Projection", MmeSock(light["proj"]), "NodeSocketMatrix"),
                ("Direction", MmeSock(light["d"]), "NodeSocketVector"),
                ("Eye", MmeSock(light["eye"]), "NodeSocketVector"),
            ]
        )
        group.description = "MMD light direction as light view and projection matrices"

    return mme_core_group(mme_group_name("Light (MMD)", "GN", key), "GeometryNodeTree", key, build)


def mme_geo_clip_group():
    key = "core:clip:GN:v%d" % MME_CORE_VERSION

    def build(group):
        S = mme_geo_group_scene(group)
        T = S.B
        x, y, z, w = [T.new_input("Clip " + c, "NodeSocketFloat", "Clip " + c) for c in "XYZW"]
        unit = S.unit
        cam = S.camera()
        T.statement("Clip space to MMD world space")
        ortho = cam["ortho"]
        vx = T.div(x, cam["xs"])
        vy = T.div(y, cam["ys"])
        vz = T.select(ortho, T.div(T.sub(z, T.mul(w, cam["c32"])), cam["c22"]), w)
        vw = T.select(ortho, w, 1.0)
        terms = [
            T.vm("SCALE", "right * x", T.vector_of(cam["r"]), scale=vx),
            T.vm("SCALE", "up * y", T.vector_of(cam["u"]), scale=vy),
            T.vm("SCALE", "forward * z", T.vector_of(cam["f"]), scale=vz),
            T.vm("SCALE", "eye * w", T.vector_of(cam["eye"]), scale=vw),
        ]
        total = T.vm("ADD", "view to world", T.vm("ADD", "right + up", terms[0], terms[1]), T.vm("ADD", "forward + eye", terms[2], terms[3]))
        world_vector = T.vm("SCALE", "divide by w", total, scale=T.div(1.0, vw))
        world = T.split_vector(world_vector)
        T.statement("Culling (outside the MMD view volume)")
        abs_w = T.unary("abs", w)
        tiny = T.cmp("<=", abs_w, 1e-12)
        natural = T.add(T.mul(cam["q"], w), -FX_NEAR * cam["q"])
        front = T.cmp(">", w, 1e-12)
        near_out = T.cmp("<", z, T.mul(-1e-6, T.max(1.0, abs_w)))
        far_out = T.cmp(">", z, T.add(T.mul(w, 1.0 + 1e-9), 1e-6))
        outside = T.land(front, T.lor(near_out, far_out))
        behind = T.land(T.cmp("<", w, -1e-12), T.cmp(">", T.unary("abs", T.sub(z, natural)), T.mul(1e-4, T.add(abs_w, 1.0))))
        persp = T.lor(tiny, T.lor(outside, behind))
        ortho_cull = T.lor(T.cmp("<", abs_w, 1e-12), T.lor(T.cmp("<", z, 0.0), T.cmp(">", z, w)))
        culled = T.select(ortho, ortho_cull, persp)
        for value in world:
            culled = T.lor(culled, T.lnot(T.cmp("==", value, value)))
        T.statement("MMD world to object space")
        blender = T.vm("SCALE", "MMD units to Blender", T.vector_of([world[0], world[2], world[1]]), scale=unit).socket
        to_local = S.node("FunctionNodeTransformPoint")
        S.link(blender, to_local.inputs["Vector"])
        S.link(S.invert(S.self_info().outputs["Transform"]), to_local.inputs["Transform"])
        T.statement("culled vertices collapse to the origin")
        choose = S.node("GeometryNodeSwitch", "culled ? origin : position", input_type="VECTOR")
        T.feed(choose.inputs["Switch"], culled)
        S.link(to_local.outputs[0], choose.inputs["False"])
        choose.inputs["True"].default_value = (0.0, 0.0, 0.0)
        T.finish([("Position", MmeSock(choose.outputs[0]), "NodeSocketVector"), ("Culled", culled, "NodeSocketBool")])
        group.description = "MMD clip-space vertex position to this object's local space, with MMD culling"

    return mme_core_group(mme_group_name("Clip to object (MMD)", "GN", key), "GeometryNodeTree", key, build)


def mme_drop_orphan_groups(owner):
    for group in [g for g in bpy.data.node_groups if g.get(MME_OWNER_PROP) == owner and g.bl_idname == "GeometryNodeTree"]:
        if group.users == 0 and not group.use_fake_user:
            bpy.data.node_groups.remove(group)
    for group in [g for g in bpy.data.node_groups if g.get(MME_OWNER_PROP) == owner and g.bl_idname == "GeometryNodeTree"]:
        if group.users == 0 and not group.use_fake_user:
            bpy.data.node_groups.remove(group)


def mme_bake_only_tree(label, root):
    mme_drop_orphan_groups("%s|%s" % (root.get("mmd_bridge_accessory"), label))
    top = bpy.data.node_groups.new("MME " + label, "GeometryNodeTree")
    top[MME_OWNER_PROP] = "%s|%s" % (root.get("mmd_bridge_accessory"), label)
    top.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    top.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    S = MmeGeoScene(top, root, 1.0, bpy.context.scene, None, {}, None)
    group_in = S.node("NodeGroupInput")
    group_in.location = (0, 0)
    time_node = S.node("GeometryNodeInputSceneTime", "re-evaluate every frame")
    time_node.location = (0, -160)
    stamp = S.node("GeometryNodeStoreNamedAttribute", "frame stamp", data_type="FLOAT", domain="POINT")
    stamp.inputs["Name"].default_value = "mme_frame"
    stamp.location = (220, 0)
    S.link(group_in.outputs[0], stamp.inputs["Geometry"])
    S.link(time_node.outputs["Frame"], stamp.inputs["Value"])
    geometry = mme_bake_node(S, stamp.outputs[0])
    S.tree.nodes[-1].location = (440, 0)
    group_out = S.node("NodeGroupOutput")
    group_out.location = (680, 0)
    S.link(geometry, group_out.inputs[0])
    return top
