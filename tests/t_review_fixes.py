import importlib
import os
import random
import struct
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import REPO, Checks, load, stage

import bpy


# Fixes from the 2026-11-08 feature and performance review, without mmd_tools: name
# bytes MMD compares (B1), faster hashing and sampling with identical results (P4-P6),
# import format records (P7), parse reuse (P8), IK switches (C1),
# accessory additive (C5), bone names a VMD cannot hold (C8), the
# undo step (C11).
stage("ninesync")
core = load("ninesync")
core.register()
pmm = importlib.import_module("ninesync.pmm")
writer = importlib.import_module("ninesync.pmm_writer")
motion = importlib.import_module("ninesync.sync.motion")
plan_mod = importlib.import_module("ninesync.sync.plan")
morph_keys = importlib.import_module("ninesync.sync.morph_keys")
uv = importlib.import_module("ninesync.sync.uv_morphs")
ik = importlib.import_module("ninesync.sync.ik")
retime = importlib.import_module("ninesync.sync.retime")
engine = importlib.import_module("ninesync.sync.engine")
analysis = importlib.import_module("ninesync.sync.analysis")
operators = importlib.import_module("ninesync.ui.operators")
checks = Checks()
template = open(os.path.join(REPO, "assets", "kagamine.pmm"), "rb").read()
base = pmm.parse_pmm(template)

# B1: MMD keeps 19 bytes of a bone name even when that splits a Shift-JIS character,
# and refuses a project whose bytes differ from what it would have kept.
long_name = "------おなか変形補助"
cut = writer.name_bytes(long_name, writer.BONE_NAME_BYTES)
checks.check("name cut mid-character at 19 bytes", len(cut) == 19 and cut.endswith(b"\x95\xe2\x8f"), cut)
checks.check("short name kept whole", writer.name_bytes("センター", 19) == "センター".encode("cp932"))
checks.check("var_string length prefix", writer.var_string(long_name, 19)[0] == 19)
model = base.models[0]
model.bone_names = list(model.bone_names)
model.bone_names[0] = long_name
model.morph_names = list(model.morph_names)
if model.morph_names:
    model.morph_names[0] = "あいうえおかきくけこ"
written = writer.build_pmm(template, [model], base.camera_keys)
again = pmm.parse_pmm(written)
checks.check("long names pass the read-back check", again.models[0].bone_names[0] == writer.stored_name(long_name, 19))
start = written.find(b"\x13" + cut)
checks.check("PMM holds MMD's 19 bytes", start != -1)
base = pmm.parse_pmm(template)

# P4: the cached VMD interpolation row equals the plain conversion for any input type.
rng = random.Random(3)
for _ in range(50):
    source = bytes(rng.randrange(128) for _ in range(16))
    plain = pmm.vmd_interpolation_row.__wrapped__(source)
    if pmm.interpolation_to_vmd(source) != plain or pmm.interpolation_to_vmd(bytearray(source)) != plain:
        checks.check("cached interpolation row matches", False, source)
        break
else:
    checks.check("cached interpolation row matches", True)


# P5: one pass gives the same hashes as the two separate ones.
def bone_key(frame, x=0.0, w=1.0):
    return pmm.BoneKey(frame, (x, 0.0, 0.0), (0.0, 0.0, 0.0, w), bytes(range(16)))


names = ["a", "b", "c"]
tracks = [[bone_key(0), bone_key(5, 1.0)], [bone_key(0)], []]
whole, table = motion.fingerprint_and_digests(names, tracks, motion.pack_bone_key)
checks.check("one-pass fingerprint equals digest_tracks", whole == motion.digest_tracks(names, tracks, motion.pack_bone_key))
checks.check("one-pass track digests equal track_digests", table == motion.track_digests(names, tracks, motion.pack_bone_key))

# P6: vectorised morph sampling is bit-identical to the loop, key frames included.
bad = 0
for _ in range(400):
    frames = sorted(rng.sample(range(0, 300), rng.randint(1, 10)))
    keys = [pmm.MorphKey(f, struct.unpack("<f", struct.pack("<f", rng.uniform(-1, 1)))[0]) for f in frames]
    probe = sorted(set(frames) | set(rng.sample(range(-5, 310), 25)))
    for frame, value in zip(probe, morph_keys.sample_morph_many(keys, probe)):
        bad += float(value) != morph_keys.sample_morph(keys, frame)
checks.check("vectorised morph sampling is bit-identical", bad == 0, bad)

# C1: IK switches read from the display track.
def display_record(frame, previous, next_index, visible, iks, index=None):
    head = struct.pack("<i", index) if index is not None else b""
    return head + struct.pack("<iiiB", frame, previous, next_index, visible) + bytes(iks) + b"\x00"


