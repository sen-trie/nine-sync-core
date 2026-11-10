# NineSync: PMM Bridge

Import and sync MikuMikuDance `.pmm` projects in Blender, including model animations, morphs, camera movement, lighting and audio. Sync again after editing your MMD project to update your Blender scene.

This repository is part of the [combined NineSync extension](https://github.com/sen-trie/nine-sync-release).

## Requirements

- Blender 5.3+
- [mmd_tools](https://extensions.blender.org/add-ons/mmd-tools/) 4.5.5+

## Installation

1. Install and enable **mmd_tools** in Blender.
2. Download the PMM Bridge extension ZIP from [Releases](https://github.com/sen-trie/nine-sync-core/releases).
3. In Blender, open **Edit > Preferences > Get Extensions**, select **Install from Disk**, and choose the ZIP.

## Getting started

1. In the **3D Viewport**, press **N** and open the **NineSync** tab.
2. Select your `.pmm` project and configure the model targets.
3. Click **Sync Now** to import or update animations.
4. Use **Preview** to review changes before syncing, or **PMM Motion Inspector** to see how motion is assigned.

**Sync Now** skips unchanged clips and generally preserves edits to those clips in Blender. Use **Force Resync** when you want to replace a clip with the MMD version.

## License

GPL-3.0-or-later. See `LICENSE`.
