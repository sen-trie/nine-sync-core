# NineSync: PMM Bridge

Live-syncs MikuMikuDance `.pmm` projects into Blender through mmd_tools: model motion and morphs, UV morphs, NLA clips, camera, light and audio.

Accessories (`.x`) and MME effects (`.fx`) are handled by the separate add-on **NineSync: FX Importer** (`ninesync_fx`). This add-on never touches effects. When a PMM contains accessories and FX is not installed, the panel shows a notice.

## Requirements

- Blender 5.3 or newer
- mmd_tools (enabled)
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
               engine, models, morphs, UV morphs, clips, camera, light,
               audio, clean, report, project export
ui/            properties, operators, preferences, panels/
tests/         see tests/README.md
```

## Compatibility

| core  | FX    | hook API |
| ----- | ----- | -------- |
| 0.3.x | 0.2.x | 1        |
| 0.2.x | 0.2.x | 1        |
