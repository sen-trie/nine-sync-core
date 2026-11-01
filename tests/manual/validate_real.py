import addon_utils
import bpy
import importlib
import math
import os
import sys
import traceback

from collections import Counter
from mathutils import Quaternion, Vector


# Real sync check for Blender 5.3 with mmd_tools. Needs a profile with mmd_tools and
# core installed (see tests/README.md), then:
#   blender --background --python tests/manual/validate_real.py -- file.pmm
# Expected values are worked out here from the parsed PMM and MMD's conventions, not
# read back from the sync code. It never saves a .blend or touches the PMM.
ARGS = sys.argv[sys.argv.index("--") + 1 :]
PMM_PATH = ARGS[0]
TOL = 1e-3
failed = []


def check(label, ok, detail=""):
    print("%s %s%s" % ("ok  " if ok else "FAIL", label, " | %s" % (detail,) if detail != "" else ""))
    if not ok:
        failed.append(label)


def enable_all():
    names = sorted(m.__name__ for m in addon_utils.modules())
    wanted = [n for n in names if n.endswith((".mmd_tools", ".ninesync", ".ninesync_fx"))]
    for name in wanted:
        addon_utils.enable(name, default_set=False)
    return next(n for n in wanted if n.endswith(".ninesync")), wanted


package, enabled = enable_all()
print("enabled:", enabled, "| Blender", bpy.app.version_string)
pmm = importlib.import_module(package + ".pmm")
engine = importlib.import_module(package + ".sync.engine")
scale_mod = importlib.import_module(package + ".sync.scale")
animation = importlib.import_module(package + ".common.animation")
report = importlib.import_module(package + ".sync.report")
uv_morphs = importlib.import_module(package + ".sync.uv_morphs")


def fresh_project():
    with open(PMM_PATH, "rb") as handle:
        return pmm.parse_pmm(handle.read())


def actions_of(owner):
    anim = owner.animation_data if owner else None
    if anim is None:
        return []
    found = [anim.action] if anim.action else []
    for track in anim.nla_tracks:
        found += [s.action for s in track.strips if s.action]
    return list(dict.fromkeys(found))


def curves_of(owner):
    table = {}
    for action in actions_of(owner):
        for curve in animation.iter_fcurves(action):
            table.setdefault((curve.data_path, curve.array_index), curve)
    return table


def frames_of(curves):
    return sorted({round(p.co[0]) for c in curves for p in c.keyframe_points})


def vector_at(table, path, size, frame):
    curves = [table.get((path, i)) for i in range(size)]
    if any(c is None for c in curves):
        return None
    return [c.evaluate(frame) for c in curves]


def quaternion_angle(q):
    return 2.0 * math.acos(min(1.0, abs(q[3] if len(q) == 4 else 1.0)))


def moved(key):
    return any(abs(v) > 1e-6 for v in key.location) or quaternion_angle(key.rotation) > 1e-5


def import_models(project):
    roots = {}
    for model in project.models:
        if not model.path or not os.path.exists(model.path):
            print("no PMX for", model.name, repr(model.path))
            continue
        before = set(bpy.data.objects)
        bpy.ops.mmd_tools.import_model(filepath=model.path, scale=0.08, types={"MESH", "ARMATURE", "MORPHS"})
        made = [o for o in set(bpy.data.objects) - before if o.mmd_type == "ROOT"]
        roots[model.index] = made[0]
    return roots


def armature_of(root):
    return next(o for o in root.children_recursive if o.type == "ARMATURE")


# Weight of a morph track at a frame: linear between keys, held outside them.
def weight_at(keys, frame):
    if frame <= keys[0].frame:
        return keys[0].weight
    for a, b in zip(keys, keys[1:]):
        if frame <= b.frame:
            return a.weight + (b.weight - a.weight) * (frame - a.frame) / max(1, b.frame - a.frame)
    return keys[-1].weight


# Vertex morphs a morph ends up driving, walking nested group morphs.
def leaves(groups, name, factor, depth=0):
    if name not in groups:
        return [(name, factor)] if depth == 0 else []
    found = []
    for offset in groups[name].data:
        if offset.morph_type == "group_morphs" and depth < 7:
            found += leaves(groups, offset.name, factor * offset.factor, depth + 1)
        elif offset.morph_type in ("vertex_morphs", "uv_morphs"):
            found.append((offset.name, factor * offset.factor))
    return found


