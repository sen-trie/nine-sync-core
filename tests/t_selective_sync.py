import importlib
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import Checks, load, stage

import bpy


# Camera and light planned and imported each on its own, and model visibility from the
# PMM display track. Runs without mmd_tools: rigs are plain objects tagged with the
# mmd_type mmd_tools would give them, and the camera and light importers are replaced
# by stand-ins that key the rig, so the bookkeeping around them is what is tested.
stage("ninesync")
core = load("ninesync")
core.register()
pmm = importlib.import_module("ninesync.pmm")
motion = importlib.import_module("ninesync.sync.motion")
plan_mod = importlib.import_module("ninesync.sync.plan")
state = importlib.import_module("ninesync.sync.sync_state")
elements = importlib.import_module("ninesync.sync.scene_elements")
visibility = importlib.import_module("ninesync.sync.visibility")
retime = importlib.import_module("ninesync.sync.retime")
animation = importlib.import_module("ninesync.common.animation")
constants = importlib.import_module("ninesync.common.constants")
scene_utils = importlib.import_module("ninesync.common.scene_utils")
checks = Checks()
if not hasattr(bpy.types.Object, "mmd_type"):
    bpy.types.Object.mmd_type = bpy.props.StringProperty()
INTERP = bytes(range(24))


# Display track parsing, from bytes laid out as MMD writes them: the chain order is
# 0 -> 2 -> 1, which is not the record order.
def display_record(frame, previous, next_index, visible, iks, outside, index=None):
    head = struct.pack("<i", index) if index is not None else b""
    body = struct.pack("<iiiB", frame, previous, next_index, visible)
    return head + body + bytes([1] * iks) + bytes(8 * outside) + b"\x00"


data = (
    display_record(0, 0, 2, 1, 2, 1)
    + struct.pack("<i", 2)
    + display_record(20, 2, 0, 1, 2, 1, index=1)
    + display_record(10, 0, 1, 0, 2, 1, index=2)
    + b"END"
)
reader = pmm.Reader(data)
track = pmm.read_visibility_track(reader, 2, 1)
checks.check("display track follows the chain", [(k.frame, k.visible) for k in track] == [(0, 1), (10, 0), (20, 1)], track)
checks.check("display track consumes its bytes", data[reader.pos:] == b"END", reader.pos)
looped = (
    display_record(0, 0, 1, 0, 0, 0)
    + struct.pack("<i", 1)
    + display_record(5, 0, 1, 1, 0, 0, index=1)
)
checks.check("a looping chain ends", len(pmm.read_visibility_track(pmm.Reader(looped), 0, 0)) == 2)
try:
    pmm.read_visibility_track(pmm.Reader(data[:20]), 2, 1)
    checks.check("truncated display track fails", False)
except pmm.PmmError:
    checks.check("truncated display track fails", True)
kagamine = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "kagamine.pmm")
with open(kagamine, "rb") as handle:
    real = pmm.parse_pmm(handle.read())
checks.check("real PMM has a shown frame 0 key", [(k.frame, k.visible) for k in real.models[0].visibility] == [(0, 1)])
checks.check("real PMM still reads past the models", not real.scene_error and real.camera_keys)


def vis_keys(*pairs):
    return [pmm.VisibilityKey(f, v) for f, v in pairs]


def make_model(name="m", shown=((0, 1),)):
    model = pmm.PmmModel()
    model.name = name
    model.bone_names = ["center"]
    model.bones = [[pmm.BoneKey(0, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), bytes(16))]]
    model.visibility = vis_keys(*shown)
    return model


