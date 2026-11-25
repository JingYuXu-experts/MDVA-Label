"""One fixed equal-probability ensemble; cached validation only, no threshold search."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image

from .datasets import SegmentationDataset
from .diagnostic_support import sample_id
from .durable import atomic_json, commit
from .runtime import digest, now, output_path
from .metrics import binary_segmentation, segmentation_summary
from .ch_display_probe import localization_record
from .localization_audit import aggregate
from .regional_decision import paired_intervals

ROOT=Path(__file__).resolve().parents[3]
CONTROL=ROOT/'out/model-diagnostics/20261116-final'
CONTROL_LOCATIONS=ROOT/'out/recognition-improvement-20261116/display-probe-01/cases.json'


def equal_probability(a,b):
    if a.shape!=(512,512) or b.shape!=a.shape:
        raise ValueError('集成必须使用同一512原生网格')
    if any(not np.isfinite(x).all() or x.min()<0 or x.max()>1 for x in (a,b)):
        raise ValueError('模型分数含非有限值或越界')
    return .5*a.astype(np.float32)+.5*b.astype(np.float32)


def prepare(study, output):
    study=Path(study).resolve();training=json.loads((study/'protocol.json').read_text(encoding='utf-8'))
    output=output_path(output,training['dataset'])
    if output.exists():raise ValueError('集成方案使用新版本，不覆盖')
    plan={'created_utc':now(),'training_protocol_sha256':digest(study/'protocol.json'),
        'code':{n:digest(Path(__file__).with_name(n)) for n in ('scss_ensemble_review.py','metrics.py','datasets.py','ch_display_probe.py','localization_audit.py','regional_decision.py')},
        'control_index_sha256':digest(CONTROL/'case-index.json'),'control_locations_sha256':digest(CONTROL_LOCATIONS),
        'candidate':'best through20 selected by unchanged trainer rule','weights':[.5,.5],'threshold':.5,
        'scope':'One posthoc exploratory equal-weight ensemble on526 reused validation images; no test/calibration or optimizer',
        'selection':'No weight or threshold grid; no selection between best and last ensemble',
        'automatic_model_replacement':False,'practical_gate':'same original SCSS protocol gate; speed cost reported separately'}
    output.mkdir(parents=True);atomic_json(output/'protocol.json',plan);return plan


def run(study, review, output):
    from .scss_inference_benchmark import ready
    study,review,output=Path(study).resolve(),Path(review).resolve(),Path(output).resolve()
    checked=ready(study,review)
    training=json.loads((study/'protocol.json').read_text(encoding='utf-8'))
    if output_path(output,training['dataset'])!=output:raise ValueError('输出越界')
    plan=json.loads((output/'protocol.json').read_text(encoding='utf-8'))
    if plan['training_protocol_sha256']!=digest(study/'protocol.json') or plan['weights']!=[.5,.5] or plan['threshold']!=.5:raise ValueError('集成方案改变')
    for name,sha in plan['code'].items():
        if digest(Path(__file__).with_name(name))!=sha:raise ValueError('集成评价代码改变')
    if digest(CONTROL/'case-index.json')!=plan['control_index_sha256'] or digest(CONTROL_LOCATIONS)!=plan['control_locations_sha256']:raise ValueError('原候选缓存目录改变')
    controls={r['uri']:r for r in json.loads((CONTROL/'case-index.json').read_text(encoding='utf-8')) if r['run_id']=='CH-C2-s2026' and r['diagnostic_subset']=='validation'}
    data=SegmentationDataset(training['dataset'],'CH','validation')
    if set(controls)!={r['uri'] for r in data.rows}:raise ValueError('比较样本集合不同')
    identity={'training_protocol_sha256':digest(study/'protocol.json'),'ensemble_protocol_sha256':digest(output/'protocol.json'),
        'review_sha256':digest(review/'summary.json'),'candidate_checkpoint_sha256':checked['identity']['checkpoint_sha256'],
        'scope':'526 CH validation only; equal probability ensemble','threshold':.5}
    if (output/'identity.json').exists():
        if json.loads((output/'identity.json').read_text(encoding='utf-8'))!=identity:raise ValueError('集成缓存身份不同')
    else:atomic_json(output/'identity.json',identity)
    locations=[];scores=[];baseline_scores=[]
    for i,row in enumerate(data.rows):
        if (output/'PAUSE').exists():atomic_json(output/'status.json',{'state':'paused','completed':i,'total':len(data)});return {'state':'paused'}
        sid=sample_id(row['uri']);source=data.source(row['uri']);control=controls[row['uri']]
        original=CONTROL/'probabilities/CH-C2-s2026'/(sid+'.npy')
        if digest(original)!=control['probability_sha256']:raise ValueError('原候选分数缓存改变')
        record=json.loads((review/'cases'/sid/'result.json').read_text(encoding='utf-8'))
        candidate=review/'cases'/sid/'probability.npz'
        if record['identity_sha256']!=digest(review/'identity.json') or digest(candidate)!=record['assets']['probability.npz'] or digest(source)!=record['image_sha256']:raise ValueError('新候选缓存或原图身份改变')
        a=np.load(original,allow_pickle=False)
        with np.load(candidate,allow_pickle=False) as values:b=values['probability']
        probability=equal_probability(a,b);prediction=probability>=.5
        reference=np.load(data.source(row['mask_uri']),allow_pickle=False)>0
        control_score=binary_segmentation(a>=.5,reference,boundary=True)
        candidate_score=binary_segmentation(b>=.5,reference,boundary=True)
        if any(control_score[k]!=control[k] or candidate_score[k]!=record['score'][k] for k in ('tp','fp','fn')):raise ValueError('原始预测缓存计数复算不一致')
        score=binary_segmentation(prediction,reference,boundary=True)
        folder=output/'cases'/sid;folder.mkdir(parents=True,exist_ok=True)
        commit(folder/'prediction.png',lambda stream:Image.fromarray(prediction.astype('uint8')*255).save(stream,format='PNG'))
        commit(folder/'probability.npz',lambda stream:np.savez_compressed(stream,probability=probability))
        loc=localization_record(row,prediction,reference,score,'equal-CH-C2-SCSS-best20')
        atomic_json(folder/'result.json',{'uri':row['uri'],'identity_sha256':digest(output/'identity.json'),'image_sha256':digest(source),
            'score':score,'control_score':control_score,'localization':loc,'assets':{n:digest(folder/n) for n in ('prediction.png','probability.npz')}})
        locations.append(loc);scores.append(score);baseline_scores.append(control_score)
        if (i+1)%50==0 or i+1==len(data):atomic_json(output/'status.json',{'state':'cpu_cached_evaluation','completed':i+1,'total':len(data),'training':False})
    metrics=segmentation_summary(scores);localization=aggregate(locations);gates=training['final_practical_gate']
    checks={'dice':metrics['dice']>=gates['dice'],'precision':metrics['precision']>=gates['precision'],'half_coverage':localization['reference_coverage']['half_covered_fraction']>=gates['half_coverage_fraction']}
    original_locations=json.loads(CONTROL_LOCATIONS.read_text(encoding='utf-8'))['original']
    result={'state':'completed','created_utc':now(),'identity':identity,'metrics':metrics,'control_metrics':segmentation_summary(baseline_scores),
        'localization':localization,'control_localization':aggregate(original_locations),
        'practical_checks':checks,'practical_gate_passed':all(checks.values()),'exact_replay':True,
        'checkpoint_policy_label':'原CNN193与SCSS-best20：等权概率集成',
        'paired_uncertainty':paired_intervals(original_locations,locations),
        'test_accessed':False,'calibration_accessed':False,'models_replaced':False,
        'limitations':['Posthoc single-seed reused-validation experiment; not independent test or calibration.',
            'Requires two model forwards; no cost-free precision or scientific novelty claim.',
            'Connected regions are not physical instances; independent target-free FPR unmeasured.']}
    result['by_year']={y:{'images':sum(r['time_utc'].startswith(y) for r in data.rows),
        'candidate':segmentation_summary([s for r,s in zip(data.rows,scores) if r['time_utc'].startswith(y)]),
        'control':segmentation_summary([s for r,s in zip(data.rows,baseline_scores) if r['time_utc'].startswith(y)])} for y in sorted({r['time_utc'][:4] for r in data.rows})}
    atomic_json(output/'locations.json',locations);atomic_json(output/'summary.json',result)
    atomic_json(output/'status.json',{'state':'completed','completed':len(data),'total':len(data),'training':False})
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('prepare','run'));p.add_argument('--study',required=True);p.add_argument('--review');p.add_argument('--output',required=True);a=p.parse_args()
    r=prepare(a.study,a.output) if a.action=='prepare' else run(a.study,a.review,a.output)
    print(json.dumps({k:r[k] for k in ('state','metrics','practical_checks') if k in r}))
