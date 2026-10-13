# TMM research and design result

## Problem interpretation

Design a planar two-layer antireflection coating on a semi-infinite glass substrate (refractive index 1.52, lossless) with air incidence (refractive index 1.0). The stack must consist of exactly one layer of SiO2 (n=1.46) and one layer of TiO2 (n=2.40) in either order. Each layer thickness is constrained between 40 nm and 160 nm. The objective is to minimize the arithmetic mean power reflectance for s-polarized light at normal incidence, sampled from 450 nm to 700 nm with 5 nm spacing, using deterministic Transfer Matrix Method calculations. A mean reflectance below 0.03 is a soft scoring preference. The optimization allows at most two research rounds with one route and up to 64 solver evaluations per round using seed 17. Results must be validated by spectrum and numeric check before claiming target achievement.

## Method research

- No reusable literature method was available; planning used explicit optical theory assumptions.

## Routes executed

- **Optimization of SiO2-over-TiO2 Two-Layer AR Stack** — status `failed`, source `planned`, verified candidates 0, best soft score n/a.
- **Grid Search Optimization of Air|SiO2|TiO2|Glass Two-Layer AR Coating** — status `failed`, source `planned`, verified candidates 0, best soft score n/a.

## Planning-source comparison

The memory-only control and literature-planned routes are separated by source. Frozen-standard scores are used for a cross-source verdict only when both arms have scoreable representatives; score deltas inside a route are diagnostics.
- Frozen-standard comparison: `control_or_literature_arm_missing`.
- `planned`: 1 route(s), 0 with verified candidates, 2 executed round(s).

## Recommended candidate portfolio

No candidate passed deterministic physics verification.

## Feedback and stopping decision

The loop stopped with `stop_best_effort`: The bounded research budget ended; verified candidates are preserved.

## Limitations

- Which layer ordering (SiO2/TiO2 vs TiO2/SiO2) provides the global minimum reflectance under the specific 40-160 nm thickness constraints.
- Whether the soft target of mean reflectance < 0.03 is physically achievable with only two layers of these specific materials.
- Some planned routes produced recorded failures or capability limits; see the iteration artifacts.
- No traceable literature item directly influenced the executed route; the result is theory-guided.

## Literature provenance

- No literature reference was used by the executed route.
