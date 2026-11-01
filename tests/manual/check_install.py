import glob
import os
import shutil
import subprocess
import sys
import tempfile


# Runs with plain Python: python tests/manual/check_install.py <blender.exe> [core.zip] [fx.zip]
# Each Blender start is a separate process, so "restart" is real. The profile is a
# throwaway folder; the user's Blender config is not touched.
BLENDER = sys.argv[1]
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROFILE = tempfile.mkdtemp(prefix="ninesync_profile_")
ENV = dict(os.environ, BLENDER_USER_RESOURCES=PROFILE)
PROBE = os.path.join(PROFILE, "probe.py")
failed = []
open(PROBE, "w").write(
    """
import addon_utils, bpy
names = sorted(m.__name__ for m in addon_utils.modules() if "ninesync" in m.__name__)
on = sorted(a for a in bpy.context.preferences.addons.keys() if "ninesync" in a)
reg = bpy.app.driver_namespace.get("ninesync_hooks", {})
core = next((a for a in on if a.endswith(".ninesync")), None)
prefs = bpy.context.preferences.addons[core].preferences if core else None
print("PROBE", names, on, reg.get("core"), sorted(reg.get("post_sync", {})), type(prefs).__name__, hasattr(prefs, "use_import_scale"), hasattr(bpy.types.Scene, "mmd_bridge"), hasattr(bpy.types.Scene, "ninesync_fx"))
"""
)


def blender(*args):
    cmd = [BLENDER, "--background", *args]
    done = subprocess.run(cmd, env=ENV, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return done.returncode, done.stdout + done.stderr


def command(*args):
    code, out = blender("--command", "extension", *args)
    print("$ extension", " ".join(os.path.basename(a) for a in args), "->", code)
    if code != 0:
        print(out)
        failed.append(args[0])


def probe():
    _, out = blender("--python", PROBE)
    bad = [line for line in out.splitlines() if "Traceback" in line or "Error" in line]
    line = next((x for x in out.splitlines() if x.startswith("PROBE")), "no probe output\n" + out)
    print(line)
    if bad:
        print("errors:", bad)
        failed.append("probe")
    return line


def expect(label, ok):
    print("%s %s" % ("ok  " if ok else "FAIL", label))
    if not ok:
        failed.append(label)


def build(source, out):
    code, text = blender("--command", "extension", "build", "--source-dir", source, "--output-dir", out)
    if code != 0:
        print(text)
        raise SystemExit("build failed for %s" % source)
    return glob.glob(os.path.join(out, "*.zip"))[0]


work = tempfile.mkdtemp(prefix="ninesync_zip_")
os.makedirs(os.path.join(work, "core"))
core_zip = sys.argv[2] if len(sys.argv) > 2 else build(REPO, os.path.join(work, "core"))
fx_zip = sys.argv[3] if len(sys.argv) > 3 else None
for path in filter(None, (core_zip, fx_zip)):
    if not os.path.isfile(path):
        raise SystemExit("no such zip: %s" % path)
print("core zip:", core_zip)

command("install-file", "-r", "user_default", "--enable", core_zip)
line = probe()
expect("core enabled under bl_ext", "bl_ext.user_default.ninesync" in line)
expect("preferences resolve", "NineSyncPreferences True" in line)
expect("core alone: FX pointer absent", line.rstrip().endswith("True False"))
line = probe()
expect("still enabled after restart", "bl_ext.user_default.ninesync'" in line.split("]")[1])
if fx_zip:
    command("install-file", "-r", "user_default", "--enable", fx_zip)
    line = probe()
    expect("both enabled", line.rstrip().endswith("True True") and "ninesync_fx" in line)
    command("remove", "ninesync_fx")
    line = probe()
    expect("core survives FX removal", line.rstrip().endswith("True False"))
command("remove", "ninesync")
line = probe()
expect("uninstalled", "ninesync" not in line.replace("PROBE", ""))
left = os.path.join(PROFILE, "extensions", "user_default", "ninesync")
expect("no folder left", not os.path.exists(left))
shutil.rmtree(PROFILE, ignore_errors=True)
shutil.rmtree(work, ignore_errors=True)
print("failed:", failed)
sys.exit(1 if failed else 0)
