import bpy
import hashlib
import json
import types
from array import array

from ..common.animation import collect_owners, iter_fcurves, strips_using
from ..common.constants import CAMERA_COLLECTION, LIGHT_COLLECTION
from ..common.logger import log
from ..common.scene_utils import child_of_type, find_rig_root
from .clips import clip_track_names, find_bridge_clip_any, primary_key_owner
from .mmd_tools_link import mmd_tools_installed, mmd_tools_modules
from .visibility import find_visibility_action


RECORD_VERSION = 1


# Armature, meshes and shape key holders of a rig. mmd_tools' placeholder() can add a
# shape key, so planning reads the placeholder itself and stays free of side effects.
def rig_layout(root):
    armature = next(
        (c for c in root.children_recursive if c.type == "ARMATURE"), None
    )
    if armature is None:
        return None
    if mmd_tools_installed():
        meshes = list(mmd_tools_modules()[1].Model(root).meshes())
    else:
        meshes = [c for c in armature.children_recursive if c.type == "MESH"]
    placeholder = next(
        (
            c
            for c in root.children
            if getattr(c, "mmd_type", "") == "PLACEHOLDER" and c.type == "MESH"
        ),
        None,
    )
    key_owners = []
    for obj in ([placeholder] if placeholder is not None else []) + meshes:
        keys = getattr(getattr(obj, "data", None), "shape_keys", None)
        if keys is not None and keys not in key_owners:
            key_owners.append(keys)
    return types.SimpleNamespace(
        armature=armature,
        meshes=meshes,
        placeholder=placeholder,
        key_owners=key_owners,
        primary_keys=primary_key_owner(key_owners, placeholder),
    )


# Hash of what an action animates. Compared with the value saved after a sync to tell a
# Blender-side edit from an untouched action.
def action_digest(action):
    sha = hashlib.sha256()
    curves = sorted(iter_fcurves(action), key=lambda c: (c.data_path, c.array_index))
    sha.update(str(len(curves)).encode("ascii"))
    for fcurve in curves:
        points = fcurve.keyframe_points
        count = len(points)
        sha.update(("%s|%d|%d|" % (fcurve.data_path, fcurve.array_index, count)).encode("utf-8", "replace"))
        for prop in ("co", "handle_left", "handle_right"):
            buffer = array("f", bytes(8 * count))
            points.foreach_get(prop, buffer)
            sha.update(buffer.tobytes())
        modes = array("i", bytes(4 * count))
        points.foreach_get("interpolation", modes)
        sha.update(modes.tobytes())
    return sha.hexdigest()[:24]


# One destination as the record stores it. linked is false when the action survives but
# no longer plays on its owner (strip removed, action unassigned).
def output_entry(owner, action):
    entry = {"owner": owner.name, "action": "", "digest": "", "linked": False}
    if action is None:
        return entry
    anim = owner.animation_data
    entry["action"] = action.name
    entry["digest"] = action_digest(action)
    entry["linked"] = anim is not None and (
        anim.action == action or bool(strips_using(anim, action))
    )
    return entry


def active_action(owner):
    anim = owner.animation_data
    return anim.action if anim else None


# The actions a part writes to, found the way import_model finds them.
def part_outputs(row, layout, settings, part, label):
    use_clips = bool(settings.use_clips)
    if part == "visible":
        found = [(m, find_visibility_action(m, label)) for m in layout.meshes]
        return [output_entry(m, action) for m, action in found if action is not None]
    if part == "motion":
        clip = row.clip if use_clips else active_action(layout.armature)
        return [output_entry(layout.armature, clip)]
    entries = []
    for owner in layout.key_owners:
        if not use_clips:
            action = active_action(owner)
        elif owner == layout.primary_keys and row.shape_clip is not None:
            action = row.shape_clip
        else:
            action = find_bridge_clip_any(owner, clip_track_names(label)[1:3])
        entries.append(output_entry(owner, action))
    return entries


