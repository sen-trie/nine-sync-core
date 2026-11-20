import argparse
import csv
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import time

# Benchmark driver. Each variant is an extension zip installed into its own throwaway
# profile (BLENDER_USER_RESOURCES) next to a copy of mmd_tools; every run is a fresh
# Blender process. Variants are interleaved ABBA so drift hits all of them alike.
#   python -I tests/perf/run_bench.py --blender BLENDER --mmd-tools DIR --out DIR
#       --variant b0=b0.zip [--variant p0a=p0a.zip ...] --plan PLAN.json [--plan ...]
#       [--runs 5] [--dump] [--profile] [--physics]
# Output: <out>/<variant>/<plan>_runN.json, a dump per plan when --dump, and
# <out>/summary.csv / summary.json. Nothing here writes to the source tree.
HERE = os.path.dirname(os.path.abspath(__file__))
BENCH = os.path.join(HERE, "bench_sync.py")


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare_profile(blender, root, zip_path, mmd_tools):
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(os.path.join(root, "extensions", "blender_org"))
    shutil.copytree(mmd_tools, os.path.join(root, "extensions", "blender_org", "mmd_tools"))
    env = dict(os.environ, BLENDER_USER_RESOURCES=root)
    subprocess.run(
        [blender, "--background", "--factory-startup", "--command", "extension", "install-file", "-r", "user_default", zip_path],
        env=env,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return env


def run_one(blender, env, plan, out_json, log_path, extra, timeout):
    command = [blender, "--background", "--factory-startup", "--python", BENCH, "--", plan, out_json] + extra
    start = time.perf_counter()
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        try:
            code = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            code = "timeout"
    return code, round(time.perf_counter() - start, 2)


def plan_name(path):
    return os.path.splitext(os.path.basename(path))[0].replace("_plan", "")


def stats(values):
    values = [v for v in values if v is not None]
    if not values:
        return {}
    return {
        "median": round(statistics.median(values), 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "n": len(values),
        "values": [round(v, 4) for v in values],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--blender", required=True)
    parser.add_argument("--mmd-tools", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--variant", action="append", required=True, help="name=zip")
    parser.add_argument("--plan", action="append", required=True)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--nochange", type=int, default=3)
    parser.add_argument("--force", type=int, default=3)
    parser.add_argument("--dump", action="store_true")
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--physics", action="store_true", help="import stand-ins with rigid bodies")
    # Incremental scenario: every run first restores ORIGINAL over the plan's benchcopy_
    # PMM, then bench_sync.py swaps EDITED in after the cold sync and syncs again.
    parser.add_argument("--edit", help="edited PMM")
    parser.add_argument("--original", help="pristine PMM restored before each run")
    args = parser.parse_args()
    if bool(args.edit) != bool(args.original):
        parser.error("--edit and --original go together")
    os.makedirs(args.out, exist_ok=True)
    variants = []
    for spec in args.variant:
        name, zip_path = spec.split("=", 1)
        root = os.path.join(args.out, "profiles", name)
        env = prepare_profile(args.blender, root, zip_path, args.mmd_tools)
        os.makedirs(os.path.join(args.out, name), exist_ok=True)
        variants.append({"name": name, "zip": zip_path, "zip_sha256": sha256(zip_path), "env": env})
    results = []

    # Restores the pristine PMM before an edit run; returns the extra bench_sync arguments.
    def edit_args(plan):
        if not args.edit:
            return []
        target = json.load(open(plan, encoding="utf-8"))["pmm"]
        if not os.path.basename(target).startswith("benchcopy_"):
            raise SystemExit("--edit needs a plan whose PMM is a benchcopy_ file")
        shutil.copyfile(args.original, target)
        return ["--edit", args.edit]

    for plan in args.plan:
        tag = plan_name(plan)
        for run in range(args.runs):
            order = variants if run % 4 in (0, 3) else list(reversed(variants))
            for variant in order:
                base = os.path.join(args.out, variant["name"], "%s_run%d" % (tag, run + 1))
                extra = ["--nochange", str(args.nochange), "--force", str(args.force)] + edit_args(plan) + (["--physics"] if args.physics else [])
                code, elapsed = run_one(args.blender, variant["env"], plan, base + ".json", base + ".log", extra, args.timeout)
                row = {"variant": variant["name"], "plan": tag, "run": run + 1, "exit": code, "process_s": elapsed}
                if code == 0 and os.path.exists(base + ".json"):
                    row["data"] = json.load(open(base + ".json", encoding="utf-8"))
                results.append(row)
                print("%s %s run %d exit=%s %.1fs cold=%s" % (variant["name"], tag, run + 1, code, elapsed, row.get("data", {}).get("cold", {}).get("wall_s")), flush=True)
        for variant in variants:
            base = os.path.join(args.out, variant["name"], tag)
            if args.dump:
                extra = ["--nochange", "1", "--force", "1", "--dump", base + "_dump.npz"] + edit_args(plan) + (["--physics"] if args.physics else [])
                code, _ = run_one(args.blender, variant["env"], plan, base + "_dumprun.json", base + "_dumprun.log", extra, args.timeout)
                print("%s %s dump exit=%s" % (variant["name"], tag, code), flush=True)
            if args.profile:
                extra = ["--nochange", "0", "--force", "0", "--profile", base + "_profile.txt"]
                code, _ = run_one(args.blender, variant["env"], plan, base + "_profilerun.json", base + "_profilerun.log", extra, args.timeout)
                print("%s %s profile exit=%s" % (variant["name"], tag, code), flush=True)
    summary = {"variants": [{k: v for k, v in var.items() if k != "env"} for var in variants], "plans": {}}
    rows = []
    for variant in variants:
        for plan in args.plan:
            tag = plan_name(plan)
            mine = [r for r in results if r["variant"] == variant["name"] and r["plan"] == tag]
            ok = [r["data"] for r in mine if "data" in r]
            cold = [d["cold"]["wall_s"] for d in ok]
            solve = [d["cold"]["solve_s"] for d in ok]
            force = [f["wall_s"] for d in ok for f in d["force"]]
            nochange = [f["wall_s"] for d in ok for f in d["nochange"]]
            rss = [d.get("peak_rss_mb") for d in ok]
            edited = [d["edited"] for d in ok if "edited" in d]
            entry = {
                "edited": stats([e["wall_s"] for e in edited]),
                "edited_solve": stats([e.get("solve_s") for e in edited]),
                "edited_frame_evals": edited[0].get("solver_frame_evals") if edited else None,
                "cold": stats(cold),
                "cold_solve": stats(solve),
                "force": stats(force),
                "nochange": stats(nochange),
                "peak_rss_mb": stats(rss),
                "failed_runs": [r["run"] for r in mine if "data" not in r],
                "frame_evals": ok[0]["cold"]["solver_frame_evals"] if ok else None,
                "summary_fields": ok[0]["cold"]["summary"] if ok else None,
                "mapped": ok[0]["models_mapped"] if ok else None,
                "slots": ok[0]["model_slots"] if ok else None,
                "missing": ok[0]["standins_missing"] if ok else None,
                "env": {k: ok[0].get(k) for k in ("blender", "blender_hash", "python", "mmd_tools", "ninesync", "platform", "cpu")} if ok else None,
            }
            summary["plans"].setdefault(tag, {})[variant["name"]] = entry
            rows.append([variant["name"], tag, entry["cold"].get("median"), entry["cold"].get("min"), entry["cold"].get("max"), entry["cold_solve"].get("median"), entry["force"].get("median"), entry["nochange"].get("median"), entry["peak_rss_mb"].get("max"), entry["frame_evals"], len(entry["failed_runs"])])
    with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=1)
    with open(os.path.join(args.out, "summary.csv"), "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["variant", "plan", "cold_median_s", "cold_min_s", "cold_max_s", "solve_median_s", "force_median_s", "nochange_median_s", "peak_rss_max_mb", "solver_frame_evals", "failed_runs"])
        writer.writerows(rows)
    for row in rows:
        print(",".join(str(x) for x in row))


if __name__ == "__main__":
    sys.exit(main())
