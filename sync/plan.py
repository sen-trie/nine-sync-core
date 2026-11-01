import bpy
import os
import struct
import types

from ..common.addon import addon_version
from ..common.constants import ACTION_PREFIX
from ..common.scene_utils import model_key
from ..common.timing import phase
from ..pmm import PmmError
from .clips import model_label
from .mmd_tools_link import mmd_tools_version
from .morphs import group_morph_table, resolve_morph_names
from .motion import (
    bone_key_moves,
    bone_track_moves,
    digest_values,
    morph_track_moves,
    motion_fingerprint,
    pack_bone_key,
    pack_morph_key,
    shape_fingerprint,
    track_digests,
    track_range,
    vmd_bone_name,
)
from .retime import retime_factor
from .scale import current_scale
from .sync_state import (
    active_action,
    compare_outputs,
    part_outputs,
    read_record,
    rig_layout,
)
from .targets import match_roots, target_usable


CREATE = "Create"
UPDATE = "Update"
SKIP = "Skip (Unchanged)"
BLOCKED = "Blocked"
PART_LABELS = {"motion": "Motion", "shape": "Shapekey"}
READ_ERRORS = (ValueError, IndexError, OverflowError, struct.error)


# One destination of a model row: its bones or its shape keys.
class PartPlan:
    def __init__(self, part):
        self.part = part
        self.operation = BLOCKED
        self.reason = ""
        self.warnings = []
        self.destination = ""
        self.exists = False
        self.fingerprint = ""
        self.settings = ""
        self.frame_range = None
        self.keys = 0
        self.tracks = {}
        self.before = None
        self.changed = None

    @property
    def runs(self):
        return self.operation in (CREATE, UPDATE)


# One PMM model. Holds names, not Blender references, so a cached preview never keeps a
# pointer that an undo could free; the engine looks rows up again by key.
class PlanItem:
    def __init__(self, key, name):
        self.key = key
        self.pmm_name = name
        self.source = ""
        self.target_name = ""
        self.selected = True
        self.mapping = "unassigned"
        self.reason = ""
        self.warnings = []
        self.parts = {}
        self.bones = None
        self.morphs = None

    @property
    def run_parts(self):
        return tuple(name for name, part in self.parts.items() if part.runs)

    @property
    def operation(self):
        if not self.parts:
            return BLOCKED if self.reason else SKIP
        operations = [part.operation for part in self.parts.values()]
        for wanted in (CREATE, UPDATE, BLOCKED):
            if wanted in operations:
                return wanted
        return SKIP


class SyncPlan:
    def __init__(self):
        self.items = []
        self.notes = []
        self.factor = 1.0


# Parses PMM bytes. The one place malformed input becomes PmmError for every caller,
# since a corrupt file can fail in the unpacking as well as in the parser's own checks.
def parse_checked(data, parse):
    try:
        return parse(data)
    except READ_ERRORS as error:
        raise PmmError("malformed PMM: %s" % error)


# Target for a PMM row: the assigned one when usable, else what refresh_targets will
# auto-match. Read-only, so the preview agrees with a sync that has not assigned it yet.
def resolve_target(model, row):
    root = row.target if row is not None else None
    if root is not None and target_usable(root):
        return root, "assigned"
    tier = match_roots(model)
    if not tier:
        return None, "unassigned"
    return tier[0], "ambiguous" if len(tier) > 1 else "auto"


# Original bone name to pose bone, the same key mmd_tools' makePmxBoneMap uses.
def bone_name_table(armature):
    table = {}
    for bone in armature.pose.bones:
        mmd_bone = getattr(bone, "mmd_bone", None)
        name = mmd_bone.name_j if mmd_bone is not None else ""
        table[name or bone.get("mmd_bone_name_j", bone.get("name_j", bone.name))] = bone
    return table


# Which source tracks reach the rig. 'cut' are names longer than a VMD can carry: they
# exist on the rig but the importer cannot find them.
def map_bones(model, table):
    report = types.SimpleNamespace(
        tracks=len(model.bones),
        matched=0,
        renamed=0,
        missing=[],
        cut=[],
        losing=[],
        animated=0,
        animated_matched=0,
        mappable_keys=0,
        lost_keys=0,
    )
    for name, keys in zip(model.bone_names, model.bones):
        moves = bone_track_moves(keys)
        report.animated += 1 if moves else 0
        lookup = vmd_bone_name(name)
        if lookup in table:
            report.matched += 1
            report.renamed += 1 if lookup != name else 0
            report.animated_matched += 1 if moves else 0
            report.mappable_keys += len(keys)
            continue
        (report.cut if name in table else report.missing).append(name)
        moving = sum(1 for k in keys if bone_key_moves(k))
        report.lost_keys += moving
        if moving:
            report.losing.append(name)
    return report


