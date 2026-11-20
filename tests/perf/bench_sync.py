import addon_utils
import bpy
import ctypes
import importlib
import json
import os
import platform
import sys
import time

import numpy as np


# Runs inside one fresh Blender process: imports a corpus plan's stand-in models, then
# times a cold sync, unchanged resyncs and forced resyncs. Optionally dumps the synced
# animation (and evaluated bone matrices of outside-parent armatures) for compare_output.py.
#   blender --background --factory-startup --python tests/perf/bench_sync.py -- PLAN OUT
#       [--nochange N] [--force N] [--dump FILE.npz] [--profile FILE.txt] [--edit EDITED.pmm]
#       [--physics]
# Wall-clock timers are the measurement; --profile is diagnostic only.
ARGS = sys.argv[sys.argv.index("--") + 1 :]
PLAN = json.load(open(ARGS[0], encoding="utf-8"))
OUT = ARGS[1]


def option(name, default=None, cast=str):
    if name in ARGS:
        return cast(ARGS[ARGS.index(name) + 1])
    return default


NOCHANGE = option("--nochange", 3, int)
FORCE = option("--force", 3, int)
DUMP = option("--dump")
PROFILE = option("--profile")
EDIT = option("--edit")
# Imports the stand-ins with their rigid bodies and joints, as a user's scene would have.
PHYSICS = "--physics" in ARGS


def cache_stats(package):
    try:
        return dict(importlib.import_module(package + ".sync.solve_cache").stats)
    except ImportError:
        return None


