# TMM research and design result

## Problem interpretation

Design a planar two-layer antireflection coating on a semi-infinite glass substrate (refractive index 1.52, lossless) with air incidence (index 1.0). The stack must consist of exactly one layer of lossless SiO2 (n=1.46) and one layer of lossless TiO2 (n=2.40) in either order. Layer thicknesses are constrained between 40 nm and 160 nm. The objective is to minimize the arithmetic mean power reflectance for s-polarized light at normal incidence over the wavelength range 450 nm to 700 nm (sampled every 5 nm). A mean reflectance below 0.03 is a soft scoring preference. The method must use deterministic Transfer Matrix Method (TMM) only. The optimization allows up to two research rounds with one route and a maximum of 64 solver evaluations per round using seed 17. Decisions to adjust or stop must be based on observed results and independent numeric checks.

## Method research

- No reusable literature method was available; planning used explicit optical theory assumptions.

## Routes executed

- **Optimization of TiO2/SiO2/Glass High-Low Antireflection Stack** — status `completed`, source `planned`, verified candidates 2, best soft score 0.9812.
- **Expansion to 3-Layer SiO2/TiO2 Antireflection Stack** — status `failed`, source `planned`, verified candidates 0, best soft score n/a.

## Planning-source comparison

The memory-only control and literature-planned routes are separated by source. Frozen-standard scores are used for a cross-source verdict only when both arms have scoreable representatives; score deltas inside a route are diagnostics.
- Frozen-standard comparison: `control_or_literature_arm_missing`.
- `planned`: 1 route(s), 1 with verified candidates, 2 executed round(s).

## Recommended candidate portfolio

Every route was measured on the same quantities, chosen from the request before any route ran, and ranked by the one fixed expression `-mean_reflectance_450_700nm`, so these rows are directly comparable.
- **Best performance**: `opt_min_r_mean_s_pol_450_700__grad__9fbba01872a2` from Optimization of TiO2/SiO2/Glass High-Low Antireflection Stack.
- **Most robust**: `opt_min_r_mean_s_pol_450_700__grad__9fbba01872a2` from Optimization of TiO2/SiO2/Glass High-Low Antireflection Stack.
- **Simplest verified**: `opt_min_r_mean_s_pol_450_700__baseline` from Optimization of TiO2/SiO2/Glass High-Low Antireflection Stack.

- `opt_min_r_mean_s_pol_450_700__grad__9fbba01872a2` stack (2 layers): n=1.46 / n=2.4.
- `opt_min_r_mean_s_pol_450_700__baseline` stack (2 layers): n=1.46 / n=2.4.

| Candidate | Route | Frozen standard score | Comparable spectral summary | Robustness | Thicknesses (nm) |
|---|---|---:|---|---:|---|
| `opt_min_r_mean_s_pol_450_700__grad__9fbba01872a2` | Optimization of TiO2/SiO2/Glass High-Low Antireflection Stack | -0.0188 | mean_reflectance_450_700nm=0.0188 | 0.7267 | 90.54, 109.55 |
| `opt_min_r_mean_s_pol_450_700__baseline` | Optimization of TiO2/SiO2/Glass High-Low Antireflection Stack | -0.0383 | mean_reflectance_450_700nm=0.0383 | n/a | 100.00, 100.00 |

## Manufacturing uncertainty

- `opt_min_r_mean_s_pol_450_700__grad__9fbba01872a2`: model=absolute_normal, sigma_nm=0.000, relative_fraction=0.0000, common_angle_bound_deg=0.000, samples=1, failed=0; target_domain_max_R mean=0.028054 ± 0.000000; target_domain_mean_R mean=0.018800 ± 0.000000; target_domain_min_R mean=0.010264 ± 0.000000.

## Feedback and stopping decision

The loop stopped with `stop_completed`: Route route_01 left the race as stopped_round_limit: reached the per-route cap of 2 executed rounds

## Limitations

- Whether the 40-160 nm thickness window is sufficient to achieve the <0.03 mean reflectance target for either topology.
- Which of the two layer orders provides the global minimum for this specific material pair and bandwidth.
- Some planned routes produced recorded failures or capability limits; see the iteration artifacts.
- No traceable literature item directly influenced the executed route; the result is theory-guided.

## Literature provenance

- No literature reference was used by the executed route.
