import os
import tempfile
import time

from ..common.constants import STALE_TEMP_SECONDS, TEMP_DIR_NAME, TEMP_PREFIX
from ..common.logger import log


# Dedicated temp folder, so sweeping never touches files that are not ours.
def temp_root():
    path = os.path.join(tempfile.gettempdir(), TEMP_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


# Removes VMD files left behind by a crashed sync. Only old ones go, as a young file
# may belong to a sync in progress.
def sweep_stale_temp(max_age=STALE_TEMP_SECONDS):
    try:
        root = temp_root()
        now = time.time()
        for name in os.listdir(root):
            if not (name.startswith(TEMP_PREFIX) and name.endswith(".vmd")):
                continue
            path = os.path.join(root, name)
            try:
                if now - os.path.getmtime(path) > max_age:
                    os.remove(path)
                    log.info("removed stale temp file %s", path)
            except OSError:
                pass
    except OSError as error:
        log.warning("temp sweep failed: %s", error)


# mmd_tools can only import a VMD from disk, so the PMM keys are written to a temp
# file first.
def write_temp_vmd(data, prefix):
    handle, tmp_path = tempfile.mkstemp(suffix=".vmd", prefix=prefix, dir=temp_root())
    with os.fdopen(handle, "wb") as stream:
        stream.write(data)
    return tmp_path


# Retries briefly because Windows can still hold the file right after the importer
# read it. A leftover is swept on a later sync.
def remove_file(path):
    for attempt in range(5):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.05 * (attempt + 1))
    log.warning("could not remove temp file %s; it will be swept on a later sync", path)
