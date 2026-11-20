import traceback
from bpy.app.translations import pgettext_iface as iface_

from .. import hooks
from ..common.logger import log


# What one hook reported, after validation.
class HookOutcome:
    def __init__(self, owner):
        self.owner = owner
        self.imported = 0
        self.failed = 0
        self.warnings = []
        self.crashed = False


def count_field(value, name, outcome):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        outcome.warnings.append("hook result field '%s' is not a count of zero or more; ignored" % name)
        return 0
    return value


# Reads one hook result. An int is the imported count (API 1 as shipped); None counts
# for nothing; a mapping may carry imported, failed and warnings, each optional and
# checked, so a malformed field costs a warning and never the whole result.
def read_result(owner, value):
    outcome = HookOutcome(owner)
    if value is None:
        return outcome
    if isinstance(value, int) and not isinstance(value, bool):
        outcome.imported = max(value, 0)
        return outcome
    if not isinstance(value, dict):
        outcome.warnings.append("hook returned %s, expected a count or a dict; ignored" % type(value).__name__)
        return outcome
    if "imported" in value:
        outcome.imported = count_field(value["imported"], "imported", outcome)
    if "failed" in value:
        outcome.failed = count_field(value["failed"], "failed", outcome)
    warnings = value.get("warnings", [])
    if isinstance(warnings, str):
        warnings = [warnings]
    if isinstance(warnings, (list, tuple)):
        outcome.warnings += [str(w) for w in warnings]
    else:
        outcome.warnings.append("hook result field 'warnings' is not a list of text; ignored")
    return outcome


# One step of a generator hook may be None, a text, a fraction or (fraction, text).
def read_step(step):
    fraction, text = None, ""
    if isinstance(step, str):
        text = step
    elif isinstance(step, (int, float)) and not isinstance(step, bool):
        fraction = float(step)
    elif isinstance(step, (tuple, list)) and len(step) == 2:
        if isinstance(step[0], (int, float)) and not isinstance(step[0], bool):
            fraction = float(step[0])
        text = str(step[1])
    if fraction is not None:
        fraction = min(max(fraction, 0.0), 1.0)
    return fraction, text


# Runs the post_sync hooks as part of the sync generator. A hook that returns a
# generator is advanced one step per tick, so Esc is heard between its steps and a
# cancel closes it (its finally blocks run); any other hook runs inside one tick as
# before. Every failure is logged, counted in run.hook_failed and kept, and one hook
# failing never stops the next. The caller's cancel exception is passed in.
def hook_steps(run, context, cancelled_error, first, last):
    if not hooks.compatible():
        log.warning(
            "hook registry is API %s but Core speaks %s; skipping post_sync hooks",
            hooks.registry().get("api_version"),
            hooks.API_VERSION,
        )
        return
    owners = list(hooks.registry()["post_sync"].items())
    for position, (owner, fn) in enumerate(owners):
        span = (last - first) / max(len(owners), 1)
        base = first + span * position
        outcome = HookOutcome(owner)
        generator = None
        try:
            result = fn(context)
            if hasattr(result, "send") and hasattr(result, "close"):
                generator = result
                while True:
                    try:
                        step = next(generator)
                    except StopIteration as done:
                        result = done.value
                        break
                    fraction, text = read_step(step)
                    shown = base + span * (fraction if fraction is not None else 0.0)
                    yield from run.tick(shown, text or iface_("Running %s") % owner)
                generator = None
            outcome = read_result(owner, result)
        except cancelled_error:
            raise
        except Exception as error:
            outcome = HookOutcome(owner)
            outcome.failed = 1
            outcome.crashed = True
            outcome.warnings.append("%s hook failed: %s" % (owner, error))
            log.error("hook post_sync/%s failed:\n%s", owner, traceback.format_exc())
        finally:
            if generator is not None:
                generator.close()
        run.imported += outcome.imported
        run.hook_failed += outcome.failed
        for warning in outcome.warnings:
            log.warning("hook %s: %s", owner, warning)
            run.hook_warnings.append("%s: %s" % (owner, warning))
        log.info(
            "hook %s: imported %d, failed %d, %d warning(s)",
            owner,
            outcome.imported,
            outcome.failed,
            len(outcome.warnings),
        )
