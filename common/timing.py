import time
from contextlib import contextmanager

from .logger import log


# Wall-clock phase totals for one sync. Log only by design: nothing here reaches the UI.
class SyncTimings:
    def __init__(self):
        self.started = time.perf_counter()
        self.totals = {}
        self.counts = {}

    def add(self, name, elapsed):
        self.totals[name] = self.totals.get(name, 0.0) + elapsed


_active = {"run": None}


def begin_timings():
    _active["run"] = SyncTimings()
    return _active["run"]


# Work counters for the summary line (frame evaluations, keys written, cache hits). Like
# phases they cost nothing outside a sync.
def count(name, amount=1):
    run = _active["run"]
    if run is not None:
        run.counts[name] = run.counts.get(name, 0) + amount


# Adds time measured by the caller to a phase without a log line, for work timed in many
# small pieces such as one frame evaluation per frame.
def accumulate(name, elapsed):
    run = _active["run"]
    if run is not None:
        run.add(name, elapsed)


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
    fields += ["%s=%s" % (k, v) for k, v in run.counts.items()]
    fields += ["%s=%s" % (k, v) for k, v in counts.items()]
    log.info("timing summary %s", " ".join(fields))
