import bpy
import textwrap
from bpy.app.translations import pgettext_iface as iface_

from .. import hooks
from ..common.constants import FX_URL, WRAP_CHAR, WRAP_MARGIN
from ..sync.mmd_tools_link import missing_dependencies


_status_icons = {}


# Wraps text to the sidebar width, since labels do not wrap on their own.
def draw_wrapped(layout, text, width=None):
    if width is None:
        width = 38
        region = getattr(bpy.context, "region", None)
        if region is not None and region.width > 1:
            scale = bpy.context.preferences.system.ui_scale or 1.0
            width = max(16, int((region.width / scale - WRAP_MARGIN) / WRAP_CHAR))
    column = layout.column(align=True)
    column.scale_y = 0.85
    for line in textwrap.wrap(text, width):
        column.label(text=line)


# One labelled field: icon and label in a fixed left column, the field filling the rest.
# A label passed as the property's own text shares the row with the field and gets
# squeezed or cut, so labels of neighbouring rows would start and end at different
# places.
def draw_field(layout, data, prop, text, icon="NONE", factor=0.45):
    split = layout.split(factor=factor, align=True)
    split.label(text=text, icon=icon)
    split.prop(data, prop, text="")
    return split


def draw_dependency_notices(layout):
    for label, message, url in missing_dependencies():
        box = layout.box()
        box.alert = True
        box.label(text=message, icon="ERROR")
        box.operator("wm.url_open", text=iface_("Get %s") % label, icon="URL").url = url


# Shown when the PMM has accessories but FX is not installed.
def draw_fx_notice(layout, settings):
    if settings.pmm_accessory_count <= 0 or hooks.fx_installed():
        return
    box = layout.box()
    box.label(
        text=iface_("This PMM has %d accessory(ies)") % settings.pmm_accessory_count,
        icon="INFO",
    )
    draw_wrapped(box, iface_("Install NineSync: FX to import them and their MME effects."))
    if FX_URL:
        box.operator("wm.url_open", text="Get NineSync: FX", icon="URL").url = FX_URL


def status_icon(state):
    if not _status_icons:
        available = set()
        try:
            available = set(
                bpy.types.UILayout.bl_rna.functions["label"]
                .parameters["icon"]
                .enum_items.keys()
            )
        except Exception:
            pass

        def pick(*names):
            for name in names:
                if name in available:
                    return name
            return names[-1]

        _status_icons.update(
            {
                "ERROR": pick("STRIP_COLOR_01", "SEQUENCE_COLOR_01", "ERROR"),
                "WARN": pick("STRIP_COLOR_03", "SEQUENCE_COLOR_03", "ERROR"),
                "OK": pick("STRIP_COLOR_04", "SEQUENCE_COLOR_04", "CHECKMARK"),
                "NEW": pick("STRIP_COLOR_05", "SEQUENCE_COLOR_05", "DOT"),
                "OFF": pick("STRIP_COLOR_09", "SEQUENCE_COLOR_09", "RADIOBUT_OFF"),
            }
        )
    return _status_icons.get(state, "BLANK1")
