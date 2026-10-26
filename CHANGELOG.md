# Changelog

## 0.2.0

- Split from the combined NineSync. Accessories (.x) and MME effects (.fx) moved to the separate add-on NineSync: FX Importer.
- Added the hook registry (`post_sync`, `clean`, `report`) and a notice when a PMM has accessories and FX is missing.
- Clean dialog shows "Accessories & effects" only when FX is installed.
- Stale settings check now tests a core field; the old combined add-on is detected and a warning panel is shown instead.
- Camera scale warning: "Import at Scale 1.0 From Now On" presets mmd_tools importers and shares the value with FX.
