import bpy
import numpy
import os
import re
from mathutils import Color

from ..common.animation import iter_fcurves
from ..common.constants import ACCESSORY_UNIT, PMM_FPS, PROP_FOLDER
from ..common.logger import log
from ..common.scene_utils import scene_fps
from ..pmm import AccessoryKey, PmmAccessory, PmmError
from ..x_writer import XMaterial, XMesh, build_x_text
from .scale import export_scale


DEFAULT_MATERIAL = XMaterial((0.8, 0.8, 0.8, 1.0), (0.0, 0.0, 0.0))


# Shadow flag MMD gives a freshly loaded accessory (off).
DEFAULT_SHADOW = 0


# Blender (x, y, z) is MMD (x, z, y); the scene scale turns Blender units back into
# MMD units (the inverse of what an import multiplies by).
def to_mmd(vector, unit):
    return (vector[0] / unit, vector[2] / unit, vector[1] / unit)


def principled_node(material):
    tree = material.node_tree
    return next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None) if tree else None


# Average colour of the first image in the material, as a scene-linear colour. Byte
# images read back as sRGB values, float ones as linear.
def image_average(material):
    tree = material.node_tree
    nodes = tree.nodes if tree else ()
    image = next((n.image for n in nodes if n.type == "TEX_IMAGE" and n.image), None)
    if image is None or not image.has_data and not image.pixels:
        return None
    channels = image.channels
    pixels = numpy.empty(len(image.pixels), dtype=numpy.float32)
    image.pixels.foreach_get(pixels)
    if not pixels.size or channels < 3:
        return None
    average = pixels.reshape(-1, channels)[:, :3].mean(axis=0)
    color = Color((float(v) for v in average))
    return tuple(color if image.is_float else color.from_srgb_to_scene_linear())


# The colour a material starts with on the prop palette: its texture when it has one,
# since a textured Principled node keeps a meaningless default in Base Color.
def suggested_color(material):
    if material is None:
        return DEFAULT_MATERIAL.color[:3]
    average = image_average(material)
    if average is not None:
        return average
    node = principled_node(material)
    if node is not None:
        return tuple(node.inputs["Base Color"].default_value)[:3]
    return tuple(material.diffuse_color)[:3]


# Keeps a prop's palette in step with its material slots: new materials get a
# suggested colour, picked colours stay, removed materials go.
def refresh_prop_colors(item):
    obj = item.target
    wanted = [slot.material for slot in obj.material_slots if slot.material] if obj else []
    names = {m.name for m in wanted}
    for index in reversed(range(len(item.colors))):
        if item.colors[index].name not in names:
            item.colors.remove(index)
    present = {c.name for c in item.colors}
    for material in wanted:
        if material.name not in present:
            row = item.colors.add()
            row.name = material.name
            row.color = suggested_color(material)


# The palette colour when the user has one, otherwise the Principled or viewport
# colour; written as sRGB, which is what MMD shows. No emissive: MMD's default light
# already brightens a colour, and any added on top washes it out.
def x_material(material, palette):
    if material is None:
        return DEFAULT_MATERIAL
    color = tuple(material.diffuse_color)
    node = principled_node(material)
    if node is not None:
        base = node.inputs["Base Color"].default_value
        color = (base[0], base[1], base[2], node.inputs["Alpha"].default_value)
    rgb = palette.get(material.name, color[:3])
    rgb = tuple(Color(rgb).from_scene_linear_to_srgb())
    return XMaterial((*rgb, color[3]), (0.0, 0.0, 0.0))


# The evaluated mesh (modifiers applied) at the current frame, with the object's
# scale baked in and its rotation and position left out: those become the keys, and
# the object origin is the accessory's pivot. Scale is baked per axis because an
# accessory only has one uniform scale value.
def x_mesh(obj, depsgraph, rest_scale, unit, palette):
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        mesh.calc_loop_triangles()
        if not len(mesh.loop_triangles):
            raise PmmError("'%s' has no faces to export" % obj.name)
        normals = mesh.corner_normals
        materials = [x_material(m, palette) for m in mesh.materials] or [DEFAULT_MATERIAL]
        positions = []
        out_normals = []
        triangles = []
        face_materials = []
        for tri in mesh.loop_triangles:
            base = len(positions)
            for loop in tri.loops:
                co = mesh.vertices[mesh.loops[loop].vertex_index].co
                positions.append(
                    to_mmd(
                        (co[0] * rest_scale[0], co[1] * rest_scale[1], co[2] * rest_scale[2]),
                        unit * ACCESSORY_UNIT,
                    )
                )
                n = normals[loop].vector
                fixed = (n[0] / rest_scale[0], n[1] / rest_scale[1], n[2] / rest_scale[2])
                length = (fixed[0] ** 2 + fixed[1] ** 2 + fixed[2] ** 2) ** 0.5 or 1.0
                out_normals.append(
                    to_mmd((fixed[0] / length, fixed[1] / length, fixed[2] / length), 1.0)
                )
            # to_mmd mirrors, so the corners are reversed to keep the front side
            # facing out; MMD culls back faces, and a wall would turn see-through.
            triangles.append((base, base + 2, base + 1))
            face_materials.append(min(tri.material_index, len(materials) - 1))
        return XMesh(positions, out_normals, triangles, face_materials, materials)
    finally:
        evaluated.to_mesh_clear()


