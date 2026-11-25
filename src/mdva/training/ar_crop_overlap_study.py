"""Factorize detectable location and contour quality for the same AR crop candidates."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

import numpy as np
import torch
from torch import nn
from torch.nn import functional as F

from .ar_candidate_crop_study import BATCH, EPOCHS, LEDGER, SEED, CropRanker, apply_filters, matrices, thresholds_for
from .ar_hmi_candidate_ranker import tally
from .ar_hmi_temporal_ranker import matching, time_partition
from .ar_scale_study import ROOT
from .durable import atomic_checkpoint, atomic_json
from .journal import append, read
from .reviewed_train import load_checkpoint, restore_rng, rng_state, seed_all
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


ARMS = ('direct','factorized')


def overlap_targets(row):
    geometry = row['geometry']
    iou = np.asarray(geometry['iou'],dtype=float).reshape(len(row['matches']),len(geometry['reference_ids']))
    overlap = iou.max(axis=1,initial=0) > 0
    if np.any(np.asarray(row['matches'],dtype=bool)&~overlap):
        raise ValueError('已匹配候选缺少任何参考交叠')
    return overlap


def overlap_vector(rows):
    return np.concatenate([overlap_targets(row) for row in rows])


def binary_weights(truth):
    positives = int(truth.sum())
    if not 0 < positives < len(truth):
        raise ValueError('训练目标缺少正类或负类')
    return float((len(truth)-positives)/positives)


def training_loss(logits, match, overlap, weights, arm):
    if arm=='direct':
        return F.binary_cross_entropy_with_logits(logits[:,1],match,pos_weight=weights['match'])
    available = F.binary_cross_entropy_with_logits(logits[:,0],overlap.float(),pos_weight=weights['overlap'])
    geometry = F.binary_cross_entropy_with_logits(logits[:,1],match,reduction='none',pos_weight=weights['conditional'])
    return available + (geometry*overlap).sum()/overlap.sum().clamp_min(1)


def rank_values(logits, arm):
    if arm=='direct':
        return logits[:,1]
    return F.logsigmoid(logits[:,0])+F.logsigmoid(logits[:,1])


def open_caches(source):
    report,plan=(read(source/name) for name in ('summary.json','protocol.json'))
    if (report['state']!='completed' or report['test_accessed'] or report['calibration_accessed']
            or report['protocol_sha256']!=digest(source/'protocol.json')
            or report['development_sha256']!=digest(source/'development.json')
            or report['thresholds_sha256']!=digest(source/'thresholds.json')):
        raise ValueError('已冻结候选缓存或结果身份不符')
    training = []
    for i,uri in enumerate(plan['train']):
        path=source/'train'/f'{i:04d}.npz'
        row=read(path.with_suffix('.json'))
        if row['uri']!=uri or digest(path)!=row['cache_sha256']:
            raise ValueError('训练候选缓存错位')
        training.append({**row,'crops':str(path)})
    development = []
    for i,record in enumerate(read(source/'development.json')):
        path=source/'validation'/f'{i:04d}.npz'
        row=read(path.with_suffix('.json'))
        if row['uri']!=record['uri'] or digest(path)!=row['cache_sha256'] or row['baseline']!=record['baseline']:
            raise ValueError('开发候选缓存错位')
        development.append({**row,'crops':str(path)})
    return training,development,report


def train_arm(study,arm,data,identity,resource):
    seed_all(SEED)
    model = CropRanker()
    model.output[-1] = nn.Linear(model.output[-1].in_features,2)
    model.cuda()
    optimizer = torch.optim.AdamW(model.parameters(),lr=.001,weight_decay=.0001)
    path = study/arm/'last.pt'
    start,history = 0,[]
    if path.is_file():
        saved = load_checkpoint(path)
        if saved['protocol_sha256']!=identity or saved['arm']!=arm:
            raise ValueError('局部复核恢复身份发生变化')
        model.load_state_dict(saved['model'])
        optimizer.load_state_dict(saved['optimizer'])
        restore_rng(saved['rng'])
        start,history = saved['epoch'],saved['history']
    x,g,y,z = (torch.from_numpy(array) for array in data)
    weights = {name:torch.tensor(binary_weights(v),device='cuda') for name,v in
               (('match',y),('overlap',z),('conditional',y[z]))}
    model.train()
    for epoch in range(start+1,EPOCHS+1):
        if (study/'PAUSE').exists() or (study/arm/'PAUSE').exists():
            atomic_json(study/'progress.json',{'state':'paused','arm':arm,'epoch':epoch-1})
            return None
        order = torch.randperm(len(y),generator=torch.Generator().manual_seed(SEED+epoch))
        losses=[]
        for index in order.split(BATCH):
            optimizer.zero_grad(set_to_none=True)
            estimate = model(x[index].cuda().float()/255,g[index].cuda())
            loss = training_loss(estimate,y[index].cuda().float(),z[index].cuda(),weights,arm)
            if not torch.isfinite(loss):
                raise ValueError('分项复核训练损失非有限')
            loss.backward()
            optimizer.step()
            losses.append(float(loss.detach()))
        history.append({'epoch':epoch,'loss':float(np.mean(losses))})
        atomic_checkpoint(path,{'protocol_sha256':identity,'arm':arm,'epoch':epoch,'model':model.state_dict(),
                                'optimizer':optimizer.state_dict(),'history':history,'rng':rng_state()})
        atomic_json(study/'progress.json',{'state':'training','arm':arm,'epoch':epoch,'budget':EPOCHS,
                                           'resources':resource.sample()})
    return model.eval()


def score(models,data):
    x,g,_ = (torch.from_numpy(a) for a in data)
    values, overlap = {}, None
    with torch.inference_mode():
        for name,model in models.items():
            chunks = [model(x[i:i+BATCH].cuda().float()/255,g[i:i+BATCH].cuda()).cpu()
                      for i in range(0,len(x),BATCH)]
            logits = torch.cat(chunks)
            if not torch.isfinite(logits).all():
                raise ValueError('局部复核模型得分非有限')
            values[name] = rank_values(logits,name).numpy()
            if name=='factorized':
                overlap = logits[:,0].numpy()
    return values,overlap


def kept_types(rows, arm):
    breakdown = Counter()
    for row in rows:
        chosen = np.asarray(row['decisions'][arm]['kept'],dtype=bool)
        matched,_ = matching(row['geometry'],chosen)
        target = overlap_targets(row)
        rejected_iou = chosen.copy()
        rejected_iou[matched] = False
        breakdown.update(unmatched_with_overlap=int(np.count_nonzero(rejected_iou&target)),
                         unmatched_without_overlap=int(np.count_nonzero(rejected_iou&~target)))
    return dict(breakdown)


def run(dataset,source,study):
    from sklearn.metrics import average_precision_score
    from sklearn.preprocessing import StandardScaler

    study = output_path(study,dataset)
    if (study/'summary.json').exists() or study==source or study.is_relative_to(source):
        raise ValueError('以新目录保存分项复核')
    training,development,prior = open_caches(source)
    partition = time_partition(training)
    plan = {'prior_summary_sha256':digest(source/'summary.json'),'source_protocol_sha256':digest(source/'protocol.json'),
            'code_sha256':digest(__file__),'manifest_sha256':digest(dataset/'manifest-hashes.json'),
            'training_cache_sha256':[r['cache_sha256'] for r in training],
            'learning_target':'one-to-one native mask IoU at least 0.5; overlap task checks any reference pixel',
            'validation_used_to_fit':False,'test_accessed':False,'calibration_accessed':False}
    protocol_path=study/'protocol.json'
    if protocol_path.exists() and read(protocol_path)!=plan:
        raise ValueError('分项复核试验配置或来源改变')
    atomic_json(protocol_path,plan)
    append(LEDGER,'development','活动区位置与轮廓分项复核','保持同一候选裁片、标签与阈值期，比较直接匹配和分项学习。')
    x,g,y = matrices(partition['fit'])
    normalized = StandardScaler().fit(g)
    train_data=(x,normalized.transform(g).astype(np.float32),y,overlap_vector(partition['fit']))
    def transform(rows):
        x,geometry,match=matrices(rows)
        return x,normalized.transform(geometry).astype(np.float32),match
    outcomes={}
    with exclusive(ROOT/'.mdva/training-gpu.lock'):
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        resource=Resources()
        models={}
        for arm in ARMS:
            model=train_arm(study,arm,train_data,digest(protocol_path),resource)
            if model is None:
                return {'state':'paused'}
            models[arm]=model
        policy=transform(partition['policy'])
        thresholds=thresholds_for(score(models,policy)[0],policy[-1])
        atomic_json(study/'thresholds.json',{'fixed':thresholds,
             'normalization':{'means':normalized.mean_.tolist(),'scale':normalized.scale_.tolist()}})
        for name,rows in (('train_late_holdout',partition['held']),('development',development)):
            data=transform(rows)
            values,location=score(models,data)
            verified=apply_filters(rows,values,thresholds)
            outcomes[name]={'images':len(rows),'objects':{arm:tally(verified,arm) for arm in ('original',*ARMS)},
                            'remaining_unmatched':{arm:kept_types(verified,arm) for arm in ARMS},
                            'candidate_ap':{arm:float(average_precision_score(data[-1],values[arm])) for arm in ARMS},
                            'location_ap':float(average_precision_score(overlap_vector(rows),location))}
            atomic_json(study/f'{name}.json',verified)
        usage=resource.sample()
    result={'state':'completed','created_utc':now(),'results':outcomes,'previous':prior['results'],
            'protocol_sha256':digest(protocol_path),'development_sha256':digest(study/'development.json'),
            'weights':{arm:digest(study/arm/'last.pt') for arm in ARMS},'resource':usage,'test_accessed':False,
            'calibration_accessed':False,'deployed':False}
    atomic_json(study/'summary.json',result)
    atomic_json(study/'progress.json',{'state':'completed','epoch':EPOCHS,'budget':EPOCHS})
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('dataset','source','study'):
        parser.add_argument('--'+key,required=True,type=Path)
    configure_environment()
    result=run(**{name:path.resolve() for name,path in vars(parser.parse_args()).items()})
    print(json.dumps({key:result[key] for key in ('state','results') if key in result},ensure_ascii=False))


if __name__=='__main__':
    main()
