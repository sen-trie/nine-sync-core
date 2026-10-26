import bpy
import os

from ..common.constants import AUDIO_STRIP_NAME
from ..common.logger import log
from ..common.scene_utils import fps_differs, scene_fps


# Search order: the override, the stored PMM path, then next to the PMM file (also
# Audio and BGM folders), because projects get moved between machines.
def find_audio_file(project, settings):
    pmm_dir = os.path.dirname(bpy.path.abspath(settings.filepath))
    stored = project.audio_path.replace("\\", "/")
    base = stored.rsplit("/", 1)[-1]
    candidates = []
    if settings.audio_path:
        candidates.append(bpy.path.abspath(settings.audio_path))
    candidates += [project.audio_path, stored]
    if base:
        candidates += [
            os.path.join(pmm_dir, base),
            os.path.join(pmm_dir, "Audio", base),
            os.path.join(pmm_dir, "BGM", base),
        ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


# One-line audio status for the panel.
def describe_audio(project, settings):
    if settings.audio_path:
        found = os.path.isfile(bpy.path.abspath(settings.audio_path))
        return "Override file %s" % ("found" if found else "not found")
    if not project.audio_enabled or not project.audio_path:
        return "PMM has no audio"
    base = project.audio_path.replace("\\", "/").rsplit("/", 1)[-1]
    return "PMM audio: %s (%s)" % (
        base,
        (
            "found"
            if find_audio_file(project, settings)
            else "not found, set an Audio File"
        ),
    )


def sequence_strips(editor):
    strips = getattr(editor, "strips", None)
    if strips is None:
        strips = getattr(editor, "sequences", None)
    return strips


# Adds the PMM's audio as a sound strip at frame 0, the same as MMD. A strip already
# pointing at the same file is left alone so manual repositioning survives.
def import_audio(scene, project, settings):
    if not settings.sync_audio:
        return False
    if not settings.audio_path and not (project.audio_enabled and project.audio_path):
        log.info("PMM has no audio")
        return False
    path = find_audio_file(project, settings)
    if path is None:
        log.warning(
            "audio file not found (PMM path %s); set an Audio File in the Audio panel",
            project.audio_path,
        )
        return False
    editor = scene.sequence_editor or scene.sequence_editor_create()
    strips = sequence_strips(editor)
    existing = next((s for s in strips if s.name == AUDIO_STRIP_NAME), None)
    if existing is not None:
        sound = getattr(existing, "sound", None)
        current = (
            os.path.normcase(os.path.abspath(bpy.path.abspath(sound.filepath)))
            if sound is not None
            else ""
        )
        if current == os.path.normcase(os.path.abspath(path)):
            log.info(
                "audio strip '%s' already uses %s; position left as is",
                existing.name,
                path,
            )
            return True
        strips.remove(existing)
    channel = max((s.channel for s in strips), default=0) + 1
    strips.new_sound(
        name=AUDIO_STRIP_NAME, filepath=path, channel=channel, frame_start=0
    )
    if hasattr(scene, "use_audio_scrub"):
        scene.use_audio_scrub = True
    log.info("audio strip added on channel %d at frame 0: %s", channel, path)
    if fps_differs(scene) and not settings.retime_fps:
        log.warning(
            "scene is %.3f fps and retiming is off, so animation and audio will drift apart",
            scene_fps(scene),
        )
    return True
