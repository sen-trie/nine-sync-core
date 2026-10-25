import hashlib
import numpy as np
import os
import re
from typing import NamedTuple, Tuple


X_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[{}]|[;,]|[^\s{};,"]+')


class XBlock:
    __slots__ = ("kind", "name", "data", "children")

    def __init__(self, kind, name):
        self.kind = kind
        self.name = name
        self.data = []
        self.children = []


class XMaterial(NamedTuple):
    name: str
    diffuse: Tuple[float, float, float, float]
    power: float
    specular: Tuple[float, float, float]
    emissive: Tuple[float, float, float]
    texture: str


class XMesh:
    __slots__ = (
        "vertices",
        "faces",
        "uvs",
        "face_materials",
        "materials",
        "colors",
        "normals",
    )

    def __init__(self):
        self.vertices = []
        self.faces = []
        self.uvs = []
        self.face_materials = []
        self.materials = []
        self.colors = {}
        self.normals = {}


class XScene:
    __slots__ = ("meshes", "warnings", "mode")

    def __init__(self):
        self.meshes = []
        self.warnings = []
        self.mode = "txt"


class XError(Exception):
    pass


UNSUPPORTED_BLOCKS = (
    "SkinWeights",
    "XSkinMeshHeader",
    "AnimationSet",
    "Animation",
    "AnimTicksPerSecond",
)


def x_header_mode(raw):
    if len(raw) < 16 or raw[:4] != b"xof ":
        raise XError("not a DirectX .x file")
    return raw[8:12].decode("ascii", errors="replace").strip()


def x_tokens(text):
    text = re.sub(r"//[^\n]*|#[^\n]*", "", text)
    return X_TOKEN.findall(text)


def x_number(token):
    try:
        return float(token)
    except ValueError:
        return None


def x_skip_block(tokens, i):
    depth = 0
    while i < len(tokens):
        if tokens[i] == "{":
            depth += 1
        elif tokens[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise XError("unbalanced braces")


def x_parse_body(tokens, i, block):
    while i < len(tokens):
        token = tokens[i]
        if token == "}":
            return i + 1
        if token in (";", ","):
            i += 1
            continue
        if token == "{":
            j = i + 1
            ref = []
            while j < len(tokens) and tokens[j] != "}":
                if tokens[j] not in (";", ","):
                    ref.append(tokens[j])
                j += 1
            child = XBlock("__ref__", ref[0] if ref else "")
            block.children.append(child)
            i = j + 1
            continue
        if token.startswith('"'):
            block.data.append(token[1:-1])
            i += 1
            continue
        number = x_number(token)
        if number is not None:
            block.data.append(number)
            i += 1
            continue
        kind = token
        i += 1
        name = ""
        if i < len(tokens) and tokens[i] != "{":
            name = tokens[i]
            i += 1
        if i >= len(tokens) or tokens[i] != "{":
            raise XError("expected '{' after %s" % kind)
        if kind == "template":
            i = x_skip_block(tokens, i)
            continue
        i += 1
        child = XBlock(kind, name)
        if i < len(tokens) and tokens[i].startswith("<") and tokens[i].endswith(">"):
            i += 1
        i = x_parse_body(tokens, i, child)
        block.children.append(child)
    return i


def x_parse_tree(text):
    root = XBlock("root", "")
    tokens = x_tokens(text)
    x_parse_body(tokens, 0, root)
    return root


def x_mat_mul(a, b):
    return [
        sum(a[r * 4 + k] * b[k * 4 + c] for k in range(4))
        for r in range(4)
        for c in range(4)
    ]


X_IDENTITY = [
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
]


def x_collect_materials(root):
    found = {}

    def walk(block):
        for child in block.children:
            if child.kind == "Material" and child.name:
                found[child.name] = child
            walk(child)

    walk(root)
    return found


def x_read_material(block, name):
    d = block.data
    if len(d) < 11:
        raise XError("short Material block")
    texture = ""
    for child in block.children:
        if child.kind == "TextureFilename" and child.data:
            texture = str(child.data[0])
    return XMaterial(
        name or "Material",
        (d[0], d[1], d[2], d[3]),
        d[4],
        (d[5], d[6], d[7]),
        (d[8], d[9], d[10]),
        texture,
    )


def x_read_mesh(block, matrix, named_materials):
    d = block.data
    mesh = XMesh()
    pos = 0
    count = int(d[pos])
    pos += 1
    raw = d[pos : pos + count * 3]
    pos += count * 3
    for k in range(count):
        x, y, z = raw[k * 3 : k * 3 + 3]
        m = matrix
        mesh.vertices.append(
            (
                x * m[0] + y * m[4] + z * m[8] + m[12],
                x * m[1] + y * m[5] + z * m[9] + m[13],
                x * m[2] + y * m[6] + z * m[10] + m[14],
            )
        )
    face_count = int(d[pos])
    pos += 1
    for _ in range(face_count):
        n = int(d[pos])
        pos += 1
        mesh.faces.append(tuple(int(v) for v in d[pos : pos + n]))
        pos += n
    for child in block.children:
        cd = child.data
        if child.kind == "MeshTextureCoords" and cd:
            n = int(cd[0])
            mesh.uvs = [(cd[1 + k * 2], cd[2 + k * 2]) for k in range(n)]
        elif child.kind == "MeshMaterialList" and len(cd) >= 2:
            n_mat = int(cd[0])
            n_idx = int(cd[1])
            indexes = [int(v) for v in cd[2 : 2 + n_idx]]
            if n_idx == 1 and face_count > 1:
                indexes = indexes * face_count
            mesh.face_materials = indexes
            for sub in child.children:
                if sub.kind == "Material":
                    mesh.materials.append(x_read_material(sub, sub.name))
                elif sub.kind == "__ref__":
                    target = named_materials.get(sub.name)
                    if target is not None:
                        mesh.materials.append(x_read_material(target, target.name))
            if n_mat and len(mesh.materials) < n_mat:
                while len(mesh.materials) < n_mat:
                    mesh.materials.append(
                        XMaterial(
                            "Material",
                            (0.8, 0.8, 0.8, 1.0),
                            0.0,
                            (0.0, 0.0, 0.0),
                            (0.0, 0.0, 0.0),
                            "",
                        )
                    )
        elif child.kind == "MeshNormals" and cd:
            count = int(cd[0])
            m = matrix
            listed = []
            for k in range(count):
                nx, ny, nz = cd[1 + k * 3 : 4 + k * 3]
                listed.append(
                    (
                        nx * m[0] + ny * m[4] + nz * m[8],
                        nx * m[1] + ny * m[5] + nz * m[9],
                        nx * m[2] + ny * m[6] + nz * m[10],
                    )
                )
            pos = 1 + count * 3
            if pos < len(cd):
                face_count_n = int(cd[pos])
                pos += 1
                for index in range(face_count_n):
                    if pos >= len(cd):
                        break
                    n = int(cd[pos])
                    pos += 1
                    corners = [int(v) for v in cd[pos : pos + n]]
                    pos += n
                    if index < len(mesh.faces):
                        for vertex, normal in zip(mesh.faces[index], corners):
                            if 0 <= normal < len(listed):
                                mesh.normals[vertex] = listed[normal]
        elif child.kind == "MeshVertexColors" and cd:
            n = int(cd[0])
            for k in range(n):
                base = 1 + k * 5
                mesh.colors[int(cd[base])] = (
                    cd[base + 1],
                    cd[base + 2],
                    cd[base + 3],
                    cd[base + 4],
                )
    return mesh


def x_walk(block, matrix, scene, named_materials):
    for child in block.children:
        if child.kind == "Frame":
            local = list(X_IDENTITY)
            for sub in child.children:
                if sub.kind == "FrameTransformMatrix" and len(sub.data) >= 16:
                    local = [float(v) for v in sub.data[:16]]
            x_walk(child, x_mat_mul(local, matrix), scene, named_materials)
        elif child.kind == "Mesh":
            scene.meshes.append(x_read_mesh(child, matrix, named_materials))
            for sub in child.children:
                if sub.kind in UNSUPPORTED_BLOCKS:
                    note = (
                        "%s ignored (skinned/animated .x is not supported)" % sub.kind
                    )
                    if note not in scene.warnings:
                        scene.warnings.append(note)
        elif child.kind in UNSUPPORTED_BLOCKS:
            note = "%s ignored (skinned/animated .x is not supported)" % child.kind
            if note not in scene.warnings:
                scene.warnings.append(note)


def x_decode(raw):
    raw = raw.replace(b"\x00", b"")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp932", errors="replace")


_x_cache = {}


def parse_x_bytes(raw):
    key = (len(raw), hashlib.md5(raw).hexdigest())
    found = _x_cache.get(key)
    if found is not None:
        return found
    scene = parse_x_uncached(raw)
    _x_cache.clear()
    _x_cache[key] = scene
    return scene


def parse_x_uncached(raw):
    scene = XScene()
    scene.mode = x_header_mode(raw)
    if scene.mode != "txt":
        return scene
    body = x_decode(raw[16:])
    root = x_parse_tree(body)
    named = x_collect_materials(root)
    x_walk(root, list(X_IDENTITY), scene, named)
    for block in root.children:
        if block.kind in UNSUPPORTED_BLOCKS:
            note = "%s ignored (skinned/animated .x is not supported)" % block.kind
            if note not in scene.warnings:
                scene.warnings.append(note)
    return scene


def resolve_x_texture(name, folder):
    cleaned = name.replace("\\\\", "\\").replace("\\", "/")
    base = cleaned.rsplit("/", 1)[-1]
    for candidate in (
        cleaned,
        os.path.join(folder, base),
        os.path.join(folder, "textures", base),
    ):
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def merge_x_meshes(xscene):
    vertices = []
    faces = []
    face_materials = []
    uvs = []
    colors = []
    materials = []
    has_colors = False
    for mesh in xscene.meshes:
        vertex_offset = len(vertices)
        material_offset = len(materials)
        vertices.extend(mesh.vertices)
        for k in range(len(mesh.vertices)):
            uvs.append(mesh.uvs[k] if k < len(mesh.uvs) else (0.0, 0.0))
            colors.append(mesh.colors.get(k, (1.0, 1.0, 1.0, 1.0)))
        has_colors = has_colors or bool(mesh.colors)
        local_materials = list(mesh.materials) or [
            XMaterial(
                "Material",
                (0.8, 0.8, 0.8, 1.0),
                0.0,
                (0.0, 0.0, 0.0),
                (0.0, 0.0, 0.0),
                "",
            )
        ]
        materials.extend(local_materials)
        for index, face in enumerate(mesh.faces):
            faces.append(tuple(v + vertex_offset for v in face))
            local = (
                mesh.face_materials[index] if index < len(mesh.face_materials) else 0
            )
            face_materials.append(
                material_offset + min(max(local, 0), len(local_materials) - 1)
            )
    return (
        vertices,
        faces,
        face_materials,
        uvs,
        colors if has_colors else None,
        materials,
    )


def x_effect_geometry(xscene):
    vertices, faces, face_materials, uvs, _colors, materials = merge_x_meshes(xscene)
    normals = []
    for mesh in xscene.meshes:
        for k in range(len(mesh.vertices)):
            normals.append(mesh.normals.get(k))
    positions = np.array(vertices, dtype=np.float64).reshape(-1, 3)
    uv_array = np.array(uvs, dtype=np.float64).reshape(-1, 2)
    normal_array = np.zeros_like(positions)
    missing = np.ones(len(positions), dtype=bool)
    for index, value in enumerate(normals):
        if value is not None:
            normal_array[index] = value
            missing[index] = False
    if missing.any() and faces:
        accum = np.zeros_like(positions)
        for face in faces:
            if len(face) < 3:
                continue
            a, b, c = positions[face[0]], positions[face[1]], positions[face[2]]
            n = np.cross(b - a, c - a)
            for vertex in face:
                accum[vertex] += n
        lengths = np.linalg.norm(accum, axis=1, keepdims=True)
        accum = np.where(lengths > 1e-12, accum / np.maximum(lengths, 1e-12), 0.0)
        normal_array[missing] = accum[missing]
    return positions, normal_array, uv_array, faces, np.array(face_materials, dtype=np.int64), materials
