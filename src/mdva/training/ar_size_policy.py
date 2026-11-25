"""Fit size-aware AR filtering on a training-period threshold split."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch
from torch import nn

from .ar_candidate_crop_study import CropRanker, RETENTION, apply_filters, matrices, thresholds_for
from .ar_crop_overlap_study import kept_types, open_caches, score
from .ar_hmi_candidate_ranker import tally
from .ar_hmi_temporal_ranker import time_partition
from .ar_scale_study import ROOT
from .durable import atomic_json
from .journal import read
from .reviewed_train import load_checkpoint
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


SIZES = ('under256','256to1023','1024plus')


def size_indices(rows):
    area=np.array([p['area'] for row in rows for p in row['parts']])
    return np.searchsorted([256,1024],area,side='right')


def cutoff_options(scores, truth, cap):
    options={}
    for value in np.r_[scores.min(initial=0),np.nextafter(np.unique(scores),np.inf)]:
        discarded=scores<value
        lost=int(np.count_nonzero(discarded&truth))
        bad=int(np.count_nonzero(discarded&~truth))
        if lost<=cap and (lost not in options or bad>options[lost][0]):
            options[lost]=(bad,float(value))
    return options


def optimized_cuts(values, tags, truth):
    total=int(truth.sum())
    cap=int(np.floor(total*(1-RETENTION)+1e-9))
    policies={0:(0,[])}
    counters={}
    for i,name in enumerate(SIZES):
        group=tags==i
        # Preserve every verified tiny candidate in the threshold period.
        options=cutoff_options(values[group].astype(np.float64),truth[group],0 if i==0 else cap)
        updated={}
        for losses,(gains,cutoffs) in policies.items():
            for count,(avoided,threshold) in options.items():
                cost=losses+count
                if cost>cap:
                    continue
                if cost not in updated or gains+avoided>updated[cost][0]:
                    updated[cost]=(gains+avoided,[*cutoffs,threshold])
        policies=updated
        counters[name]={'candidates':int(group.sum()),'known_matched':int((truth&group).sum())}
    selected=max(policies,key=lambda losses:(policies[losses][0],-losses))
    dropped,cuts=policies[selected]
    return np.asarray(cuts),{'matched_allowed_to_remove':cap,'matched_removed':selected,
                            'unmatched_removed':dropped,'sizes':counters}


def run(dataset,crops,direct,output):
    output=output_path(output,dataset)
    if output.exists() or any(output.is_relative_to(p) for p in (crops,direct)):
        raise ValueError('分组阈值使用独立研究目录')
    fit,development,_=open_caches(crops)
    baseline=read(direct/'summary.json')
    state=direct/'direct/last.pt'
    if (baseline['state']!='completed' or baseline['test_accessed'] or baseline['calibration_accessed']
            or baseline['weights']['direct']!=digest(state)
            or baseline['protocol_sha256']!=digest(direct/'protocol.json')
            or baseline['development_sha256']!=digest(direct/'development.json')
            or read(direct/'protocol.json')['prior_summary_sha256']!=digest(crops/'summary.json')):
        raise ValueError('分组阈值未能绑定到固定的模型与候选')
    selected=load_checkpoint(state)
    if selected['protocol_sha256']!=baseline['protocol_sha256'] or selected['arm']!='direct':
        raise ValueError('分组阈值的模型身份不符')
    normalization=read(direct/'thresholds.json')
    center,scale=(np.asarray(normalization['normalization'][k],dtype=np.float32) for k in ('means','scale'))
    def data(rows):
        x,g,y=matrices(rows)
        return x,((g-center)/scale).astype(np.float32),y
    plan={'baseline_sha256':digest(direct/'summary.json'),'normalization_sha256':digest(direct/'thresholds.json'),
          'crop_summary_sha256':digest(crops/'summary.json'),'code_sha256':digest(__file__),
          'threshold_fit':'training-period protocol, optimize fewer unconfirmed candidates under 98% verified retention',
          'tiny_training_retention':'no verified tiny matches discarded','test_accessed':False,'calibration_accessed':False}
    atomic_json(output/'protocol.json',plan)
    split=time_partition(fit)
    model=CropRanker()
    model.output[-1]=nn.Linear(model.output[-1].in_features,2)
    model.load_state_dict(selected['model'])
    result={}
    with exclusive(ROOT/'.mdva/training-gpu.lock'):
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        usage=Resources()
        models={'direct':model.cuda().eval()}
        policy_data=data(split['policy'])
        values=score(models,policy_data)[0]['direct']
        global_cut=thresholds_for({'direct':values},policy_data[-1])['direct']
        frozen=normalization['fixed']['direct']
        if not np.isclose(global_cut,frozen,rtol=0,atol=1e-5):
            raise ValueError('无法重现未分组的原训练期冻结阈值')
        group_cut,record=optimized_cuts(values,size_indices(split['policy']),policy_data[-1])
        atomic_json(output/'thresholds.json',{'global':frozen,'size_cuts':dict(zip(SIZES,group_cut.tolist(),strict=True)),
                                              'policy_counts':record})
        for label,rows in (('train_late_holdout',split['held']),('development',development)):
            actual=data(rows)
            scored=score(models,actual)[0]['direct'].astype(np.float64)
            shifted={'global':scored-frozen,'size':scored-group_cut[size_indices(rows)]}
            outcome=apply_filters(rows,shifted,{'global':0.,'size':0.})
            objects={arm:tally(outcome,arm) for arm in ('original','global','size')}
            if objects['global']!=baseline['results'][label]['objects']['direct']:
                raise ValueError('分组实验没有精确重现原对象结果')
            result[label]={'objects':objects,'images':len(rows),
                           'remaining_unmatched':{arm:kept_types(outcome,arm) for arm in ('global','size')}}
            atomic_json(output/f'{label}.json',outcome)
        resource=usage.sample()
    summary={'state':'completed','created_utc':now(),'results':result,'policy':record,
             'protocol_sha256':digest(output/'protocol.json'),'development_sha256':digest(output/'development.json'),
             'resource':resource,'test_accessed':False,'calibration_accessed':False,'deployed':False}
    atomic_json(output/'summary.json',summary)
    return summary


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('dataset','crops','direct','output'):
        parser.add_argument('--'+name,required=True,type=Path)
    configure_environment()
    print(json.dumps(run(**{k:v.resolve() for k,v in vars(parser.parse_args()).items()}),ensure_ascii=False))


if __name__ == '__main__':
    main()
