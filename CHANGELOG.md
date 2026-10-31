# Changelog

## Unreleased

- Create MMD Project lists mesh objects as Props (.x): each is written as its own .x file in an `accessories` folder beside the PMM, and its position, rotation, scale and visibility keys over the scene frame range go into the PMM as an accessory, so a placeholder (cube, stand-in, prop) can be blocked in Blender and opened in MMD. The mesh is baked at the first frame with scale applied and the object origin as pivot; modifiers are applied, materials become a flat colour. Needs no FX.
- Create PMM numbers bone and morph key chains the way MMD does (shared index sequence starting at the bone or morph count), so keys after frame 0 stay on their own bone.
- Create PMM lists all PMX morphs, writes the frame-0 pose as each bone's initial state (no bone selected), and always converts frames from the scene fps to 30.
- Create PMM sets the MMD play range (from and stop frame) to the scene frame range.
- Create MMD Project panel above the Syncer: spawn the MMD camera rig, check the mmd_tools models to include (with their original PMX paths), optionally export existing bone, morph and camera animation, and write a PMM that is set as the Syncer's PMM with model and camera mappings kept.

## 0.2.0

- Split from the combined NineSync. Accessories (.x) and MME effects (.fx) moved to the separate add-on NineSync: FX Importer.
- Added the hook registry (`post_sync`, `clean`, `report`) and a notice when a PMM has accessories and FX is missing.
- Clean dialog shows "Accessories & effects" only when FX is installed.
- Stale settings check now tests a core field; the old combined add-on is detected and a warning panel is shown instead.
- Camera scale warning: "Import at Scale 1.0 From Now On" presets mmd_tools importers and shares the value with FX.