data = (
    display_record(0, 0, 2, 1, (1, 1))
    + struct.pack("<i", 2)
    + display_record(20, 2, 0, 1, (1, 1), index=1)
    + display_record(10, 0, 1, 1, (0, 1), index=2)
)
shown, ik_keys = pmm.read_display_track(pmm.Reader(data), 2, 0)
checks.check("IK states follow the chain", [(k.frame, k.states) for k in ik_keys] == [(0, (1, 1)), (10, (0, 1)), (20, (1, 1))], ik_keys)
ik_model = pmm.PmmModel()
ik_model.bone_names = ["leg", "左足ＩＫ", "右足ＩＫ"]
ik_model.constraints = [1, 2]
ik_model.ik_keys = ik_keys
checks.check("only switched IKs are listed", motion.ik_switches(ik_model) == {0: [(0, 1), (10, 0), (20, 1)]})
checks.check("IK switches change the fingerprint", motion.ik_fingerprint(ik_model) != "")
ik_model.ik_keys = [pmm.IkKey(0, (1, 1)), pmm.IkKey(10, (1, 1))]
checks.check("all-on IK adds nothing", motion.ik_fingerprint(ik_model) == "" and motion.ik_switches(ik_model) == {})
checks.check("kagamine has no IK switches", motion.ik_switches(base.models[0]) == {})
ik_model.ik_keys = ik_keys
retimed = types.SimpleNamespace(models=[ik_model], camera_keys=[], light_keys=[], accessories=[], play_range=pmm.PlayRange(30, 300, True, True, False))
for name in ("bones", "morphs", "visibility"):
    setattr(ik_model, name, [])
retime.retime_project(retimed, 0.8)
checks.check("IK keys retimed", [k.frame for k in ik_model.ik_keys] == [0, 8, 16], ik_model.ik_keys)
checks.check("play range retimed", retimed.play_range[:2] == (24, 240), retimed.play_range)

# C1 in Blender: IK and the chain's rotation limit keyed CONSTANT on the active action.
scene = bpy.context.scene
arm_data = bpy.data.armatures.new("ik_arm")
arm = bpy.data.objects.new("ik_arm", arm_data)
scene.collection.objects.link(arm)
bpy.context.view_layer.objects.active = arm
bpy.ops.object.mode_set(mode="EDIT")
for name, head, tail, parent in (("thigh", (0, 0, 2), (0, 0, 1), None), ("knee", (0, 0, 1), (0, 0, 0), "thigh"), ("左足ＩＫ", (0, 0, 0), (0, 1, 0), None)):
    bone = arm_data.edit_bones.new(name)
    bone.head, bone.tail = head, tail
    if parent:
        bone.parent = arm_data.edit_bones[parent]
bpy.ops.object.mode_set(mode="OBJECT")
solver = arm.pose.bones["knee"].constraints.new("IK")
solver.target, solver.subtarget, solver.chain_count = arm, "左足ＩＫ", 2
limit = arm.pose.bones["thigh"].constraints.new("LIMIT_ROTATION")
arm.animation_data_create().action = bpy.data.actions.new("MMDBridge_test_motion")
switched = ik.ik_switch_constraints(arm, arm.pose.bones["左足ＩＫ"])
checks.check("IK switch reaches IK and chain limits", switched == [solver, limit], switched)
table = plan_mod.vmd_bone_table(arm)
keyed = ik.apply_ik_keys(arm, ik_model, table)
checks.check("IK keys applied to both constraints", keyed == 2, keyed)
for frame, expected in ((0, 1.0), (7, 1.0), (8, 0.0), (15, 0.0), (16, 1.0)):
    scene.frame_set(frame)
    if (solver.influence, limit.influence) != (expected, expected):
        checks.check("IK influence switches on key frames", False, (frame, solver.influence, limit.influence))
        break
else:
    checks.check("IK influence switches on key frames", True)
ik_model.ik_keys = [pmm.IkKey(0, (1, 1))]
arm.animation_data.action = bpy.data.actions.new("MMDBridge_test_motion2")
solver.influence = 0.0
ik.apply_ik_keys(arm, ik_model, table)
checks.check("IK back on when the PMM no longer switches it", solver.influence == 1.0 and limit.influence == 1.0)
solver.influence = limit.influence = 0.0
checks.check("Clean restores IK influence", ik.restore_ik_influence(arm) == 2 and solver.influence == 1.0)

# C8: names a VMD cannot hold whole reach their bone when no other bone shares them.
bpy.context.view_layer.objects.active = arm
bpy.ops.object.mode_set(mode="EDIT")
for name in ("右腕捩じりボーン補助親", "左腕捩じりボーン補助一", "左腕捩じりボーン補助二"):
    bone = arm_data.edit_bones.new(name)
    bone.head, bone.tail = (0, 0, 0), (0, 0.5, 0)
bpy.ops.object.mode_set(mode="OBJECT")
table = plan_mod.vmd_bone_table(arm)
unique = motion.vmd_bone_name("右腕捩じりボーン補助親")
shared = motion.vmd_bone_name("左腕捩じりボーン補助一")
checks.check("unique carried name maps to its bone", table.get(unique) == arm.pose.bones["右腕捩じりボーン補助親"], unique)
checks.check("shared carried name stays unmapped", shared not in table, shared)
long_model = pmm.PmmModel()
long_model.bone_names = ["右腕捩じりボーン補助親", "左腕捩じりボーン補助一"]
long_model.bones = [[bone_key(0), bone_key(5, 1.0)], [bone_key(0), bone_key(5, 1.0)]]
report = plan_mod.map_bones(long_model, table)
checks.check("plan reports the shared name as cut", report.matched == 1 and report.cut == ["左腕捩じりボーン補助一"], (report.matched, report.cut))

