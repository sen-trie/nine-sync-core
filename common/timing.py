import time
from contextlib import contextmanager

from .logger import log


# Wall-clock phase totals for one sync. Log only by design: nothing here reaches the UI.
class SyncTimings:
    def __init__(self):
        self.started = time.perf_counter()
        self.totals = {}

    def add(self, name, elapsed):
        self.totals[name] = self.totals.get(name, 0.0) + elapsed


_active = {"run": None}


def begin_timings():
    _active["run"] = SyncTimings()
    return _active["run"]


# Phases measured outside a sync, or nested in a preview, cost nothing and log nothing:
# a preview must not create the log text block.
@contextmanager
def phase(name, model=""):
    run = _active["run"]
    if run is None:
        yield
        return
    start = time.perf_counter()
    status = "ok"
    try:
        yield
    except BaseException:
        status = "error"
        raise
    finally:
        elapsed = time.perf_counter() - start
        run.add(name, elapsed)
        log.debug(
            "timing phase=%s model=%s elapsed_ms=%.1f status=%s",
            name,
            model or "-",
            elapsed * 1000.0,
            status,
        )


# Writes the one-line summary and ends the run. Failed and cancelled syncs reach this too.
def finish_timings(status, **counts):
    run = _active["run"]
    _active["run"] = None
    if run is None:
        return
    fields = ["status=%s" % status]
    fields.append("total_ms=%.1f" % ((time.perf_counter() - run.started) * 1000.0))
    fields += ["%s_ms=%.1f" % (k, v * 1000.0) for k, v in run.totals.items()]
    fields += ["%s=%s" % (k, v) for k, v in counts.items()]
    log.info("timing summary %s", " ".join(fields))
