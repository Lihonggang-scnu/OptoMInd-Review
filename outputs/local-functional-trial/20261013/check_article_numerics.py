import json
import numpy as np
from tmm import coh_tmm
from trial_transport import ROOT, write_json
run = ROOT / 'article/run-06'
history = json.loads((run / 'ITERATION_HISTORY.json').read_text(encoding='utf-8'))
first = history[0]
candidate = min(first['candidate_summaries'], key=lambda row: row['objective_report']['target_attainment']['fixedscore.mean_reflectance_450_700nm']['observed'])
path = next(run.glob('iterations/iteration_01/tmm_run/experiments/*/candidates/' + candidate['candidate_id'] + '/SIMULATION_RESULT.json'))
actual = json.loads(path.read_text(encoding='utf-8'))
wavelengths = np.asarray(actual['wavelengths_nm'])
assert np.array_equal(wavelengths, np.arange(450., 701., 5.))
assert candidate['layer_materials'] == ['n=1.46', 'n=2.4']
thickness = candidate['thicknesses_nm']
assert len(thickness) == 2 and all(40 <= value <= 160 for value in thickness)
reference = np.asarray([coh_tmm('s', [1., 1.46, 2.4, 1.52], [np.inf, *thickness, np.inf], 0., wavelength)['R'] for wavelength in wavelengths])
observed = np.asarray(actual['channels']['angle=0|pol=s']['R'])
error = float(np.max(np.abs(reference-observed)))
assert error <= 1e-8, error
np.savetxt(ROOT / 'article/INDEPENDENT_SPECTRUM.csv', np.column_stack([wavelengths, observed, reference]),
           delimiter=',', header='wavelength_nm,VeriTMM_R,independent_tmm_R', comments='')
report = {'candidate_id': candidate['candidate_id'], 'thicknesses_nm': thickness,
    'layer_indices': [1.46, 2.4], 'mean_R_51_samples': float(reference.mean()),
    'target_mean_R': 0.03, 'target_met': bool(reference.mean() < 0.03),
    'max_absolute_spectrum_error': error, 'tolerance': 1e-8,
    'round_1_forward_evaluations': first['budget_usage']['forward_evaluations'],
    'round_2': {k: history[1].get(k) for k in ('route_title', 'compilation_status','run_status')},
    'objective_report_band_mean_R': candidate['objective_report']['target_attainment']['fixedscore.mean_reflectance_450_700nm']['observed'],
    'independent_trapezoidal_band_mean_R': float(np.trapezoid(reference, wavelengths)/(wavelengths[-1]-wavelengths[0])),
    'overall_acceptance': 'partial: actual spectrum independently verified; objective uses trapezoidal band mean rather than requested arithmetic sample mean; feedback proposed out-of-contract three-layer change and was blocked',
    'numeric_grid_note': 'Both means use the same 51-point grid. The difference is quadrature weighting, not floating-point error. The frozen arithmetic mean is independently reported; no tolerance was relaxed.'}
write_json(ROOT / 'article/NUMERIC_ACCEPTANCE.json', report)
print(json.dumps(report))
