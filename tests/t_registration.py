import importlib
import itertools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import Checks, fx_folder, load, stage

import bpy


FX_MODULE = "ninesync_fx"
modules = ["ninesync"] + ([FX_MODULE] if fx_folder() else [])
stage(*modules)
loaded = {name: load(name) for name in modules}
hooks = importlib.import_module("ninesync.hooks")
CORE_PANELS = [
    c.bl_idname for c in importlib.import_module("ninesync.ui").CLASSES if issubclass(c, bpy.types.Panel)
]
checks = Checks()


def clean_state(label):
    reg = hooks.registry()
    left = {k: list(reg[k]) for k in hooks.KINDS if reg[k]}
    pointers = hasattr(bpy.types.Scene, "mmd_bridge") or hasattr(bpy.types.Scene, "ninesync_fx")
    handlers = [h for h in bpy.app.handlers.load_post if "ninesync" in getattr(h, "__name__", "")]
    checks.check("%s: no hooks left" % label, not left, left or "")
    checks.check("%s: core cleared" % label, reg["core"] is None)
    checks.check("%s: scene pointers gone" % label, not pointers)
    checks.check("%s: load handlers gone" % label, not handlers)
    checks.check("%s: panels gone" % label, not any(hasattr(bpy.types, p) for p in CORE_PANELS))


def state_while_on(on, label):
    core_on = "ninesync" in on
    fx_on = FX_MODULE in on
    scene_core = hasattr(bpy.types.Scene, "mmd_bridge")
    scene_fx = hasattr(bpy.types.Scene, "ninesync_fx")
    checks.check("%s: core state" % label, hooks.core_installed() == core_on and scene_core == core_on)
    checks.check("%s: fx state" % label, hooks.fx_installed() == fx_on and scene_fx == fx_on)
    checks.check("%s: core panels" % label, all(hasattr(bpy.types, p) == core_on for p in CORE_PANELS))
    checks.check("%s: no warning panel" % label, not hasattr(bpy.types, "NINESYNC_PT_legacy"))
    if core_on:
        relink = importlib.import_module("ninesync.ui.relink")
        checks.check("%s: settings not stale" % label, not relink.settings_outdated(bpy.context.scene))


clean_state("start")
for size in range(1, len(modules) + 1):
    for up in itertools.permutations(modules, size):
        for down in itertools.permutations(up):
            label = "up %s down %s" % ("+".join(up), "+".join(down))
            for name in up:
                loaded[name].register()
            state_while_on(up, label)
            for name in down:
                loaded[name].unregister()
            clean_state(label)

for _ in range(3):
    for name in modules:
        loaded[name].register()
    loaded["ninesync"].reload_in_place()
    for name in reversed(modules):
        loaded[name].unregister()
clean_state("after cycles")
print("tested", modules, "on Blender", bpy.app.version_string)
checks.finish()