# A shape key shows its own keys plus each keyed group's weight times its factor,
# clamped to the slider, as MMD adds them.
def check_morphs(model, root, label):
    groups = {g.name: g for g in root.mmd_root.group_morphs}
    tracks = {}
    for name, keys in zip(model.morph_names, model.morphs):
        if keys and any(abs(k.weight) > 1e-6 for k in keys):
            for leaf, factor in leaves(groups, name, 1.0):
                tracks.setdefault(leaf, []).append((keys, factor))
    shapes = {}
    for mesh in (o for o in root.children_recursive if o.type == "MESH" and o.data.shape_keys):
        shapes.setdefault(mesh.data.shape_keys.as_pointer(), mesh.data.shape_keys)
    bad = []
    checked = 0
    uv_checked = 0
    for leaf, items in tracks.items():
        is_uv = leaf in root.mmd_root.uv_morphs
        block_name = uv_morphs.uv_key_name(leaf) if is_uv else leaf
        owners = [k for k in shapes.values() if block_name in k.key_blocks]
        if not owners:
            continue
        block = owners[0].key_blocks[block_name]
        curve = curves_of(owners[0]).get((block.path_from_id("value"), 0))
        for frame in sorted({k.frame for keys, _ in items for k in keys}):
            total = sum(factor * weight_at(keys, frame) for keys, factor in items)
            want = min(max(total, block.slider_min), block.slider_max)
            got = curve.evaluate(frame) if curve else 0.0
            checked += 1
            uv_checked += 1 if is_uv else 0
            if abs(got - want) > TOL:
                bad.append((leaf, frame, round(want, 3), round(got, 3)))
    check("%s: morph values with groups expanded" % label, not bad, ("%d values, %d of them UV" % (checked, uv_checked)) if not bad else bad[:6])


def check_model(model, root, factor, label):
    armature = armature_of(root)
    table = curves_of(armature)
    unit = scale_mod.current_scale(bpy.context.scene.mmd_bridge)
    by_japanese = {b.mmd_bone.name_j: b for b in armature.pose.bones if b.mmd_bone.name_j}
    frame_bad = Counter()
    value_bad = []
    keyed = 0
    absent = 0
    lost = 0
    for name, keys in zip(model.bone_names, model.bones):
        bone = by_japanese.get(name) or armature.pose.bones.get(name)
        if bone is None:
            absent += 1
            lost += sum(1 for k in keys if moved(k))
            continue
        path = 'pose.bones["%s"]' % bone.name
        curves = [c for (p, _), c in table.items() if p.startswith(path + ".")]
        moving = [k for k in keys if moved(k)]
        wanted = sorted(k.frame for k in keys)
        got = frames_of(curves)
        if moving and not set(wanted) <= set(got):
            frame_bad["missing frames"] += 1
        for key in keys:
            keyed += 1
            location = vector_at(table, path + ".location", 3, key.frame)
            rotation = vector_at(table, path + ".rotation_quaternion", 4, key.frame)
            want_len = Vector(key.location).length * unit
            want_angle = quaternion_angle(key.rotation)
            got_len = Vector(location).length if location else 0.0
            got_angle = quaternion_angle(tuple(rotation[1:]) + (rotation[0],)) if rotation else 0.0
            if abs(got_len - want_len) > TOL * max(1, want_len) or abs(got_angle - want_angle) > 2 * TOL:
                value_bad.append((name, key.frame, round(want_len, 4), round(got_len, 4), round(want_angle, 4), round(got_angle, 4)))
    check("%s: bone keys frames" % label, not frame_bad, "%d keys, %d bones not in PMX" % (keyed, absent))
    check("%s: no real motion on unmatched bones" % label, not lost, lost or "")
    check("%s: bone location length and rotation angle" % label, not value_bad, value_bad[:4] or "")
    check_morphs(model, root, label)
    return keyed


def check_camera(project, label):
    scene = bpy.context.scene
    unit = scale_mod.current_scale(scene.mmd_bridge)
    camera = scene.camera
    root = camera.parent
    table = curves_of(root)
    distance = curves_of(camera).get(("location", 1))
    wanted = sorted(k.frame for k in project.camera_keys)
    got = frames_of([c for (p, _), c in table.items() if p in ("location", "rotation_euler")])
    check("%s: camera frames" % label, set(wanted) <= set(got), (wanted[:6], got[:6]))
    bad = []
    for key in project.camera_keys:
        location = vector_at(table, "location", 3, key.frame)
        rotation = vector_at(table, "rotation_euler", 3, key.frame)
        want = [key.look_at[0] * unit, key.look_at[2] * unit, key.look_at[1] * unit]
        fov = table[("mmd_camera.angle", 0)].evaluate(key.frame)
        rows = [
            (location, want, "look_at"),
            ([distance.evaluate(key.frame)], [key.distance * unit], "distance"),
            ([fov], [math.radians(key.fov)], "fov"),
            (rotation, [key.angle[0], key.angle[2], key.angle[1]], "angle"),
        ]
        for got_row, want_row, name in rows:
            if any(abs(a - b) > TOL * 5 for a, b in zip(got_row, want_row)):
                bad.append((key.frame, name, [round(v, 3) for v in want_row], [round(v, 3) for v in got_row]))
    check("%s: camera values" % label, not bad, bad[:4] or "")


