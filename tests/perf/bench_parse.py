import addon_utils
import cProfile
import importlib
import io
import json
import pstats
import statistics
import sys
import time

# PMM parse timing (P2-C): the parser as installed against a block-read prototype of the
# bone and morph key sections, on every PMM of the given plans. Checks the prototype
# parses to the same values before timing it.
#   blender --background --factory-startup --python tests/perf/bench_parse.py -- OUT.json PLAN...
ARGS = sys.argv[sys.argv.index("--") + 1 :]
OUT = ARGS[0]
PLANS = ARGS[1:]
REPEAT = 15

names = sorted(m.__name__ for m in addon_utils.modules())
package = next(n for n in names if n.endswith(".ninesync"))
addon_utils.enable(package, default_set=False)
pmm = importlib.import_module(package + ".pmm")
original = {"bone": pmm.read_bone_track, "morph": pmm.read_morph_track}


def block(reader, layout, count):
    size = layout.size * count
    reader.need(size)
    view = memoryview(reader.data)[reader.pos : reader.pos + size]
    reader.pos += size
    return layout.iter_unpack(view)


def fast_bone_track(reader, bone_count):
    initials = [(v[0], v[3], tuple(v[4:7]), tuple(v[7:11]), v[2]) for v in block(reader, pmm.BONE_INITIAL, bone_count)]
    count = reader.count()
    additional = {v[0]: (v[1], v[4], tuple(v[5:8]), tuple(v[8:12]), v[3]) for v in block(reader, pmm.BONE_ADDITIONAL, count)}
    return [pmm.collect_bone_chain(initials[i], additional) for i in range(bone_count)]


def fast_morph_track(reader, morph_count):
    initials = [(v[0], v[3], v[2]) for v in block(reader, pmm.MORPH_INITIAL, morph_count)]
    count = reader.count()
    additional = {v[0]: (v[1], v[4], v[3]) for v in block(reader, pmm.MORPH_ADDITIONAL, count)}
    return [pmm.collect_morph_chain(initials[i], additional) for i in range(morph_count)]


def use(fast):
    pmm.read_bone_track = fast_bone_track if fast else original["bone"]
    pmm.read_morph_track = fast_morph_track if fast else original["morph"]


# Plain values of everything the parse returns; objects with slots by their slot values,
# so the comparison does not depend on object addresses.
def plain(value):
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    slots = getattr(type(value), "__slots__", None)
    if slots and not isinstance(value, tuple):
        return [(s, plain(getattr(value, s, None))) for s in slots]
    if hasattr(value, "__dict__"):
        return sorted((k, plain(v)) for k, v in vars(value).items())
    return value


def snapshot(project):
    return repr(plain(project))


results = []
paths = []
for plan in PLANS:
    path = json.load(open(plan, encoding="utf-8"))["pmm"]
    if path not in paths:
        paths.append(path)
for path in paths:
    data = open(path, "rb").read()
    use(False)
    reference = snapshot(pmm.parse_pmm(data))
    use(True)
    same = snapshot(pmm.parse_pmm(data)) == reference
    row = {"pmm": path.replace("\\", "/").rsplit("/", 1)[-1], "bytes": len(data), "same": same}
    for label, fast in (("current", False), ("block", True), ("current_again", False), ("block_again", True)):
        use(fast)
        times = []
        for _ in range(REPEAT):
            start = time.perf_counter()
            pmm.parse_pmm(data)
            times.append(time.perf_counter() - start)
        row.setdefault(label.replace("_again", ""), []).extend(times)
    for label in ("current", "block"):
        row[label] = round(statistics.median(row[label]) * 1000, 3)
    results.append(row)
    print(row, flush=True)
largest = max(paths, key=lambda p: len(open(p, "rb").read()))
use(False)
profiler = cProfile.Profile()
data = open(largest, "rb").read()
profiler.enable()
for _ in range(5):
    pmm.parse_pmm(data)
profiler.disable()
out = io.StringIO()
pstats.Stats(profiler, stream=out).strip_dirs().sort_stats("tottime").print_stats(15)
json.dump({"rows": results, "profile_largest": out.getvalue()}, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
