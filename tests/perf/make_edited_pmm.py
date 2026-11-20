import addon_utils
import importlib
import json
import struct
import sys

# Writes a copy of a plan's PMM with one bone key of one model moved, for the
# incremental-sync scenarios (P2-B). The source PMM is only read. The key is found by its
# frame, location and rotation bytes, which must occur exactly once.
#   blender --background --factory-startup --python tests/perf/make_edited_pmm.py --
#       PLAN.json OUT.pmm MODEL_NAME BONE_NAME [DX] [NEAR_FRAME]
# NEAR_FRAME picks the key closest to that frame instead of the middle of the track.
ARGS = sys.argv[sys.argv.index("--") + 1 :]
plan = json.load(open(ARGS[0], encoding="utf-8"))
out_path, model_name, bone_name = ARGS[1], ARGS[2], ARGS[3]
dx = float(ARGS[4]) if len(ARGS) > 4 else 0.5
near = int(ARGS[5]) if len(ARGS) > 5 else None
names = sorted(m.__name__ for m in addon_utils.modules())
package = next(n for n in names if n.endswith(".ninesync"))
addon_utils.enable(package, default_set=False)
pmm = importlib.import_module(package + ".pmm")
data = bytearray(open(plan["pmm"], "rb").read())
project = pmm.parse_pmm(bytes(data))
model = next(m for m in project.models if m.name.strip() == model_name.strip())
track = model.bones[model.bone_names.index(bone_name)]
done = None
centre = near if near is not None else track[len(track) // 2].frame
for key in sorted(track, key=lambda k: abs(k.frame - centre)):
    pattern = struct.pack("<3f4f", *key.location, *key.rotation)
    # In both bone key layouts the frame sits 28 bytes before the location.
    hits = []
    start = data.find(pattern)
    while start >= 0:
        if start >= 28 and struct.unpack_from("<i", data, start - 28)[0] == key.frame:
            hits.append(start)
        start = data.find(pattern, start + 1)
    if len(hits) == 1 and key.frame > 0:
        at = hits[0]
        x = struct.unpack_from("<f", data, at)[0]
        struct.pack_into("<f", data, at, x + dx)
        done = {"model": model.index, "bone": bone_name, "frame": key.frame, "offset": at, "old_x": x, "new_x": x + dx}
        break
if done is None:
    raise SystemExit("no unique key found")
open(out_path, "wb").write(bytes(data))
edited = pmm.parse_pmm(bytes(data))
check = edited.models[model.index].bones[model.bone_names.index(bone_name)]
assert any(abs(k.location[0] - done["new_x"]) < 1e-4 and k.frame == done["frame"] for k in check), "edit not found after reparse"
print("EDITED", json.dumps(done, ensure_ascii=False))
