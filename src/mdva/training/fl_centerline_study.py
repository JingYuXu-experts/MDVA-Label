"""Controlled H-alpha filament fine-tuning with an optional soft centerline loss."""

from __future__ import annotations

import argparse
from collections import Counter,defaultdict
import json
from pathlib import Path

import numpy as np
from scipy.ndimage import label
from skimage.morphology import skeletonize
import torch
from torch.nn import functional as F

from . import reviewed_train as engine
from .ar_scale_study import ROOT
from .datasets import SegmentationDataset
from .diagnostic_support import connected_scores
from .durable import atomic_json
from .fl_author_model import AuthorGraph,SOURCE,loss_value
from .fl_pilot_review import region_summary
from .journal import append,read
from .metrics import binary_segmentation,segmentation_summary
from .runtime import Resources,configure_environment,digest,exclusive,now,output_path


BUDGET=3
STEPS=8
WEIGHT=.25
PER_STATION=128
ARMS=('control','centerline')
LEDGER=ROOT/'out/training-20261112-v2'
BASE_IDENTITY=engine.code_identity
CONNECTIVITY=np.ones((3,3),dtype=bool)


def code_identity():
    names=('fl_centerline_study.py','fl_author_model.py','fl_pilot_review.py','diagnostic_support.py')
    return {**BASE_IDENTITY(),**{name:digest(Path(__file__).with_name(name)) for name in names}}


def erosion(value):
    a=-F.max_pool2d(-value,(3,1),stride=1,padding=(1,0))
    b=-F.max_pool2d(-value,(1,3),stride=1,padding=(0,1))
    return torch.minimum(a,b)


def residual(value):
    opened=F.max_pool2d(erosion(value),3,stride=1,padding=1)
    return (value-opened).relu()


def soft_skeleton(values,steps=STEPS):
    line=residual(values)
    for _ in range(steps):
        values=erosion(values)
        next_layer=residual(values)
        line=line+(1-line)*next_layer
    return line


def centerline_loss(probability,reference):
    predicted=soft_skeleton(probability)
    with torch.no_grad():
        truth=soft_skeleton(reference)
    eps=1e-6
    dimensions=(1,2,3)
    precision=((predicted*reference).sum(dimensions)+eps)/(predicted.sum(dimensions)+eps)
    recall=((truth*probability).sum(dimensions)+eps)/(truth.sum(dimensions)+eps)
    return (1-2*precision*recall/(precision+recall).clamp_min(eps)).mean()