# Visibility fingerprints.
hidden = make_model(shown=((0, 1), (10, 0)))
checks.check("shown-only track does not hide", not motion.visibility_hides(make_model().visibility))
checks.check("hidden key hides", motion.visibility_hides(hidden.visibility))
checks.check("visibility fingerprint repeats", motion.visibility_fingerprint(hidden) == motion.visibility_fingerprint(make_model(shown=((0, 1), (10, 0)))))
checks.check("visibility change detected", motion.visibility_fingerprint(hidden) != motion.visibility_fingerprint(make_model(shown=((0, 1), (11, 0)))))
checks.check("bone fingerprint ignores visibility", motion.motion_fingerprint(hidden) == motion.motion_fingerprint(make_model()))
blink = make_model(shown=((0, 1), (30, 0), (31, 1)))
project = pmm.PmmProject()
project.models = [blink]
retime.retime_project(project, 0.5)
checks.check("retime keeps a one-frame hide", [(k.frame, k.visible) for k in blink.visibility] == [(0, 1), (15, 0), (16, 1)], blink.visibility)
summary = motion.summarize_model(hidden)
checks.check("summary counts hidden keys", summary.hidden_keys == 1 and summary.visibility_keys == 2)


# Camera and light fingerprints: every field counts, and they are independent.
def camera_key(frame=0, **changes):
    key = pmm.CameraKey(frame, -45.0, (0.0, 10.0, 0.0), (0.0, 0.0, 0.0), -1, 0, INTERP, 0, 30)
    return key._replace(**changes)


def light_key(frame=0, **changes):
    return pmm.LightKey(frame, (0.6, 0.6, 0.6), (-0.5, -1.0, 0.5))._replace(**changes)


def cam_print(keys):
    return motion.keys_fingerprint(keys, motion.pack_camera_key)


base_camera = cam_print([camera_key()])
checks.check("camera fingerprint repeats", base_camera == cam_print([camera_key()]))
for field, value in (
    ("frame", 1),
    ("distance", -40.0),
    ("look_at", (0.0, 11.0, 0.0)),
    ("angle", (0.1, 0.0, 0.0)),
    ("parent_model", 0),
    ("parent_bone", 3),
    ("interpolation", bytes(24)),
    ("perspective", 1),
    ("fov", 31),
):
    checks.check("camera %s change detected" % field, cam_print([camera_key(**{field: value})]) != base_camera)
base_light = motion.keys_fingerprint([light_key()], motion.pack_light_key)
for field, value in (("frame", 2), ("color", (1.0, 0.6, 0.6)), ("direction", (0.5, -1.0, 0.5))):
    checks.check(
        "light %s change detected" % field,
        motion.keys_fingerprint([light_key(**{field: value})], motion.pack_light_key) != base_light,
    )


# Fake camera and light rigs, found the way the planner finds the real ones.
scene = bpy.context.scene
settings = scene.mmd_bridge
settings.filepath = kagamine
settings.auto_scale = False
settings.scale = 1.0


def make_rig(kind):
    collection_name = constants.CAMERA_COLLECTION if kind == "camera" else constants.LIGHT_COLLECTION
    collection = scene_utils.ensure_collection(scene, collection_name)
    root = bpy.data.objects.new(kind + "_root", None)
    root.mmd_type = kind.upper()
    data = bpy.data.cameras.new(kind) if kind == "camera" else bpy.data.lights.new(kind, "SUN")
    child = bpy.data.objects.new(kind + "_child", data)
    child.parent = root
    collection.objects.link(root)
    collection.objects.link(child)
    return root, child


rigs = {"camera": make_rig("camera"), "light": make_rig("light")}
imports = []


# Stand-in importers: key the rig root like mmd_tools would, under the bridge prefix.
def fake_import(kind):
    def run(scene_, project_, settings_):
        imports.append(kind)
        root = rigs[kind][0]
        keys = project_.camera_keys if kind == "camera" else project_.light_keys
        for key in keys:
            root.location = (key.frame * 0.1, 0.0, 0.0)
            root.keyframe_insert("location", frame=key.frame)
        root.animation_data.action.name = constants.ACTION_PREFIX + ("Scene" if kind == "camera" else "Light")
    return run


elements.ELEMENTS["camera"] = (lambda s, st: rigs["camera"], "camera_keys", "Scene", fake_import("camera"))
elements.ELEMENTS["light"] = (lambda s, st: rigs["light"], "light_keys", "Light", fake_import("light"))


