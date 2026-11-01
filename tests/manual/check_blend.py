import addon_utils
import bpy
import os
import sys
import traceback


# Saved-file check: blender --background file.blend --python tests/manual/check_blend.py
# Opens a real .blend made by any earlier build, reads the stored settings and re-syncs
# in memory. Nothing is saved.
for name in sorted(m.__name__ for m in addon_utils.modules()):
    if name.endswith((".mmd_tools", ".ninesync", ".ninesync_fx")):
        addon_utils.enable(name, default_set=False)

exit_code = 0
scene = bpy.context.scene
try:
    settings = scene.mmd_bridge
    collections = sorted(c.name for c in bpy.data.collections if c.name.startswith("MMD_"))
    accessories = [o.name for o in bpy.data.objects if o.get("mmd_bridge_accessory")]
    effects = [o.name for o in bpy.data.objects if any(k.startswith("mme_") for k in o.keys())]
    actions = [a.name for a in bpy.data.actions if a.name.startswith("MMDBridge_")]
    print("BLEND", os.path.basename(bpy.data.filepath), "Blender", ".".join(map(str, bpy.data.version)))
    print("BLEND filepath", repr(settings.filepath), "targets", [(t.pmm_name, t.target.name if t.target else None) for t in settings.targets])
    print("BLEND collections", collections, "| accessories", len(accessories), "| mme objects", len(effects), "| bridge actions", len(actions))
    before = set(bpy.data.objects.keys())
    if settings.filepath and os.path.exists(bpy.path.abspath(settings.filepath)):
        result = bpy.ops.ninesync.sync_now()
        lost = sorted(n for n in before if n not in bpy.data.objects and not n.startswith("MMD_"))
        kept = sum(1 for n in accessories if n in bpy.data.objects)
        print("BLEND sync", result, repr(settings.status), "| objects lost", lost[:8], "| accessories kept", kept, "of", len(accessories))
        if "FINISHED" not in result or lost or kept != len(accessories):
            exit_code = 1
    else:
        print("BLEND sync skipped: stored PMM path is not on disk")
except Exception:
    traceback.print_exc()
    exit_code = 1
sys.stdout.flush()
os._exit(exit_code)
