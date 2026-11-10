import addon_utils
import bpy
import importlib
import os
import shutil
import sys
import tempfile
import traceback


# Change detection, plan, preview, cancel and failure checks on real Blender with
# mmd_tools. Needs the profile described in tests/README.md, then:
#   blender --background --python tests/manual/validate_sync_plan.py -- file.pmm
# PMM edits are applied to the parsed project in memory, so the PMM is only read. The
# .blend round trip is saved to a temp folder.
ARGS = sys.argv[sys.argv.index("--") + 1 :]
PMM_PATH = ARGS[0]
failed = []


def check(label, ok, detail=""):
    print("%s %s%s" % ("ok  " if ok else "FAIL", label, " | %s" % (detail,) if detail != "" else ""))
    if not ok:
        failed.append(label)


names = sorted(m.__name__ for m in addon_utils.modules())
for name in [n for n in names if n.endswith((".mmd_tools", ".ninesync"))]:
    addon_utils.enable(name, default_set=False)
package = next(n for n in names if n.endswith(".ninesync"))


def mod(name):
    return importlib.import_module(package + "." + name)


pmm = mod("pmm")
engine = mod("sync.engine")
analysis = mod("sync.analysis")
plan_mod = mod("sync.plan")
state = mod("sync.sync_state")
importer = mod("sync.model_import")
progress = mod("sync.progress")
animation = mod("common.animation")
TEMP = tempfile.mkdtemp(prefix="ninesync_plan_")
mutations = {"apply": None}
calls = {"importer": []}
real_parse = pmm.parse_pmm
real_importer = importer.run_importer


def parse_with_mutation(data):
    project = real_parse(data)
    if mutations["apply"] is not None:
        mutations["apply"](project)
    return project


def counting_importer(path, scale, targets, name, detect, *extra):
    calls["importer"].append(name)
    return real_importer(path, scale, targets, name, detect, *extra)


engine.parse_pmm = parse_with_mutation
analysis.parse_pmm = parse_with_mutation
# Mutations change the parse without changing the file, so a sync must not reuse the
# preview's parse of the same bytes, as it does for a real file.
engine.parsed_copy = lambda digest: None
importer.run_importer = counting_importer


def sync(**options):
    calls["importer"].clear()
    return engine.run_sync(bpy.context.scene, **options)


def preview():
    return analysis.analyze(bpy.context.scene).plan


def operations(plan=None):
    plan = plan or preview()
    return {i.pmm_name: {n: p.operation for n, p in i.parts.items()} for i in plan.items}


def snapshot_ids():
    return tuple(
        len(c)
        for c in (
            bpy.data.objects,
            bpy.data.actions,
            bpy.data.texts,
            bpy.data.meshes,
            bpy.data.shape_keys,
            bpy.data.collections,
        )
    )


def digests():
    return {a.name: state.action_digest(a) for a in bpy.data.actions}


def nudge_bone(index):
    def apply(project):
        keys = project.models[index].bones[0]
        keys[0] = keys[0]._replace(location=(keys[0].location[0] + 1.0, *keys[0].location[1:]))

    return apply


def nudge_interpolation(index):
    def apply(project):
        model = project.models[index]
        keys = next(k for k in model.bones if len(k) > 1)
        raw = bytearray(keys[-1].interpolation)
        raw[0] = (raw[0] + 7) % 127
        keys[-1] = keys[-1]._replace(interpolation=bytes(raw))

    return apply


def nudge_morph(index):
    def apply(project):
        keys = project.models[index].morphs[0]
        keys[0] = keys[0]._replace(weight=0.5)

    return apply


def expect_clean_exit(label):
    check("%s: progress released" % label, not progress.progress_state()["active"] and not engine.sync_running())


def row_record(row):
    return state.read_record(row)


# A motion-only setting change: a baked physics VMD path on one row. The file need not
# exist; a missing file still changes the motion settings digest.
def set_physics_lever(settings, row, on):
    settings.use_physics_vmd = on
    row.physics_vmd = "//ninesync_test_missing_physics.vmd" if on else ""


