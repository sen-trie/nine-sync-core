# NineSync: PMM Bridge

Live-syncs MikuMikuDance `.pmm` projects into Blender through mmd_tools: model motion and morphs, UV morphs, NLA clips, camera, light and audio.

Accessories (`.x`) and MME effects (`.fx`) are handled by the separate add-on **NineSync: FX Importer** (`ninesync_fx`). This add-on never touches effects. When a PMM contains accessories and FX is not installed, the panel shows a notice.

## Requirements

- Blender 5.3 or newer
- mmd_tools, installed and enabled separately (Core does not bundle it). Validated with mmd_tools 4.5.5 on Blender 5.3; other versions are untested, and no minimum version is claimed
- Optional: NineSync: FX Importer for accessories and effects

## Install

1. Install and enable mmd_tools.
2. In Blender, open Edit > Preferences > Get Extensions, choose Install from Disk in the top-right menu, and pick the `ninesync` zip.
3. Open the 3D Viewport sidebar (N) and find the `NineSync` tab.

To build the zip from this folder:

```
blender --command extension build --source-dir . --output-dir <folder>
```

The add-on runs on its own. NineSync: FX Importer is a separate zip, installed the same way, in either order.

## What a sync updates

Sync Now works from a plan. **Preview** opens a window listing the PMX models that changed since the last sync, before and after, with unchanged models at the bottom. **PMM Motion Inspector**, at the bottom of the sidebar, shows the source motion and how it maps to each target. Neither changes the scene, and a sync always plans afresh, so what runs matches what the file says at that moment. Each enabled model has up to two destinations, its Motion Clip and its Shapekey Clip, and each gets one of:

| Operation | When |
| --- | --- |
| Create | The destination does not exist yet, or was deleted |
| Update | The PMM keys for it changed, you selected a different clip, its settings changed, or it was never synced |
| Skip (Unchanged) | Nothing relevant changed |
| Blocked | No target, or settings changed on a clip you edited in Blender |

**Your edits.** If you edit a synced clip in Blender and the PMM has not changed, the clip is skipped and the edit stays. A settings change alone will not replace it: the clip shows Blocked until you press Force Resync. New keys in the PMM do update it, with a warning in the preview; as always, only keys inside the PMM frame span are replaced. Sync Now is the normal choice. Force Resync, at the top of the Inspector, imports everything regardless, and each model in the Inspector has its own.

Camera, light, audio and FX hooks run on every sync; only model motion is skipped.

**How change detection works.** A model is identified by its PMM slot and name (`model_key`, for example `0:Miku`), so two models with the same name stay separate. Renaming a model or moving it to another slot in MMD makes it a new row, which syncs as new. Bone and morph tracks are hashed separately from their parsed keys, including interpolation, so re-saving a PMM without changing its motion causes no import.

The last successful state is stored as JSON on each model row (`sync_record`), so it saves, undoes and travels with the `.blend` and cannot apply to another file. It holds the hashes, the import settings, a hash of the PMM path (never the path itself) and a content hash of each destination action. A record moves forward only after its part imported successfully; a failed part forgets its record and retries, and Clean or changing the target clears it.

Esc cancels between models, never during one, so each model ends up fully synced or untouched. The window redraws between models; a single model's import blocks, so progress moves in stages rather than by exact percentage. Phase timings go to the `NineSyncLog` text only.

## Using it with NineSync: FX

Both add-ons put their panels in the sidebar tab `NineSync`. They never import each other. They meet in a small registry stored in `bpy.app.driver_namespace["ninesync_hooks"]` (see `hooks.py`, identical in both repos):

| Hook                    | Called by core                                 | FX does                                                 |
| ----------------------- | ---------------------------------------------- | ------------------------------------------------------- |
| `post_sync(ctx)`        | `sync()`, after camera and light, before audio | imports accessories and effects, returns the item count |
| `clean(scene, options)` | Clean, when "Accessories & effects" is ticked  | deletes accessory objects, returns a counts dict        |
| `report(context)`       | Support Report                                 | returns extra report lines                              |

`ctx` is a `types.SimpleNamespace` with `scene`, `project` (already retimed), `pmm_path`, `targets` (`{model_key: mmd_tools root}`), `unit`, `retimed` (always True from core), `integrated` (True), `core_version`.

`registry()["shared"]["import_scale"]` carries the "set import scale" preset to FX's accessory importer.

`API_VERSION` is 1. On a mismatch each side logs a warning and skips the hooks.

## Layout

```
__init__.py    register, load handler, hook registration
hooks.py       hook registry client (byte-identical in ninesync-fx)
pmm.py         PMM parser (core only; FX reads PMM data through ctx.project)
pmm_writer.py  writes a PMM for Create MMD Project
x_writer.py    writes .x props for Create MMD Project
common/        constants, logging, scene and animation helpers (no sync logic)
sync/          everything that touches mmd_tools or writes animation:
               engine, plan, change detection, models, morphs, UV morphs,
               clips, camera, light, audio, clean, report, project export
ui/            properties, operators, preferences, panels/
tests/         see tests/README.md
```

## Compatibility

Only the combinations listed here are documented; others are untested.

| core  | FX    | hook API |
| ----- | ----- | -------- |
| 0.4.x | 0.3.x | 1        |
| 0.3.x | 0.3.x | 1        |
| 0.2.x | 0.2.x | 1        |

## License

Copyright (C) 2026 sentrie

GPL-3.0-or-later; see `LICENSE`.
