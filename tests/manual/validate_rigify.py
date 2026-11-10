import bpy, importlib, math, os, struct, tempfile, time
from mathutils import Vector
# Rigify remap check for a model converted with MMD Tools Append, every limb in FK and
# IK. Needs a .blend with the converted rig and a real VMD. Run in a throwaway profile
# with mmd_tools and core installed (see tests/README.md):
#   blender --background --factory-startup file.blend --python tests/manual/validate_rigify.py -- motion.vmd [last_frame]
# NINESYNC_RIGIFY_ROOT names the mmd_tools root of the converted rig; NINESYNC_RIGIFY_DROP
# names a VMD bone to leave out (a stage-placement root, say). The rig's drivers must run
# (Preferences > Save & Load > Auto Run Python Scripts), as the remap unbinds the rig
# through them. For each of four switch settings it bakes the VMD, then prints how far
# each limb's bones are from where the plain MMD model puts them, as a percentage of
# the model's height. Never saves the .blend.
import addon_utils, sys
ARGS = sys.argv[sys.argv.index("--") + 1:]
VMD = ARGS[0]
LIMIT = int(ARGS[1]) if len(ARGS) > 1 else 300
ROOT = os.environ["NINESYNC_RIGIFY_ROOT"]
DROP = tuple(x for x in [os.environ.get("NINESYNC_RIGIFY_DROP", "")] if x)
bpy.context.preferences.filepaths.use_scripts_auto_execute = True
for name in sorted(m.__name__ for m in addon_utils.modules()):
    if name.endswith((".mmd_tools", ".ninesync")):
        addon_utils.enable(name, default_set=False)
package = next(n for n in sorted(sys.modules) if n.endswith(".ninesync") and n.count(".") >= 1 and "." not in n.rsplit(".ninesync", 1)[1])
pmm = importlib.import_module(package + ".pmm")
link = importlib.import_module(package + ".sync.mmd_tools_link")
plan = importlib.import_module(package + ".sync.plan")
rigify = importlib.import_module(package + ".sync.rigify")
clips = importlib.import_module(package + ".sync.clips")
rmap = importlib.import_module(package + ".common.rigify_map")
scene = bpy.context.scene
rr = bpy.data.objects[ROOT]
rig = next(c for c in rr.children_recursive if c.type == "ARMATURE")
raw = open(VMD, "rb").read()
count = struct.unpack_from("<I", raw, 50)[0]
tracks = {}
for i in range(count):
    o = 54 + i * 111
    name = raw[o:o + 15].split(b"\0")[0].decode("cp932", "replace")
    frame, = struct.unpack_from("<I", raw, o + 15)
    if frame > LIMIT or name in DROP: continue
    tracks.setdefault(name, []).append(pmm.BoneKey(frame, struct.unpack_from("<3f", raw, o + 19), struct.unpack_from("<4f", raw, o + 31), raw[o + 47:o + 111]))
