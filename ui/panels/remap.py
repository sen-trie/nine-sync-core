import json

import bpy
from bpy.props import BoolProperty, IntProperty, StringProperty
from bpy.types import Operator, Panel, UIList

from ...common.constants import NINESYNC_CATEGORY
from ...common.scene_utils import find_armature, model_key
from ...pmm import PmmError, parse_pmm
from ...sync.rigify import (
    candidate_controls,
    is_rigify_armature,
    mapping_tree,
    matching_bones,
    matching_slots,
    missing_mmd_bones,
    mmd_label,
    pmm_moving_bones,
    shared_controls,
    slot_key,
    slot_tag,
    sync_slots,
)
from ...sync.targets import target_usable


# Rows whose target is a Rigify rig converted with MMD Tools Append.
def rigify_rows(settings):
    return [
        row
        for row in settings.targets
        if row.target is not None
        and target_usable(row.target)
        and is_rigify_armature(find_armature(row.target))
    ]


# Only the exceptions get an icon: a mapping that is carried needs no tick.
STATUS_ICONS = {
    "active": "NONE",
    "inactive": "REMOVE",
    "off": "X",
    "no control": "ERROR",
    "no bone": "ERROR",
    "group": "GROUP_BONE",
}


# What the open dialogs list: the armature's name, the slots and bones the search matched
# in order, the bones the PMM moves, and the controls two rows share. A list draws from
# this because it cannot reach the dialog that opened it.
VIEW = {"rig": "", "shown": [], "free": [], "found": [], "shared": set()}


# The target row and its Rigify armature for an operator's index, or (None, None).
def target_and_rig(context, index):
    targets = context.scene.mmd_bridge.targets
    row = targets[index] if 0 <= index < len(targets) else None
    rig = find_armature(row.target) if row is not None and row.target is not None else None
    if rig is None or not is_rigify_armature(rig):
        return None, None
    return row, rig


# The bones the PMM moves for a target's model, that the rig has, as (PMM name, rig bone)
# pairs. Empty when the PMM cannot be read or has no such model.
def pmm_found(context, row, armature):
    path = bpy.path.abspath(context.scene.mmd_bridge.filepath)
    try:
        with open(path, "rb") as handle:
            project = parse_pmm(handle.read())
    except (OSError, PmmError):
        return []
    model = next((m for m in project.models if model_key(m) == row.key), None)
    return pmm_moving_bones(armature, model) if model is not None else []


# The tree as flat lines (indent, text, status), one per node, parents first.
def tree_lines(armature, item=None):
    lines = []

    def add(node, depth):
        name_j, control, kind, status, _group, children = node
        text = "%s  >  %s" % (name_j, control)
        if kind != "any":
            text += "  (%s)" % kind.upper()
        if status != "active":
            text += "  - %s" % status
        lines.append((depth, text, status))
        for child in children:
            add(child, depth + 1)

    for group, mode, nodes in mapping_tree(armature, item):
        title = group.replace("_", " ").title()
        if mode is not None:
            title += "  (%s)" % mode
        lines.append((0, title, "group"))
        for node in nodes:
            add(node, 1)
    return lines



# Opens the Show Mappings popup for one Rigify target.
class NINESYNC_OT_show_rigify_map(Operator):
    bl_idname = "ninesync.show_rigify_map"
    bl_label = "Show Mappings"
    bl_description = "List which Rigify control each MMD bone drives, as a tree"
    bl_options = {"INTERNAL"}

    index: IntProperty(options={"HIDDEN"})

    def execute(self, context):
        row, armature = target_and_rig(context, self.index)
        if armature is None:
            return {"CANCELLED"}
        wm = context.window_manager
        wm.ninesync_rigify_target = self.index
        # Not keep_open: a button in a kept-open popup cannot close it, so Close would do nothing.
        return bpy.ops.wm.call_panel(name="NINESYNC_PT_show_map", keep_open=False)


# Closes a popup: running any operator from it does.
class NINESYNC_OT_close_popup(Operator):
    bl_idname = "ninesync.close_popup"
    bl_label = "Close"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        return {"FINISHED"}


