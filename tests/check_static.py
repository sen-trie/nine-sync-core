import ast
import os
import re
import sys
import tomllib

from support import REPO, Checks, fx_folder


TOP_LEVEL = ("hooks", "pmm", "pmm_writer", "x_writer")
FORBIDDEN_IMPORTS = ("ninesync_fx", "xfile", "fx", "mme")
STALE_PRODUCT = re.compile(r"combined (NineSync|add-?on)|monolith|MMD PMM Bridge", re.IGNORECASE)
# Layer of each module group, then the layers it may import from.
ALLOWED = {
    "common": {"common"},
    "top": {"common", "top"},
    "sync": {"common", "top", "sync"},
    "ui": {"common", "top", "sync", "ui"},
    "root": {"common", "top", "sync", "ui"},
}


def sources():
    skipped = ("__pycache__", ".git", ".github", "debug-src", "docs", "tests", "src")
    for folder, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in skipped]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(folder, name)


def layer_of(parts):
    if parts[0] in ("common", "sync", "ui"):
        return parts[0]
    if parts[0] in TOP_LEVEL:
        return "top"
    return "root"


# Resolves a relative import to a path of parts from the repo root.
def resolve(parts, node):
    base = list(parts[:-1])
    for _ in range(node.level - 1):
        base.pop()
    return base + (node.module.split(".") if node.module else [])


def check_imports(checks):
    bad_layer = []
    bad_module = []
    for path in sources():
        rel = os.path.relpath(path, REPO).replace(os.sep, "/")
        parts = rel[:-3].split("/")
        layer = layer_of(parts)
        for node in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module]
            elif isinstance(node, ast.ImportFrom):
                target = resolve(parts, node)
                modules = [target] if target else [[a.name] for a in node.names]
                for module in modules:
                    if layer_of(module) not in ALLOWED[layer]:
                        bad_layer.append("%s -> %s" % (rel, ".".join(module)))
                continue
            else:
                continue
            bad_module += ["%s -> %s" % (rel, n) for n in names if n.split(".")[0] in FORBIDDEN_IMPORTS]
    checks.check("import layers run one way", not bad_layer, bad_layer or "")
    checks.check("no FX or effect imports", not bad_module, bad_module or "")


def check_versions(checks):
    with open(os.path.join(REPO, "blender_manifest.toml"), "rb") as handle:
        manifest = tomllib.load(handle)
    source = open(os.path.join(REPO, "__init__.py"), encoding="utf-8").read()
    found = re.search(r"ADDON_VERSION = \((\d+), (\d+), (\d+)\)", source)
    version = ".".join(found.groups())
    changelog = open(os.path.join(REPO, "CHANGELOG.md"), encoding="utf-8").read()
    checks.check("manifest id is ninesync", manifest["id"] == "ninesync")
    checks.check("manifest matches bl_info", manifest["version"] == version, version)
    checks.check("changelog lists the version", "## %s" % version in changelog)


def check_copy(checks):
    stale = []
    docs = ("README.md", "CLAUDE.md", os.path.join("docs", "HANDOFF.md"))
    # CLAUDE.md and docs/ are local to the owner's checkout, so CI does not have them.
    present = [p for p in (os.path.join(REPO, n) for n in docs) if os.path.exists(p)]
    for path in list(sources()) + present:
        text = open(path, encoding="utf-8").read()
        stale += ["%s: %s" % (os.path.relpath(path, REPO), m.group()) for m in STALE_PRODUCT.finditer(text)]
    checks.check("no references to the pre-split add-on in current copy", not stale, stale or "")


# FX is optional locally; --require-fx (used by CI) turns its absence into a failure.
def check_hooks(checks):
    folder = fx_folder()
    if folder is None:
        checks.check("FX checkout available", "--require-fx" not in sys.argv, "skipped hooks.py comparison")
        return
    own = open(os.path.join(REPO, "hooks.py"), "rb").read()
    other = open(os.path.join(folder, "hooks.py"), "rb").read()
    checks.check("hooks.py byte-identical in both repos", own == other)
    checks.check("FX has no pmm.py", not os.path.exists(os.path.join(folder, "pmm.py")))
    with open(os.path.join(folder, "blender_manifest.toml"), "rb") as handle:
        checks.check("FX module id", tomllib.load(handle)["id"] == "ninesync_fx")


if __name__ == "__main__":
    checks = Checks()
    check_imports(checks)
    check_versions(checks)
    check_copy(checks)
    check_hooks(checks)
    checks.finish()
