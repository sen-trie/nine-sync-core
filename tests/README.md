# Tests

Run from the repo root. Each file is a standalone script that prints `ok` or `FAIL` lines and exits non-zero on any failure.

| Script | Needs | Covers |
|---|---|---|
| `check_static.py` | Python 3.11+ | import layers, no FX imports, version consistency, current copy, `hooks.py` byte identity |
| `t_registration.py` | Blender or `bpy` | core alone, FX alone, both, every load and unload order, reload cycles, nothing left behind |
| `t_hook_contract.py` | Blender or `bpy` | registry behaviour, `sync()` order, `ctx` fields, retimed keys |
| `t_animation.py` | Blender or `bpy` | slotted actions, user-key snapshots, light keys, NLA cleanup |
| `t_sync_plan.py` | Blender or `bpy` | fingerprints, summaries, the plan decision table, records, preview errors, progress and driver exit paths, timings |
| `t_selective_sync.py` | Blender or `bpy` | display-track parsing, camera and light planned and imported independently (with stand-in importers), model visibility keys, NLA ownership and removal |
| `t_review_fixes.py` | Blender or `bpy` | names cut as MMD cuts them, identical fast hashing and morph sampling, IK switch keys, carried bone names, additive accessories, import-format records, parse reuse, UV morph helpers, the undo step |
| `manual/check_install.py` | Blender | extension zip: install, enable, restart, preferences id, uninstall |
| `manual/validate_real.py` | Blender, mmd_tools, PMX files | a real sync checked against values from the PMM, resync, 60 fps retime, Clean |
| `manual/validate_rigify.py` | Blender, mmd_tools, a Rigify-converted `.blend`, a VMD | the Rigify remap on every limb in FK and IK against the plain MMD pose |
| `manual/check_blend.py` | Blender, mmd_tools | a saved `.blend` opens and re-syncs in memory |
| `manual/validate_sync_plan.py` | Blender, mmd_tools, PMX files | skips, per-part updates, `.blend` round trip, deleted, switched and edited actions, Force Resync, failure, cancel, malformed PMM, Clean |

FX is found through `NINESYNC_FX` or a sibling folder `nine-sync-fx` / `ninesync-fx`. Without it the registration test covers core alone. `check_static.py --require-fx` fails instead of skipping the comparison.

## Automated scripts

```
python tests/check_static.py
blender --background --factory-startup --python tests/t_registration.py
blender --background --factory-startup --python tests/t_hook_contract.py
blender --background --factory-startup --python tests/t_animation.py
blender --background --factory-startup --python tests/t_sync_plan.py
blender --background --factory-startup --python tests/t_selective_sync.py
blender --background --factory-startup --python tests/t_review_fixes.py
```

With the wheel (`pip install bpy==5.2.3`, Python 3.13) use `python tests/t_registration.py` and so on. `tests/support.py` lowers `MIN_BLENDER` for older wheels. CI (`.github/workflows/ci.yml`) runs these on the wheel, which is not Blender 5.3.

## Real Blender checks

Use a throwaway profile, a separate Blender process from any other session, and never run two at once on one profile.

```
export BLENDER_USER_RESOURCES=/path/to/empty/folder
mkdir -p "$BLENDER_USER_RESOURCES/extensions/blender_org"
cp -r <your 5.3 config>/extensions/blender_org/mmd_tools "$BLENDER_USER_RESOURCES/extensions/blender_org/"
blender --background --factory-startup --command extension build --source-dir . --output-dir out
blender --background --factory-startup --command extension install-file -r user_default out/ninesync-0.6.0.zip

blender --background --python tests/manual/validate_real.py -- /path/to/project.pmm
blender --background --python tests/manual/validate_sync_plan.py -- /path/to/project.pmm
blender --background /path/to/file.blend --python tests/manual/check_blend.py
python tests/manual/check_install.py /path/to/blender [core.zip] [fx.zip]
```

`validate_real.py` imports each model from the path stored in the PMM, so those PMX files must exist. If FX is installed in the profile it also checks the integrated path. Neither script saves a `.blend` or changes a PMM. Record results in `docs/VALIDATION.md` (local to the owner's checkout, like `CLAUDE.md`).