# The mappings as a tree: every MMD bone with the Rigify control it drives, indented under
# the mapped control above it, in two columns as wide as their text.
class NINESYNC_PT_show_map(Panel):
    bl_label = "Show Mappings"
    bl_idname = "NINESYNC_PT_show_map"
    bl_space_type = "VIEW_3D"
    bl_region_type = "WINDOW"
    bl_ui_units_x = 32

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager
        row, armature = target_and_rig(context, wm.ninesync_rigify_target)
        if armature is None:
            layout.label(text="No Rigify rig on this target")
            return
        layout.label(text="Show Mappings")
        lines = tree_lines(armature, row)
        layout.label(text="MMD bone  >  Rigify control")
        half = (len(lines) + 1) // 2
        columns = layout.split(factor=0.5)
        for chunk in (lines[:half], lines[half:]):
            col = columns.column(align=True)
            col.scale_y = 0.75
            for depth, text, status in chunk:
                line = col.row(align=True)
                line.alert = status in ("no control", "no bone")
                line.enabled = status not in ("inactive", "off")
                line.label(text=("    " * depth) + text, icon=STATUS_ICONS[status])
        layout.operator("ninesync.close_popup", text="Close")


# Re-lists what the search matches and keeps that list until the search changes, so a row
# whose control is cleared or assigned stays in view. Slots are listed normally; with Only
# Missing, the PMM bones no control follows, by name.
def search_changed(self, context):
    row, armature = target_and_rig(context, self.index)
    if armature is None:
        return
    if self.only_missing:
        VIEW["free"] = matching_bones(armature, [f.mmd for f in context.window_manager.ninesync_rigify_free], self.search)
    else:
        VIEW["shown"] = matching_slots(armature, row, self.search)


# Fills the list of PMM bones no control follows, as they are now.
def load_free(context, row, armature):
    wm = context.window_manager
    wm.ninesync_rigify_free.clear()
    for bone in missing_mmd_bones(armature, row, VIEW["found"]):
        wm.ninesync_rigify_free.add().mmd = bone
    wm.ninesync_rigify_target = list(context.scene.mmd_bridge.targets).index(row)


def only_missing_changed(self, context):
    row, armature = target_and_rig(context, self.index)
    if armature is not None and self.only_missing:
        VIEW["found"] = pmm_found(context, row, armature)
        load_free(context, row, armature)
    search_changed(self, context)


# Orders a list by a fixed ranking and hides what is not in it. Items that `always` holds
# are shown first whatever the ranking says, so a row just added is seen at once.
def ranked(items, key, ranking, flag, always=lambda item: False, group=None):
    rank = {k: i for i, k in enumerate(ranking)}
    # With a group (the MMD bone), items sharing it sit together, at their best rank.
    first = {}
    for item in items:
        if group is not None and key(item) in rank:
            first[group(item)] = min(first.get(group(item), len(rank)), rank[key(item)])
    flags = [flag if key(item) in rank or always(item) else 0 for item in items]
    ordered = sorted(
        range(len(items)),
        key=lambda i: (0, i, 0)
        if always(items[i])
        else (1, first.get(group(items[i]), 0) if group is not None else 0, rank.get(key(items[i]), len(rank) + i)),
    )
    new_order = [0] * len(items)
    for position, index in enumerate(ordered):
        new_order[index] = position
    return flags, new_order


# One mapping: the MMD bone, fixed, and the Rigify control chosen for it.
class NINESYNC_UL_rigify_map(UIList):
    def filter_items(self, context, data, propname):
        rig = bpy.data.objects.get(VIEW["rig"])
        VIEW["shared"] = shared_controls(rig, data) if rig is not None else set()
        return ranked(getattr(data, propname), slot_key, VIEW["shown"], self.bitflag_filter_item, lambda item: not item.base, lambda item: item.bone)

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        rig = bpy.data.objects.get(VIEW["rig"])
        if rig is None:
            return
        clash = bool(item.control) and item.control in VIEW["shared"]
        cells = layout.split(factor=0.5)
        label = cells.row()
        label.alert = clash
        if item.base or item.bone:
            tag = slot_tag(data, item)
            label.label(text=mmd_label(rig, item.bone) + ("  (%s)" % tag if tag else ""))
        else:
            label.prop_search(item, "bone", rig.pose, "bones", text="", icon="BONE_DATA")
        field = cells.row(align=True)
        field.alert = clash or (bool(item.control) and item.control not in rig.pose.bones)
        field.prop_search(item, "control", rig.pose, "bones", text="", icon="BONE_DATA")
        if not item.base:
            field.operator("ninesync.remove_rigify_map", text="", icon="X").slot = index
        elif item.control != item.base:
            undo = field.operator("ninesync.reset_rigify_map", text="", icon="LOOP_BACK")
            undo.index, undo.key = active_data.index, slot_key(item)


