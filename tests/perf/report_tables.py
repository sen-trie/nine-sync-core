import json
import sys

# Markdown tables from run_bench.py summaries: per plan and variant, the cold, solve,
# forced and unchanged medians (min-max), peak RSS and frame evaluations, plus each
# variant's change against the first one given.
#   python -I tests/perf/report_tables.py SUMMARY.json [SUMMARY.json ...]
def fmt(entry, key):
    stats = entry.get(key) or {}
    if not stats:
        return "-"
    return "%.3f (%.3f-%.3f)" % (stats["median"], stats["min"], stats["max"])


def change(new, old):
    if not old or not new:
        return "-"
    return "%+.1f%%" % ((new - old) / old * 100.0)


def main():
    plans = {}
    order = []
    for path in sys.argv[1:]:
        summary = json.load(open(path, encoding="utf-8"))
        for plan, variants in summary["plans"].items():
            for name, entry in variants.items():
                plans.setdefault(plan, {})[name] = entry
                if name not in order:
                    order.append(name)
    print("| plan | variant | cold s median (min-max) | vs %s | solve s | force s | no-change s | peak RSS MB | frame evals | failed |" % order[0])
    print("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for plan in sorted(plans):
        base = plans[plan].get(order[0], {}).get("cold", {}).get("median")
        for name in order:
            entry = plans[plan].get(name)
            if entry is None:
                continue
            cold = entry.get("cold", {}).get("median")
            print(
                "| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |"
                % (
                    plan,
                    name,
                    fmt(entry, "cold"),
                    change(cold, base) if name != order[0] else "",
                    fmt(entry, "cold_solve"),
                    fmt(entry, "force"),
                    fmt(entry, "nochange"),
                    (entry.get("peak_rss_mb") or {}).get("max", "-"),
                    entry.get("frame_evals"),
                    len(entry.get("failed_runs", [])),
                )
            )
    totals = {}
    for name in order:
        colds = [plans[p][name]["cold"]["median"] for p in plans if name in plans[p] and plans[p][name].get("cold")]
        if colds:
            colds.sort()
            totals[name] = (sum(colds), colds[len(colds) // 2], len(colds))
    print()
    print("| variant | plans | sum of cold medians s | median of cold medians s |")
    print("|---|---:|---:|---:|")
    for name, (total, median, n) in totals.items():
        print("| %s | %d | %.3f | %.3f |" % (name, n, total, median))


if __name__ == "__main__":
    main()
