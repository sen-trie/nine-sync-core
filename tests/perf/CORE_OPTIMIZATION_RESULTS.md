# NineSync Core: sync and conversion optimisation results (P0-P2)

Headless Blender 5.3.0 (02f0899d78e3, Python 3.13.13), mmd_tools 4.5.14, NineSync 0.9.0, Windows 11, AMD64 Family 25. Every timed run is a fresh Blender process with its own throwaway profile (`BLENDER_USER_RESOURCES`), the extension zip installed next to a copy of mmd_tools. Builds are interleaved ABBA within each session.

Builds:

| build | contents | zip sha256 (first 16) |
|---|---|---|
| B0 | baseline (HEAD 961bfe3) | 3bc726651ccfa72d |
| B1 | B0 + P0-A bulk F-curve writes + P0-B isolated solve scene | b5d7d43eb76bad91 |
| B2 | B1 + P1-A session solve cache | 25adeab12239a438 |
| B3 | B2 + P2-B parent-motion fingerprint and parent-first order + P2-C block parse | 6c8c466a2153d3f1 |

## Cumulative table (checkpoint session, 18 projects)

5 cold syncs per project and build (fresh process each), then 3 no-change and 3 forced resyncs in each of those processes. Medians, seconds. All four builds ran in one session. Plans 12 and 13 were re-measured in a second session (all four builds again, ABBA): during the first one, part of their runs were 2.5-3x slower in every phase (parse, importer and plan alike) for every build, B0 included, which points to machine contention, not code.

| build | sum of cold | sum of forced | sum of no-change | swing cold | swing forced | swing no-change | other 17 cold | other 17 no-change |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| B0 | 25.580 | 30.745 | 1.495 | 16.540 | 21.311 | 0.165 | 9.040 | 1.331 |
| B1 | 15.955 (-37.6%) | 16.576 (-46.1%) | 1.499 | 6.933 (-58.1%) | 7.140 (-66.5%) | 0.166 | 9.022 | 1.333 |
| B2 | 16.024 (-37.4%) | 12.017 (-60.9%) | 1.501 | 7.012 (-57.6%) | 2.577 (-87.9%) | 0.166 | 9.012 | 1.335 |
| B3 | 15.940 (-37.7%) | 11.938 (-61.2%) | 1.413 (-5.5%) | 7.021 (-57.5%) | 2.571 (-87.9%) | 0.164 | 8.919 (-1.3%) | 1.248 (-6.2%) |

Peak RSS is unchanged within noise (swing: B0 730 MB, B3 735 MB).

Swing is the only project of the corpus with outside-parent bindings onto mapped models (3347 solved frames), so P0 and P1 only move swing. P2-C (parse) is the only change that reaches every project, and shows as about 5-10% on no-change resyncs and on the smallest projects (15, 16).

### Per project (cold medians, s; forced and no-change for B0 and B3)

