# NineSync: PMM Bridge

Live-syncs MikuMikuDance `.pmm` projects into Blender through mmd_tools: model motion and morphs, UV morphs, NLA clips, camera, light and audio.

Accessories (`.x`) and MME effects (`.fx`) are handled by the separate add-on **NineSync: FX Importer** (`ninesync_fx`). This add-on never touches effects. When a PMM contains accessories and FX is not installed, the panel shows a notice.

## Requirements

- Blender 5.3 or newer
- mmd_tools (enabled)
- Optional: NineSync: FX Importer for accessories and effects

## Using it with NineSync: FX

Both add-ons put their panels in the sidebar tab `NineSync`. They never import each other. They meet in a small registry stored in `bpy.app.driver_namespace["ninesync_hooks"]` (see `hooks.py`, identical in both repos):

| Hook | Called by core | FX does |
|---|---|---|
| `post_sync(ctx)` | `sync()`, after camera and light, before audio | imports accessories and effects, returns the item count |
| `clean(scene, options)` | Clean, when "Accessories & effects" is ticked | deletes accessory objects, returns a counts dict |
| `report(context)` | Support Report | returns extra report lines |

`ctx` is a `types.SimpleNamespace` with `scene`, `project` (already retimed), `pmm_path`, `targets` (`{model_key: mmd_tools root}`), `unit`, `retimed` (always True from core), `integrated` (True), `core_version`.

`registry()["shared"]["import_scale"]` carries the "set import scale" preset to FX's accessory importer.

`API_VERSION` is 1. On a mismatch each side logs a warning and skips the hooks.

## Old combined NineSync

If the old single add-on is still enabled, this add-on registers only a warning panel. Disable the old one and restart Blender.

## Layout

```
__init__.py   register, load handler, hook registration
common.py     constants and small helpers
hooks.py      hook registry client (identical in ninesync-fx)
pmm.py        PMM parser (vendored into ninesync-fx; keep identical)
bridge.py     model, camera, light, audio, UV morph sync, clean, report
ui.py         properties, operators, panels, preferences
tests/        headless tests
```

## Compatibility

| core | FX | hook API |
|---|---|---|
| 0.2.x | 0.2.x | 1 |
