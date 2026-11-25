"""Run the separately authorized fixed AR/CH test scope, once per model."""
from pathlib import Path
import argparse
import json
import gc

from .runtime import configure_environment, exclusive, now
from .durable import atomic_json
from .diagnostic_support import read
from .recognition_release import validate_authorized_scope


def run(experiment):
    from .delivery import selection_lock, evaluate_run
    from .run_locations import run_folder
    import torch
    experiment=Path(experiment).resolve()
    scope=validate_authorized_scope(experiment)
    if not (experiment/'selection-lock.json').exists():selection_lock(experiment)
    with exclusive(experiment/'recognition-test-runner.lock'):
        try:
            for number,identifier in enumerate(scope['recognition']):
                atomic_json(experiment/'recognition-test-state.json',{'state':'running','updated_utc':now(),
                    'active':identifier,'completed':number,'total':12,'task_type':'frozen_test_evaluation'})
                target=run_folder(experiment,identifier)/'evaluation/test/summary.json'
                if target.exists():
                    result=read(target)
                    if result['checkpoint_sha256']!=scope['recognition'][identifier]['checkpoint_sha256']:
                        raise ValueError('已存在测试结果身份不同，拒绝覆盖')
                else:result=evaluate_run(experiment,identifier,'test')
                print(json.dumps({'id':identifier,'dice':result['dice'],'images':result.get('images'),
                                  'completed':number+1},ensure_ascii=False),flush=True)
                gc.collect();torch.cuda.empty_cache()
            atomic_json(experiment/'recognition-test-state.json',{'state':'completed','updated_utc':now(),
                        'completed':12,'total':12,'task_type':'frozen_test_evaluation'})
            stages=read(experiment/'release-stages.json',{})
            atomic_json(experiment/'release-stages.json',{**stages,'M9':'completed'})
        except Exception as exc:
            atomic_json(experiment/'recognition-test-state.json',{'state':'failed','updated_utc':now(),
                'active':identifier,'error':str(exc),'completed':number,'total':12,
                'recovery':'保留首次结果，不据测试修改模型、精度、阈值或配置'})
            raise


if __name__=='__main__':
    configure_environment()
    p=argparse.ArgumentParser();p.add_argument('--experiment',type=Path,required=True)
    run(p.parse_args().experiment)
