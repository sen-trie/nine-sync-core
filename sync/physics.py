import bpy
import os

from ..common.constants import ACTION_PREFIX, CLIP_TRACK_NAME
from ..common.logger import log
from .clips import find_bridge_clip, place_clip, release_stash
from .mmd_tools_link import run_importer
from .scale import current_scale


# Experimental. Layers a baked physics VMD above the PMM clip on its own NLA track,
# and can switch off the rigid body world so it does not fight the baked result.
def import_physics_clip(item, armature, settings, label):
    path = bpy.path.abspath(item.physics_vmd) if item.physics_vmd else ""
    if not path:
        return False
    if not os.path.isfile(path):
        log.warning("physics VMD not found: %s", path)
        return False
    anim = armature.animation_data or armature.animation_data_create()
    active_before = anim.action
    stash = {
        armature.as_pointer(): (
            armature,
            active_before,
            active_before.use_fake_user if active_before else False,
        )
    }
    if active_before is not None:
        active_before.use_fake_user = True
    track = CLIP_TRACK_NAME + "_" + label + "_physics"
    existing = find_bridge_clip(armature, track)
    placed = False
    try:
        anim.action = None
        run_importer(
            path,
            current_scale(settings),
            [armature],
            ACTION_PREFIX + label + "_physics",
            False,
        )
        place_clip(
            armature,
            existing,
            track,
            ACTION_PREFIX + label + "_physics_clip",
            active_before,
        )
        placed = True
    finally:
        release_stash(stash, restore=not placed)
    if settings.physics_disable_rigid_body:
        world = bpy.context.scene.rigidbody_world
        if world is not None and world.enabled:
            world.enabled = False
            log.info(
                "rigid body world disabled so it does not fight the baked physics clip"
            )
    log.info("baked physics VMD applied as a clip above the PMM clip: %s", path)
    return True
