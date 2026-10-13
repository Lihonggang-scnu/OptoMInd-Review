# TMM research and design result

## Problem interpretation

Design a planar two-layer antireflection coating on a semi-infinite glass substrate (refractive index 1.52, lossless) with air incidence (index 1.0). The stack must consist of exactly one layer of SiO2 (n=1.46) and one layer of TiO2 (n=2.40) in either order. Layer thicknesses are constrained between 40 nm and 160 nm. The objective is to minimize the arithmetic mean power reflectance for s-polarized light at normal incidence over the wavelength range 450 nm to 700 nm (sampled every 5 nm). A mean reflectance below 0.03 is a stated soft target preference. The method must use deterministic Transfer Matrix Method (TMM) only. The optimization process allows up to two research rounds with a maximum of 64 solver evaluations per round using seed 17. Final validation requires observing the full spectrum and performing an independent numeric check before claiming the target is met.

## Method research

- No reusable literature method was available; planning used explicit optical theory assumptions.

## Routes executed

- **Optimization of TiO2/SiO2/Glass Stack** — status `budget_exhausted`, source `planned`, verified candidates 0, best soft score n/a.
- **Relaxed Bounds Grid-Search for TiO2/SiO2 AR Coating** — status `not_run`, source `planned`, verified candidates 0, best soft score n/a.

## Planning-source comparison

The memory-only control and literature-planned routes are separated by source. Frozen-standard scores are used for a cross-source verdict only when both arms have scoreable representatives; score deltas inside a route are diagnostics.
- Frozen-standard comparison: `control_or_literature_arm_missing`.
- `planned`: 1 route(s), 0 with verified candidates, 2 executed round(s).

## Recommended candidate portfolio

No candidate passed deterministic physics verification.

## Feedback and stopping decision

The loop stopped with `stop_best_effort`: The bounded research budget ended; verified candidates are preserved.

## Limitations

- The optimal layer ordering (SiO2-first vs. TiO2-first) remains unknown and is the primary variable to be resolved by these routes.
- Whether the 0.03 mean reflectance target is physically achievable with only two layers and the given thickness constraints is unknown.
- Some planned routes produced recorded failures or capability limits; see the iteration artifacts.
- No traceable literature item directly influenced the executed route; the result is theory-guided.

## Literature provenance

- No literature reference was used by the executed route.
