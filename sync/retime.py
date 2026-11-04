from ..common.constants import PMM_FPS
from ..common.scene_utils import fps_differs, scene_fps


def retime_factor(scene, settings):
    if not settings.retime_fps or settings.update_scene_settings:
        return 1.0
    if not fps_differs(scene):
        return 1.0
    return scene_fps(scene) / PMM_FPS


# Scales key frames by the factor; keys landing on the same frame collapse into one.
# hold_cuts keeps keys that were one frame apart on consecutive frames, so a camera
# cut stays a cut.
def retime_keys(keys, factor, hold_cuts=False):
    scaled = {}
    previous_old = None
    previous_new = None
    for key in sorted(keys, key=lambda k: k.frame):
        new = int(round(key.frame * factor))
        if hold_cuts and previous_old is not None and key.frame - previous_old <= 1:
            new = previous_new + 1
        scaled[new] = key._replace(frame=new)
        previous_old, previous_new = key.frame, new
    return [scaled[frame] for frame in sorted(scaled)]


# Retimes every track in place before hooks run, so FX sees the same timing as core.
def retime_project(project, factor):
    if abs(factor - 1.0) < 1e-6:
        return
    for model in project.models:
        model.bones = [retime_keys(keys, factor) for keys in model.bones]
        model.morphs = [retime_keys(keys, factor) for keys in model.morphs]
        model.visibility = retime_keys(model.visibility, factor, True)
        model.last_frame = int(round(model.last_frame * factor))
    project.camera_keys = retime_keys(project.camera_keys, factor, True)
    project.light_keys = retime_keys(project.light_keys, factor, True)
    for acc in project.accessories:
        acc.keys = retime_keys(acc.keys, factor)