def check_light(project, label):
    light = bpy.data.objects["MMD_Sun_light"]
    table = curves_of(light)
    colors = curves_of(light.data)
    bad = []
    for key in project.light_keys:
        location = vector_at(table, "location", 3, key.frame)
        color = vector_at(colors, "color", 3, key.frame)
        want = [-key.direction[0], -key.direction[2], -key.direction[1]]
        if any(abs(a - b) > TOL for a, b in zip(location, want)) or any(abs(a - b) > TOL for a, b in zip(color, key.color)):
            bad.append((key.frame, want, location, key.color, color))
    check("%s: light colour and direction" % label, not bad, "%d keys" % len(project.light_keys) if not bad else bad[:3])
    # MMD blends light keys linearly and cuts on keys one frame apart.
    modes = {p.interpolation for c in list(table.values()) + list(colors.values()) for p in c.keyframe_points}
    check("%s: light keys not eased" % label, modes <= {"LINEAR", "CONSTANT"}, sorted(modes))
    check("%s: light actions stay separate" % label, actions_of(light) != actions_of(light.data))


def check_audio(project, label):
    scene = bpy.context.scene
    if not (project.audio_enabled and project.audio_path and os.path.exists(project.audio_path)):
        print("audio: PMM has none on disk")
        return
    strips = scene.sequence_editor.strips_all if scene.sequence_editor else []
    sounds = [s for s in strips if s.type == "SOUND"]
    check("%s: one sound strip at frame 0" % label, len(sounds) == 1 and sounds[0].frame_start == 0, [(s.name, s.frame_start) for s in sounds])
    same = sounds and os.path.normcase(bpy.path.abspath(sounds[0].sound.filepath)) == os.path.normcase(project.audio_path)
    check("%s: strip uses the PMM audio file" % label, bool(same))


def key_total(root):
    return sum(len(c.keyframe_points) for c in curves_of(armature_of(root)).values())


def sync(label):
    result = bpy.ops.ninesync.sync_now()
    settings = bpy.context.scene.mmd_bridge
    check("%s: sync finished" % label, "FINISHED" in result, settings.status)
    for target in settings.targets:
        print("   target", target.pmm_name, target.state, target.status_text, target.target.name if target.target else None)


def check_all(project, roots, factor, label):
    by_index = {m.index: m for m in project.models}
    for index, root in roots.items():
        check_model(by_index[index], root, factor, "%s/%s" % (label, by_index[index].name))
    if project.camera_keys:
        check_camera(project, label)
    if project.light_keys:
        check_light(project, label)
    check_audio(project, label)


def main():
    project = fresh_project()
    print("PMM:", os.path.basename(PMM_PATH), "models", len(project.models), "camera", len(project.camera_keys), "light", len(project.light_keys), "accessories", len(project.accessories))
    scene = bpy.context.scene
    scene.render.fps = 30
    scene.render.fps_base = 1.0
    roots = import_models(project)
    settings = scene.mmd_bridge
    settings.filepath = PMM_PATH
    sync("30 fps")
    check_all(project, roots, 1.0, "30 fps")
    first_totals = {i: key_total(r) for i, r in roots.items()}

    sync("resync")
    check("resync adds no keys", first_totals == {i: key_total(r) for i, r in roots.items()})
    sounds = [s for s in (scene.sequence_editor.strips_all if scene.sequence_editor else []) if s.type == "SOUND"]
    check("resync keeps one sound strip", len(sounds) <= 1)

    settings.retime_fps = True
    settings.update_scene_settings = False
    scene.render.fps = 60
    sync("60 fps")
    retimed = fresh_project()
    engine.retime_project(retimed, 2.0)
    check_all(retimed, roots, 2.0, "60 fps")

    text = report.build_report(bpy.context)
    print("report lines:", len(text.splitlines()))
    check("report has no failed hook", "failed:" not in text)
    fx_present = any(n.endswith(".ninesync_fx") for n in enabled)
    if fx_present:
        count = len([o for o in bpy.data.objects if o.get("mmd_bridge_accessory")])
        print("FX accessories imported through post_sync:", count, "of", len(project.accessories))
        check("FX lines in the report", "FX" in text)
    bpy.ops.ninesync.clean(animation=True, pose=True, uv=True, scene=True, audio=True, fx=fx_present)
    if fx_present:
        count = len([o for o in bpy.data.objects if o.get("mmd_bridge_accessory")])
        check("clean through the FX hook removed accessories", count == 0, count)
    states = [(t.pmm_name, t.status_text) for t in settings.targets if t.target is not None]
    check("clean finished every model", all(text == "cleaned" for _, text in states), states)
    left = sum(len(actions_of(armature_of(r))) for r in roots.values())
    check("clean removed model animation", left == 0, left)
    bridge = [a.name for a in bpy.data.actions if a.name.startswith("MMDBridge_")]
    check("clean left no bridge actions", not bridge, bridge[:6] or "")
    posed = [
        b.name
        for r in roots.values()
        for b in armature_of(r).pose.bones
        if b.location.length > 1e-6 or Quaternion(b.rotation_quaternion).angle > 1e-5
    ]
    check("clean rested the bones", not posed, posed[:6] or "")


try:
    main()
except Exception:
    traceback.print_exc()
    failed.append("crashed")
print("FAILED:", failed)
sys.stdout.flush()
os._exit(1 if failed else 0)