# C5: the additive switch read from and written to the accessory state.
acc = pmm.PmmAccessory()
acc.index, acc.name, acc.path, acc.additive = 0, "glow", "C:\\glow.x", True
acc.keys = [pmm.AccessoryKey(0, 1, -1, 0, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 1.0, 1)]
out = pmm.parse_pmm(writer.build_pmm(template, [], base.camera_keys, accessories=[acc]))
checks.check("additive accessory round trip", [a.additive for a in out.accessories] == [True])
acc.additive = False
out = pmm.parse_pmm(writer.build_pmm(template, [], base.camera_keys, accessories=[acc]))
checks.check("plain accessory stays plain", [a.additive for a in out.accessories] == [False])

# C6: play range bytes, and applying only what MMD switched on.
block = bytearray(28)
block[17], block[18], block[19] = 1, 1, 0
struct.pack_into("<ii", block, 20, 12, 345)
checks.check("play range decoded", pmm.read_play_range(bytes(block)) == pmm.PlayRange(12, 345, True, False, True))
written = pmm.parse_pmm(writer.build_pmm(template, [], base.camera_keys, (10, 90)))
checks.check("Create PMM range reads back as on", written.play_range[:4] == (10, 90, True, True), written.play_range)

# P7: a record hashed with a released add-on version still matches.
settings = scene.mmd_bridge
root = bpy.data.objects.new("rig_root", None)
current, legacy = plan_mod.part_settings("motion", scene, settings, root, "sig", None)
old = plan_mod.digest_values(
    ((0, 5, 0), plan_mod.mmd_tools_version(), round(retime.retime_factor(scene, settings), 6), round(plan_mod.current_scale(settings), 6), True, False, root.name),
    "CURVE",
    None,
    "sig",
)
checks.check("0.5.0 motion hash is a legacy match", old in legacy and current not in legacy)
saved = {"settings": old, "outputs": []}
part = plan_mod.PartPlan("motion")
part.exists, part.settings, part.legacy_settings = True, current, legacy
plan_mod.decide(part, saved, "", [], False, True)
checks.check("legacy record skips and is marked for upgrade", part.operation == plan_mod.SKIP and part.upgrade)

# P8: the preview's parse is reused only for the same bytes, as an independent copy.
analysis._cache["parsed"] = (analysis.content_digest(template), pmm.copy_project(base))
copy = analysis.parsed_copy(analysis.content_digest(template))
checks.check("same bytes reuse the parse", copy is not None and len(copy.models) == len(base.models))
retime.retime_project(copy, 2.0)
checks.check("retiming the copy leaves the cache", analysis._cache["parsed"][1].camera_keys == base.camera_keys)
checks.check("other bytes parse again", analysis.parsed_copy("0" * 12) is None)
analysis.clear_analysis()

# P1 helpers: one pass over the deform weights, faces found with numpy.
bpy.ops.mesh.primitive_grid_add(x_subdivisions=6, y_subdivisions=6)
grid = bpy.context.object
plus = grid.vertex_groups.new(name="UV_eye+X")
minus = grid.vertex_groups.new(name="UV_eye-Y")
other = grid.vertex_groups.new(name="UV_mouth+Y")
plus.add([0, 1, 2], 0.5, "REPLACE")
minus.add([2, 3], 0.25, "REPLACE")
other.add([30], 1.0, "REPLACE")
morphs = {
    "eye": types.SimpleNamespace(name="eye", data_type="VERTEX_GROUP", vertex_group_scale=2.0),
    "mouth": types.SimpleNamespace(name="mouth", data_type="VERTEX_GROUP", vertex_group_scale=1.0),
}
maps = uv.uv_offset_maps(grid, morphs)
checks.check("one-pass UV offsets", maps["eye"] == {0: [1.0, 0, 0, 0], 1: [1.0, 0, 0, 0], 2: [1.0, -0.5, 0, 0], 3: [0, -0.5, 0, 0]} and maps["mouth"] == {30: [0, 1.0, 0, 0]}, maps)
grid.data.polygons[0].material_index = 1
table = uv.face_table(grid.data)
expected = {p.material_index for p in grid.data.polygons if set(p.vertices) & {0, 30}}
checks.check("faces on the vertices found", uv.touched_material_indices(table, [0, 30], len(grid.data.vertices)) == expected, expected)
checks.check("no vertices, no faces", uv.touched_material_indices(table, [], len(grid.data.vertices)) == set())

# C11: a sync is one undo step.
checks.check("Sync Now pushes an undo step", "UNDO" in operators.MMDBRIDGE_OT_sync_now.bl_options)

core.unregister()
checks.finish()
