# Changelog

## 0.7.0

- Sync IK toggle keyframes, using Blender IK contrainsts
- Cut bone, morph and model names to fit byte-limits when creating PMM.
- Sync Now is one undo step, also when cancelled or failed partway.

## 0.6.0

- Known limits of MMDBridge Physics: the baked VMD is not retimed to the scene frame rate, so set MMDBridge's export FPS to the scene's.
- Preview warns when the PMM keys bones that the PMX hides.
- Create PMM writes timeline markers as keyframes on a hidden "Markers" accessory.
- Clean up N-panel elements

## 0.5.0

- Camera and light are change-aware.
- Keyframes for model visibility are synced in Blender.
- Rearrange UI elements in the N-panel

## 0.4.0

- Change detection: Sync Now imports only what changed. Each model's bone and morph keys are checked separately, so a morph edit leaves the Motion Clip alone and an edit to another model imports nothing here. Clips edited in Blender are kept while the PMM is unchanged.
- Preview: a window listing the PMX models that changed, before and after, with the changed bones and morphs named and unchanged models at the bottom.
  - PMM Motion Inspector, below Options: source counts and frame ranges, target mapping, motion that cannot reach the rig, and Force Resync.

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