def map_morphs(model, resolved):
    report = types.SimpleNamespace(
        tracks=len(model.morphs),
        exact=0,
        remapped=[],
        missing=[],
        animated=0,
        animated_matched=0,
        mappable_keys=0,
        lost_keys=0,
    )
    for name, keys in zip(model.morph_names, model.morphs):
        moves = morph_track_moves(keys)
        report.animated += 1 if moves else 0
        target = resolved.get(name)
        if target is None:
            if moves:
                report.missing.append(name)
                report.lost_keys += len(keys)
            continue
        report.exact += 1 if target == name else 0
        if target != name:
            report.remapped.append((name, target))
        report.animated_matched += 1 if moves else 0
        report.mappable_keys += len(keys)
    return report


# What a baked physics VMD contributes to the import. None when off or unset.
def physics_state(row, settings):
    if not settings.use_physics_vmd or not row or not row.physics_vmd:
        return None
    path = bpy.path.abspath(row.physics_vmd)
    try:
        stat = os.stat(path)
    except OSError:
        return (row.physics_vmd, "missing")
    return (row.physics_vmd, stat.st_size, stat.st_mtime_ns, settings.physics_disable_rigid_body)


# Everything besides the PMM keys that shapes a part's action. A change here means the
# existing output no longer matches what a sync would produce.
def part_settings(part, scene, settings, root, signature, row):
    common = (
        tuple(addon_version()),
        mmd_tools_version(),
        round(retime_factor(scene, settings), 6),
        round(current_scale(settings), 6),
        bool(settings.use_clips),
        bool(settings.update_scene_settings),
        root.name,
    )
    if part == "motion":
        return digest_values(
            common, settings.interpolation_layout, physics_state(row, settings), signature
        )
    return digest_values(common, bool(settings.uv_morphs), signature)


# Action name shown in the preview, and whether it already exists.
def describe_destination(part, entries, label, use_clips):
    names = [e["action"] for e in entries if e["action"]]
    if names:
        extra = " (+%d)" % (len(names) - 1) if len(names) > 1 else ""
        return names[0] + extra, True
    if use_clips:
        suffix = "_motion_clip" if part == "motion" else "_shapekey_clip"
    else:
        suffix = "_bone" if part == "motion" else "_facial"
    return ACTION_PREFIX + label + suffix, False


# The decision table. An edit in Blender is never replaced because of a mere settings
# or version change; new PMM keys or an explicit selection are what authorize it.
def decide(plan, record_part, record_note, current, source_changed, use_clips):
    verb = UPDATE if plan.exists else CREATE
    if record_part is None:
        plan.operation = verb
        plan.reason = record_note
        return
    state, detail = compare_outputs(record_part["outputs"], current, use_clips)
    settings_changed = record_part.get("settings") != plan.settings
    if state == "missing":
        plan.operation = verb
        plan.reason = "Output '%s' is gone" % detail
    elif state == "switched":
        plan.operation = verb
        plan.reason = "Selected action changed to '%s'" % detail
    elif source_changed:
        plan.operation = UPDATE
        plan.reason = "PMM motion changed"
        if state == "edited":
            plan.warnings.append(
                "'%s' was edited in Blender; keys inside the PMM span will be replaced" % detail
            )
    elif settings_changed and state == "edited":
        plan.operation = BLOCKED
        plan.reason = "Settings or rig changed, but '%s' was edited in Blender; use Force Resync in the PMM Motion Inspector to replace it" % detail
    elif settings_changed:
        plan.operation = UPDATE
        plan.reason = "Import settings or rig changed"
    elif state == "edited":
        plan.operation = SKIP
        plan.reason = "PMM unchanged; your edits to '%s' are kept" % detail
    else:
        plan.operation = SKIP
        plan.reason = "PMM unchanged since last sync"


# Track names that differ from the last sync, or None for a record from before tracks
# were saved.
def changed_tracks(saved, current):
    if saved is None:
        return None
    names = [n for n, digest in current.items() if saved.get(n) != digest]
    return names + [n for n in saved if n not in current]


