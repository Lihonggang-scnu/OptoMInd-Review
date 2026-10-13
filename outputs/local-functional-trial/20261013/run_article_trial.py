"""One bounded real-model Article harness case; outputs remain outside source."""
import json
import os
import sys
from pathlib import Path
from trial_transport import ROOT, TrialClient, install_article_clients, write_json

os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
os.environ['OPTOMIND_ECONOMY_TEXT_CEILING'] = '0'
install_article_clients()
from optomind_optics.harness.problem_analyzer import QwenTMMProblemAnalyzer
from optomind_optics.harness.strategy_planner import QwenTMMStrategyPlanner
from optomind_optics.harness.task_compiler import QwenTMMTaskCompiler
from optomind_optics.harness.method_research import TMMMethodResearchAdapter
from optomind_optics.harness.orchestrator import TMMHarnessOrchestrator, TMMHarnessConfig
from optomind_optics.harness.research_orchestrator import TMMResearchHarness, TMMResearchHarnessConfig

class TrialSolver(TMMHarnessOrchestrator):
    def run(self, task):
        write_json(self.work_dir / 'TRIAL_PREFLIGHT_TASK.json', task.model_dump(mode='json'))
        if task.budget.maximum_forward_evaluations > 64:
            raise ValueError('Model-compiled solver budget exceeds frozen 64-evaluation ceiling')
        if task.uncertainty.random_seed != 17:
            raise ValueError('Model-compiled seed differs from frozen seed 17')
        for experiment in task.experiments:
            simulation = experiment.tmm_task.get('simulation', experiment.tmm_task)
            layers = simulation['stack']['layers']
            if len(layers) != 2 or sorted(layer.get('constant_n', -1) for layer in layers) != [1.46, 2.4]:
                raise ValueError('Proposal failed constant-index, two-layer physical contract')
            if any(layer.get('material') or layer.get('constant_k', 0) != 0 for layer in layers):
                raise ValueError('Dispersive or absorbing layer outside frozen physical contract')
        return super().run(task)

class TrialCompiler(QwenTMMTaskCompiler):
    def compile(self, *args, **kwargs):
        result = super().compile(*args, **kwargs)
        if result.task is None:
            return result
        # Budget is a program-owned OpticalDesignTask field, absent from the
        # model draft schema. Bind the predeclared trial policy before use.
        task = result.task
        experiments = []
        for experiment in task.experiments:
            payload = json.loads(json.dumps(experiment.tmm_task))
            if experiment.mode.value == 'optimize':
                # The gradient adapter does not enforce the forward cap passed
                # by the scheduler. This pilot bounds every trajectory before
                # execution; keep the 64-call ceiling and the fixed seed.
                payload['optimizer'].update(method='adam', max_steps=8, starts=1, seed=17,
                                            quantization_nm=None)
            experiments.append(experiment.model_copy(update={'tmm_task': payload}))
        task = task.model_copy(update={
            'experiments': tuple(experiments),
            'budget': task.budget.model_copy(update={'maximum_forward_evaluations': 64, 'wall_time_seconds': 90}),
            'uncertainty': task.uncertainty.model_copy(update={'random_seed': 17, 'thickness_sigma_nm': 0.0,
                'thickness_samples': 1, 'maximum_material_scenarios': 1, 'material_dataset_policy': 'resolved_only'}),
        })
        return result.model_copy(update={'task': task})

def main():
    protocol = json.loads((ROOT / 'TRIAL_PROTOCOL.json').read_text(encoding='utf-8'))
    output = ROOT / 'article' / 'run-06'
    if output.exists():
        raise RuntimeError('Refusing to overwrite a paid trial; use an explicit new attempt')
    config = TMMResearchHarnessConfig(
        maximum_iterations=2, maximum_initial_routes=1, max_routes=1,
        max_rounds_per_route=2, minimum_rounds_before_llm_stop=1,
        maximum_refinement_rounds=1, maximum_method_research_rounds=1,
        wall_time_seconds=1200, online_method_research=False,
        use_qwen_policy_inside_tmm=False, route_planning_enabled=False,
        control_route_enabled=False, parallel_tmm=False, confirmation_gates=[],
        research_mode='bounded_design',
    )
    harness = TMMResearchHarness(
        output, run_id='real-ar-two-layer-20261013',
        problem_analyzer=QwenTMMProblemAnalyzer(client=TrialClient(model='qwen3.5-plus', lane='article-analysis')),
        method_researcher=TMMMethodResearchAdapter(review_kb_paths=[], online_enabled=False, require_method_guidance=False),
        strategy_planner=QwenTMMStrategyPlanner(client=TrialClient(model='qwen3.5-plus', lane='article-strategy')),
        task_compiler=TrialCompiler(client=TrialClient(model='qwen3.7-flash', lane='article-compile'), transport_retries=0),
        tmm_harness_factory=lambda directory, run_id: TrialSolver(directory, run_id=run_id,
            config=TMMHarnessConfig(enable_global_optimizer=False, global_optimizer_forward_evaluations=32, maximum_candidates_per_experiment=1, robustness_candidate_limit=1, use_qwen_policy=False)),
        config=config,
    )
    write_json(ROOT / 'article' / 'RUNTIME_IDENTITY.json', {'python': sys.executable,
        'source': __import__('optomind_optics').__file__, 'tmm': __import__('tmm_engine').__file__,
        'profile': config.model_dump(mode='json'), 'protocol': protocol['article']})
    result = harness.run(protocol['article']['question'])
    write_json(ROOT / 'article' / 'RESULT.json', result.model_dump(mode='json'))
    print(json.dumps({'status': result.status, 'stage': result.stage, 'output': str(output)}, ensure_ascii=False))

if __name__ == '__main__':
    main()