class Counters(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [
        (n, ctypes.c_size_t)
        for n in (
            "PeakWorkingSetSize",
            "WorkingSetSize",
            "QuotaPeakPagedPoolUsage",
            "QuotaPagedPoolUsage",
            "QuotaPeakNonPagedPoolUsage",
            "QuotaNonPagedPoolUsage",
            "PagefileUsage",
            "PeakPagefileUsage",
        )
    ]


def peak_rss_mb():
    try:
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return None
        return round(counters.PeakWorkingSetSize / 1048576, 1)
    except Exception:
        try:
            import resource

            return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
        except Exception:
            return None


def enable_all():
    names = sorted(m.__name__ for m in addon_utils.modules())
    wanted = [n for n in names if n.endswith((".mmd_tools", ".ninesync"))]
    for name in wanted:
        addon_utils.enable(name, default_set=False)
    core = next(n for n in wanted if n.endswith(".ninesync"))
    mmd = next((n for n in wanted if n.endswith(".mmd_tools")), None)
    return core, mmd


def module_version(name):
    if name is None:
        return None
    module = sys.modules.get(name)
    folder = os.path.dirname(getattr(module, "__file__", "") or "")
    manifest = os.path.join(folder, "blender_manifest.toml")
    if os.path.exists(manifest):
        for line in open(manifest, encoding="utf-8"):
            if line.strip().startswith("version"):
                return line.split("=", 1)[1].strip().strip('"')
    return None


# The fields of the last "timing summary" line in the NineSyncLog text.
def last_summary():
    text = bpy.data.texts.get("NineSyncLog")
    if text is None:
        return {}
    lines = [l for l in text.as_string().splitlines() if "timing summary" in l]
    if not lines:
        return {}
    fields = {}
    for part in lines[-1].split("timing summary", 1)[1].split():
        if "=" in part:
            key, value = part.split("=", 1)
            try:
                fields[key] = float(value)
            except ValueError:
                fields[key] = value
    return fields


# Harness-side probes that work on every revision: wall time and calls of the solver,
# and frame evaluations it makes (each one sits inside an internal_frames block).
class Probe:
    def __init__(self, package):
        self.op = importlib.import_module(package + ".sync.outside_parent")
        self.reset()
        solve = self.op.solve_bound_bones
        frames = self.op.internal_frames
        probe = self

        def timed_solve(*args, **kwargs):
            start = time.perf_counter()
            try:
                return solve(*args, **kwargs)
            finally:
                probe.solve_s += time.perf_counter() - start
                probe.solve_calls += 1

        def counted_frames(*args, **kwargs):
            probe.frame_evals += 1
            return frames(*args, **kwargs)

        self.op.solve_bound_bones = timed_solve
        self.op.internal_frames = counted_frames

    def reset(self):
        self.solve_s = 0.0
        self.solve_calls = 0
        self.frame_evals = 0

    def take(self):
        found = {
            "solve_s": round(self.solve_s, 4),
            "solve_calls": self.solve_calls,
            "solver_frame_evals": self.frame_evals,
        }
        self.reset()
        return found


def run_sync(probe, force=False):
    start = time.perf_counter()
    outcome = bpy.ops.ninesync.sync_now(force=force)
    wall = time.perf_counter() - start
    row = {"wall_s": round(wall, 4), "outcome": sorted(outcome), "status": bpy.context.scene.mmd_bridge.status}
    row.update(probe.take())
    row["summary"] = last_summary()
    row["peak_rss_mb"] = peak_rss_mb()
    return row


def channel_rows(action):
    rows = []
    for layer in action.layers:
        for strip in layer.strips:
            for bag in getattr(strip, "channelbags", []):
                slot = next((s for s in action.slots if s.handle == bag.slot_handle), None)
                for fc in bag.fcurves:
                    rows.append((layer.name, slot.identifier if slot else "?", fc))
    return rows


# Everything the sync wrote, keyed by stable names: every action's curves (points,
# handles, interpolation) and samples of each curve between and on its keys, NLA
# layout and constraints of every animated object, and evaluated world matrices of the
# bones of armatures that carry outside parents.
def watched_armatures(package, project):
    motion = importlib.import_module(package + ".sync.motion")
    scene_utils = importlib.import_module(package + ".common.scene_utils")
    pmm = importlib.import_module(package + ".pmm")
    by_key = {t.key: t for t in bpy.context.scene.mmd_bridge.targets}
    mapped = {m.index for m in project.models if (by_key.get(scene_utils.model_key(m)) or None) is not None and by_key[scene_utils.model_key(m)].target is not None}
    watched = []
    for model in project.models:
        bindings = motion.outside_bindings(model)
        live = {
            model.bone_names[b]
            for b, keys in bindings.items()
            if 0 <= b < len(model.bone_names)
            and any(p is not None and (p[0] == pmm.OUTSIDE_FREE or p[0] in mapped) for _f, p in keys)
        }
        if not live or model.index not in mapped:
            continue
        arm = scene_utils.find_armature(by_key[scene_utils.model_key(model)].target)
        if arm is not None:
            names = {pb.name for pb in arm.pose.bones if pb.name in live or getattr(pb.mmd_bone, "name_j", None) in live}
            names |= {motion.vmd_bone_name(n) for n in live}
            watched.append((model, arm, names))
    return watched


def owned_actions(obj):
    anim = obj.animation_data
    if anim is None:
        return set()
    found = {anim.action.name} if anim.action else set()
    for track in anim.nla_tracks:
        found |= {s.action.name for s in track.strips if s.action}
    return found


def dump(path, package, project):
    arrays = {}
    meta = {"curves": [], "nla": {}, "constraints": {}, "pose": {}}
    watched = watched_armatures(package, project)
    sampled = set()
    bound_paths = set()
    for _model, arm, names in watched:
        sampled |= owned_actions(arm)
        bound_paths |= {'pose.bones["%s"]' % n for n in names}
    meta["sampled_actions"] = sorted(sampled)
    meta["bound_paths"] = sorted(bound_paths)

    def is_sampled(action, fc):
        return action.name in sampled and any(fc.data_path.startswith(p) for p in bound_paths)
    for action in sorted(bpy.data.actions, key=lambda a: a.name):
        for layer, slot, fc in channel_rows(action):
            key = "%s|%s|%s|%s|%d" % (action.name, layer, slot, fc.data_path, fc.array_index)
            points = fc.keyframe_points
            n = len(points)
            co = np.empty(2 * n)
            left = np.empty(2 * n)
            right = np.empty(2 * n)
            interp = np.empty(n, dtype=np.int32)
            lt = np.empty(n, dtype=np.int32)
            rt = np.empty(n, dtype=np.int32)
            points.foreach_get("co", co)
            points.foreach_get("handle_left", left)
            points.foreach_get("handle_right", right)
            points.foreach_get("interpolation", interp)
            points.foreach_get("handle_left_type", lt)
            points.foreach_get("handle_right_type", rt)
            i = len(meta["curves"])
            meta["curves"].append(key)
            arrays["co%d" % i] = co
            arrays["hl%d" % i] = left
            arrays["hr%d" % i] = right
            arrays["ip%d" % i] = interp
            arrays["ht%d" % i] = np.concatenate([lt, rt])
            if n and is_sampled(action, fc):
                low, high = int(np.floor(co[0::2].min())) - 1, int(np.ceil(co[0::2].max())) + 1
                times = np.arange(low * 4, high * 4 + 1) / 4.0
                arrays["ev%d" % i] = np.array([fc.evaluate(t) for t in times])
                arrays["et%d" % i] = times
    for obj in sorted(bpy.data.objects, key=lambda o: o.name):
        anim = obj.animation_data
        if anim is not None:
            meta["nla"][obj.name] = {
                "action": anim.action.name if anim.action else None,
                "slot": anim.action_slot.identifier if anim.action_slot else None,
                "tracks": [
                    [t.name, t.mute, [[s.name, s.action.name if s.action else None, s.frame_start, s.frame_end, s.blend_type, s.influence] for s in t.strips]]
                    for t in anim.nla_tracks
                ],
            }
        if obj.type == "ARMATURE":
            cons = []
            for pb in obj.pose.bones:
                for c in pb.constraints:
                    cons.append([pb.name, c.name, c.type, c.mute, round(c.influence, 6), getattr(getattr(c, "target", None), "name", None), getattr(c, "subtarget", None)])
                meta["pose"].setdefault(obj.name, []).append([pb.name, pb.rotation_mode])
            meta["constraints"][obj.name] = cons
    # Evaluated matrices of armatures whose model has outside-parent bindings.
    scene = bpy.context.scene
    keep = scene.frame_current
    meta["watched"] = []
    for model, arm, _names in watched:
        last = max(model.last_frame, 1)
        times = [(f, 0.0) for f in range(0, last + 2)] + [(f, s) for f in range(0, last + 1, 3) for s in (0.25, 0.5)]
        names = [pb.name for pb in arm.pose.bones]
        mats = np.empty((len(times), len(names), 16))
        for ti, (frame, sub) in enumerate(times):
            scene.frame_set(frame, subframe=sub)
            world = arm.matrix_world
            for bi, pb in enumerate(arm.pose.bones):
                m = world @ pb.matrix
                mats[ti, bi] = [m[r][c] for r in range(4) for c in range(4)]
        j = len(meta["watched"])
        meta["watched"].append({"armature": arm.name, "bones": names})
        arrays["wt%d" % j] = np.array([f + s for f, s in times])
        arrays["wm%d" % j] = mats
    scene.frame_set(keep)
    arrays["meta"] = np.frombuffer(json.dumps(meta, ensure_ascii=False).encode("utf-8"), dtype=np.uint8)
    np.savez_compressed(path, **arrays)


def main():
    package, mmd = enable_all()
    pmm = importlib.import_module(package + ".pmm")
    targets = importlib.import_module(package + ".sync.targets")
    scene_utils = importlib.import_module(package + ".common.scene_utils")
    result = {
        "plan": PLAN.get("id"),
        "blender": bpy.app.version_string,
        "blender_hash": bpy.app.build_hash.decode() if isinstance(bpy.app.build_hash, bytes) else str(bpy.app.build_hash),
        "python": sys.version.split()[0],
        "mmd_tools": module_version(mmd),
        "ninesync": module_version(package),
        "platform": platform.platform(),
        "cpu": platform.processor(),
        "models_planned": len(PLAN.get("models", {})),
    }
    probe = Probe(package)
    with open(PLAN["pmm"], "rb") as handle:
        data = handle.read()
    start = time.perf_counter()
    project = pmm.parse_pmm(data)
    result["parse_s"] = round(time.perf_counter() - start, 4)
    result["pmm_bytes"] = len(data)
    result["model_slots"] = len(project.models)
    scene = bpy.context.scene
    scene.render.fps = 30
    scene.render.fps_base = 1.0
    roots = {}
    missing = []
    start = time.perf_counter()
    for slot, path in PLAN.get("models", {}).items():
        if not os.path.exists(path):
            missing.append(slot)
            continue
        before = set(bpy.data.objects)
        types = {"MESH", "ARMATURE", "MORPHS"} | ({"PHYSICS"} if PHYSICS else set())
        bpy.ops.mmd_tools.import_model(filepath=path, scale=0.08, types=types)
        made = [o for o in set(bpy.data.objects) - before if o.mmd_type == "ROOT"]
        if made:
            roots[int(slot)] = made[0]
    result["standin_import_s"] = round(time.perf_counter() - start, 3)
    result["standins_missing"] = missing
    result["models_mapped"] = len(roots)
    settings = scene.mmd_bridge
    settings.filepath = PLAN["pmm"]
    settings.sync_audio = False
    targets.refresh_targets(settings, project)
    by_key = {t.key: t for t in settings.targets}
    for model in project.models:
        if model.index in roots:
            by_key[scene_utils.model_key(model)].target = roots[model.index]
    result["slots_unmapped"] = sum(1 for t in settings.targets if t.target is None)
    profiler = None
    if PROFILE:
        import cProfile

        profiler = cProfile.Profile()
        profiler.enable()
    result["cold"] = run_sync(probe)
    if profiler is not None:
        import io
        import pstats

        profiler.disable()
        out = io.StringIO()
        stats = pstats.Stats(profiler, stream=out).strip_dirs()
        stats.sort_stats("cumulative").print_stats(40)
        stats.sort_stats("tottime").print_stats(40)
        calls = {}
        for (file, line, func), (cc, nc, tt, ct, _callers) in stats.stats.items():
            if func in ("<method 'keyframe_insert' of 'bpy_struct' objects>", "<method 'foreach_set' of 'bpy_prop_collection' objects>"):
                calls[func] = {"ncalls": nc, "tottime": round(tt, 4)}
        result["profile_calls"] = calls
        open(PROFILE, "w", encoding="utf-8").write(out.getvalue())
    if DUMP:
        dump(DUMP, package, project)
    result["nochange"] = [run_sync(probe) for _ in range(NOCHANGE)]
    result["force"] = [run_sync(probe, force=True) for _ in range(FORCE)]
    if DUMP and FORCE:
        dump(DUMP.replace(".npz", "_force.npz"), package, project)
    if EDIT:
        # Incremental scenario: the PMM on disk is replaced by an edited one (same path),
        # then a plain sync runs. Only ever done on a bench copy.
        target = PLAN["pmm"]
        if not os.path.basename(target).startswith("benchcopy_"):
            raise SystemExit("--edit only overwrites a benchcopy_ PMM")
        with open(EDIT, "rb") as source, open(target, "wb") as handle:
            handle.write(source.read())
        result["edited"] = run_sync(probe)
        result["edited"]["solve_cache"] = cache_stats(package)
        if DUMP:
            dump(DUMP.replace(".npz", "_edited.npz"), package, pmm.parse_pmm(open(target, "rb").read()))
    result["solve_cache"] = cache_stats(package)
    result["peak_rss_mb"] = peak_rss_mb()
    with open(OUT, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=1)


try:
    main()
except Exception:
    import traceback

    traceback.print_exc()
    sys.exit(1)
