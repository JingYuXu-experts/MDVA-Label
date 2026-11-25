"""Fresh full-data FL pilots with frozen train and development-validation data."""
import argparse,json,time
from pathlib import Path
import torch
from . import reviewed_train as engine
from .fl_author_model import AuthorGraph,loss_value,SOURCE
from .runtime import digest,now,output_path
from .durable import atomic_json
from .journal import append
ROOT=Path(__file__).resolve().parents[3]
BASE_IDENTITY=engine.code_identity

def identity():
    return {**BASE_IDENTITY(),**{n:digest(Path(__file__).with_name(n)) for n in ('fl_main.py','fl_author_model.py')}}

def prepare(rate,study):
    rate=Path(rate).resolve();report=json.loads((rate/'summary.json').read_text(encoding='utf-8'))
    if report['state']!='completed' or not report['learning_gate_passed']:raise ValueError('固定暗条学习门槛未通过，不启动完整训练')
    control=json.loads((rate/'protocol.json').read_text(encoding='utf-8'))
    if report['protocol_sha256']!=digest(rate/'protocol.json'):raise ValueError('学习门槛证据改变')
    parent=Path(control['parent']);base=json.loads((parent/'protocol.json').read_text(encoding='utf-8'))
    study=output_path(study,base['dataset'])
    if study.exists():raise ValueError('完整暗条试点使用新目录')
    config={'id':'FL-author-BN-w10-lr1e-4-s2026','category':'FL','model':'fl_author_unet','width':32,'size':512,'loss':'bce_dice','seed':2026,
        'augment':True,'station_balance':True,'micro_batch':2,'effective_batch':8,'max_epochs':40,'min_epochs':20,'patience':12}
    plan={'created_utc':now(),'config':config,'dataset':base['dataset'],'data_identity':engine.data_identity(base['dataset']),
        'code':identity(),'source_provenance_sha256':digest(SOURCE/'provenance.json'),'learning_gate_sha256':digest(rate/'summary.json'),
        'first_budget_epochs':8,'optimizer':'Adam1e-4, existing loss-based ReduceLROnPlateau factor0.5/patience4, FP32 finite-gradient guard, no GradScaler skips',
        'initialization':'fresh seed2026; no16-image warm-start or pretrained weights',
        'scope':'Full FL train/development-validation only, no calibration/test; local author-backbone adaptation, not original weak-label1024 paper reproduction',
        'continue_gate':'After8, compare full validation Dice and station-wise errors against frozen original FL-F0-s2026; require at least3pp overall gain and no station drop above3pp before proposing more budget; no auto extension or release'}
    study.mkdir(parents=True);atomic_json(study/'protocol.json',plan);return plan


def prepare_from_validation(review, study):
    """Test scaling to full training data after measuring the small-cohort gap."""
    from .datasets import SegmentationDataset

    review = Path(review).resolve()
    report = json.loads((review/'summary.json').read_text(encoding='utf-8'))
    protocol = json.loads((review/'protocol.json').read_text(encoding='utf-8'))
    if (report['state'] != 'completed' or report['test_accessed'] or report['calibration_accessed']
            or report['protocol_sha256'] != digest(review/'protocol.json') or protocol['split'] != 'validation'):
        raise ValueError('完整开发验证证据不符')
    dataset = Path(protocol['dataset'])
    if protocol['manifest_sha256'] != digest(dataset/'manifest-hashes.json'):
        raise ValueError('训练数据清单改变')
    if any(r['metrics']['images'] != 182 for r in report['results'].values()):
        raise ValueError('必须完成182张开发验证图')
    study = output_path(study, dataset)
    if study.exists():
        raise ValueError('使用新的完整训练版本')
    if len(SegmentationDataset(dataset, 'FL', 'train')) != 3033:
        raise ValueError('完整训练清单数量不符')
    config = {'id':'FL-author-BN-w10-full-s2026', 'category':'FL', 'model':'fl_author_unet',
              'width':32, 'size':512, 'loss':'bce_dice', 'seed':2026, 'augment':True,
              'station_balance':True, 'micro_batch':2, 'effective_batch':8, 'max_epochs':40,
              'min_epochs':20, 'patience':12, 'lr':.001, 'positive_weight':10}
    plan = {'created_utc':now(), 'config':config, 'dataset':str(dataset), 'data_identity':engine.data_identity(dataset),
            'code':identity(), 'source_provenance_sha256':digest(SOURCE/'provenance.json'),
            'validation_evidence_sha256':digest(review/'summary.json'), 'first_budget_epochs':8,
            'initialization':'Fresh seed2026. No reuse of 16-image weights.',
            'rationale':'The16-image gate failed and stays recorded. A new full-data pilot tests generalization directly. BN+w10 at1e-3 scored higher on fixed development validation than its lower-LR counterpart. The whole recipe is compared to existing F0; no single-factor superiority claim.',
            'scope':'3033 FL training images plus182 development validation images only',
            'decision':'Review fixed8-epoch budget against original F0 with Dice, station results and region precision/recall before extending. No automatic release or test access.'}
    study.mkdir(parents=True)
    atomic_json(study/'protocol.json', plan)
    return plan

