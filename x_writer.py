from typing import List, NamedTuple, Tuple


class XMaterial(NamedTuple):
    color: Tuple[float, float, float, float]
    emissive: Tuple[float, float, float]


# Corner-based mesh: every triangle owns its three vertices, so each corner can carry
# its own normal and flat shading survives without MMD having to smooth anything.
class XMesh(NamedTuple):
    positions: List[Tuple[float, float, float]]
    normals: List[Tuple[float, float, float]]
    triangles: List[Tuple[int, int, int]]
    face_materials: List[int]
    materials: List[XMaterial]


def vector_lines(vectors):
    rows = ["%.6f;%.6f;%.6f;" % v for v in vectors]
    return ",\n".join(" " * 2 + r for r in rows) + ";\n"


def face_lines(triangles):
    rows = ["3;%d,%d,%d;" % t for t in triangles]
    return ",\n".join(" " * 2 + r for r in rows) + ";\n"


def material_block(material):
    r, g, b, a = material.color
    er, eg, eb = material.emissive
    return (
        "  Material {\n"
        "   %.6f;%.6f;%.6f;%.6f;;\n"
        "   0.000000;\n"
        "   0.000000;0.000000;0.000000;;\n"
        "   %.6f;%.6f;%.6f;;\n"
        "  }\n" % (r, g, b, a, er, eg, eb)
    )


# DirectX text .x (header "xof 0303txt 0032"), the form MMD's loader reads. Meshes
# without any material get one grey default so MMD never sees an empty list.
def build_x_text(mesh):
    if not mesh.positions or not mesh.triangles:
        raise ValueError("mesh has no faces to write")
    materials = mesh.materials or [XMaterial((0.8, 0.8, 0.8, 1.0), (0.4, 0.4, 0.4))]
    face_materials = mesh.face_materials or [0] * len(mesh.triangles)
    return (
        "xof 0303txt 0032\n\n"
        "Mesh {\n"
        " %d;\n" % len(mesh.positions)
        + vector_lines(mesh.positions)
        + " %d;\n" % len(mesh.triangles)
        + face_lines(mesh.triangles)
        + " MeshNormals {\n"
        "  %d;\n" % len(mesh.normals)
        + vector_lines(mesh.normals)
        + "  %d;\n" % len(mesh.triangles)
        + face_lines(mesh.triangles)
        + " }\n"
        " MeshMaterialList {\n"
        "  %d;\n  %d;\n" % (len(materials), len(face_materials))
        + ",".join(str(i) for i in face_materials)
        + ";;\n"
        + "".join(material_block(m) for m in materials)
        + " }\n"
        "}\n"
    )
