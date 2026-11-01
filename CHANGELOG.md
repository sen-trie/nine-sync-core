# Changelog

## 0.3.0

- Create MMD Project panel: build a PMM from Blender, with the chosen mmd_tools models, their bone, morph and camera animation, and the scene frame range. Spawn MMD Camera adds the camera rig to export, and Link to Syncer optionally sets the new PMM as the Syncer's.
- Props (.x): write mesh objects as accessories in the PMM, with their position, rotation, scale and visibility keys and a colour palette per material. Needs no FX.
- Clean MMD Labels: point the project at a labelled copy of each PMX, so MMD shows readable names. The original PMX is untouched.
- Convert root motion: a button on each model that moves root empty motion onto its parent bones.

## 0.2.0

- Accessories (.x) and MME effects (.fx) are handled by the separate add-on NineSync: FX Importer.
- Added the hook registry (`post_sync`, `clean`, `report`) and a notice when a PMM has accessories and FX is missing.
- Clean dialog shows "Accessories & effects" only when FX is installed.
- Stale settings check now tests a core field.
- Camera scale warning: "Import at Scale 1.0 From Now On" presets mmd_tools importers and shares the value with FX.