def train(study):
    study=Path(study).resolve();p=json.loads((study/'protocol.json').read_text(encoding='utf-8'))
    if p['code']!=identity() or p['data_identity']!=engine.data_identity(p['dataset']) or digest(SOURCE/'provenance.json')!=p['source_provenance_sha256']:raise ValueError('完整试点身份改变')
    config=p['config']
    learning_rate=config.get('lr', 1e-4)
    positive_weight=config.get('positive_weight', 10)
    run=study/'run';saved=engine.load_checkpoint(run/'last.pt') if (run/'last.pt').exists() else None
    completed=saved['epoch'] if saved else 0
    if completed>=8:return {'state':'budget_complete','completed_epoch':completed}
    if (run/'PAUSE').exists() or (study/'PAUSE').exists():raise ValueError('暂停标记仍在')
    original=(engine.architecture,engine.code_identity,engine.loss_value,engine.atomic_json,engine.torch)
    stamp={'time':0.}
    def tracked(path,value):
        original[3](path,value)
        if Path(path).name=='status.json' and (time.monotonic()-stamp['time']>10 or value['state']!='running'):
            record={'state':value['state'],'epoch':value.get('completed_epoch',0),'target':8,'best_dice':value.get('best_validation_dice'),
                'updated_utc':now(),'resource':value.get('resource'),'reason':value.get('reason'),'scope':'3033训练/182开发验证；非独立测试'}
            atomic_json(study/'page-progress.json',record)
            append(ROOT/'out/training-20261112-v2','development','暗条完整数据试点',f'{record["state"]} · {record["epoch"]}/8轮；开发验证，未替换应用模型。',details={'fl_full_study':record})
            stamp['time']=time.monotonic()
    class Adam(torch.optim.Adam):
        def __init__(self,params,lr=1e-3,**kw):super().__init__(params,lr=learning_rate,**kw)
        def step(self,*a,**kw):
            if any(v.grad is not None and not torch.isfinite(v.grad).all() for group in self.param_groups for v in group['params']):raise ValueError('非有限梯度')
            return super().step(*a,**kw)
    class Optim:
        def __getattr__(self,name):return Adam if name=='Adam' else getattr(torch.optim,name)
    class Amp:
        def GradScaler(self,*a,**kw):return torch.amp.GradScaler('cuda',enabled=False)
    class Torch:
        optim=Optim();amp=Amp()
        def __getattr__(self,name):return getattr(torch,name)
    try:
        engine.architecture=lambda config:(AuthorGraph('batch'),{'implementation':'pinned MIT author backbone, local solar recipe'})
        engine.code_identity=identity;engine.loss_value=lambda logits,target,kind:loss_value(logits,target,positive_weight)
        engine.atomic_json=tracked;engine.torch=Torch()
        torch.backends.cuda.matmul.allow_tf32=False;torch.backends.cudnn.allow_tf32=False
        return engine.train(p['config'],p['dataset'],run,bool(saved),stop_after=8-completed)
    finally:engine.architecture,engine.code_identity,engine.loss_value,engine.atomic_json,engine.torch=original

if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment();p=argparse.ArgumentParser();p.add_argument('action',choices=('prepare','prepare-from-validation','train'));p.add_argument('--rate');p.add_argument('--review');p.add_argument('--study',required=True);a=p.parse_args()
    if a.action=='prepare-from-validation':r=prepare_from_validation(a.review,a.study)
    elif a.action=='prepare':r=prepare(a.rate,a.study)
    else:r=train(a.study)
    print(json.dumps({'state':r.get('state')}))
