from ..common.constants import UNIT_SCALE
from .targets import target_usable


# Import scale for motion. With Auto Scale it follows the first usable model's root
# empty (display size * 0.2), so motion matches how that model was imported.
def current_scale(settings):
    if settings.auto_scale:
        for item in settings.targets:
            if item.enabled and target_usable(item.target):
                size = getattr(item.target, "empty_display_size", 0.0)
                if size > 0.0:
                    return size * 0.2
    return settings.scale


# The scale to import one model's motion at. mmd_tools writes bone locations in the
# armature's own units, so a rig whose object is scaled up (a Rigify conversion made in
# metres and enlarged to MMD units, say) needs the import scale divided by that
# scale, or every bone location comes out that many times too large. An unscaled
# armature gets the plain scale.
def model_scale(settings, armature):
    scale = current_scale(settings)
    if armature is None:
        return scale
    factor = sum(abs(v) for v in armature.matrix_world.to_scale()) / 3.0
    if factor < 1e-6 or abs(factor - 1.0) < 1e-4:
        return scale
    return scale / factor


# The same rule read from the Create MMD Project checklist: an export works without a
# Syncer, whose target list may be empty, and falling back to the 0.08 default there
# scales everything by 12.5.
def export_scale(settings):
    if settings.auto_scale:
        for item in settings.export_items:
            if item.enabled and target_usable(item.target):
                size = getattr(item.target, "empty_display_size", 0.0)
                if size > 0.0:
                    return size * 0.2
    return settings.scale


# Camera interpolation only matches MMD at scale 1.0. True when camera sync is on,
# the scale differs, and the user has not dismissed the warning for this scale.
def scale_warning_active(settings):
    if not settings.sync_camera:
        return False
    scale = current_scale(settings)
    if abs(scale - UNIT_SCALE) < 1e-6:
        return False
    return abs(scale - settings.scale_warning_dismissed_for) > 1e-9
