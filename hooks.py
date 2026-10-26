import bpy

from .common import log


API_VERSION = 1

KEY = "ninesync_hooks"

KINDS = ("post_sync", "clean", "report")

FX_OWNER = "ninesync_fx"


def registry():
    reg = bpy.app.driver_namespace.get(KEY)
    if reg is None:
        reg = {"api_version": API_VERSION}
        bpy.app.driver_namespace[KEY] = reg
    reg.setdefault("core", None)
    reg.setdefault("shared", {})
    for kind in KINDS:
        reg.setdefault(kind, {})
    return reg


def compatible():
    return registry().get("api_version") == API_VERSION


def add(kind, owner, fn):
    registry()[kind][owner] = fn


def remove(owner):
    reg = registry()
    for kind in KINDS:
        reg[kind].pop(owner, None)


def register_core(version):
    registry()["core"] = {"version": tuple(version)}


def unregister_core():
    registry()["core"] = None


def core_installed():
    return registry().get("core") is not None


def fx_installed():
    return FX_OWNER in registry()["post_sync"]


def run(kind, *args):
    if not compatible():
        log.warning(
            "hook registry is API %s but this add-on speaks %s; skipping %s hooks",
            registry().get("api_version"),
            API_VERSION,
            kind,
        )
        return []
    results = []
    for owner, fn in list(registry()[kind].items()):
        try:
            results.append(fn(*args))
        except Exception:
            import traceback

            log.error("hook %s/%s failed:\n%s", kind, owner, traceback.format_exc())
    return results