# Plans one part of a mapped model.
def plan_part(part, item, model, row, root, layout, scene, settings, signature, record, force):
    use_clips = bool(settings.use_clips)
    label = model_label(model)
    plan = PartPlan(part)
    tracks = model.bones if part == "motion" else model.morphs
    with phase("fingerprint", label):
        plan.fingerprint = (motion_fingerprint if part == "motion" else shape_fingerprint)(model)
        names = model.bone_names if part == "motion" else model.morph_names
        pack = pack_bone_key if part == "motion" else pack_morph_key
        plan.tracks = track_digests(names, tracks, pack)
    plan.frame_range = track_range(tracks)
    plan.keys = sum(len(k) for k in tracks)
    plan.settings = part_settings(part, scene, settings, root, signature, row)
    current = part_outputs(row, layout, settings, part, label) if row is not None else []
    plan.destination, plan.exists = describe_destination(part, current, label, use_clips)
    if not use_clips:
        replaced = [
            active.name
            for active in (active_action(o) for o in owners_of(part, layout))
            if active is not None and not active.name.startswith(ACTION_PREFIX)
        ]
        if replaced:
            plan.warnings.append("Active action '%s' will be replaced" % replaced[0])
    saved = None
    note = "Not synced yet"
    if record is not None:
        if record.get("source") != item.source:
            note = "PMM file changed"
        elif record.get("target") != item.target_name:
            note = "Target changed"
        else:
            saved = record.get("parts", {}).get(part)
    if saved is not None:
        plan.before = (saved.get("keys"), saved.get("range"))
        plan.changed = changed_tracks(saved.get("tracks"), plan.tracks)
    if force:
        plan.operation = UPDATE if plan.exists else CREATE
        plan.reason = "Force Resync"
        return plan
    changed = saved is not None and saved.get("fingerprint") != plan.fingerprint
    decide(plan, saved, note, current, changed, use_clips)
    return plan


def owners_of(part, layout):
    return [layout.armature] if part == "motion" else layout.key_owners


# Plans one PMM model into an item. Reads the rig and the record; writes nothing.
def plan_model(model, row, scene, settings, source, force):
    key = model_key(model)
    item = PlanItem(key, model.name or model.name_en or key)
    item.source = source
    item.selected = row.enabled if row is not None else True
    root, item.mapping = resolve_target(model, row)
    if root is None:
        item.reason = "No target assigned"
        item.warnings.append("No mmd_tools model matches; assign a target")
        return item
    item.target_name = root.name
    if item.mapping == "ambiguous":
        item.warnings.append("Several models match '%s'; check the target" % item.pmm_name)
    layout = rig_layout(root)
    if layout is None:
        item.reason = "Target has no armature"
        return item
    with phase("mapping", item.pmm_name):
        table = bone_name_table(layout.armature)
        resolved = resolve_morph_names(root, layout.meshes, model)[0]
        item.bones = map_bones(model, table)
        item.morphs = map_morphs(model, resolved)
    if item.bones.lost_keys:
        item.warnings.append(
            "%d moving key(s) on bones the target lacks will be omitted" % item.bones.lost_keys
        )
    if item.morphs.missing:
        item.warnings.append(
            "%d animated morph(s) have no match on the target" % len(item.morphs.missing)
        )
    if not item.selected:
        return item
    record = read_record(row) if row is not None else None
    bone_signature = digest_values(layout.armature.name, sorted(table))
    groups = sorted(
        (name, tuple((o.morph_type, o.name, round(o.factor, 6)) for o in group.data))
        for name, group in group_morph_table(root).items()
    )
    morph_signature = digest_values(sorted(resolved.items()), groups)
    if model.bones:
        item.parts["motion"] = plan_part(
            "motion", item, model, row, root, layout, scene, settings, bone_signature, record, force
        )
    if model.morphs and layout.meshes:
        item.parts["shape"] = plan_part(
            "shape", item, model, row, root, layout, scene, settings, morph_signature, record, force
        )
    if "motion" in item.parts and row is not None and settings.use_physics_vmd and row.physics_vmd:
        if not os.path.isfile(bpy.path.abspath(row.physics_vmd)):
            item.parts["motion"].warnings.append("Baked physics VMD not found")
    return item


# Identity of the PMM a record belongs to. Hashed: this goes into the .blend, and the
# path can hold the user's account name.
def source_identity(settings):
    path = os.path.normcase(os.path.normpath(bpy.path.abspath(settings.filepath)))
    return digest_values(path)


# The shared, side-effect free plan: preview shows it and a sync executes a fresh copy,
# so the two cannot disagree. force_keys limits a forced run to those rows.
def build_plan(scene, project, force=False, force_keys=()):
    settings = scene.mmd_bridge
    plan = SyncPlan()
    plan.factor = retime_factor(scene, settings)
    source = source_identity(settings)
    rows = {row.key: row for row in settings.targets}
    claimed = {}
    for model in project.models:
        key = model_key(model)
        forced = force or key in force_keys
        item = plan_model(model, rows.get(key), scene, settings, source, forced)
        if item.selected and item.target_name:
            claimed.setdefault(item.target_name, []).append(item)
        plan.items.append(item)
    for sharing in claimed.values():
        if len(sharing) > 1:
            for item in sharing:
                item.mapping = "ambiguous"
                item.warnings.append(
                    "%d PMM models share the target '%s'" % (len(sharing), item.target_name)
                )
    if settings.sync_camera and project.camera_keys:
        plan.notes.append("Camera: updated on every sync (%d keys)" % len(project.camera_keys))
    if settings.sync_light and project.light_keys:
        plan.notes.append("Light: updated on every sync (%d keys)" % len(project.light_keys))
    return plan