def scene_project(camera=None, light=None):
    project = pmm.PmmProject()
    project.camera_keys = camera or [camera_key(0), camera_key(20, distance=-30.0)]
    project.light_keys = light or [light_key(0), light_key(15, color=(1.0, 0.0, 0.0))]
    return project


def sync_scene(project, force=False):
    imports.clear()
    plan = plan_mod.build_plan(scene, project, force)
    done = elements.import_scene_elements(scene, project, settings, plan)
    return plan, done


def ops(plan):
    return {kind: part.operation for kind, part in plan.scene.items()}


project = scene_project()
before = (len(bpy.data.actions), settings.scene_record)
plan = plan_mod.build_plan(scene, project)
checks.check("scene planning writes nothing", before == (len(bpy.data.actions), settings.scene_record))
checks.check("first sync creates both", ops(plan) == {"camera": plan_mod.CREATE, "light": plan_mod.CREATE}, ops(plan))
plan, done = sync_scene(project)
checks.check("both imported", done == 2 and sorted(imports) == ["camera", "light"], imports)
record = json.loads(settings.scene_record)
checks.check("record holds both parts", sorted(record["parts"]) == ["camera", "light"])
checks.check("record keeps no path", kagamine not in settings.scene_record)
actions = {k: rigs[k][0].animation_data.action for k in rigs}
plan, done = sync_scene(scene_project())
checks.check("unchanged PMM skips both", ops(plan) == {"camera": plan_mod.SKIP, "light": plan_mod.SKIP} and done == 0, ops(plan))
checks.check("skipped actions are the same datablocks", all(rigs[k][0].animation_data.action == actions[k] for k in rigs))

plan, done = sync_scene(scene_project(camera=[camera_key(0), camera_key(20, fov=45)]))
checks.check("camera edit: camera updates, light skips", ops(plan) == {"camera": plan_mod.UPDATE, "light": plan_mod.SKIP}, ops(plan))
checks.check("camera edit imports only the camera", imports == ["camera"], imports)
checks.check("light action untouched by a camera update", rigs["light"][0].animation_data.action == actions["light"])
light_only = scene_project(camera=[camera_key(0), camera_key(20, fov=45)], light=[light_key(0)])
plan, done = sync_scene(light_only)
checks.check("light edit: light updates, camera skips", ops(plan) == {"camera": plan_mod.SKIP, "light": plan_mod.UPDATE}, ops(plan))
checks.check("light edit imports only the light", imports == ["light"], imports)

settings.sync_camera = False
plan, done = sync_scene(scene_project(camera=[camera_key(0, fov=10)], light=[light_key(0), light_key(5)]))
checks.check("camera off is left alone", ops(plan)["camera"] == plan_mod.SKIP and "off" in plan.scene["camera"].reason and imports == ["light"], imports)
settings.sync_camera = True

current = scene_project(camera=[camera_key(0, fov=10)], light=[light_key(0), light_key(5)])
sync_scene(current)
plan, done = sync_scene(current, force=True)
checks.check("force reimports both", sorted(imports) == ["camera", "light"] and plan.scene["camera"].reason == "Force Resync", imports)

# Destination checks.
fcurve = animation.iter_fcurves(rigs["camera"][0].animation_data.action)[0]
fcurve.keyframe_points[0].co[1] = 5.0
plan = plan_mod.build_plan(scene, current)
checks.check("edited camera is kept and named", ops(plan)["camera"] == plan_mod.SKIP and "edits" in plan.scene["camera"].reason, plan.scene["camera"].reason)
settings.follow_rotation = not settings.follow_rotation
plan = plan_mod.build_plan(scene, current)
checks.check("settings change over an edit blocks", ops(plan)["camera"] == plan_mod.BLOCKED, ops(plan))
checks.check("camera settings leave the light alone", ops(plan)["light"] == plan_mod.SKIP, ops(plan))
settings.follow_rotation = not settings.follow_rotation
rigs["camera"][0].animation_data.action = None
plan = plan_mod.build_plan(scene, current)
checks.check("removed camera action rebuilds", plan.scene["camera"].runs and ops(plan)["light"] == plan_mod.SKIP, ops(plan))
sync_scene(current)
rigs["light"][0].animation_data.action.name = "renamed by user"
plan = plan_mod.build_plan(scene, current)
checks.check("renamed but unchanged action stays valid", ops(plan)["light"] == plan_mod.SKIP, plan.scene["light"].reason)

