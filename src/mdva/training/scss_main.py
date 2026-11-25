"""Full-data SCSS GN candidate after convergence gate; frozen tests stay closed."""
from __future__ import annotations
import argparse,json,time
from functools import partial
from pathlib import Path
import torch
from . import reviewed_train as engine
from .scss_models import architecture
from .fast_data import CachedDataset,loader_for
from .runtime import digest,now,output_path
from .durable import atomic_json
from .scss_study import emit
ROOT=Path(__file__).resolve().parents[3]
BASE_IDENTITY=engine.code_identity


def identity():
    return {**BASE_IDENTITY(),**{n:digest(Path(__file__).with_name(n)) for n in ('scss_main.py','scss_models.py','scss_study.py','fast_data.py')}}


def prepare(dataset,convergence,study):
    convergence=Path(convergence);study=output_path(study,dataset)
    if study.exists():raise ValueError('正式开发版本已存在，不能覆盖')
    decision=json.loads((convergence/'decision.json').read_text(encoding='utf-8'))
    if decision.get('gate_passed') is not True:raise ValueError('训练可学习性收敛门槛未过，不启动全量训练')
    protocol_file=convergence/'rate-control/protocol.json'
    if decision['protocol_sha256']!=digest(protocol_file):raise ValueError('收敛核验身份不同')
    config={'id':'CH-SCSS-GN-s2026','category':'CH','model':'scss','normalization':'group','width':32,'size':512,
        'loss':'bce_dice','seed':2026,'augment':True,'station_balance':False,'micro_batch':2,'effective_batch':8,
        'max_epochs':40,'min_epochs':20,'patience':12,'threshold':.5,'data_workers':0,
        'cache_directory':str(ROOT/'out/training-cache/20261116-v1'),'study':'scss-full-data-20261117'}
    protocol={'created_utc':now(),'config':config,'code':identity(),'dataset':str(Path(dataset).resolve()),
        'manifest_sha256':digest(Path(dataset)/'manifest-hashes.json'),'learning_gate_sha256':digest(convergence/'decision.json'),
        'initialization':'fresh Xavier, no small-sample warm-start','first_budget_epochs':8,
        'optimization':'Adam1e-3, ReduceLROnPlateau loss patience4 factor0.5 min1e-5; FP32 forward; disabled GradScaler; finite gradient guard',
        'scope':'2410 train/526 validation only; original calibration/test sealed; exploratory development, no automatic deployment',
        'budget_gate':{'validation_dice_at8':.75,'last4_minus_first4_mean_dice':.02,'or_validation_dice_at8':.79},
        'final_practical_gate':{'dice':.8207132495672046,'precision':.8317458251862891,'half_coverage_fraction':.7087941787941788},
        'basis':'SCSS solar single-channel structure; GroupNorm local adaptation. Earlier BN/GN, dropout and LR outcomes remain archived.'}
    study.mkdir(parents=True);atomic_json(study/'protocol.json',protocol)
    return protocol


def train(study,resume=False,stop_after=8):
    study=Path(study).resolve();p=json.loads((study/'protocol.json').read_text(encoding='utf-8'))
    if p['code']!=identity() or p['manifest_sha256']!=digest(Path(p['dataset'])/'manifest-hashes.json'):raise ValueError('正式开发代码或数据改变')
    config=p['config'];run=study/'run'
    if config['study']!='scss-full-data-20261117' or config['normalization']!='group':raise ValueError('未登记模型')
    original=(engine.architecture,engine.code_identity,engine.SegmentationDataset,engine.loader_for,engine.atomic_json,engine.torch)
    last={'time':0.,'epoch':-1}
    def tracked(path,value):
        original[4](path,value)
        if Path(path).name=='status.json' and value.get('id')==config['id']:
            epoch=value.get('completed_epoch',0);state=value['state']
            if time.monotonic()-last['time']>10 or epoch!=last['epoch'] or state!='running':
                shown='completed' if state=='paused' and value.get('reason')=='本次运行epoch预算用尽' else state
                emit(study,'SCSS-GN-main','完整训练·首阶段预算',shown,epoch,stop_after,
                     validation_dice=value.get('best_validation_dice'),resource=value.get('resource'),
                     message=value.get('reason','从头训练；每轮完整验证，模型尚未替换。'))
                last.update(time=time.monotonic(),epoch=epoch)
    class CheckedAdam(torch.optim.Adam):
        def step(self,*args,**kwargs):
            for g in self.param_groups:
                for parameter in g['params']:
                    if parameter.grad is not None and not torch.isfinite(parameter.grad).all():raise ValueError('非有限梯度，保留上一完整检查点')
            return super().step(*args,**kwargs)
    class Optim:
        Adam=CheckedAdam
        def __getattr__(self,name):return getattr(torch.optim,name)
    class Amp:
        @staticmethod
        def GradScaler(*args,**kwargs):return torch.amp.GradScaler('cuda',enabled=False)
        def __getattr__(self,name):return getattr(torch.amp,name)
    class Torch:
        optim=Optim();amp=Amp()
        def __getattr__(self,name):return getattr(torch,name)
    try:
        engine.architecture=architecture;engine.code_identity=identity
        engine.SegmentationDataset=partial(CachedDataset,cache_root=Path(config['cache_directory']));engine.loader_for=loader_for
        engine.atomic_json=tracked;engine.torch=Torch()
        torch.backends.cudnn.allow_tf32=False;torch.backends.cuda.matmul.allow_tf32=False
        return engine.train(config,p['dataset'],run,resume,stop_after=stop_after)
    finally:
        engine.architecture,engine.code_identity,engine.SegmentationDataset,engine.loader_for,engine.atomic_json,engine.torch=original


if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment();p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','train']);p.add_argument('--dataset');p.add_argument('--convergence');p.add_argument('--study',required=True);p.add_argument('--resume',action='store_true');p.add_argument('--stop-after',type=int,default=8);a=p.parse_args()
    r=prepare(a.dataset,a.convergence,a.study) if a.action=='prepare' else train(a.study,a.resume,a.stop_after)
    print(json.dumps({'state':r.get('state'),'epoch':r.get('completed_epoch')}),flush=True)
