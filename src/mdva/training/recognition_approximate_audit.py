"""Audit approximate solar-region matches on existing validation predictions."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from scipy.ndimage import label
from scipy.optimize import linear_sum_assignment

from .datasets import SegmentationDataset
from .durable import atomic_json
from .journal import read
from .runtime import digest, now, output_path


MODELS = {'AR':'AR-A0-s42', 'CH':'CH-C2-s2026', 'FL':'FL-F0-s42'}
REFERENCE_FLOOR = .75
CANDIDATE_FLOOR = .50
TARGET = .80


def pair_regions(prediction, reference, category):
    """Maximize the number of one-to-one qualifying matches, then prefer overlap."""
    neighbors = np.ones((3,3),dtype=np.uint8)
    candidates, count = label(prediction,structure=None if category=='AR' else neighbors)
    if category=='AR':
        ids, inverse = np.unique(reference,return_inverse=True)
        if ids.size and ids[0]!=0:
            ids = np.r_[0,ids]
            inverse += 1
        source = inverse.reshape(reference.shape)
    else:
        source, refs = label(reference>0,structure=neighbors)
        ids = np.arange(refs+1)
    hits = np.bincount((candidates.ravel().astype(np.int64)*len(ids) + source.ravel()),
                       minlength=(count+1)*len(ids)).reshape((count+1,len(ids)))
    # Both coverages use the original pixel counts, including background overlap.
    p_area = hits.sum(axis=1)[1:]
    r_area = hits.sum(axis=0)[1:]
    overlap = hits[1:,1:]
    recall = overlap/r_area[None,:]
    precision = overlap/p_area[:,None]
    good = (recall>=REFERENCE_FLOOR) & (precision>=CANDIDATE_FLOOR)
    quality = 2*overlap/(p_area[:,None]+r_area[None,:])
    priority = good * (1.+quality/(min(good.shape)+1))
    left,right = linear_sum_assignment(-priority)
    matches = [(int(p+1),int(ids[r+1]),float(recall[p,r]),float(precision[p,r]))
               for p,r in zip(left,right,strict=True) if good[p,r]]
    return {'references':len(r_area),'candidates':count,'matched':len(matches),
        'unmatched':count-len(matches),'unmatched_references':len(r_area)-len(matches),
        'outside_all_reference':int((overlap.sum(axis=1)==0).sum()),
        'empty_reference_images':int(not len(r_area)),
        'matching_regions':[{'candidate':p,'reference':r,'reference_coverage':a,'candidate_coverage':b}
                            for p,r,a,b in matches]}


def totals(rows):
    count = Counter()
    for row in rows:
        count.update({k:v for k,v in row.items() if isinstance(v,int)})
    result = dict(count)
    if count['candidates']:
        result['reference_precision'] = count['matched']/count['candidates']
    if count['references']:
        result['recall'] = count['matched']/count['references']
    result['meets_both_80_on_evaluated_reference'] = (
        result.get('reference_precision',0.)>=TARGET and result.get('recall',0.)>=TARGET)
    return result


def run(dataset, diagnostic, output):
    output = output_path(output,dataset)
    if (output/'summary.json').exists():
        raise ValueError('需要新的验收版本')
    report, identity = read(diagnostic/'report.json'),read(diagnostic/'identity.json')
    index = diagnostic/'case-index.json'
    if report['state']!='completed' or report['test_unlocked'] or identity['test_accessed']:
        raise ValueError('只读取旧验证缓存，正式测试继续封存')
    hashes = {r['id']:r['checkpoint_sha256'] for r in identity['checkpoints']}
    protocol = {'code_sha256':digest(__file__),'manifest_sha256':digest(dataset/'manifest-hashes.json'),
        'diagnostic_index_sha256':digest(index),'diagnostic_identity_sha256':digest(diagnostic/'identity.json'),
        'models':MODELS,'probability_threshold':.5,'split':'validation','reference_matched_minimum':REFERENCE_FLOOR,
        'candidate_matched_minimum':CANDIDATE_FLOOR,'class_precision_recall_target':TARGET,
        'outside_reference':'Require independent review; conservative denominator, never declared scientifically false by this score.',
        'test_accessed':False,'calibration_accessed':False}
    atomic_json(output/'protocol.json',protocol)
    available = read(index)
    results, stations = {}, {}
    for category, run_id in MODELS.items():
        source = SegmentationDataset(dataset,category,'validation')
        registered = {r['uri']:r for r in source.rows}
        chosen = [r for r in available if r['run_id']==run_id and r['diagnostic_subset']=='validation']
        if len(chosen)!=len(registered) or {r['uri'] for r in chosen}!=set(registered):
            raise ValueError('验证图像集合改变')
        cases = []
        groups = defaultdict(list)
        for row in chosen:
            item = registered[row['uri']]
            path = diagnostic/'probabilities'/run_id/(row['sample_id']+'.npy')
            if (item['mask_uri']!=row['mask_uri'] or row['split']!='validation'
                    or hashes[run_id]!=row['checkpoint_sha256'] or digest(path)!=row['probability_sha256']):
                raise ValueError('验证概率图或标签缓存身份不一致')
            values = np.load(path,allow_pickle=False)
            labels = np.load(source.source(item['mask_uri']),allow_pickle=False)
            if (values.shape!=labels.shape or not np.isfinite(values).all()
                    or (values<0).any() or (values>1).any()):
                raise ValueError('概率或标注网格不一致')
            outcome = pair_regions(values>=.5,labels,category)
            groups[row['instrument']].append(outcome)
            cases.append({'uri':row['uri'],'instrument':row['instrument'],'metrics':outcome})
        atomic_json(output/f'{category}-cases.json',cases)
        results[category] = {'images':len(cases),**totals([r['metrics'] for r in cases])}
        stations[category] = {tag:{'images':len(rows),**totals(rows)} for tag,rows in groups.items()}
    atomic_json(output/'summary.json',{'state':'completed','recorded_utc':now(),'results':results,'by_station':stations,
        'protocol_sha256':digest(output/'protocol.json'),'test_accessed':False,'calibration_accessed':False,
        'unattended_release_authorized':False})


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dataset','diagnostic','output'):
        parser.add_argument('--'+name,required=True,type=Path)
    run(**vars(parser.parse_args()))
