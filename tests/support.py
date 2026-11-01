import importlib
import os
import shutil
import sys
import tempfile


HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SKIP = shutil.ignore_patterns(
    ".git", ".github", "__pycache__", "*.pyc", "*.blend1", "debug-src", "docs", "tests", "tools"
)


# The FX checkout, from NINESYNC_FX or a sibling folder; None when it is not here.
def fx_folder():
    explicit = os.environ.get("NINESYNC_FX")
    if explicit:
        path = os.path.abspath(explicit)
        return path if os.path.isfile(os.path.join(path, "__init__.py")) else None
    parent = os.path.dirname(REPO)
    for name in ("nine-sync-fx", "ninesync-fx"):
        path = os.path.join(parent, name)
        if os.path.isfile(os.path.join(path, "__init__.py")):
            return path
    return None


# Packages are copied, not symlinked: a repo folder is not named like its module,
# and symlinks need elevated rights on Windows.
def stage(*modules):
    root = tempfile.mkdtemp(prefix="ninesync_pkgs_")
    sources = {"ninesync": REPO, "ninesync_fx": fx_folder()}
    for module in modules:
        source = sources[module]
        if source is None:
            raise SystemExit("set NINESYNC_FX to the FX checkout")
        shutil.copytree(source, os.path.join(root, module), ignore=SKIP)
    sys.path.insert(0, root)
    return root


# Imports a staged add-on. Wheels older than 5.3 need the floor lowered on the package
# module, because __init__.py binds MIN_BLENDER by name.
def load(module):
    import bpy

    package = importlib.import_module(module)
    if bpy.app.version < package.MIN_BLENDER:
        package.MIN_BLENDER = tuple(bpy.app.version)
    return package


class Checks:
    def __init__(self):
        self.failed = []

    def check(self, label, ok, detail=""):
        suffix = " | %s" % (detail,) if detail != "" else ""
        print("%s %s%s" % ("ok  " if ok else "FAIL", label, suffix))
        if not ok:
            self.failed.append(label)

    def finish(self):
        print("%d failed" % len(self.failed))
        sys.stdout.flush()
        # sys.exit does not set Blender's own exit code under --python, so leave hard.
        os._exit(1 if self.failed else 0)