| # | PMM | mapped/slots | B0 | B1 | B2 | B3 | B3 vs B0 | forced B0 | forced B3 | no-change B0 | no-change B3 | solved frames |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 01 | kilometer(TN) | 5/12 | 1.015 | 1.017 | 1.012 | 0.991 | -2.4% | 1.100 | 1.097 | 0.281 | 0.260 | 0 |
| 02 | Flandance | 2/6 | 0.820 | 0.821 | 0.816 | 0.812 | -0.9% | 0.865 | 0.856 | 0.070 | 0.067 | 0 |
| 03 | Walking 1s-2s-3s | 1/1 | 0.144 | 0.143 | 0.142 | 0.144 | -0.1% | 0.146 | 0.146 | 0.016 | 0.016 | 0 |
| 04 | meiling thing | 1/2 | 0.194 | 0.192 | 0.192 | 0.192 | -1.3% | 0.191 | 0.191 | 0.026 | 0.026 | 0 |
| 05 | cirnos | 3/4 | 0.728 | 0.730 | 0.729 | 0.727 | -0.3% | 0.775 | 0.778 | 0.078 | 0.077 | 0 |
| 06 | Rumiaing | 2/8 | 1.083 | 1.076 | 1.081 | 1.073 | -1.0% | 1.154 | 1.146 | 0.073 | 0.068 | 0 |
| 07 | swing | 4/24 | 16.540 | 6.933 | 7.012 | 7.021 | -57.5% | 21.311 | 2.571 | 0.165 | 0.164 | 3347 |
| 08 | remi dance | 2/13 | 0.449 | 0.450 | 0.449 | 0.448 | -0.2% | 0.469 | 0.471 | 0.050 | 0.049 | 0 |
| 09 | Serious Spark | 1/23 | 0.385 | 0.385 | 0.385 | 0.380 | -1.4% | 0.389 | 0.384 | 0.054 | 0.051 | 0 |
| 10 | Reisen Brainrot2 | 1/6 | 1.105 | 1.093 | 1.098 | 1.093 | -1.1% | 1.175 | 1.175 | 0.070 | 0.065 | 0 |
| 11 | Nuh uh | 2/14 | 0.833 | 0.833 | 0.828 | 0.816 | -2.1% | 0.849 | 0.837 | 0.119 | 0.108 | 0 |
| 12 | IN 7 | 2/16 | 0.469 | 0.468 | 0.470 | 0.467 | -0.4% | 0.487 | 0.486 | 0.058 | 0.056 | 0 |
| 13 | seijaruto5 | 1/9 | 0.338 | 0.336 | 0.335 | 0.333 | -1.6% | 0.351 | 0.349 | 0.041 | 0.038 | 0 |
| 14 | seijaruto12 | 1/9 | 0.399 | 0.399 | 0.401 | 0.398 | -0.3% | 0.397 | 0.395 | 0.042 | 0.040 | 0 |
| 15 | Yume Nikki walk | 0/5 | 0.186 | 0.187 | 0.186 | 0.166 | -11.1% | 0.171 | 0.153 | 0.180 | 0.162 | 0 |
| 16 | okina crab | 0/19 | 0.047 | 0.046 | 0.047 | 0.042 | -10.4% | 0.045 | 0.040 | 0.037 | 0.033 | 0 |
| 17 | koiCarol4 | 1/72 | 0.575 | 0.576 | 0.573 | 0.572 | -0.6% | 0.591 | 0.588 | 0.093 | 0.091 | 0 |
| 18 | IN 12 | 1/15 | 0.270 | 0.269 | 0.270 | 0.268 | -0.8% | 0.279 | 0.277 | 0.041 | 0.040 | 0 |

Min-max spreads per cell are in the session summaries; outside plans 12-13 of the first session they stay within a few percent of the median.

### Where swing's time goes (cold, medians of 5, ms)

| phase | B0 | B3 |
|---|---:|---:|
| total | 16534 | 7015 |
| mmd_tools VMD importer | 1714 | 1713 |
| IK keys + outside-parent solve | 14362 | 4853 |
| of which frame evaluation | (not split in B0) | 4560 (3346 frame_set calls) |
| of which solved-key writes | (inside the above) | 24 |
| parse | 46 | 37 |
| plan | 80 | 86 |

Forced resync on B3: 2571 ms, of which importer 1711, solve 14 (cache hit, 6 verification frames), writes 24.

## Correctness gate

Every build was compared with B0 on every project, cold and forced (108 comparisons, `compare_output.py`): every action curve, evaluated samples of the bound bones' curves at quarter frames, NLA and constraint layout, and the evaluated world matrix of every bone of the outside-parent armatures on every frame and on .25/.5 subframes of every third frame. Gate: translation <= 1e-5, angle <= 1e-4.

| build | comparisons passed | max translation | max angle (rad) | max curve sample diff | curves on one side only | NLA / constraint diffs |
|---|---:|---:|---:|---:|---:|---:|
| B1 | 36/36 | 7.75e-7 | 3.59e-6 | 2.98e-8 | 0 | 0 |
| B2 | 36/36 | 7.75e-7 | 3.59e-6 | 2.98e-8 | 0 | 0 |
| B3 | 36/36 | 7.75e-7 | 3.59e-6 | 2.98e-8 | 0 | 0 |

The remaining difference is float noise from the bulk writer, which keys the solved span with LINEAR interpolation and writes the span in one `foreach_set` (B0 left per-frame `keyframe_insert` Bezier keys; both evaluate the same on whole and quarter frames, within 3e-8). Stored key handles inside the solved span therefore differ structurally from B0 (7 curves on swing), which the gate allows because they are never evaluated.

The same solver is checked without mmd_tools by `tests/t_outside_parent.py` against a frozen copy of the B0 per-frame solver on synthetic rigs (FK and constrained chains, IK, moving parent object, scaled armatures, quaternion flips, two parents in turn, released bones, a bone bound to its own armature, drivers, scripted drivers and physics, which must fall back to the full scene, mesh targets), plus cache hit/miss/stale cases and the bulk merge itself.

## Sub-experiment ledger

