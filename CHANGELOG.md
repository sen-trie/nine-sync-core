# Changelog

## Unreleased

- Fixed Create PMM with more than one model: MMD only brought up the first because every model claimed draw and compute order 1. Each model now gets its own orders, timeline rows and morph panel state, matching what MMD saves for the same PMX.
- Create PMM picks which MMD camera rig to write (default: the scene camera's rig). It no longer creates or rebuilds a rig, which deleted the camera the scene was using, and it writes the first camera key as the state MMD shows on load.
- Create PMM no longer changes the .blend. The Syncer link is now the opt-in Link to Syncer option.
- Convert button on each model in Create MMD Project: moves root empty motion onto the parent bone and resets the root. Destructive and only ever run by hand, it keeps keys already on the bone and makes keys only on existing keyframes. Create PMM warns about root motion left unconverted.
- Clean MMD Labels (on by default): Create PMM points the project at a copy of each PMX in a `models` folder beside the PMM, with English names so MMD shows no Null_xx or garbled rows, and with keyed hidden bones unhidden so they play. The original PMX is untouched.
- Props have a colour palette, one colour per material, starting from each texture's average colour. Colours are written as sRGB.
- Prop keys come from explicit Blender keyframes only; curves are no longer sampled.
- Create PMM takes its scale from the checked models instead of the Syncer, so a first export with no Syncer targets is no longer 12.5x off.
- Props (.x) are written with reversed triangle winding: the Blender to MMD axis swap mirrors them, so MMD was culling the side facing the viewer and walls turned see-through.
- Tracks that start after frame 0 hold their first value from frame 0. Spawn MMD Camera selects an existing rig instead of rebuilding it.
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