def main():
    with open(PMM_PATH, "rb") as handle:
        source = real_parse(handle.read())
    scene = bpy.context.scene
    settings = scene.mmd_bridge
    scene.render.fps = 30
    scene.render.fps_base = 1.0
    roots = {}
    for model in source.models:
        if model.path and os.path.exists(model.path):
            before = set(bpy.data.objects)
            bpy.ops.mmd_tools.import_model(filepath=model.path, scale=0.08, types={"MESH", "ARMATURE", "MORPHS"})
            roots[model.index] = next(o for o in set(bpy.data.objects) - before if o.mmd_type == "ROOT")
    settings.filepath = PMM_PATH
    print("PMM", os.path.basename(PMM_PATH), "models", len(source.models), "with PMX", len(roots))

    # Inspector and preview before any sync.
    before = snapshot_ids()
    result = analysis.analyze(scene)
    again = analysis.analyze(scene)
    check("preview reads without changing data", snapshot_ids() == before, (before, snapshot_ids()))
    check("preview left the target list alone", len(settings.targets) == 0)
    check("repeat preview agrees", operations(result.plan) == operations(again.plan))
    for model, summary in zip(source.models, result.summaries):
        check(
            "summary counts match source for %s" % model.name,
            summary.bone_keys == sum(len(k) for k in model.bones)
            and summary.morph_keys == sum(len(k) for k in model.morphs)
            and summary.bone_tracks == len(model.bones),
        )
    planned = operations(result.plan)
    check("first preview plans Create", any(plan_mod.CREATE in p.values() for p in planned.values()), planned)
    for item in result.plan.items:
        print("   ", item.pmm_name, item.mapping, item.target_name, {n: (p.operation, p.reason) for n, p in item.parts.items()}, item.warnings)
        if item.bones:
            print("    bones", item.bones.matched, "/", item.bones.tracks, "missing", item.bones.missing[:5], "cut", item.bones.cut[:5], "| morphs missing", item.morphs.missing[:5])

    # First sync matches the first preview.
    check("first sync", sync(), settings.status)
    ran = [r for r in settings.targets if r.target is not None]
    check("records written", all(row_record(r) for r in ran if r.enabled), [r.pmm_name for r in ran])
    expect_clean_exit("first sync")
    log_text = bpy.data.texts["NineSyncLog"].as_string()
    check("timings in the log", "timing phase=importer" in log_text and "timing summary status=ok" in log_text)
    first = digests()

    # Unchanged repeat.
    planned = operations()
    check("unchanged preview all skip", all(o == plan_mod.SKIP for p in planned.values() for o in p.values()), planned)
    check("unchanged resync", sync(), settings.status)
    check("unchanged resync skipped the importer", not calls["importer"], calls["importer"])
    check("unchanged resync left actions alone", digests() == first)

    # Save and reload: the record and digests must survive a file round trip.
    saved = os.path.join(TEMP, "roundtrip.blend")
    bpy.ops.wm.save_as_mainfile(filepath=saved, copy=False)
    bpy.ops.wm.open_mainfile(filepath=saved)
    scene = bpy.context.scene
    settings = scene.mmd_bridge
    planned = operations()
    check("reloaded .blend still skips", all(o == plan_mod.SKIP for p in planned.values() for o in p.values()), planned)
    check("sync after reload skipped the importer", sync() and not calls["importer"], calls["importer"])
    rows = [r for r in settings.targets if r.target is not None and r.enabled]
    index_of = {r.pmm_name: int(r.key.split(":")[0]) for r in rows}
    first_row, last_row = rows[0], rows[-1]
    first_index = index_of[first_row.pmm_name]
    last_index = index_of[last_row.pmm_name]
    two_models = first_row.key != last_row.key

    # One model's motion changes.
    mutations["apply"] = nudge_bone(first_index)
    planned = operations()
    check("changed bone plans Update on that part", planned[first_row.pmm_name]["motion"] == plan_mod.UPDATE, planned)
    check("changed bone leaves shape alone", planned[first_row.pmm_name].get("shape") == plan_mod.SKIP, planned)
    part = next(i for i in preview().items if i.key == first_row.key).parts["motion"]
    check("preview names the changed bone", len(part.changed) == 1, part.changed)
    check("preview has the before state", part.before and part.before[0] == part.keys, part.before)
    if two_models:
        check("other model stays skipped", set(planned[last_row.pmm_name].values()) == {plan_mod.SKIP}, planned)
    before_digests = digests()
    check("changed bone sync", sync(), settings.status)
    check("only one import ran", len(calls["importer"]) == 1, calls["importer"])
    shape_name = first_row.shape_clip.name if first_row.shape_clip else None
    after = digests()
    check("motion clip changed", after[first_row.clip.name] != before_digests[first_row.clip.name])
    if shape_name:
        check("shape clip untouched", after[shape_name] == before_digests[shape_name])
    if two_models and last_row.clip:
        check("other model's clip untouched", after[last_row.clip.name] == before_digests[last_row.clip.name])
    planned = operations()
    check("record caught up", set(planned[first_row.pmm_name].values()) == {plan_mod.SKIP}, planned)
    # A part imported alone must equal the same part from a full import.
    partial = {a: d for a, d in digests().items() if a in (first_row.clip.name, shape_name)}
    check("full resync after partial", sync(force=True), settings.status)
    full = {a: d for a, d in digests().items() if a in (first_row.clip.name, shape_name)}
    check("partial import equals full import", partial == full, (partial, full))

    # Interpolation only.
    mutations["apply"] = nudge_interpolation(first_index)
    check("interpolation change detected", operations()[first_row.pmm_name]["motion"] == plan_mod.UPDATE)
    mutations["apply"] = None
    sync()

    # Morph only.
    mutations["apply"] = nudge_morph(first_index)
    planned = operations()[first_row.pmm_name]
    check("morph change plans shape only", planned.get("motion") == plan_mod.SKIP and planned.get("shape") == plan_mod.UPDATE, planned)
    motion_before = state.action_digest(first_row.clip)
    check("morph change sync", sync(), settings.status)
    check("morph sync left motion clip alone", state.action_digest(first_row.clip) == motion_before)
    planned = operations()[first_row.pmm_name]
    check("morph record caught up", set(planned.values()) == {plan_mod.SKIP}, planned)
    mutations["apply"] = None
    sync()

    # Settings.
    settings.uv_morphs = not settings.uv_morphs
    planned = operations()[first_row.pmm_name]
    check("UV option invalidates shape only", planned.get("motion") == plan_mod.SKIP and planned.get("shape") == plan_mod.UPDATE, planned)
    settings.uv_morphs = not settings.uv_morphs
    set_physics_lever(settings, first_row, True)
    planned = operations()[first_row.pmm_name]
    check("physics VMD invalidates motion only", planned["motion"] == plan_mod.UPDATE and planned.get("shape") in (None, plan_mod.SKIP), planned)
    set_physics_lever(settings, first_row, False)
    scene.render.fps = 60
    planned = operations()[first_row.pmm_name]
    check("fps change invalidates both", set(planned.values()) == {plan_mod.UPDATE}, planned)
    scene.render.fps = 30

    # Stale preview: the token notices, and sync plans afresh regardless.
    analysis.analyze(scene)
    first_row.enabled = False
    check("preview goes stale on selection change", analysis.current_analysis(scene).stale)
    first_row.enabled = True

    # A deleted destination.
    clip_name = first_row.clip.name
    armature = next(c for c in first_row.target.children_recursive if c.type == "ARMATURE")
    bpy.data.actions.remove(first_row.clip)
    planned = operations()[first_row.pmm_name]
    check("deleted clip plans Create", planned["motion"] == plan_mod.CREATE, planned)
    check("deleted clip resync", sync(), settings.status)
    check("clip recreated", first_row.clip is not None, clip_name)
    check("nothing else reimported", len(calls["importer"]) == 1, calls["importer"])

    # A strip removed from the NLA with the action kept.
    anim = armature.animation_data
    for track, strip in animation.strips_using(anim, first_row.clip):
        track.strips.remove(strip)
    planned = operations()[first_row.pmm_name]
    check("detached clip is re-synced", planned["motion"] in (plan_mod.UPDATE, plan_mod.CREATE), planned)
    sync()

    # Selecting another action.
    picked = bpy.data.actions.new("MyDance")
    picked.use_fake_user = True
    first_row.clip = picked
    planned = preview()
    part = next(i for i in planned.items if i.key == first_row.key).parts["motion"]
    check("switched clip plans Update", part.operation == plan_mod.UPDATE and "changed" in part.reason, part.reason)
    check("switched clip names it", part.destination == "MyDance", part.destination)
    check("switched sync", sync(), settings.status)
    check("user clip kept its name", first_row.clip.name == "MyDance", first_row.clip.name)

    # A user edit inside the action.
    curve = animation.iter_fcurves(first_row.clip)[0]
    curve.keyframe_points.insert(100000.0, 1.0)
    curve.update()
    item = next(i for i in preview().items if i.key == first_row.key)
    check("edited action is skipped and kept", item.parts["motion"].operation == plan_mod.SKIP and "edits" in item.parts["motion"].reason, item.parts["motion"].reason)
    sync()
    has_edit = any(abs(p.co[0] - 100000.0) < 0.5 for p in animation.iter_fcurves(first_row.clip)[0].keyframe_points) or any(
        abs(p.co[0] - 100000.0) < 0.5 for c in animation.iter_fcurves(first_row.clip) for p in c.keyframe_points
    )
    check("edit survived a sync", has_edit)
    set_physics_lever(settings, first_row, True)
    item = next(i for i in preview().items if i.key == first_row.key)
    check("settings change on edited action blocks", item.parts["motion"].operation == plan_mod.BLOCKED, item.parts["motion"].reason)
    edited = state.action_digest(first_row.clip)
    sync()
    check("blocked action not written", state.action_digest(first_row.clip) == edited)
    check("blocked row reports it", first_row.state == "WARN", first_row.status_text)
    set_physics_lever(settings, first_row, False)
    mutations["apply"] = nudge_bone(first_index)
    item = next(i for i in preview().items if i.key == first_row.key)
    motion = item.parts["motion"]
    check("new PMM keys on an edited action update with a warning", motion.operation == plan_mod.UPDATE and any("edited" in w for w in motion.warnings), (motion.operation, motion.warnings))
    mutations["apply"] = None
    sync()

    # Force Resync, for one row and for all.
    check("force one row", sync(force_keys=(first_row.key,)), settings.status)
    check("force one row ran only that row", len(calls["importer"]) == 1, calls["importer"])
    check("force all", sync(force=True), settings.status)
    check("force all imported every selected row", len(calls["importer"]) == len(rows), calls["importer"])

    # Unselected row.
    if two_models:
        last_row.enabled = False
        other = last_row.clip
        other_digest = state.action_digest(other)
        mutations["apply"] = nudge_bone(last_index)
        sync()
        check("disabled row untouched", state.action_digest(other) == other_digest and not calls["importer"], calls["importer"])
        mutations["apply"] = None
        last_row.enabled = True
        sync(force=True)

    # A failure partway through several models.
    sync(force=True)
    good_records = {r.key: r.sync_record for r in rows}
    mutations["apply"] = lambda project: [nudge_bone(i)(project) for i in index_of.values()]
    seen = []

    def failing(path, scale, targets, name, detect, *extra):
        seen.append(name)
        if len(seen) == 2:
            raise RuntimeError("expected test failure")
        return real_importer(path, scale, targets, name, detect, *extra)

    importer.run_importer = failing
    check("failed import still finishes the sync", sync(), settings.status)
    importer.run_importer = counting_importer
    expect_clean_exit("failure")
    if two_models:
        check("failed row shows ERROR", last_row.state == "ERROR", last_row.state)
        check("first row moved on", first_row.sync_record != good_records[first_row.key])
        record = state.read_record(last_row)
        check("failed part forgot its record", "motion" not in record["parts"], sorted(record["parts"]))
    check("retry syncs the failed row only", sync() and len(calls["importer"]) == (1 if two_models else 0), calls["importer"])
    check("after retry nothing is left", all(o == plan_mod.SKIP for p in operations().values() for o in p.values()), operations())

    # Cancel between models.
    mutations["apply"] = None
    before_cancel = {r.key: r.sync_record for r in rows}
    driver = engine.SyncDriver(scene, force=True)
    stepped = 0
    while driver.step():
        stepped += 1
        if driver.run.synced >= 1:
            driver.cancel()
    check("cancel ends the driver", driver.outcome == "cancelled", driver.outcome)
    check("cancel status", "Cancelled" in settings.status, settings.status)
    expect_clean_exit("cancel")
    if two_models:
        check("cancelled model keeps its record", last_row.sync_record == before_cancel[last_row.key])
    check("timing logged for a cancel", "timing summary status=cancelled" in bpy.data.texts["NineSyncLog"].as_string())
    check("sync after cancel works", sync(), settings.status)

    # Malformed file.
    bad = os.path.join(TEMP, "bad.pmm")
    with open(PMM_PATH, "rb") as handle:
        whole = handle.read()
    with open(bad, "wb") as handle:
        handle.write(whole[: len(whole) // 3])
    keep = settings.filepath
    settings.filepath = bad
    before = snapshot_ids()
    result = analysis.analyze(scene)
    check("malformed PMM reports an error", bool(result.error), result.error)
    check("malformed preview changes nothing", snapshot_ids() == before)
    check("malformed sync fails cleanly", not sync(), settings.status)
    expect_clean_exit("malformed")
    check("timing logged for a failure", "timing summary status=error" in bpy.data.texts["NineSyncLog"].as_string())
    settings.filepath = keep

    # Clean makes the next sync run.
    sync(force=True)
    bpy.ops.ninesync.clean(animation=True, pose=True, uv=True, scene=False, audio=False)
    planned = operations()
    check("clean invalidates", not any(o == plan_mod.SKIP for p in planned.values() for o in p.values()), planned)
    check("sync after clean", sync(), settings.status)
    check("sync after clean imported", len(calls["importer"]) == len(rows), calls["importer"])


try:
    main()
except Exception:
    traceback.print_exc()
    failed.append("crashed")
shutil.rmtree(TEMP, ignore_errors=True)
print("FAILED:", failed)
sys.stdout.flush()
os._exit(1 if failed else 0)
