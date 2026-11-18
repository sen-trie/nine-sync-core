import importlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import Checks, load, stage

import bpy


stage("ninesync")
core = load("ninesync")
core.register()
logger = importlib.import_module("ninesync.common.logger")
constants = importlib.import_module("ninesync.common.constants")
checks = Checks()
BS = "\\"

win = ("C:", "Users", "ab")
unix = ("", "home", "ab")
roots = (("C:", "Users", "ab"),)
nt = os.name == "nt"
redact = logger.redact_roots

checks.check("backslash path", redact("C:" + BS + "Users" + BS + "ab" + BS + "x.pmm", roots) == "~" + BS + "x.pmm")
checks.check("forward slash path", redact("C:/Users/ab/x.pmm", roots) == "~/x.pmm")
double = BS * 2
checks.check(
    "doubled backslash (repr output)",
    redact("C:" + double + "Users" + double + "ab" + double + "x", roots) == "~" + double + "x",
)
checks.check("mixed separators", redact("C:/Users" + BS + "ab/x", roots) == "~/x")
other = "C:" + BS + "Users" + BS + "abc" + BS + "x"
checks.check("same leading characters, other folder", redact(other, roots) == other)
checks.check("not inside a longer word", redact("XC:/Users/ab/x", roots) == "XC:/Users/ab/x")
checks.check("quoted and at end", redact('open "C:/Users/ab"', roots) == 'open "~"')
checks.check("several paths in one message", redact("a C:/Users/ab/p b C:/Users/ab/q", roots) == "a ~/p b ~/q")
checks.check("no home path unchanged", redact("frame 12 done", roots) == "frame 12 done")
checks.check("no roots unchanged", redact("C:/Users/ab/x", ()) == "C:/Users/ab/x")
checks.check("empty text", redact("", roots) == "")
if nt:
    checks.check("case-insensitive on Windows", redact("c:/users/AB/x", roots) == "~/x")

real_home = os.path.expanduser("~")
text = "sync start: %s" % os.path.join(real_home, "x.pmm")
if len([p for p in real_home.replace(BS, "/").split("/") if p]) >= 2:
    out = logger.redact_home(text)
    checks.check("redact_home on the real home", out.startswith("sync start: ~") and real_home not in out, out)

saved_expand = os.path.expanduser
os.path.expanduser = lambda p: ""
try:
    unknown = logger.redact_home("C:/Users/ab/x")
finally:
    os.path.expanduser = saved_expand
checks.check("unknown home does not raise or guess", "~" not in unknown.replace("C:/Users/ab/x", ""))

# Real write into the NineSyncLog text, then save and reopen.
logger.setup_logging()
name = os.path.basename(real_home.rstrip("/" + BS))
logger.log.info("sync start: %s", os.path.join(real_home, "x.pmm"))
logger.log.info("forward: %s", real_home.replace(BS, "/") + "/y.pmm")
logger.log.info("repr: %r", os.path.join(real_home, "z.pmm"))
body = bpy.data.texts[constants.LOG_TEXT_NAME].as_string()
checks.check("log text has the token", "sync start: ~" in body, body[-300:])
checks.check("log text has no account name", name not in body.replace("ninesync", ""), name)
folder = tempfile.mkdtemp(prefix="ninesync_log_")
path = os.path.join(folder, "log.blend")
bpy.ops.wm.save_as_mainfile(filepath=path)
bpy.ops.wm.open_mainfile(filepath=path)
body = bpy.data.texts[constants.LOG_TEXT_NAME].as_string()
checks.check("still redacted after save and reopen", "sync start: ~" in body and name not in body.replace("ninesync", ""))

# The preserving trim still runs on redacted lines.
logger.setup_logging()
for i in range(constants.LOG_TEXT_MAX_LINES + 50):
    logger.log.info("line %d %s", i, os.path.join(real_home, "q.pmm"))
text_block = bpy.data.texts[constants.LOG_TEXT_NAME]
lines = text_block.as_string().splitlines()
checks.check("trim keeps the newest lines", lines[-1].endswith("q.pmm") and "line %d" % (constants.LOG_TEXT_MAX_LINES + 49) in lines[-1])
checks.check("trim bounded", len(lines) <= constants.LOG_TEXT_MAX_LINES + 1, len(lines))

checks.finish()
