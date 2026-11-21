from ..common.constants import PMM_FPS
from ..common.scene_utils import fps_differs, scene_fps


def retime_factor(scene, settings):
    if not settings.retime_fps:
        return 1.0
    if not fps_differs(scene):
        return 1.0
    return scene_fps(scene) / PMM_FPS


# True for the second key of a cut: one frame after the key before it, with the keys on
# either side of the pair more than a frame away. A run of three or more keys a frame
# apart is a baked track (a camera keyed on every frame), not a cut.
def is_cut(frames, index):
    if index < 1 or frames[index] - frames[index - 1] > 1:
        return False
    before = index < 2 or frames[index - 1] - frames[index - 2] > 1
    after = index + 1 >= len(frames) or frames[index + 1] - frames[index] > 1
    return before and after


# Scales key frames by the factor; keys landing on the same frame collapse into one.
# hold_cuts keeps the two keys of a cut on consecutive frames, so a camera cut stays a
# cut, while a baked track is scaled like any other.
def retime_keys(keys, factor, hold_cuts=False):
    ordered = sorted(keys, key=lambda k: k.frame)
    frames = [k.frame for k in ordered]
    scaled = {}
    previous_new = None
    for index, key in enumerate(ordered):
        new = int(round(key.frame * factor))
        if hold_cuts and is_cut(frames, index):
            new = previous_new + 1
        scaled[new] = key._replace(frame=new)
        previous_new = new
    return [scaled[frame] for frame in sorted(scaled)]


# Retimes every track in place before hooks run, so FX sees the same timing as core.
def retime_project(project, factor):
    if abs(factor - 1.0) < 1e-6:
        return
    for model in project.models:
        model.bones = [retime_keys(keys, factor) for keys in model.bones]
        model.morphs = [retime_keys(keys, factor) for keys in model.morphs]
        model.visibility = retime_keys(model.visibility, factor, True)
        model.ik_keys = retime_keys(model.ik_keys, factor, True)
        model.outside_keys = retime_keys(model.outside_keys, factor, True)
        model.last_frame = int(round(model.last_frame * factor))
    if project.play_range is not None:
        project.play_range = project.play_range._replace(
            start=int(round(project.play_range.start * factor)),
            end=int(round(project.play_range.end * factor)),
        )
    project.camera_keys = retime_keys(project.camera_keys, factor, True)
    project.light_keys = retime_keys(project.light_keys, factor, True)
    for acc in project.accessories:
        acc.keys = retime_keys(acc.keys, factor)