# Out-of-span user keys survive a rebuild of their element.
root = rigs["light"][0]
light_action = root.animation_data.action
user_curve = next(c for c in animation.iter_fcurves(light_action) if c.array_index == 1)
user_curve.keyframe_points.insert(500.0, 7.0)
plan, done = sync_scene(scene_project(camera=[camera_key(0, fov=10)], light=[light_key(0), light_key(9)]))
kept = animation.find_fcurve(root, "location", 1)
checks.check("user key outside the span survives", kept is not None and any(abs(p.co[0] - 500.0) < 1e-3 for p in kept.keyframe_points))

# A failure in one element leaves the other free and forgets only its own record.
broken = elements.ELEMENTS["camera"]


def explode(*_args):
    raise RuntimeError("expected test failure")


elements.ELEMENTS["camera"] = (broken[0], broken[1], broken[2], explode)
plan, done = sync_scene(scene_project(camera=[camera_key(0, fov=99)], light=[light_key(0), light_key(3)]))
record = json.loads(settings.scene_record)
checks.check("light imports after a camera failure", done == 1 and imports == ["light"], imports)
checks.check("failed camera record forgotten", "camera" not in record["parts"] and "light" in record["parts"], sorted(record["parts"]))
elements.ELEMENTS["camera"] = broken

failing = scene_project()
failing.scene_error = "unexpected end of file"
plan, done = sync_scene(failing)
checks.check("unreadable scene section blocks both", ops(plan) == {"camera": plan_mod.BLOCKED, "light": plan_mod.BLOCKED} and done == 0)
other = settings.filepath
settings.filepath = os.path.join(os.path.dirname(kagamine), "other.pmm")
plan = plan_mod.build_plan(scene, scene_project())
checks.check("another PMM is not trusted", plan.scene["light"].runs and plan.scene["light"].reason == "PMM file changed", plan.scene["light"].reason)
settings.filepath = other


# Visibility on a plain rig.
def make_target(name):
    root = bpy.data.objects.new(name, None)
    root.mmd_type = "ROOT"
    armature = bpy.data.objects.new(name + "_arm", bpy.data.armatures.new(name))
    armature.parent = root
    meshes = []
    for index in range(2):
        mesh = bpy.data.objects.new("%s_mesh%d" % (name, index), bpy.data.meshes.new(name))
        mesh.parent = armature
        meshes.append(mesh)
    for obj in [root, armature] + meshes:
        scene.collection.objects.link(obj)
    return root, meshes


root, meshes = make_target("vis")
bpy.context.view_layer.update()
# The user's own object animation on a mesh, which visibility must not replace.
user_action = bpy.data.actions.new("user keys")
meshes[0].animation_data_create().action = user_action
meshes[0].keyframe_insert("location", frame=1)
model = make_model("vis", shown=((0, 0), (10, 1), (20, 0)))
row = settings.targets.add()
row.key = scene_utils.model_key(model)
row.pmm_name = "vis"
row.target = root
vis_project = scene_project()
vis_project.models = [model]
plan = plan_mod.build_plan(scene, vis_project)
checks.check("visibility off by default", "visible" not in plan.items[0].parts, sorted(plan.items[0].parts))
settings.sync_visibility = True
plan = plan_mod.build_plan(scene, vis_project)
part = plan.items[0].parts.get("visible")
checks.check("hidden model plans visibility", part is not None and part.operation == plan_mod.CREATE, part and part.operation)
checks.check("visibility destination named", part is not None and part.destination == "MMDBridge_vis_visibility", part and part.destination)

