import bpy
import logging
import os
import re
import sys

from .constants import LOG_TEXT_MAX_LINES, LOG_TEXT_NAME


log = logging.getLogger("NineSync")

REDACT_TOKEN = "~"
_REDACT_CACHE = {}


# The folders whose prefix is replaced by "~" in the saved log: the home folder, plus Blender's
# user config and extension folders when they live under another profile root. A root with fewer
# than two path parts (a drive or "/") is never used, so nothing is redacted by guesswork.
def _redact_roots():
    candidates = [os.path.expanduser("~")]
    for getter in (lambda: bpy.utils.resource_path("USER"), lambda: bpy.utils.extension_path_user(__package__.split(".")[0])):
        try:
            candidates.append(getter())
        except Exception:
            pass
    kept = []
    for raw in candidates:
        if not raw or raw == "~":
            continue
        parts = tuple(p for p in re.split(r"[\\/]+", raw) if p)
        if len(parts) < 2:
            continue
        key = [x.lower() for x in parts] if os.name == "nt" else list(parts)
        if any(key[: len(r)] == r for r, _ in kept):
            continue
        kept.append((key, parts))
    roots = [parts for _, parts in kept]
    return tuple(roots)


# Replaces the leading roots in text, matching backslash, slash and doubled backslash alike and
# ignoring case on Windows, at a path boundary only. Split from redact_home so tests can pass their own roots.
def redact_roots(text, roots):
    if not text or not roots:
        return text
    pattern = _REDACT_CACHE.get(roots)
    if pattern is None:
        sep = r"[\\/]+"
        alts = [sep.join(re.escape(part) for part in parts) for parts in roots]
        flags = re.IGNORECASE if os.name == "nt" else 0
        pattern = re.compile(r"(?<!\w)(?:" + "|".join(alts) + r")(?![\w.\-])", flags)
        _REDACT_CACHE[roots] = pattern
    return pattern.sub(REDACT_TOKEN, text)


# Keeps the account name out of the text saved in the .blend; the rest of the path is left as
# it was. Fails open: if the folders cannot be found the message is written unchanged.
def redact_home(text):
    try:
        return redact_roots(text, _redact_roots())
    except Exception:
        return text


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
            text.write(redact_home(self.format(record)) + "\n")
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
