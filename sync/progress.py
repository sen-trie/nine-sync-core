import bpy


_state = {"active": False, "fraction": 0.0, "text": ""}


# Read by the main panel to draw its bar. The stage text is plain progress, not timing.
def progress_state():
    return _state


def reset_progress():
    _state.update(active=False, fraction=0.0, text="")


# Blender's cursor progress, the status bar text and the panel bar, driven as one. The
# fraction only ever rises, and end() is safe to call twice from a finally block.
class SyncProgress:
    def __init__(self):
        self.manager = None
        self.workspace = None

    def begin(self):
        self.manager = bpy.context.window_manager
        self.workspace = bpy.context.workspace
        _state.update(active=True, fraction=0.0, text="")
        if self.manager is not None:
            self.manager.progress_begin(0, 1000)

    def update(self, fraction, text):
        _state["fraction"] = max(_state["fraction"], min(fraction, 1.0))
        _state["text"] = text
        if self.manager is not None:
            self.manager.progress_update(int(_state["fraction"] * 1000))
        if self.workspace is not None:
            self.workspace.status_text_set("NineSync: %s (Esc to cancel)" % text)

    def end(self):
        manager, workspace = self.manager, self.workspace
        self.manager = self.workspace = None
        reset_progress()
        # After a file load these can be freed; there is then nothing left to release.
        try:
            if manager is not None:
                manager.progress_end()
            if workspace is not None:
                workspace.status_text_set(None)
        except ReferenceError:
            pass
