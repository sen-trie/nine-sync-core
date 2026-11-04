from bpy.types import Panel

from ...common.constants import NINESYNC_CATEGORY
from ...sync.analysis import current_analysis
from ...sync.engine import sync_running
from ...sync.plan import BLOCKED, CREATE, PART_LABELS, SKIP, UPDATE
from ..helpers import draw_wrapped


LIST_LIMIT = 6
PREVIEW_WRAP = 100
UNCHANGED_COLUMN_AT = 10
MAPPING_TEXT = {
    "assigned": "assigned",
    "auto": "will auto-assign",
    "ambiguous": "ambiguous",
    "unassigned": "unassigned",
}
TRACK_WORD = {"motion": "bone(s)", "shape": "morph(s)"}
OPERATION_ICON = {CREATE: "ADD", UPDATE: "FILE_REFRESH", SKIP: "CHECKMARK", BLOCKED: "ERROR"}


def range_text(span):
    return "frames %d-%d" % tuple(span) if span else "no keys"


# Long name lists stay readable in a sidebar; the sync log has the full list.
def names_text(names):
    shown = ", ".join(names[:LIST_LIMIT])
    return shown + (" +%d more" % (len(names) - LIST_LIMIT) if len(names) > LIST_LIMIT else "")


def state_text(keys, span):
    return "%d key(s), %s" % (keys, range_text(span))


# The left side of a change. A record older than this field has no numbers to show.
def before_text(part):
    if part.before is None:
        return "Not synced yet" if not part.exists else "No previous record"
    keys, span = part.before
    return "Previous sync" if keys is None else state_text(keys, span)


def change_text(part):
    if part.changed and part.part in TRACK_WORD:
        return "%d %s changed: %s" % (len(part.changed), TRACK_WORD[part.part], names_text(part.changed))
    return part.reason


# One changed destination: label, before, arrow, after, left to right.
def draw_part_change(box, part):
    row = box.split(factor=0.13, align=True)
    row.label(text=PART_LABELS[part.part])
    sides = row.split(factor=0.47, align=True)
    sides.label(text=before_text(part))
    after = sides.row(align=True)
    after.label(text="", icon="FORWARD")
    after.label(text=state_text(part.keys, part.frame_range))
    note = box.column(align=True)
    note.scale_y = 0.85
    verb = "New" if part.operation == CREATE else "Into"
    note.label(text="%s: %s" % (verb, part.destination))
    draw_wrapped(note, change_text(part), PREVIEW_WRAP)
    for warning in part.warnings:
        alert = box.column()
        alert.alert = True
        draw_wrapped(alert, warning, PREVIEW_WRAP)


def draw_changed_item(layout, item):
    box = layout.box()
    box.label(text="%s  >  %s" % (item.pmm_name, item.target_name), icon="ARMATURE_DATA")
    for part in item.parts.values():
        if part.runs:
            draw_part_change(box, part)
    for warning in item.warnings:
        alert = box.column()
        alert.alert = True
        draw_wrapped(alert, warning, PREVIEW_WRAP)


# Rows a sync cannot touch, with what stands in the way.
def draw_blocked(layout, blocked):
    box = layout.box()
    box.label(text="Not synced", icon="ERROR")
    for item in blocked:
        reason = item.reason or next(
            p.reason for p in item.parts.values() if p.operation == BLOCKED
        )
        draw_wrapped(box, "%s: %s" % (item.pmm_name, reason), PREVIEW_WRAP)


def draw_unchanged(layout, items):
    flow = layout.column_flow(columns=2 if len(items) > UNCHANGED_COLUMN_AT else 1)
    for item in items:
        edited = any("edits" in part.reason for part in item.parts.values())
        note = "Unchanged, your edits are kept" if edited else "Unchanged"
        flow.label(text="%s (%s)" % (item.pmm_name, note))


# The Preview window: what changed first, what is unchanged last.
def draw_preview(layout, scene):
    result = current_analysis(scene)
    if result is None or result.error or result.plan is None:
        draw_wrapped(layout, result.error if result else "Nothing to preview", PREVIEW_WRAP)
        return
    selected = [item for item in result.plan.items if item.selected]
    changed = [item for item in selected if item.run_parts]
    unchanged = [item for item in selected if not item.run_parts and item.operation == SKIP]
    blocked = [item for item in selected if item.operation == BLOCKED or (
        item.run_parts and any(p.operation == BLOCKED for p in item.parts.values())
    )]
    layout.label(text="Changed PMX Models (%d)" % len(changed))
    if not changed:
        layout.label(text="No PMX model changed since the last sync")
    for item in changed:
        draw_changed_item(layout, item)
    if blocked:
        draw_blocked(layout, blocked)
    if unchanged:
        layout.separator()
        draw_unchanged(layout, unchanged)
    left_out = len(result.plan.items) - len(selected)
    if left_out:
        layout.label(text="%d PMX model(s) not selected" % left_out)
    if result.plan.scene:
        layout.separator()
        draw_scene(layout, result.plan.scene)


