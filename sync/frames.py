import contextlib

from .. import hooks


INTERNAL_FRAMES_KEY = "internal_frames"


# True while Core steps frames for its own solves, bakes and exports. FX reads the
# same shared key to skip work that only matters for frames the user sees.
def internal_frames_active():
    return int(hooks.registry()["shared"].get(INTERNAL_FRAMES_KEY, 0) or 0) > 0


# Scoped marker for Core's private frame_set loops. A depth counter lets loops nest, and
# the finally releases it on exceptions and cancel. The registry is looked up again on exit
# because a file load replaces it while an operator is still running.
@contextlib.contextmanager
def internal_frames():
    shared = hooks.registry()["shared"]
    shared[INTERNAL_FRAMES_KEY] = int(shared.get(INTERNAL_FRAMES_KEY, 0) or 0) + 1
    try:
        yield
    finally:
        shared = hooks.registry()["shared"]
        depth = int(shared.get(INTERNAL_FRAMES_KEY, 0) or 0) - 1
        if depth > 0:
            shared[INTERNAL_FRAMES_KEY] = depth
        else:
            shared.pop(INTERNAL_FRAMES_KEY, None)


# Drops the marker outright; used when Core unregisters or the registry is rebuilt.
def clear_internal_frames():
    hooks.registry()["shared"].pop(INTERNAL_FRAMES_KEY, None)