# Whether the saved outputs still stand: "ok", "missing" (action gone or detached),
# "switched" (another action selected) or "edited" (same action, different content).
# Without clips nothing is user-selected, so a different active action counts as edited.
def compare_outputs(recorded, current, use_clips):
    by_owner = {e["owner"]: e for e in current}
    worst = ("ok", "")
    for saved in recorded:
        now = by_owner.get(saved["owner"])
        if now is None or not now["action"]:
            return "missing", saved["action"] or saved["owner"]
        renamed = now["action"] != saved["action"]
        same_content = now["digest"] == saved["digest"]
        if renamed and use_clips and not same_content:
            return "switched", now["action"]
        if not now["linked"]:
            return "missing", now["action"]
        if not same_content:
            worst = ("edited", now["action"])
    return worst


# Reads a row's record. Anything unreadable counts as no record, which only costs a sync.
def read_record(row):
    try:
        record = json.loads(row.sync_record) if row.sync_record else None
    except ValueError:
        record = None
    if not isinstance(record, dict) or record.get("version") != RECORD_VERSION:
        return None
    return record


# Saves the state the named parts reached. Only called after they imported successfully.
def write_parts(row, entry, layout, settings, label, names):
    record = read_record(row) or {}
    if record.get("source") != entry.source or record.get("target") != entry.target_name:
        record = {}
    record.update(version=RECORD_VERSION, source=entry.source, target=entry.target_name)
    parts = record.setdefault("parts", {})
    for name in names:
        planned = entry.parts[name]
        parts[name] = {
            "fingerprint": planned.fingerprint,
            "settings": planned.settings,
            "keys": planned.keys,
            "range": planned.frame_range,
            "tracks": planned.tracks,
            "outputs": part_outputs(row, layout, settings, name, label),
        }
    row.sync_record = json.dumps(record, sort_keys=True, ensure_ascii=False)


# A failed part may have half-replaced its action, so its record cannot be trusted and
# the next sync treats it as never synced. Parts that were not attempted keep theirs.
def forget_parts(row, names):
    record = read_record(row)
    if record is None:
        return
    for name in names:
        record.get("parts", {}).pop(name, None)
    row.sync_record = json.dumps(record, sort_keys=True, ensure_ascii=False)
    log.info("sync record cleared for '%s': %s", row.pmm_name, list(names))



SCENE_RIGS = {"camera": (CAMERA_COLLECTION, "CAMERA"), "light": (LIGHT_COLLECTION, "LIGHT")}


# (root, child) of the camera or light rig, or None. Looks only: ensure_*_rig would
# create the collection and the rig.
def find_scene_rig(kind):
    collection_name, mmd_type = SCENE_RIGS[kind]
    collection = bpy.data.collections.get(collection_name)
    root = find_rig_root(collection, mmd_type) if collection is not None else None
    if root is None:
        return None
    child = child_of_type(root, mmd_type)
    return (root, child) if child is not None else None


# What a camera or light import writes: the active action of each rig ID that has one.
def scene_outputs(kind):
    rig = find_scene_rig(kind)
    if rig is None:
        return []
    owners = collect_owners(list(rig))
    return [output_entry(o, active_action(o)) for o in owners if active_action(o) is not None]


# The camera and light record. It sits on the scene settings, like the model records sit
# on their rows, and is keyed by PMM like them.
def read_scene_record(settings, source):
    try:
        record = json.loads(settings.scene_record) if settings.scene_record else None
    except ValueError:
        record = None
    if not isinstance(record, dict) or record.get("version") != RECORD_VERSION:
        return None
    return record if record.get("source") == source else None


# Saves one scene part after it imported successfully. A record of another PMM is
# replaced, not merged.
def write_scene_part(settings, source, kind, planned):
    record = read_scene_record(settings, source) or {}
    record.update(version=RECORD_VERSION, source=source)
    record.setdefault("parts", {})[kind] = {
        "fingerprint": planned.fingerprint,
        "settings": planned.settings,
        "keys": planned.keys,
        "range": planned.frame_range,
        "outputs": scene_outputs(kind),
    }
    settings.scene_record = json.dumps(record, sort_keys=True, ensure_ascii=False)


# A failed camera or light import may have half-rebuilt its rig, so its record goes.
def forget_scene_part(settings, kind):
    try:
        record = json.loads(settings.scene_record) if settings.scene_record else None
    except ValueError:
        record = None
    if not isinstance(record, dict):
        settings.scene_record = ""
        return
    record.get("parts", {}).pop(kind, None)
    settings.scene_record = json.dumps(record, sort_keys=True, ensure_ascii=False)
    log.info("sync record cleared for %s", kind)