def sample_training(dataset):
    bands=defaultdict(list)
    for row in dataset.rows:
        bands[row['instrument']].append(row)
    rng=np.random.default_rng(2026)
    result=[]
    for _,rows in sorted(bands.items()):
        ordered=sorted(rows,key=lambda r:(r['time_utc'],r['uri']))
        if len(ordered)<PER_STATION:
            raise ValueError('不同台站需要相同的训练图数')
        for lane in np.array_split(np.arange(len(ordered)),8):
            result.extend(ordered[int(i)]['uri'] for i in sorted(rng.choice(lane,size=PER_STATION//8,replace=False)))
    return result


def prepare(origin,archive,audit,study):
    plan=read(origin/'protocol.json')
    check,diagnosis=read(archive/'summary.json'),read(audit/'summary.json')
    source=archive/'checkpoint.pt'
    if (any(r['state']!='completed' or r['test_accessed'] or r['calibration_accessed'] for r in (check,diagnosis))
            or check['plan_sha256']!=digest(origin/'protocol.json') or check['checkpoint_sha256']!=digest(source)
            or diagnosis['protocol_sha256']!=digest(audit/'protocol.json')
            or diagnosis['images_sha256']!=digest(audit/'images.json')
            or read(audit/'protocol.json')['input_checkpoint_sha256']!=digest(source)):
        raise ValueError('暗条起点或开发诊断身份不符')
    data=SegmentationDataset(plan['dataset'],'FL','train',512)
    protocol={'dataset':plan['dataset'],'origin_sha256':digest(origin/'protocol.json'),'checkpoint':str(source),
              'checkpoint_sha256':digest(source),'diagnosis_sha256':digest(audit/'summary.json'),
              'data_identity':engine.data_identity(plan['dataset']),'code':code_identity(),
              'backbone_sha256':digest(SOURCE/'provenance.json'),'train_selection':sample_training(data),
              'config':{**plan['config'],'augment':False,'station_balance':False,'micro_batch':1,'effective_batch':8,
                        'max_epochs':BUDGET,'min_epochs':BUDGET,'lr':1e-4},
              'soft_cldice':{'iterations':STEPS,'loss_weight':WEIGHT,
                             'source':'https://openaccess.thecvf.com/content/CVPR2021/html/Shit_clDice_-_A_Novel_Topology-Preserving_Loss_Function_for_Tubular_Structure_CVPR_2021_paper.html'},
              'test_accessed':False,'calibration_accessed':False}
    study=output_path(study,plan['dataset'])
    if any(study.is_relative_to(p) for p in (origin,archive,audit)):
        raise ValueError('暗条训练需要独立输出')
    marker=study/'protocol.json'
    if marker.is_file() and read(marker)!=protocol:
        raise ValueError('暗条配对训练的来源或方案已经改变')
    atomic_json(marker,protocol)
    saved=engine.load_checkpoint(source)
    if saved['identity']!=read(origin/'run/identity.json') or saved['epoch']!=check['epoch']:
        raise ValueError('暗条归档检查点身份不符')
    return protocol,saved['model'],study


def train(protocol,weights,study,arm):
    run=study/arm/'run'
    state=read(run/'status.json',{})
    if (study/'PAUSE').is_file() or (run/'PAUSE').is_file():
        return None
    config={**protocol['config'],'id':f'FL-centerline-{arm}-s2026','arm':arm,
            'study_protocol_sha256':digest(study/'protocol.json')}
    if state.get('state')=='completed' and state.get('completed_epoch')==BUDGET:
        return config
    model=AuthorGraph('batch')
    model.load_state_dict(weights)

    class StudyDataset(SegmentationDataset):
        def __init__(self,directory,category,split,size):
            super().__init__(directory,category,split,size)
            if split=='train':
                by_uri={r['uri']:r for r in self.rows}
                self.rows=[by_uri[uri] for uri in protocol['train_selection']]

    class FiniteAdam(torch.optim.Adam):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**{**kwargs,'lr':config['lr']})

        def step(self,*args,**kwargs):
            if any(p.grad is not None and not torch.isfinite(p.grad).all()
                   for group in self.param_groups for p in group['params']):
                raise ValueError('暗条细线损失出现非有限梯度')
            return super().step(*args,**kwargs)

    class Optim:
        Adam=FiniteAdam
        lr_scheduler=torch.optim.lr_scheduler

    class Amp:
        def GradScaler(self,*args,**kwargs):
            return torch.amp.GradScaler('cuda',enabled=False)

    class TrainTorch:
        optim=Optim()
        amp=Amp()

        def __getattr__(self,key):
            return getattr(torch,key)

    def loss(logits,target,_kind):
        pixel=loss_value(logits,target,protocol['config']['positive_weight'])
        return pixel if arm=='control' else (1-WEIGHT)*pixel+WEIGHT*centerline_loss(logits.float().sigmoid(),target.float())

    original=(engine.architecture,engine.code_identity,engine.loss_value,engine.torch,engine.SegmentationDataset)
    append(LEDGER,'development','暗条中心线配对训练',f'{arm} 组，固定同图同起点训练3轮。',run_id=config['id'])
    try:
        engine.architecture=lambda _: (model,{'model':'verified author backbone','centerline_weight':WEIGHT if arm=='centerline' else 0.})
        engine.code_identity=code_identity
        engine.loss_value=loss
        engine.torch=TrainTorch()
        engine.SegmentationDataset=StudyDataset
        torch.backends.cudnn.allow_tf32=False
        torch.backends.cuda.matmul.allow_tf32=False
        state=engine.train(config,protocol['dataset'],run,resume=(run/'last.pt').is_file())
        return config if state['state']=='completed' else None
    finally:
        (engine.architecture,engine.code_identity,engine.loss_value,engine.torch,engine.SegmentationDataset)=original


def coverage(mask,native):
    pred,ref=(skeletonize(x.copy()) for x in (mask,native>0))
    return {'reference_pixels':int(ref.sum()),'predicted_pixels':int(pred.sum()),
            'reference_centerline_in_prediction':int((ref&mask).sum()),
            'predicted_centerline_in_reference':int((pred&(native>0)).sum())}


def review(protocol,study,configs):
    cases=SegmentationDataset(protocol['dataset'],'FL','validation',512)
    if len(cases)!=182:
        raise ValueError('暗条开发集位置不符')
    models={}
    weights,history={},{}
    for arm,config in configs.items():
        p=study/arm/'run/last.pt'
        saved=engine.load_checkpoint(p)
        if (saved['identity']['config']!=config or saved['identity']['code']!=protocol['code']
                or saved['epoch']!=BUDGET or saved['identity']!=read(p.parent/'identity.json')):
            raise ValueError('暗条实验结果与已存方案不符')
        model=AuthorGraph('batch')
        model.load_state_dict(saved['model'])
        models[arm]=model
        weights[arm]=digest(p)
        history[arm]=saved['history'][-1]['validation']['dice']
    with exclusive(ROOT/'.mdva/training-gpu.lock'):
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        usage=Resources()
        for model in models.values():
            model.cuda().eval()
        rows=[]
        with torch.inference_mode():
            for i,r in enumerate(cases.rows):
                x=cases[i]['image'][None].cuda()
                ground=np.load(cases.source(r['mask_uri']),allow_pickle=False)>0
                objects=label(ground,structure=CONNECTIVITY)[0]
                row={'uri':r['uri'],'time_utc':r['time_utc'],'station':r['instrument'],'arms':{}}
                for arm,model in models.items():
                    logits=model(x)
                    if not torch.isfinite(logits).all():
                        raise ValueError('暗条对照推理存在非有限值')
                    mask=logits.float().sigmoid()[0,0].cpu().numpy()>=.5
                    row['arms'][arm]={'pixel':binary_segmentation(mask,ground),
                                      'regions':connected_scores(mask,objects),'centerline':coverage(mask,ground)}
                rows.append(row)
                if i%24==0:
                    usage.sample()
        resource=usage.sample()
    measured={}
    for arm in ARMS:
        r=[row['arms'][arm] for row in rows]
        pixel=segmentation_summary([a['pixel'] for a in r])
        if abs(pixel['dice']-history[arm])>1e-5:
            raise ValueError('暗条训练与开发复算指标不一致')
        line=sum((Counter(a['centerline']) for a in r),Counter())
        measured[arm]={'pixel':pixel,'regions':region_summary([a['regions'] for a in r]),
                       'centerline':dict(line),'by_station':{s:segmentation_summary([row['arms'][arm]['pixel'] for row in rows if row['station']==s])
                                                           for s in sorted({row['station'] for row in rows})}}
    atomic_json(study/'images.json',rows)
    report={'state':'completed','created_utc':now(),'development_images':len(rows),'results':measured,
            'weights_sha256':weights,'protocol_sha256':digest(study/'protocol.json'),'images_sha256':digest(study/'images.json'),
            'test_accessed':False,'calibration_accessed':False,'deployed':False,'resource':resource}
    atomic_json(study/'summary.json',report)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('origin','archive','audit','study'):
        parser.add_argument('--'+name,required=True,type=Path)
    configure_environment()
    args={k:p.resolve() for k,p in vars(parser.parse_args()).items()}
    protocol,weights,study=prepare(**args)
    configs={}
    for arm in ARMS:
        configs[arm]=train(protocol,weights,study,arm)
        if not configs[arm]:
            return
    report=review(protocol,study,configs)
    print(json.dumps({key:report[key] for key in ('state','development_images','results')},ensure_ascii=False))


if __name__=='__main__':
    main()