# A PMM bone no control follows, fixed, and a field to choose the Rigify control for it.
class NINESYNC_UL_rigify_free(UIList):
    def filter_items(self, context, data, propname):
        row = None
        targets = context.scene.mmd_bridge.targets
        if 0 <= data.ninesync_rigify_target < len(targets):
            row = targets[data.ninesync_rigify_target]
        rig = bpy.data.objects.get(VIEW["rig"])
        VIEW["shared"] = shared_controls(rig, row) if rig is not None and row is not None else set()
        return ranked(getattr(data, propname), lambda item: item.mmd, VIEW["free"], self.bitflag_filter_item)

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        rig = bpy.data.objects.get(VIEW["rig"])
        if rig is None:
            return
        clash = bool(item.control) and item.control in VIEW["shared"]
        cells = layout.split(factor=0.5)
        label = cells.row()
        label.alert = clash
        label.label(text=mmd_label(rig, item.mmd))
        field = cells.row()
        field.alert = clash
        field.prop_search(item, "control", rig.pose, "bones", text="", icon="BONE_DATA")


# Edits which Rigify control follows each MMD bone, in a scrollable flat list. The MMD
# bone is fixed and the control is what you pick; a search filters from both ends, on the
# bone's name and the control's. A control should follow one MMD bone, so a control two
# rows share turns both red; an MMD bone may have several controls. Clearing a field
# switches that mapping off; the arrow puts a row back to the built-in table. Only Missing
# lists the bones the PMM moves that no control follows, to pick one for each. Cancel puts
# every mapping back as it was when the dialog opened.
class NINESYNC_OT_edit_rigify_map(Operator):
    bl_idname = "ninesync.edit_rigify_map"
    bl_label = "Edit Mappings"
    bl_description = "Choose which Rigify control follows each MMD bone. Search by either name"
    bl_options = {"INTERNAL"}

    index: IntProperty(options={"HIDDEN"})
    active: IntProperty(options={"HIDDEN"})
    search: StringProperty(
        name="Search",
        description="Words matched against the MMD bone and the Rigify control",
        update=search_changed,
        options={"TEXTEDIT_UPDATE"},
    )
    only_missing: BoolProperty(
        name="Only Missing",
        description="List only the bones the PMM moves that no Rigify control follows, to choose one for each",
        update=only_missing_changed,
    )
    backup: StringProperty(options={"HIDDEN"})

    def invoke(self, context, event):
        row, armature = target_and_rig(context, self.index)
        if armature is None:
            return {"CANCELLED"}
        sync_slots(row, armature)
        self.backup = json.dumps({slot_key(s): s.control for s in row.rigify_slots})
        wm = context.window_manager
        wm.ninesync_rigify_target = self.index
        wm.ninesync_rigify_controls.clear()
        for name in candidate_controls(armature):
            wm.ninesync_rigify_controls.add().name = name
        VIEW["rig"] = armature.name
        VIEW["shown"] = matching_slots(armature, row, "")
        VIEW["free"] = []
        VIEW["found"] = pmm_found(context, row, armature)
        # The PMM bones with no control get an empty row of their own, listed first.
        have = {s.bone for s in row.rigify_slots if not s.base}
        for bone in missing_mmd_bones(armature, row, VIEW["found"]):
            if bone not in have:
                row.rigify_slots.add().bone = bone
        self.only_missing = False
        return wm.invoke_props_dialog(self, width=400, confirm_text="Done")

    def execute(self, context):
        return {"FINISHED"}

    # The edits are made on the target as they are typed, so Cancel puts the mappings back
    # as they were when the dialog opened; a slot added since is switched off.
    def cancel(self, context):
        row, _armature = target_and_rig(context, self.index)
        if row is None or not self.backup:
            return
        before = json.loads(self.backup)
        for slot in row.rigify_slots:
            slot.control = before.get(slot_key(slot), "")

    def draw(self, context):
        layout = self.layout
        row, armature = target_and_rig(context, self.index)
        if armature is None:
            layout.label(text="No Rigify rig on this target")
            return
        top = layout.row(align=True)
        top.prop(self, "search", icon="VIEWZOOM", text="")
        top.prop(self, "only_missing", text="Only Missing", toggle=True)
        head = layout.split(factor=0.5)
        head.label(text="MMD bone:")
        head.label(text="Rigify control:")
        if self.only_missing:
            layout.template_list(
                "NINESYNC_UL_rigify_free", "", context.window_manager, "ninesync_rigify_free", self, "active", rows=18, maxrows=18
            )
        else:
            layout.template_list(
                "NINESYNC_UL_rigify_map", "", row, "rigify_slots", self, "active", rows=18, maxrows=18
            )
        footer = layout.row()
        if not self.only_missing:
            footer.operator("ninesync.add_rigify_map", text="Add Mapping", icon="ADD").index = self.index
        everything = footer.operator("ninesync.reset_rigify_map", text="Reset All To Defaults", icon="LOOP_BACK")
        everything.index, everything.key = self.index, ""