model = pmm.PmmModel(); model.name = "t"; model.bone_names = list(tracks); model.bones = [sorted(v) for v in tracks.values()]
tmp = os.path.join(tempfile.gettempdir(), "ninesync_limb.vmd")
open(tmp, "wb").write(pmm.build_vmd(model, None, bones=True, morphs=False))
scale = rr.empty_display_size * 0.2
anim = rig.animation_data or rig.animation_data_create()
prev_action = anim.action
link.run_importer(tmp, scale, [rr, rig], "LT_rig", False, plan.vmd_bone_table)
motion = anim.action
clips.add_clip_strip(anim, motion, "LT_motion")
anim.action = prev_action
bpy.context.evaluated_depsgraph_get()
by_j = rigify.mmd_bones_by_name(rig)
height = max(b.head_local.z for b in rig.data.bones)
probe = Vector((2, 2, 2))
move = lambda n: rig.pose.bones[n].matrix @ rig.data.bones[n].matrix_local.inverted()
allf = sorted({k.frame for ks in model.bones for k in ks})
frames = allf[::max(1, len(allf) // 20)]
def snapshot():
    out = {}
    for f in frames:
        scene.frame_set(f); bpy.context.view_layer.update()
        out[f] = {j: move(n) @ (rig.data.bones[n].head_local + probe) for j, n in by_j.items()}
    return out
saved = rigify.append_properties(rig)
rigify.set_append_properties(rig, {k: 0.0 for k in saved})
reference = snapshot()
rigify.set_append_properties(rig, saved)
limb_bones = {
    "ARM_L": ["左肩", "左腕", "左ひじ", "左手首", "左親指１", "左人指１", "左薬指１"],
    "ARM_R": ["右肩", "右腕", "右ひじ", "右手首", "右親指１", "右人指１", "右薬指１"],
    "LEG_L": ["左足", "左ひざ", "左足首", "左足ＩＫ", "左足首D", "左ひざD", "左足D"],
    "LEG_R": ["右足", "右ひざ", "右足首", "右足ＩＫ", "右足首D", "右ひざD", "右足D"],
    "SPINE": ["下半身", "上半身", "上半身2", "首", "頭", "腰"],
}
owners = rmap.LIMB_OWNERS
original = {g: rig.pose.bones[o].get("IK_FK") for g, o in owners.items()}
print("limb switches at start:", original)
scenarios = [("arms FK, legs FK", {"ARM_L": 1.0, "ARM_R": 1.0, "LEG_L": 1.0, "LEG_R": 1.0}),
             ("arms IK, legs IK", {"ARM_L": 0.0, "ARM_R": 0.0, "LEG_L": 0.0, "LEG_R": 0.0}),
             ("arms FK, legs IK", {"ARM_L": 1.0, "ARM_R": 1.0, "LEG_L": 0.0, "LEG_R": 0.0}),
             ("arms IK, legs FK", {"ARM_L": 0.0, "ARM_R": 0.0, "LEG_L": 1.0, "LEG_R": 1.0})]
results = {}
for title, values in scenarios:
    for g, v in values.items():
        rig.pose.bones[owners[g]]["IK_FK"] = v
    rig.update_tag(); bpy.context.view_layer.update()
    t0 = time.time()
    n = rigify.retarget(rig, model, "LT")
    took = time.time() - t0
    final = snapshot()
    worst = {}
    for f in frames:
        for j, p in final[f].items():
            if j in reference[f]:
                worst[j] = max(worst.get(j, 0), (p - reference[f][j]).length / height * 100)
    print("== %s  (%d frames, %.1fs)" % (title, n, took))
    for g, names in limb_bones.items():
        row = ["%s %.2f" % (j, worst[j]) for j in names if j in worst]
        print("   %-6s %s" % (g, " | ".join(row)))
    big = sorted(((e, j) for j, e in worst.items() if e > 0.5), reverse=True)
    print("   bones over 0.5%% of height: %d of %d; worst: %s" % (len(big), len(worst), [(j, round(e, 2)) for e, j in big[:6]]))
    for t in list(anim.nla_tracks):
        if t.name.endswith("_LT_rigify"):
            acts = [s.action for s in t.strips]
            anim.nla_tracks.remove(t)
            for a in acts: bpy.data.actions.remove(a)
for g, v in original.items():
    if v is not None: rig.pose.bones[owners[g]]["IK_FK"] = v
rig.update_tag()
for t in list(anim.nla_tracks):
    if t.name == "LT_motion":
        acts = [s.action for s in t.strips]
        anim.nla_tracks.remove(t)
        for a in acts: bpy.data.actions.remove(a)
anim.action = prev_action
scene.frame_set(scene.frame_current)
print("restored; switches:", {g: rig.pose.bones[o].get("IK_FK") for g, o in owners.items()}, "tracks", [t.name for t in anim.nla_tracks])