# Frames worth a key: the keyframes placed in Blender, plus the last frame of a constant
# hold, which would otherwise turn into a slide. Curves are not sampled, so MMD moves
# between keys linearly; only motion with no keys of its own to read, from constraints,
# drivers or NLA, is sampled on every frame of the scene range.
def sample_frames(obj, scene):
    start, end = scene.frame_start, scene.frame_end
    frames = {start, end}
    owner = obj
    while owner is not None:
        anim = owner.animation_data
        if owner.constraints or (anim and (anim.drivers or anim.nla_tracks)):
            return set(range(start, end + 1))
        if anim is not None and anim.action is not None:
            for fcurve in iter_fcurves(anim.action, anim.action_slot):
                points = fcurve.keyframe_points
                for index, point in enumerate(points):
                    frame = int(round(point.co[0]))
                    if start <= frame <= end:
                        frames.add(frame)
                    if index + 1 < len(points) and point.interpolation == "CONSTANT":
                        hold = int(round(points[index + 1].co[0])) - 1
                        if start <= hold <= end:
                            frames.add(hold)
        owner = owner.parent
    return frames


# World matrix and visibility of every object on its own sample frames, from one pass
# over the timeline; the scene's current frame is put back afterwards.
def sample_objects(scene, wanted):
    saved = scene.frame_current
    samples = {obj: [] for obj in wanted}
    try:
        for frame in sorted(set().union(*wanted.values())):
            scene.frame_set(frame)
            for obj, frames in wanted.items():
                if frame in frames:
                    hidden = obj.hide_viewport or obj.hide_render
                    samples[obj].append((frame, obj.matrix_world.copy(), not hidden))
    finally:
        scene.frame_set(saved)
    return samples


def mean(values):
    return sum(values) / len(values)


# Euler angles are in MMD's order (YXZ) with the sign changes FX applies on import,
# so a sync back lands the prop where it was. Scale is relative to the scale baked
# into the mesh; MMD keeps a single uniform value.
def accessory_keys(samples, rest_scale, scene, unit):
    factor = scene_fps(scene) / PMM_FPS
    by_frame = {}
    previous = None
    for frame, matrix, visible in samples:
        quaternion = matrix.to_3x3().normalized().to_quaternion()
        euler = quaternion.to_euler("YXZ", previous) if previous else quaternion.to_euler("YXZ")
        previous = euler
        pmm_frame = int(round(frame / factor))
        if pmm_frame < 0:
            continue
        by_frame[pmm_frame] = AccessoryKey(
            pmm_frame,
            int(visible),
            -1,
            0,
            to_mmd(matrix.to_translation(), unit),
            (-euler.x, -euler.z, -euler.y),
            mean(matrix.to_scale()) / mean(rest_scale),
            DEFAULT_SHADOW,
            1.0,
        )
    keys = [by_frame[f] for f in sorted(by_frame)]
    if keys and keys[0].frame != 0:
        keys.insert(0, keys[0]._replace(frame=0))
    return keys


# A file name MMD's cp932 path field and Windows both accept, unique among the files
# written in this export.
def prop_file_name(obj, taken, extension=".x"):
    stem = re.sub(r"[^A-Za-z0-9_\-. ]", "_", obj.name).strip(" .")[:60] or "prop"
    name = stem + extension
    number = 2
    while name.lower() in taken:
        name = "%s_%d%s" % (stem, number, extension)
        number += 1
    taken.add(name.lower())
    return name


# Writes one .x per object into an 'accessories' folder beside the PMM and returns
# the accessory records for the PMM. Objects are expected to be meshes; the scene is
# stepped through its frame range to read their motion.
def collect_accessories(scene, settings, objects, pmm_path):
    folder = os.path.join(os.path.dirname(os.path.abspath(pmm_path)), PROP_FOLDER)
    os.makedirs(folder, exist_ok=True)
    unit = export_scale(settings)
    wanted = {obj: sample_frames(obj, scene) for obj in objects}
    palettes = {
        item.target: {c.name: tuple(c.color) for c in item.colors}
        for item in settings.export_props
    }
    saved = scene.frame_current
    scene.frame_set(scene.frame_start)
    try:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        rest = {
            obj: tuple(abs(v) or 1.0 for v in obj.matrix_world.to_scale()) for obj in objects
        }
        meshes = {
            obj: x_mesh(obj, depsgraph, rest[obj], unit, palettes.get(obj, {})) for obj in objects
        }
    finally:
        scene.frame_set(saved)
    samples = sample_objects(scene, wanted)
    taken = set()
    accessories = []
    for index, obj in enumerate(objects):
        acc = PmmAccessory()
        acc.index = index
        acc.name = prop_file_name(obj, taken)
        acc.path = os.path.join(folder, acc.name)
        acc.draw_order = index
        acc.keys = accessory_keys(samples[obj], rest[obj], scene, unit)
        with open(acc.path, "w", encoding="ascii", newline="\n") as stream:
            stream.write(build_x_text(meshes[obj]))
        log.info("prop '%s': %s, %d key(s)", obj.name, acc.path, len(acc.keys))
        accessories.append(acc)
    return accessories
