"""Compare same-observation calibrated HMI fields with display-only candidate verification."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

import numpy as np
from PIL import Image
import torch
from torch import nn
from torch.nn import functional as F

from .ar_candidate_crop_study import BATCH, EPOCHS, LEDGER, SEED, SIDE, CropRanker, apply_filters, matrices, resize_patch, thresholds_for
from .ar_crop_overlap_study import binary_weights, kept_types, open_caches
from .ar_hmi_candidate_ranker import tally
from .ar_hmi_registration import correlation
from .ar_hmi_temporal_ranker import time_partition
from .ar_scale_study import ROOT
from .durable import atomic_checkpoint, atomic_json
from .journal import append, read
from .reviewed_train import load_checkpoint, restore_rng, rng_state, seed_all
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


ARMS = ('display','calibrated')
SUPPORT_MINIMUM = .90
LOCAL_MINIMUM = .60


def quantized_field(current):
    finite = np.isfinite(current)
    normalized = np.arcsinh(np.clip(np.where(finite,current,0),-1000,1000)/50) / np.arcsinh(20)
    return np.rint(127.5*(normalized+1)).astype(np.uint8),finite.astype(np.uint8)*255


def physical_crop(field, coverage, box, image):
    x0,y0,x1,y1 = box
    center=(y0+y1)//2,(x0+x1)//2
    radius=max(20,int(np.ceil(.85*max(y1-y0,x1-x0))))
    physical=resize_patch(field,radius,center,128,Image.Resampling.BILINEAR)
    available=resize_patch(coverage,radius,center,0,Image.Resampling.NEAREST)
    use=available>0
    target=image[2]>127
    if not target.any() or np.count_nonzero(use&target)/np.count_nonzero(target)<SUPPORT_MINIMUM:
        return None
    relation=correlation(physical[use].astype(float),image[0][use].astype(float))
    if relation is None or relation<LOCAL_MINIMUM:
        return None
    return np.stack((physical,available))


def catalogs(dataset, training, train_receipts, development, dev_receipts, crop_hash):
    manifest=digest(dataset/'manifest-hashes.json')
    prepared={}
    for stage,directories,rows in (('train',[train_receipts],training),('validation',dev_receipts,development)):
        allowed={row['uri'] for row in rows}
        for directory in directories:
            summary,plan=(read(directory/name) for name in ('summary.json','protocol.json'))
            if (summary['state']!='completed' or summary['test_accessed'] or summary['calibration_accessed']
                    or summary['protocol_sha256']!=digest(directory/'protocol.json')):
                raise ValueError('实测磁场来源或配准身份不符')
            if stage=='train':
                if (plan['manifest_sha256']!=manifest or plan['crop_protocol_sha256']!=crop_hash
                        or set(plan['selected'])!={row['uri'] for row in summary['results']}):
                    raise ValueError('辅助磁场训练图像超出冻结清单')
            elif (plan.get('dataset_sha256') or plan['data']['manifest-hashes.json'])!=manifest:
                raise ValueError('开发磁场与原冻结清单不符')
            for row in summary['results']:
                if row['split']!=stage:
                    continue
                if row['uri'] not in allowed or row['uri'] in prepared:
                    raise ValueError('磁场配对图像错位或重复')
                prepared[row['uri']]=(directory,row)
    return prepared


def aligned_inputs(rows, catalogs):
    x,g,y=matrices(rows)
    field=np.zeros((len(x),2,SIDE,SIDE),dtype=np.uint8)
    usable=np.zeros(len(x),dtype=bool)
    offset=0
    sources=Counter()
    for row in rows:
        key=row['uri']
        probe=catalogs.get(key)
        if probe is not None and probe[1]['state']=='display_alignment_supported':
            directory,meta=probe
            path=directory/'pairs'/meta['id']/'aligned-los.npy'
            if digest(path)!=meta['array_sha256']:
                raise ValueError('实测磁场配准文件改变')
            value=np.load(path,allow_pickle=False)
            if value.shape!=(1024,1024):
                raise ValueError('原生磁场网格与冻结候选不符')
            physical,available=quantized_field(value)
            sources['registered_images']+=1
            for i,part in enumerate(row['parts']):
                local=physical_crop(physical,available,part['bbox_xyxy'],x[offset+i])
                if local is not None:
                    field[offset+i]=local
                    usable[offset+i]=True
        offset+=len(row['parts'])
    if offset!=len(x):
        raise ValueError('物理通道和候选缓存不一致')
    return (x,g,y,field,usable),dict(sources)


def make_model():
    model=CropRanker()
    model.output[-1]=nn.Linear(model.output[-1].in_features,2)
    old=model.encoder[0]
    first=nn.Conv2d(5,old.out_channels,old.kernel_size,stride=old.stride,padding=old.padding)
    with torch.no_grad():
        first.weight.zero_()
        first.weight[:,:3].copy_(old.weight)
        first.bias.copy_(old.bias)
    model.encoder[0]=first
    return model


def train(study, arm, data, identity, resource):
    seed_all(SEED)
    model=make_model().cuda()
    optimizer=torch.optim.AdamW(model.parameters(),lr=.001,weight_decay=.0001)
    weight=torch.tensor(binary_weights(data[2]),device='cuda')
    x,g,y,extra,_=(torch.from_numpy(arr) for arr in data)
    path=study/arm/'last.pt'
    initial,losses=0,[]
    if path.is_file():
        saved=load_checkpoint(path)
        if saved['protocol_sha256']!=identity or saved['arm']!=arm:
            raise ValueError('复核模型恢复配置不符')
        model.load_state_dict(saved['model'])
        optimizer.load_state_dict(saved['optimizer'])
        restore_rng(saved['rng'])
        initial,losses=saved['epoch'],saved['history']
    model.train()
    for epoch in range(initial+1,EPOCHS+1):
        if (study/'PAUSE').is_file():
            atomic_json(study/'progress.json',{'state':'paused','arm':arm,'completed_epoch':epoch-1})
            return None
        order=torch.randperm(len(y),generator=torch.Generator().manual_seed(SEED+epoch))
        history=[]
        for i in order.split(BATCH):
            supplemental=extra[i] if arm=='calibrated' else torch.zeros_like(extra[i])
            current=torch.cat((x[i],supplemental),dim=1).cuda().float()/255
            optimizer.zero_grad(set_to_none=True)
            logits=model(current,g[i].cuda())[:,1]
            loss=F.binary_cross_entropy_with_logits(logits,y[i].cuda().float(),pos_weight=weight)
            if not torch.isfinite(loss):
                raise ValueError('复核训练异常')
            loss.backward()
            optimizer.step()
            history.append(float(loss.detach()))
        losses.append(float(np.mean(history)))
        atomic_checkpoint(path,{'epoch':epoch,'arm':arm,'protocol_sha256':identity,'model':model.state_dict(),
                               'optimizer':optimizer.state_dict(),'history':losses,'rng':rng_state()})
        atomic_json(study/'progress.json',{'state':'training','arm':arm,'completed_epoch':epoch,'budget':EPOCHS,
                                           'resource':resource.sample()})
    return model.eval()


def score(models,data):
    x,g,_,extra,_=(torch.from_numpy(arr) for arr in data)
    result={}
    with torch.inference_mode():
        for arm,model in models.items():
            scores=[]
            for i in range(0,len(x),BATCH):
                part=extra[i:i+BATCH]
                side=part if arm=='calibrated' else torch.zeros_like(part)
                stack=torch.cat((x[i:i+BATCH],side),dim=1).cuda().float()/255
                values=model(stack,g[i:i+BATCH].cuda())[:,1]
                if not torch.isfinite(values).all():
                    raise ValueError('复核分数非有限')
                scores.append(values.cpu().numpy())
            result[arm]=np.concatenate(scores)
    return result


def fallback(display, calibrated, supported, cuts):
    # Center each score at its independently set cutoff before exact display fallback.
    ctrl=display-cuts['display']
    return {'display':ctrl,'calibrated':np.where(supported,calibrated-cuts['calibrated'],ctrl)}


def run(dataset,source,training,development,study):
    from sklearn.metrics import average_precision_score
    from sklearn.preprocessing import StandardScaler

    study=output_path(study,dataset)
    if (study/'summary.json').exists() or any(study.is_relative_to(path) for path in (source,training,*development)):
        raise ValueError('新实验输出需要独立目录')
    train_rows,dev_rows,old=open_caches(source)
    aligned=catalogs(dataset,train_rows,training,dev_rows,development,digest(source/'protocol.json'))
    plan={'crop_summary_sha256':digest(source/'summary.json'),'code_sha256':digest(__file__),
          'physical_sources':[digest(path/'summary.json') for path in (training,*development)],
          'local_correlation_min':LOCAL_MINIMUM,'local_support_min':SUPPORT_MINIMUM,
          'scope':'Fixed single-image candidates; only same-observation raw magnetic data; display fallback.',
          'test_accessed':False,'calibration_accessed':False}
    protocol=study/'protocol.json'
    if protocol.exists() and read(protocol)!=plan:
        raise ValueError('恢复前的输入或配置已变')
    atomic_json(protocol,plan)
    append(LEDGER,'development','活动区同帧磁场复核','同一候选比较显示图与可用时辅助加入物理磁场值，两组固定16轮。')
    split=time_partition(train_rows)
    fit_data,coverage=aligned_inputs(split['fit'],aligned)
    scaler=StandardScaler().fit(fit_data[1])
    def transform(data):
        return (data[0],scaler.transform(data[1]).astype(np.float32),*data[2:])
    fit_data=transform(fit_data)
    result={'fit':{'images':len(split['fit']),'candidates':len(fit_data[2]),'magnetic_supported':int(fit_data[-1].sum()),**coverage}}
    with exclusive(ROOT/'.mdva/training-gpu.lock'):
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        usage=Resources()
        models={}
        for arm in ARMS:
            models[arm]=train(study,arm,fit_data,digest(protocol),usage)
            if models[arm] is None:
                return {'state':'paused'}
        del fit_data
        eval_data,policy_coverage=aligned_inputs(split['policy'],aligned)
        eval_data=transform(eval_data)
        fitted=score(models,eval_data)
        mask=eval_data[-1]
        cuts={**thresholds_for({'display':fitted['display']},eval_data[2]),
              **thresholds_for({'calibrated':fitted['calibrated'][mask]},eval_data[2][mask])}
        result['policy']={'images':len(split['policy']),'candidates':len(mask),'magnetic_supported':int(mask.sum()),
                          'matched_supported':int((mask&eval_data[2]).sum()),**policy_coverage}
        atomic_json(study/'thresholds.json',{'cutoffs':cuts,'mean':scaler.mean_.tolist(),'scale':scaler.scale_.tolist()})
        del eval_data
        for name,rows in (('train_late_holdout',split['held']),('development',dev_rows)):
            data,counts=aligned_inputs(rows,aligned)
            data=transform(data)
            values=score(models,data)
            selected=fallback(values['display'],values['calibrated'],data[-1],cuts)
            audited=apply_filters(rows,selected,dict.fromkeys(ARMS,0.))
            result[name]={'images':len(rows),'candidate_count':len(data[-1]),'supported':int(data[-1].sum()),**counts,
                          'objects':{arm:tally(audited,arm) for arm in ('original',*ARMS)},
                          'remaining_unmatched':{arm:kept_types(audited,arm) for arm in ARMS},
                          'common_supported_candidate_ap':{arm:float(average_precision_score(data[2][data[-1]],values[arm][data[-1]]))
                                                           for arm in ARMS}}
            atomic_json(study/f'{name}.json',audited)
        resource=usage.sample()
    result={'state':'completed','created_utc':now(),'results':result,'prior_display':old['results']['development']['objects'],
            'protocol_sha256':digest(protocol),'development_sha256':digest(study/'development.json'),
            'weights':{arm:digest(study/arm/'last.pt') for arm in ARMS},'resource':resource,'test_accessed':False,
            'calibration_accessed':False,'deployed':False}
    atomic_json(study/'summary.json',result)
    atomic_json(study/'progress.json',{'state':'completed','completed_epoch':EPOCHS,'budget':EPOCHS})
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('dataset','source','training','study'):
        parser.add_argument('--'+key,required=True,type=Path)
    parser.add_argument('--development',nargs='+',required=True,type=Path)
    configure_environment()
    args=vars(parser.parse_args())
    result=run(**{key:[x.resolve() for x in value] if key=='development' else value.resolve() for key,value in args.items()})
    print(json.dumps({k:result[k] for k in ('state','results') if k in result},ensure_ascii=False))


if __name__ == '__main__':
    main()