label = "vis"
visibility.apply_visibility(meshes, model, label)
action = visibility.find_visibility_action(meshes[1], label)
checks.check("visibility action on every mesh", all(visibility.find_visibility_action(m, label) == action for m in meshes) and action is not None)
checks.check("user active action kept", meshes[0].animation_data.action == user_action)
seen = []
for frame in (-5, 0, 5, 10, 15, 20, 40):
    scene.frame_set(frame)
    seen.append((frame, meshes[1].hide_viewport, meshes[1].hide_render, meshes[0].hide_render))
checks.check(
    "meshes hide and show on the PMM frames",
    seen == [(-5, True, True, True), (0, True, True, True), (5, True, True, True), (10, False, False, False), (15, False, False, False), (20, True, True, True), (40, True, True, True)],
    seen,
)
visibility.apply_visibility(meshes, model, label)
tracks = [t for t in meshes[0].animation_data.nla_tracks if t.name == visibility.visibility_track_name(label)]
checks.check("resync replaces, not stacks", len(tracks) == 1 and len([a for a in bpy.data.actions if a.name.startswith("MMDBridge_vis_visibility")]) == 1)
long_label = "ショタメイド白ver2_01_for_Ghost_Dance_with_foresk"
long_model = make_model(long_label, shown=((0, 0), (5, 1)))
visibility.apply_visibility(meshes, long_model, long_label)
visibility.apply_visibility(meshes, long_model, long_label)
long_tracks = [t for t in meshes[1].animation_data.nla_tracks if t.name == visibility.visibility_track_name(long_label)]
checks.check("long model names are found again", len(long_tracks) == 1 and visibility.find_visibility_action(meshes[1], long_label) is not None, [t.name for t in meshes[1].animation_data.nla_tracks])
animation.clear_bridge_nla(meshes, keep=(visibility.visibility_track_name(long_label), visibility.visibility_track_name(label)))
checks.check("long names are kept by the model import", visibility.find_visibility_action(meshes[1], long_label) is not None)
visibility.remove_visibility(meshes, long_label)
checks.check("long names are removed", visibility.find_visibility_action(meshes[1], long_label) is None)
animation.clear_bridge_nla(meshes, keep=(visibility.visibility_track_name(label),))
checks.check("model import keeps the visibility track", visibility.find_visibility_action(meshes[0], label) is not None)

# Records: an unchanged track skips, an edit updates, an emptied track removes.
entry = plan.items[0]
layout = state.rig_layout(root)
state.write_parts(row, entry, layout, settings, label, ("visible",))
plan = plan_mod.build_plan(scene, vis_project)
checks.check("unchanged visibility skips", plan.items[0].parts["visible"].operation == plan_mod.SKIP, plan.items[0].parts["visible"].reason)
checks.check("visibility leaves the other parts planned on their own", "motion" in plan.items[0].parts)
model.visibility = vis_keys((0, 1), (30, 0))
plan = plan_mod.build_plan(scene, vis_project)
checks.check("changed visibility updates", plan.items[0].parts["visible"].operation == plan_mod.UPDATE, plan.items[0].parts["visible"].reason)
model.visibility = vis_keys((0, 1))
plan = plan_mod.build_plan(scene, vis_project)
part = plan.items[0].parts.get("visible")
checks.check("emptied track plans a removal", part is not None and part.runs and "removed" in part.reason, part and part.reason)
scene.frame_set(0)
visibility.apply_visibility(meshes, model, label)
checks.check("removal drops the owned keys only", visibility.find_visibility_action(meshes[0], label) is None and meshes[0].animation_data.action == user_action)
checks.check("removal leaves the meshes shown", not any(m.hide_viewport or m.hide_render for m in meshes))
checks.check("removed action deleted", not any(a.name.startswith("MMDBridge_vis_visibility") for a in bpy.data.actions))
state.write_parts(row, plan.items[0], layout, settings, label, ("visible",))
plan = plan_mod.build_plan(scene, vis_project)
checks.check("never-hidden model with no keys plans nothing", "visible" not in plan.items[0].parts, sorted(plan.items[0].parts))

