"""Apply frozen practical gates to the paired region-loss study; no model selection on test."""
from __future__ import annotations

import argparse
from datetime import datetime,timezone
import json
from pathlib import Path

import numpy as np

from .durable import atomic_json
from .runtime import digest,now


def gates(control,weighted,protocol):
    anchor=protocol['original_candidate_anchor'];limits=protocol['continuation_gate']
    a=control['localization']['reference_coverage'];b=weighted['localization']['reference_coverage']
    return {
        'half_coverage_gain':b['half_covered_fraction']>=max(a['half_covered_fraction'],anchor['half_covered_fraction'])+limits['half_covered_gain_over_control_and_anchor'],
        'no_overlap_reduction':b['zero_overlap']<=min(a['zero_overlap'],anchor['no_overlap'])*(1-limits['relative_no_overlap_reduction_over_control_and_anchor']),
        'dice_preserved':weighted['metrics']['dice']>=max(control['metrics']['dice'],anchor['dice'])-limits['maximum_dice_drop_vs_control_and_anchor'],
        'precision_preserved':weighted['metrics']['precision']>=max(control['metrics']['precision'],anchor['precision'])-limits['maximum_precision_drop_vs_control_and_anchor']}


def paired_intervals(control,weighted):
    by_uri={r['uri']:r for r in weighted}
    if len(by_uri)!=len(weighted) or len(control)!=len(weighted):raise ValueError('重复或不完整的成对图像')
    blocks={}
    for a in control:
        b=by_uri.pop(a['uri'],None)
        if b is None:raise ValueError('图像集合不一致')
        shape=lambda r:[(v['bbox'],v['pixels']) for v in r['reference']]
        if shape(a)!=shape(b):raise ValueError('参考区域不一致')
        stamp=datetime.fromisoformat(a['time_utc'].replace('Z','+00:00'))
        if stamp.tzinfo is None:raise ValueError('未知时间不可当作UTC')
        block=int(stamp.astimezone(timezone.utc).timestamp()//86400)//27
        value=np.array([b['dice']-a['dice'],1,
            sum(v['covered_fraction']>=.5 for v in b['reference'])-sum(v['covered_fraction']>=.5 for v in a['reference']),
            sum(v['covered_fraction']==0 for v in b['reference'])-sum(v['covered_fraction']==0 for v in a['reference']),len(a['reference'])],dtype=float)
        blocks[block]=blocks.get(block,np.zeros(5))+value
    if by_uri:raise ValueError('额外图像不允许')
    matrix=np.stack(list(blocks.values()));rng=np.random.default_rng(2026)
    samples=[]
    for _ in range(2000):
        x=matrix[rng.integers(len(matrix),size=len(matrix))].sum(0)
        samples.append([x[0]/x[1],x[2]/x[4],x[3]/x[4]])
    bounds=np.percentile(samples,[2.5,97.5],axis=0)
    return {'blocks':len(blocks),'block_days':27,'bootstrap':2000,'seed':2026,
        'difference':'weighted minus control','ci95':{name:bounds[:,i].tolist() for i,name in enumerate(('dice','half_coverage_fraction','no_overlap_fraction'))},
        'scope':'paired temporal-block development uncertainty, single training seed'}


def run(root):
    root=Path(root);read=lambda p:json.loads(p.read_text(encoding='utf-8'))
    protocol=read(root/'regional-protocol.json')
    summaries=[];cases=[];identities=[]
    for arm in ('control','weighted'):
        folder=root/('regional-evaluation-'+arm)
        result=read(folder/'summary.json')
        if result['split']!='validation' or result['epoch']!=3 or result['checkpoint']!='last':raise ValueError('固定末轮验证范围不符')
        identities.append(read(root/('regional-'+arm)/'identity.json'))
        summaries.append(result);cases.append(read(folder/'cases.json'))
    a,b=identities
    ignored={'id','region_mode'}
    clean=lambda c:{k:v for k,v in c.items() if k not in ignored}
    if clean(a['config'])!=clean(b['config']) or a['code']!=b['code'] or a['data']!=b['data']:
        raise ValueError('共同起点、预算或来源不一致，不能解释为损失对照')
    if a['config']['initial_weights_sha256']!=protocol['common_start_sha256']:raise ValueError('共同起点不属于冻结协议')
    checks=gates(*summaries,protocol)
    report={'state':'completed','updated_utc':now(),'gate_passed':all(checks.values()),'checks':checks,
        'decision':'targeted_validation_only' if all(checks.values()) else 'no_expansion_no_replacement',
        'models':{s['model']:{'metrics':s['metrics'],'localization':s['localization']} for s in summaries},
        'paired_uncertainty':paired_intervals(*cases),'protocol_sha256':digest(root/'regional-protocol.json'),
        'evaluation_sha256':[digest(root/('regional-evaluation-'+arm)/'summary.json') for arm in ('control','weighted')],
        'test_accessed':False,'candidate_replaced':False,
        'interpretation':'Within this 3-epoch warm-start budget, mild region weighting has not yet established a useful reduction of missed regions. This is not a claim that every weighting scheme or longer training is ineffective.'}
    atomic_json(root/'regional-decision.json',report)
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--study',required=True);a=p.parse_args()
    print(json.dumps(run(a.study),ensure_ascii=True))
