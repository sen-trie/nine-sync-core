import os

from ..common.constants import MODEL_FOLDER
from ..common.logger import log
from .accessory_export import prop_file_name
from .mmd_tools_link import mmd_module


FRAME_LABELS = {
    "表情": "Facial",
    "ＩＫ": "IK",
    "IK": "IK",
    "体(上)": "Upper body",
    "体(下)": "Lower body",
    "腕": "Arms",
    "指": "Fingers",
    "足": "Legs",
    "髪": "Hair",
    "袖": "Sleeves",
    "スカート": "Skirt",
    "その他": "Other",
    "物理": "Physics",
    "新規枠": "New group",
    "注連縄": "Rope",
    "尻尾": "Tail",
    "服": "Clothes",
    "胸": "Chest",
    "耳": "Ears",
}


MORPH_LABELS = {
    "まばたき": "Blink",
    "笑い": "Smile eyes",
    "ウィンク": "Wink L",
    "ウィンク２": "Wink2 L",
    "ウィンク右": "Wink R",
    "ｳｨﾝｸ２右": "Wink2 R",
    "ジト目": "Half-lidded",
    "はぅ": "><",
    "なごみ": "Calm eyes",
    "びっくり": "Surprised",
    "瞳小": "Pupil small",
    "瞳大": "Pupil large",
    "瞳細": "Pupil thin",
    "瞳孔丸": "Pupil round",
    "ハート目": "Heart eyes",
    "星目": "Star eyes",
    "真面目": "Serious",
    "困る": "Troubled",
    "悲しみ": "Sad",
    "怒り": "Angry",
    "キリッ": "Sharp",
    "にこり": "Cheerful",
    "上": "Brow up",
    "下": "Brow down",
    "まゆつり": "Brow raise",
    "まゆタレ": "Brow droop",
    "あ": "A",
    "い": "I",
    "い2": "I2",
    "う": "U",
    "え": "E",
    "えー": "E long",
    "お": "O",
    "ワ": "Wa",
    "む": "Mu",
    "に": "Ni",
    "ω": "Omega",
    "にやり": "Grin",
    "口角上げ": "Mouth corners up",
    "口角下げ": "Mouth corners down",
    "口上げ": "Mouth up",
    "口下げ": "Mouth down",
    "口幅広く": "Mouth wide",
    "口幅狭く": "Mouth narrow",
    "歯下げ": "Teeth down",
    "照れ": "Blush",
    "汗": "Sweat",
}


# An existing ASCII label is the model author's and stays; empty ones show as Null_xx
# in MMD and non-ASCII ones as mojibake on a non-Japanese system. Fallbacks are numbered
# because every row must stay tellable apart.
def english_label(name, current, table, translate, fallback):
    if current and current.isascii():
        return current
    label = table.get(name) if table else None
    if label is None and translate is not None:
        label = translate(name)
    return label if label and label.isascii() else fallback


# A display name for the model selector, where the PMX's own one is unusable.
def model_label(root, index):
    return root.name if root.name.isascii() else "Model %d" % (index + 1)


# MMD reads every on-screen label from the PMX's English fields and a PMM cannot
# override them, so the project points at a labelled copy; the original PMX stays as
# it is. Bones with keys are also made visible here, because MMD never plays keys on
# hidden bones. Japanese names and all indices are unchanged, so the PMM still matches.
def write_labelled_pmx(root, index, path, pmm_path, keyed_hidden, taken):
    pmx_module = mmd_module("core.pmx")
    translate = mmd_module("translations").translateFromJp
    pmx = pmx_module.load(path)
    pmx.name_e = english_label(pmx.name, pmx.name_e, None, None, model_label(root, index))
    for number, bone in enumerate(pmx.bones):
        bone.name_e = english_label(bone.name, bone.name_e, None, translate, "Bone %d" % number)
    for number, morph in enumerate(pmx.morphs):
        morph.name_e = english_label(morph.name, morph.name_e, MORPH_LABELS, translate, "Morph %d" % number)
    for number, frame in enumerate(pmx.display):
        frame.name_e = english_label(frame.name, frame.name_e, FRAME_LABELS, None, "Group %d" % number)
    for slot in keyed_hidden:
        pmx.bones[slot].visible = True
    folder = os.path.join(os.path.dirname(os.path.abspath(pmm_path)), MODEL_FOLDER)
    os.makedirs(folder, exist_ok=True)
    out = os.path.join(folder, prop_file_name(root, taken, ".pmx"))
    pmx_module.save(out, pmx)
    log.info("labelled PMX for '%s': %s (%d bone(s) unhidden)", root.name, out, len(keyed_hidden))
    return out, pmx.name_e
