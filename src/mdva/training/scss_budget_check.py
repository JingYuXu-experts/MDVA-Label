"""One bounded continuation after rejected LR change; preserve earlier failed gates."""
from __future__ import annotations
import argparse,json
from pathlib import Path
from . import scss_rate_control as executor
from .runtime import digest,now,output_path
from .durable import atomic_json

ROOT=Path(__file__).resolve().parents[3]


def prepare(dataset,source,target):
    source=Path(source).resolve();target=output_path(target,dataset)
    if target.exists():raise ValueError('新预算版本不得覆盖旧结果')
    high=json.loads((source/'rate-control/GN-lr0.001/summary.json').read_text(encoding='utf-8'))
    low=json.loads((source/'rate-control/GN-lr0.0001/summary.json').read_text(encoding='utf-8'))
    if high['state']!='completed' or low['state']!='completed' or high['last']['dice']<=low['last']['dice']:
        raise ValueError('原学习率继续的前提不成立')
    target.mkdir(parents=True);(target/'protocol.json').write_bytes((source/'protocol.json').read_bytes())
    if (source/'page-progress.json').exists():(target/'page-progress.json').write_bytes((source/'page-progress.json').read_bytes())
    initial=source/'rate-control/GN-lr0.001/last.pt'
    p={'created_utc':now(),'scope':'CH train16 bounded convergence check, no hyperparameter change',
       'initial':str(initial),'initial_sha256':digest(initial),'base_protocol_sha256':digest(target/'protocol.json'),
       'arms':{'GN-lr0.001':.001},'steps':500,
       'gate':{'last_three_train_dice_min':.85,'finite':True},
       'hypothesis':'Original LR beat lower LR and still improves. Test finite additional update budget, not another architecture or parameter search.',
       'previous_gate_state':'Original200-step probes and LR-improvement gate remain failed; this is a new convergence-budget protocol.',
       'source_high_summary_sha256':digest(source/'rate-control/GN-lr0.001/summary.json'),
       'source_low_summary_sha256':digest(source/'rate-control/GN-lr0.0001/summary.json'),
       'code':{n:digest(Path(__file__).with_name(n)) for n in ('scss_budget_check.py','scss_rate_control.py','scss_study.py','scss_models.py','fast_data.py','reviewed_train.py','metrics.py')},
       'validation_accessed':False,'test_accessed':False,'models_replaced':False,
       'index_schedule':'Additional block uses the executor fixed300+step sample-order schedule; no new data, threshold or augmentation.'}
    (target/'rate-control').mkdir();atomic_json(target/'rate-control/protocol.json',p);return p


def run(dataset,target,resume=False):
    target=Path(target);p=json.loads((target/'rate-control/protocol.json').read_text(encoding='utf-8'))
    original=executor.emit
    def progress(root,arm,phase,state,step,total,**details):
        details['stage_note']='收敛预算检查：额外最多500次更新；原200步门槛记录保留。'
        return original(root,arm,'16图学习率控制·收敛预算',state,p['steps'] if state=='completed' else step,p['steps'],**details)
    try:
        executor.emit=progress
        r=executor.run(dataset,target,'GN-lr0.001',resume)
    finally:executor.emit=original
    if r['state']=='completed':
        h=json.loads((target/'rate-control/GN-lr0.001/history.json').read_text(encoding='utf-8'))
        passed=all(row['dice']>=p['gate']['last_three_train_dice_min'] for row in h[-3:])
        atomic_json(target/'decision.json',{'state':'completed','gate_passed':passed,'last_three_train_dice':[row['dice'] for row in h[-3:]],
            'protocol_sha256':digest(target/'rate-control/protocol.json'),'scope':'train fit only, no generalization claim',
            'decision':'eligible_for_preregistered_full_data_pilot' if passed else 'do_not_start_full_data_training_from_this_gate'})
    return r


if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment();p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','run']);p.add_argument('--dataset',required=True);p.add_argument('--source');p.add_argument('--target',required=True);p.add_argument('--resume',action='store_true');a=p.parse_args()
    r=prepare(a.dataset,a.source,a.target) if a.action=='prepare' else run(a.dataset,a.target,a.resume)
    print(json.dumps({'state':r.get('state'),'dice':r.get('last',{}).get('dice')}),flush=True)
