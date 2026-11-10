import bpy
import logging
import sys

from .constants import LOG_TEXT_MAX_LINES, LOG_TEXT_NAME


log = logging.getLogger("NineSync")


# Writes the log into the NineSyncLog text. Past the line limit the oldest lines go, not
# the whole text, so a long sync keeps its start until Support Report reads it. Trimmed
# to two thirds of the limit at a time, so the rewrite does not run on every line.
class TextBlockHandler(logging.Handler):
    def emit(self, record):
        try:
            text = bpy.data.texts.get(LOG_TEXT_NAME) or bpy.data.texts.new(
                LOG_TEXT_NAME
            )
            if len(text.lines) > LOG_TEXT_MAX_LINES:
                kept = text.as_string().splitlines()[-(LOG_TEXT_MAX_LINES * 2 // 3):]
                text.from_string("\n".join(kept) + "\n")
            text.write(self.format(record) + "\n")
        except Exception:
            pass


# Rebuilds the handlers on every register so reloading the add-on does not duplicate
# each log line.
def setup_logging():
    for handler in list(log.handlers):
        log.removeHandler(handler)
    formatter = logging.Formatter(
        "[NineSync %(asctime)s %(levelname)s] %(message)s", "%H:%M:%S"
    )
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    block = TextBlockHandler()
    block.setFormatter(formatter)
    log.addHandler(stream)
    log.addHandler(block)
    log.setLevel(logging.DEBUG)
    log.propagate = False