# Camera and light, each with its own decision; a running one shows before and after
# like a model part.
def draw_scene(layout, scene_plan):
    layout.label(text="Camera and Light")
    for part in scene_plan.values():
        if part.runs:
            box = layout.box()
            draw_part_change(box, part)
            continue
        row = layout.row()
        row.alert = part.operation == BLOCKED
        row.label(text="%s: %s" % (PART_LABELS[part.part], part.operation), icon=OPERATION_ICON[part.operation])
        note = layout.column()
        note.scale_y = 0.85
        draw_wrapped(note, part.reason, PREVIEW_WRAP)
        for warning in part.warnings:
            alert = layout.column()
            alert.alert = True
            draw_wrapped(alert, warning, PREVIEW_WRAP)


class NINESYNC_PT_inspector(Panel):
    bl_label = "PMM Motion Inspector"
    bl_idname = "NINESYNC_PT_inspector"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = NINESYNC_CATEGORY
    bl_parent_id = "NINESYNC_PT_main"
    bl_order = 4
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        running = sync_running()
        force = layout.row()
        force.enabled = not running
        force.operator("ninesync.sync_now", text="Force Resync", icon="FILE_REFRESH").force = True
        read = layout.row()
        read.enabled = not running
        read.operator_context = "EXEC_DEFAULT"
        read.operator("ninesync.preview", text="Read PMM Motion", icon="VIEWZOOM")
        result = current_analysis(context.scene)
        if result is None:
            return
        if result.error:
            box = layout.box()
            box.alert = True
            draw_wrapped(box, result.error)
            return
        if result.stale:
            row = layout.row()
            row.alert = True
            row.label(text="Out of date: read again", icon="ERROR")
        if result.plan is None:
            return
        items = {item.key: item for item in result.plan.items}
        for summary in result.summaries:
            self.draw_model(layout, summary, items.get(summary.key), running)
        for line in result.scene_tracks:
            layout.label(text=line)

    def draw_model(self, layout, summary, item, running):
        box = layout.box()
        header = box.row(align=True)
        header.label(text="#%d %s" % (summary.index, summary.name), icon="ARMATURE_DATA")
        if item is not None and item.parts and item.selected:
            button = header.row(align=True)
            button.enabled = not running
            button.operator("ninesync.sync_now", text="", icon="FILE_REFRESH").force_key = summary.key
        if summary.name_en and summary.name_en != summary.name:
            box.label(text=summary.name_en)
        column = box.column(align=True)
        column.scale_y = 0.85
        column.label(text="Source bones: %d animated of %d" % (summary.animated_bones, summary.bone_tracks))
        column.label(text="%d key(s), %s" % (summary.bone_keys, range_text(summary.bone_range)))
        column.label(text="Source morphs: %d animated of %d" % (summary.animated_morphs, summary.morph_tracks))
        column.label(text="%d key(s), %s" % (summary.morph_keys, range_text(summary.morph_range)))
        if summary.hidden_keys:
            column.label(text="Visibility: hidden on %d of %d key(s)" % (summary.hidden_keys, summary.visibility_keys))
        if item is None:
            return
        text = MAPPING_TEXT[item.mapping]
        box.label(text="Target: %s (%s)" % (item.target_name or "none", text))
        bones = item.bones
        if bones is None:
            return
        morphs = item.morphs
        column = box.column(align=True)
        column.scale_y = 0.85
        column.label(text="Reaches target: %d of %d animated bones" % (bones.animated_matched, bones.animated))
        column.label(text="%d key(s) on matched bones" % bones.mappable_keys)
        column.label(text="Reaches target: %d of %d animated morphs" % (morphs.animated_matched, morphs.animated))
        column.label(text="%d key(s) on matched morphs" % morphs.mappable_keys)
        if bones.renamed:
            draw_wrapped(box, "%d bone(s) match by their 15 byte VMD name" % bones.renamed)
        if morphs.remapped:
            draw_wrapped(box, "%d morph(s) match by English or truncated name" % len(morphs.remapped))
        idle = len(bones.missing) + len(bones.cut) - len(bones.losing)
        if idle > 0:
            draw_wrapped(box, "%d unmatched bone(s) hold no motion; nothing is lost" % idle)
        for label, names in (
            ("Motion omitted, bones not reachable", bones.losing),
            ("Animated morphs not on the target", morphs.missing),
        ):
            if names:
                alert = box.column()
                alert.alert = True
                draw_wrapped(alert, "%s (%d): %s" % (label, len(names), names_text(names)))