# Create PMM: model visibility read back from Blender and written as MMD's display track.
writer = importlib.import_module("ninesync.pmm_writer")
export = importlib.import_module("ninesync.sync.project_export")
pairs = lambda keys: [(k.frame, k.visible) for k in keys]
scene.render.fps, scene.render.fps_base = 30, 1.0
raw = writer.write_visibility_track(vis_keys((0, 0), (10, 1), (25, 0)), 2) + b"END"
reader = pmm.Reader(raw)
checks.check("display track written as MMD reads it", pairs(pmm.read_visibility_track(reader, 2, 0)) == [(0, 0), (10, 1), (25, 0)] and raw[reader.pos:] == b"END")
checks.check("empty track writes one shown key", pairs(pmm.read_visibility_track(pmm.Reader(writer.write_visibility_track([], 1)), 1, 0)) == [(0, 1)])
template = open(kagamine, "rb").read()
base = pmm.parse_pmm(template)
written = base.models[0]
written.visibility = vis_keys((0, 1), (40, 0), (90, 1))
out = pmm.parse_pmm(writer.build_pmm(template, [written], base.camera_keys))
checks.check("Create PMM keeps visibility keys", pairs(out.models[0].visibility) == [(0, 1), (40, 0), (90, 1)], pairs(out.models[0].visibility))
written.outside_parents = [-1, 0, 2]
out = pmm.parse_pmm(writer.build_pmm(template, [written], base.camera_keys))
checks.check("outside parent slots written", out.models[0].outside_parents == [-1, 0, 2] and pairs(out.models[0].visibility) == [(0, 1), (40, 0), (90, 1)], out.models[0].outside_parents)
empty = pmm.parse_pmm(writer.build_pmm(template, [], base.camera_keys))
checks.check("Create PMM without models writes a camera-only project", empty.models == [] and len(empty.camera_keys) == len(base.camera_keys))
checks.check("an unkeyed shown model exports no keys", export.collect_visibility(scene, settings, root) == [])
visibility.apply_visibility(meshes, make_model("vis", shown=((0, 0), (10, 1), (20, 0))), label)
checks.check("synced visibility exports back", pairs(export.collect_visibility(scene, settings, root)) == [(0, 0), (10, 1), (20, 0)], pairs(export.collect_visibility(scene, settings, root)))
visibility.remove_visibility(meshes, label)
for mesh in meshes:
    mesh.hide_viewport = mesh.hide_render = False
meshes[0].keyframe_insert("hide_render", frame=5)
meshes[0].hide_render = True
meshes[0].keyframe_insert("hide_render", frame=6)
meshes[0].hide_render = False
meshes[0].keyframe_insert("hide_render", frame=9)
checks.check("one hidden mesh leaves the model shown", export.collect_visibility(scene, settings, root) == [])
meshes[1].hide_viewport = True
checks.check("user keys plus a hidden mesh export the hidden span", pairs(export.collect_visibility(scene, settings, root)) == [(0, 1), (6, 0), (9, 1)], pairs(export.collect_visibility(scene, settings, root)))
scene.render.fps = 60
checks.check("export retimes to 30 fps", pairs(export.collect_visibility(scene, settings, root)) == [(0, 1), (3, 0), (4, 1)] or pairs(export.collect_visibility(scene, settings, root)) == [(0, 1), (3, 0), (5, 1)], pairs(export.collect_visibility(scene, settings, root)))
scene.render.fps = 30
checks.check("frame put back after reading", scene.frame_current == 0, scene.frame_current)
meshes[0].animation_data.action = user_action
meshes[1].hide_viewport = False

for mesh in meshes:
    mesh.parent = None
model.visibility = vis_keys((0, 0))
plan = plan_mod.build_plan(scene, vis_project)
checks.check("no meshes is a warning, not a part", "visible" not in plan.items[0].parts and any("no meshes" in w for w in plan.items[0].warnings), plan.items[0].warnings)

core.unregister()
checks.finish()