# Adds a mapping of the user's own to a target: a row to pick the MMD bone and its control.
# One empty row at a time.
class NINESYNC_OT_add_rigify_map(Operator):
    bl_idname = "ninesync.add_rigify_map"
    bl_label = "Add Mapping"
    bl_description = "Add a mapping: choose any bone of the rig and the Rigify control that follows it"
    bl_options = {"INTERNAL"}

    index: IntProperty(options={"HIDDEN"})

    def execute(self, context):
        row, _armature = target_and_rig(context, self.index)
        if row is None:
            return {"CANCELLED"}
        if not any(not s.base and not s.bone for s in row.rigify_slots):
            row.rigify_slots.add()
        return {"FINISHED"}


# Deletes a mapping the user added; the table's own mappings are cleared or reset instead.
class NINESYNC_OT_remove_rigify_map(Operator):
    bl_idname = "ninesync.remove_rigify_map"
    bl_label = "Remove Mapping"
    bl_description = "Remove this mapping you added"
    bl_options = {"INTERNAL"}

    slot: IntProperty(options={"HIDDEN"})

    def execute(self, context):
        row, _armature = target_and_rig(context, context.window_manager.ninesync_rigify_target)
        if row is None or not 0 <= self.slot < len(row.rigify_slots) or row.rigify_slots[self.slot].base:
            return {"CANCELLED"}
        row.rigify_slots.remove(self.slot)
        return {"FINISHED"}


# Puts one slot's control (or, with no key, every slot's) of a target back to the built-in
# table; a slot the user added goes back to none.
class NINESYNC_OT_reset_rigify_map(Operator):
    bl_idname = "ninesync.reset_rigify_map"
    bl_label = "Reset Mapping"
    bl_description = "Put the mapping back to the built-in table"
    bl_options = {"INTERNAL"}

    index: IntProperty(options={"HIDDEN"})
    key: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        row, _armature = target_and_rig(context, self.index)
        if row is None:
            return {"CANCELLED"}
        for slot in row.rigify_slots:
            if not self.key or slot_key(slot) == self.key:
                slot.control = slot.base
        return {"FINISHED"}


class NINESYNC_PT_remap(Panel):
    bl_label = "Rigify Remap"
    bl_idname = "NINESYNC_PT_remap"
    bl_order = 1
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = NINESYNC_CATEGORY
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "mmd_bridge", None)
        return settings is not None and bool(rigify_rows(settings))

    def draw(self, context):
        layout = self.layout
        settings = context.scene.mmd_bridge
        for row in rigify_rows(settings):
            box = layout.box()
            box.label(text=row.pmm_name, icon="ARMATURE_DATA")
            box.prop(row, "rigify_remap")
            col = box.column()
            col.enabled = row.rigify_remap
            index = list(settings.targets).index(row)
            buttons = col.row(align=True)
            buttons.operator("ninesync.show_rigify_map", icon="OUTLINER").index = index
            buttons.operator("ninesync.edit_rigify_map", icon="GREASEPENCIL").index = index
