import json
import sys

import numpy as np

# Compares two bench_sync.py dumps (reference first). Reports curves that appear on one
# side only, curves whose stored points differ, the largest difference of each curve's
# evaluated samples (quaternions compared up to sign), NLA and constraint layout, and the
# largest translation and rotation error of the evaluated bone world matrices.
#   python -I tests/perf/compare_output.py REF.npz NEW.npz [--json OUT.json]
# Exit code 1 when anything is over tolerance.
TRANSLATION_TOL = 1e-5
ANGLE_TOL = 1e-4
VALUE_TOL = 1e-5


def load(path):
    data = np.load(path)
    meta = json.loads(bytes(data["meta"]).decode("utf-8"))
    return data, meta


def curve_index(meta):
    return {key: i for i, key in enumerate(meta["curves"])}


def sampled(data, i):
    if "ev%d" % i not in data:
        return np.empty(0), np.empty(0)
    return data["et%d" % i], data["ev%d" % i]


def on_common_times(ta, va, tb, vb):
    common, ia, ib = np.intersect1d(np.round(ta * 4).astype(np.int64), np.round(tb * 4).astype(np.int64), return_indices=True)
    return common / 4.0, va[ia], vb[ib]


def rotation_angles(ma, mb):
    ra = ma[..., [0, 1, 2, 4, 5, 6, 8, 9, 10]].reshape(ma.shape[:-1] + (3, 3))
    rb = mb[..., [0, 1, 2, 4, 5, 6, 8, 9, 10]].reshape(mb.shape[:-1] + (3, 3))

    def normalize(r):
        return r / np.linalg.norm(r, axis=-2, keepdims=True).clip(1e-12)

    rel = np.einsum("...ji,...jk->...ik", normalize(ra), normalize(rb))
    trace = np.trace(rel, axis1=-2, axis2=-1)
    return np.arccos(np.clip((trace - 1.0) / 2.0, -1.0, 1.0))


def compare(ref_path, new_path):
    a, ma = load(ref_path)
    b, mb = load(new_path)
    ia, ib = curve_index(ma), curve_index(mb)
    report = {
        "only_ref": sorted(set(ia) - set(ib)),
        "only_new": sorted(set(ib) - set(ia)),
        "structural_diff": [],
        "value_errors": [],
        "max_value_error": 0.0,
        "max_value_error_at": None,
    }
    quats = {}
    for key in sorted(set(ia) & set(ib)):
        i, j = ia[key], ib[key]
        same = all(
            a[p % i].shape == b[p % j].shape and np.array_equal(a[p % i], b[p % j])
            for p in ("co%d", "hl%d", "hr%d", "ip%d", "ht%d")
        )
        if not same:
            report["structural_diff"].append(key)
        ta, va = sampled(a, i)
        tb, vb = sampled(b, j)
        times, va, vb = on_common_times(ta, va, tb, vb)
        if key.rsplit("|", 1)[0].endswith("rotation_quaternion"):
            quats.setdefault(key.rsplit("|", 1)[0], {})[int(key.rsplit("|", 1)[1])] = (times, va, vb)
            continue
        if len(times):
            err = np.abs(va - vb)
            k = int(np.argmax(err))
            if err[k] > report["max_value_error"]:
                report["max_value_error"] = float(err[k])
                report["max_value_error_at"] = [key, float(times[k])]
            if err[k] > VALUE_TOL:
                report["value_errors"].append([key, float(err[k]), float(times[k])])
    for path, parts in quats.items():
        if sorted(parts) != [0, 1, 2, 3]:
            for index, (times, va, vb) in parts.items():
                err = np.abs(va - vb)
                if len(err) and err.max() > VALUE_TOL:
                    report["value_errors"].append([path + "|%d" % index, float(err.max()), float(times[int(np.argmax(err))])])
            continue
        common = parts[0][0]
        for index in (1, 2, 3):
            common = np.intersect1d(common, parts[index][0])
        qa = np.stack([parts[k][1][np.isin(parts[k][0], common)] for k in range(4)], axis=-1)
        qb = np.stack([parts[k][2][np.isin(parts[k][0], common)] for k in range(4)], axis=-1)
        err = np.minimum(np.abs(qa - qb).max(axis=-1), np.abs(qa + qb).max(axis=-1))
        if len(err):
            k = int(np.argmax(err))
            if err[k] > report["max_value_error"]:
                report["max_value_error"] = float(err[k])
                report["max_value_error_at"] = [path, float(common[k])]
            if err[k] > VALUE_TOL:
                report["value_errors"].append([path, float(err[k]), float(common[k])])
    report["nla_equal"] = ma["nla"] == mb["nla"]
    report["constraints_equal"] = ma["constraints"] == mb["constraints"]
    report["pose_modes_equal"] = ma["pose"] == mb["pose"]
    if not report["nla_equal"]:
        report["nla_diff"] = [k for k in set(ma["nla"]) | set(mb["nla"]) if ma["nla"].get(k) != mb["nla"].get(k)]
    if not report["constraints_equal"]:
        report["constraints_diff"] = [k for k in set(ma["constraints"]) | set(mb["constraints"]) if ma["constraints"].get(k) != mb["constraints"].get(k)]
    report["matrices"] = []
    for j, watch in enumerate(ma.get("watched", [])):
        other = next((k for k, w in enumerate(mb.get("watched", [])) if w["armature"] == watch["armature"]), None)
        if other is None:
            report["matrices"].append({"armature": watch["armature"], "missing": True})
            continue
        bones_b = mb["watched"][other]["bones"]
        order = [bones_b.index(n) for n in watch["bones"]]
        ta, tb = a["wt%d" % j], b["wt%d" % other]
        xa, xb = a["wm%d" % j], b["wm%d" % other][:, order]
        if not np.array_equal(ta, tb):
            report["matrices"].append({"armature": watch["armature"], "times_differ": True})
            continue
        trans = np.abs(xa[..., [3, 7, 11]] - xb[..., [3, 7, 11]]).max(axis=-1)
        angle = rotation_angles(xa, xb)
        ti, bi = np.unravel_index(int(np.argmax(trans)), trans.shape)
        ai, abi = np.unravel_index(int(np.argmax(angle)), angle.shape)
        report["matrices"].append(
            {
                "armature": watch["armature"],
                "samples": int(trans.shape[0]),
                "bones": int(trans.shape[1]),
                "max_translation": float(trans.max()),
                "max_translation_at": [float(ta[ti]), watch["bones"][bi]],
                "max_angle": float(angle.max()),
                "max_angle_at": [float(ta[ai]), watch["bones"][abi]],
                "frames_over_tol": sorted({float(t) for t in ta[(trans > TRANSLATION_TOL).any(axis=-1) | (angle > ANGLE_TOL).any(axis=-1)]})[:50],
            }
        )
    report["ok"] = (
        not report["only_ref"]
        and not report["only_new"]
        and not report["value_errors"]
        and report["nla_equal"]
        and report["constraints_equal"]
        and report["pose_modes_equal"]
        and all(
            not m.get("missing") and not m.get("times_differ") and m["max_translation"] <= TRANSLATION_TOL and m["max_angle"] <= ANGLE_TOL
            for m in report["matrices"]
        )
    )
    return report


def main():
    report = compare(sys.argv[1], sys.argv[2])
    if "--json" in sys.argv:
        with open(sys.argv[sys.argv.index("--json") + 1], "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=1)
    short = dict(report)
    for key in ("structural_diff", "value_errors", "only_ref", "only_new"):
        short[key] = "%d: %s" % (len(report[key]), report[key][:4])
    print(json.dumps(short, ensure_ascii=False, indent=1))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