Each sub-experiment was measured against the build before it, in its own session (5 cold syncs, 3 forced, 3 no-change, ABBA), on swing plus plans 01, 03 and (from P1 on) 10, with dumps compared against B0.

| id | change | result (swing unless noted) | correctness | decision |
|---|---|---|---|---|
| P0-A | Bulk solved-key writes: two real `keyframe_insert`s per bone (they set the neighbour handles exactly as before), the rest merged into the key arrays with `foreach_get/foreach_set` | cold 17.07 -> 13.16 s (-22.9%) | pass (7.75e-7 / 3.59e-6) | accepted |
| P0-B | Solve in an isolated scene holding only the solve's dependency closure (parents, constraint, modifier and driver targets); full scene when physics, scripted drivers, simulation modifiers or other object types are involved | cold 13.16 -> 7.33 s (-57.1% vs B0) | pass (same) | accepted (= B1) |
| P0-C | Hot-loop rewrite (cached rest rolls, fewer matrix temporaries) | 7.33 -> 7.20 s (-1.7%, inside run-to-run spread) | pass but larger error (2.83e-6 / 1.38e-5) | rejected: noise-level gain, worse error; rolled back |
| P1-A | Session solve cache keyed on the content of everything the solve reads (closure objects, curves, NLA, drivers, constraints, rest pose, unanimated transforms, bindings), with a 6-frame spot check before reuse, LRU of 8, cleared on file load and unregister | cold +0.4% (noise); forced 7.30 -> 2.72 s (-62.8%) | pass | accepted (= B2) |
| P1-B | Leave bone tracks that never leave rest out of the temp VMD | cold -8.5% (swing), -23.8% (plan 01) | fail: 1330 (swing) and 5040 (plan 01) curves mmd_tools writes are missing | rejected; rolled back. Needs mmd_tools-side semantics (see handoff) |
| P1-C | FK-only fast path computing the solve without frame_set | +1.1% (overhead of the eligibility check) | pass | rejected: 0 bound rigs in the corpus are eligible (swing's wrist chain carries mmd_tools TRANSFORM constraints and arm IK); rolled back |
| P2-A | Sparse/adaptive sampling: exact collinear key reduction (1e-7) plus reuse of frames where nothing in the closure changes | cold -0.8%; frame evaluations 3347 -> 3313 (-1%) | pass, but angle error 3.6e-6 -> 7.1e-5 (close to the 1e-4 gate) | rejected: no measurable gain on real data, error budget eaten; rolled back |
| P2-B | Fine-grained incremental updates: a bound model's motion fingerprint includes the motion of the models it is bound to; parents sync before the models bound to them | no cold/forced change; no-change resync +7 ms on swing (169 -> 175 ms) after sharing the per-plan fingerprint memo (first version: +50 ms) | fixes a stale result, see below | accepted |
| P2-C | PMM parse: bone and morph key sections read with one bounds check and `iter_unpack` over a memoryview instead of one `unpack_from` per record | parse of all 18 PMMs 1016 -> 873 ms (-14%), swing 104 -> 84 ms; sync: no-change -9% on plan 01 | identical parse of all 18 PMMs (every field), truncated files still raise `PmmError` | accepted |

B3 = B2 + P2-B + P2-C.

### P2-B incremental scenario

`run_bench.py --edit/--original` on a bench copy of swing: cold sync, 3 no-change resyncs, then the PMM on disk is replaced by a copy with one key of the parent model moved (Sanae, 上半身, frame 1399, x + 0.5, inside a span where the prop's センター is bound to her 左手首), and a plain sync runs. Reference: a cold sync of the edited PMM. 4 runs per build.

| build | incremental sync s | solved frames | vs cold reference of the edited PMM |
|---|---:|---:|---|
| B2 | 1.236 | 0 (prop reported unchanged) | fail: translation 3.65e-2, angle 4.34e-2 |
| B3 | 6.206 | 3347 | pass: identical (0 / 3.65e-8) |

B2 resyncs only the edited model and leaves the bound prop's solve stale. B3 also replans the prop and re-solves it; the solve cache misses because the parent's curves changed, so the time is a full solve. An edit at frame 1342 (one frame before the binding starts) changed nothing either build solves, so the scenario edits inside a bound span.

## Release gate follow-up (2026-11-20)

### MMD playback and the behaviour changes it forced

The gate project (4 real models, all mapped; 5 bound bones with 9 bindings including a chain, a ground release and a prop passed between four hands and a head) was played in MMD 9.32 and compared with Blender renders frame by frame. Details and numbers are in `docs/VALIDATION.md` (2026-11-20). Front-view mean silhouette IoU: B0 0.695, B3 0.747, final tree 0.965. Getting there needed three changes to what the solver does, so **the final tree is deliberately no longer equivalent to B0**:

1. Edge keys: the bone's own pose is keyed on the frame before and after each bound run, so its own curve plays unchanged right up to the switch, as in MMD (B0 let the solved values bleed into the neighbouring Bezier segments).
2. Switches land on the key frame, ground release included, and keys either side of a state change hold the first key until the switch, then use the second (replaces the f60502f rules, which MMD contradicts).
3. The Child Of inverse for parentless bones places the bone like `bound_matrix`, and is built in armature space (B0's world-space inverse misplaced any model whose root object is moved or rotated).

`tests/t_outside_parent.py` now compares against the frozen B0 solver combined with MMD's switching rule, instead of B0 itself, and has new checks for the constraint placement and the straddle hold.

### Bezier vs LINEAR, checked further

| Check | Result |
|---|---|
| Arbitrary subframes: 3000 random times per curve on five synthetic cases; 400 random times added to the suite's curve check | inside runs exactly linear between whole-frame values, outside them equal to the original curve, max 5e-7 |
| Same keys written by per-key `keyframe_insert` (B0's way) vs the bulk writer | same key count, evaluation within 3e-7 |
| mmd_tools VMD export (Create PMM) | reads handles only for BEZIER segments; LINEAR and CONSTANT export `evaluate(f)` with default control points, so stored handles in solved spans never reach a PMM |
| A user turning the solved keys to Bezier (the one edit that exposes stored handles) | before: both writers left handles from the replaced curve, deviation 0.10-0.23 from the written motion. Now every key in a span but the first gets auto clamped handles: deviation 0.01-0.035 |
| Repeated editing: solving again over already solved curves | not idempotent (B0 neither): the solve reads the bone's keys as the PMM placement. Not reachable: a sync always resets and reimports the motion part before the solve (`model_import.py`) |

So the two representations are interchangeable for playback, rendering and export. They differ only in unevaluated handle data, and the new handles are the better ones if a user converts the span.

### Representative corpus: fully mapped, physics on

Five new plans in which every model slot is mapped: the gate project with its real models, and four real corpus projects with many bindings, using a stand-in for every slot (the corpus matcher's pick, otherwise a complete standard-bone model). Models were imported with rigid bodies and joints (`--physics`, new in `bench_sync.py` and `run_bench.py`). Dark Spark solved nothing (its bindings name bones the stand-ins lack) and was dropped. ABBA, 2 runs each, medians:

| plan | slots | frames evaluated | B0 cold s | final cold s | change | B0 forced s | final forced s |
|---|---:|---:|---:|---:|---:|---:|---:|
| gate2 | 4 | 713 | 6.42 | 3.42 | -46.8% | 7.86 | 3.33 |
| Danmaku License | 10 | 1423 | 32.37 | 4.99 | -84.6% | 37.90 | 4.51 |
| Baka Bastard | 10 | 1938 | 57.51 | 6.45 | -88.8% | 110.85 | 6.17 |
| IN 12 | 15 | 3410 | 208.35 | 10.98 | -94.7% | 198.91 | 9.94 |
| sum | | | 304.64 | 25.83 | -91.5% | | |

With rigid bodies in the scene, B0's full-scene evaluation steps the rigid body world on every frame, and that dominates its solve (46-260 s). The isolated closure keeps rigid bodies out, so the final tree's cost barely changes with physics (Baka Bastard without physics: 4.9 s cold). No-change resyncs stay under 0.8 s. Peak memory is 4-14% higher (isolated scene and cache). Outliers: one forced resync of IN 12 at 27.9 s, and earlier one of Baka Bastard at 14.8 s, neither reproduced in repeat runs. Correctness on these plans was not compared against MMD, because stand-ins replace the original models. Accessories were not part of the run: Core does not build them, and FX does.

### Dependency-aware partial re-solves: investigated, not implemented

Question: when a parent model's motion changes, can the dependent solve redo only the affected frames instead of all of them (P2-B invalidates everything)?

What an edit changes: a PMM edit reaches Blender as a reimport whose curves are identical except near the edited keys. On a Bezier curve, a changed key alters values only between its neighbouring keys, so the frames whose parent pose changed can be bounded exactly by diffing the old and new key arrays of every curve in the dependency closure. Once the frames are known, the solve itself can run on any subset of frames (`evaluate_frames` already takes a frame list), and the cache already keeps every solved frame.

Where that bound is not safe, so a full solve has to stay the fallback:

- history-dependent evaluation in the closure: rigid bodies, cloth, simulation nodes; a change at frame k changes every frame after k;
- drivers that read the frame or run Python, F-curve modifiers (cycles, noise), NLA changes (strip timing, blending, extrapolation), and extrapolation past a first or last key, which spreads one edit over the whole timeline;
- anything outside the curves: constraints, rest pose, mapping, bindings or object transforms (the cache key already sees these and misses);
- quaternion sign continuity: each solved frame's sign follows the previous frame, so a partial run must join its cached neighbours with matching sign, or fall back.

A cheap safety net exists: the cache's spot check could re-evaluate a few frames just outside the bound and fall back on any mismatch.

Why it is not worth doing now: P2-B re-syncs the dependent model's whole motion part, reimporting it with mmd_tools before the solve, so a partial solve saves only part of one model's sync. On the gate edit the solve is about half of a dependent model's time (gate2: 1.8 s of 3.4 s cold, over three solves). Turning it into a real saving also means keeping the dependent model's action instead of reimporting it, which changes P2-B's part model. Recommendation: revisit if users report slow incremental syncs on long bound projects. The first step would be the exact curve diff, with full re-solve as the fallback for every case listed above.

## Handoff

What changed in the tree (uncommitted):

- `common/animation.py`: `merge_linear_keys` (bulk key merge).
- `common/timing.py`: `count` / `accumulate` counters in the timing summary line.
- `sync/outside_parent.py`: dependency closure, isolated solve scene, bulk writer, cache use. Module flags `ISOLATE` and `CACHE` (both on) exist only so tests and benchmarks can compare against the full-scene and uncached paths.
- `sync/solve_cache.py`: the session solve cache. `__init__.py` clears it on load_post and unregister.
- `sync/motion.py`, `sync/plan.py`, `sync/engine.py`: P2-B (`outside_parent_models`, `outside_parent_motion`, `outside_parent_order`, `bone_fingerprint` shared per plan).
- `pmm.py`: `Reader.records` and the block reads.
- Tests: `tests/t_outside_parent.py` (new), P2-B checks in `tests/t_sync_plan.py`. The bench tools are in `tests/perf/`.

- 2026-11-20: `sync/outside_parent.py` (edge keys, switch and straddle rules, constraint placement and armature-space inverse), `common/animation.py` (`merge_linear_keys` takes several spans, holds and auto clamped span handles), `tests/t_outside_parent.py` (MMD-rule reference, random subframes), `--physics` in the bench tools.

Validation status: checked against MMD 9.32 playback, visually and by silhouette, on the gate project (`docs/VALIDATION.md`, 2026-11-20). All of it ran in headless Blender 5.3.0 with mmd_tools 4.5.14; an interactive Blender session is still pending. The corpus timings use stand-in models.

Remaining bottlenecks, largest first (swing, B3 cold 7.0 s):

1. Frame evaluation of the bound armature, 4.56 s for 3346 frames (about 1.36 ms a frame) on a 315-bone rig with IK and mmd_tools constraints, already reduced to a 2-object closure. Further gains need fewer evaluated frames, which P2-A showed the corpus does not offer, or a solver that does not use the depsgraph, which P1-C showed real MMD rigs rule out.
2. The mmd_tools VMD importer, 1.71 s on swing and the bulk of every other project's cold sync. Most of it is mmd_tools' per-key interpolation setup (`__setInterpolation`). This is upstream code; a proposal for mmd_tools (bulk handle writes) is the route, not a change here. P1-B's still-track skip would only become safe if mmd_tools wrote equivalent rest curves itself.
3. No-change resync of plan 01, 0.26 s: parse, mapping and fingerprints of 12 model slots. Small in absolute terms.

Reproduce (paths are placeholders):

```
python -I tests/perf/run_bench.py --blender BLENDER --mmd-tools MMD_TOOLS_DIR --out OUT \
    --variant b0=B0.zip --variant b3=B3.zip --plan PLAN_DIR/07_plan.json [--plan ...] [--dump]
python -I tests/perf/compare_output.py OUT/b0/07_dump.npz OUT/b3/07_dump.npz
python -I tests/perf/report_tables.py OUT/summary.json
blender --background --factory-startup --python tests/perf/bench_parse.py -- OUT.json PLAN...
blender --background --factory-startup --python tests/perf/make_edited_pmm.py -- PLAN OUT.pmm MODEL BONE [DX] [NEAR_FRAME]
```
