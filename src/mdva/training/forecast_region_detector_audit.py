"""Measure detector misses among linked forecast-region reference samples."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

import numpy as np
from scipy.ndimage import label
from scipy.optimize import linear_sum_assignment

from .datasets import SegmentationDataset
from .durable import atomic_json, commit
from .metrics import binary_segmentation
from .runtime import digest, now, output_path

MODEL = 'AR-A0-s42'


def match_reference(prediction, reference):
    """Reproduce the historical AR region matcher, retaining reference IDs."""
    components, count = label(prediction)
    regions = np.unique(reference[reference>0])
    areas = np.bincount(components.ravel(), minlength=count+1)[1:]
    intersection = np.zeros((count, len(regions)), dtype=np.int64)
    reference_areas = []
    for j, region in enumerate(regions):
        selected = reference == region
        intersection[:,j] = np.bincount(components[selected], minlength=count+1)[1:]
        reference_areas.append(int(selected.sum()))
    union = areas[:,None] + np.asarray(reference_areas)[None,:] - intersection
    scores = np.divide(intersection, union, out=np.zeros_like(union, dtype=float), where=union>0)
    matching = {int(region):None for region in regions}
    for pred, ref in zip(*linear_sum_assignment(-scores)):
        if scores[pred, ref] >= .5:
            matching[int(regions[ref])] = {'mask_iou':float(scores[pred,ref]), 'detected_area_pixels':int(areas[pred])}
    return matching, count


def run(dataset, joined, diagnostic, output):
    dataset, joined, diagnostic = map(lambda path:Path(path).resolve(), (dataset,joined,diagnostic))
    output = output_path(output,dataset)
    if output.exists() or any(output.is_relative_to(path) for path in (joined,diagnostic)):
        raise ValueError('独立审计必须使用新输出目录')
    join_summary = json.loads((joined/'summary.json').read_text(encoding='utf-8'))
    join_rows = joined/'development-joins.jsonl'
    if join_summary['join_rows_sha256']!=digest(join_rows) or join_summary['frozen_manifest_sha256']!=digest(dataset/'manifest-hashes.json'):
        raise ValueError('区域对应审计身份不符')
    data = SegmentationDataset(dataset,'AR','validation')
    expected = {r['uri']:r for r in data.rows}
    by_image = defaultdict(list)
    with join_rows.open(encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            if row['join_state']=='exact_reference_join' and row['split']==row['detector_split']=='validation':
                if row['image_uri'] not in expected or row['mask_uri']!=expected[row['image_uri']]['mask_uri']:
                    raise ValueError('审计只允许开发验证图像')
                by_image[row['image_uri']].append(row)
    index = diagnostic/'case-index.json'
    archived = {r['uri']:r for r in json.loads(index.read_text(encoding='utf-8'))
                if r['run_id']==MODEL and r['diagnostic_subset']=='validation'}
    if set(archived)!=set(expected):
        raise ValueError('原模型缓存验证集不一致')
    observations = []
    for uri, samples in by_image.items():
        row = archived[uri]
        probability = diagnostic/'probabilities'/MODEL/(row['sample_id']+'.npy')
        if digest(probability)!=row['probability_sha256'] or row['mask_uri']!=expected[uri]['mask_uri']:
            raise ValueError('模型缓存概率或原掩膜身份改变')
        values = np.load(probability,allow_pickle=False)
        reference = np.load(data.source(row['mask_uri']),allow_pickle=False)
        if values.shape!=reference.shape or not np.isfinite(values).all():
            raise ValueError('概率图与原生参考网格不符')
        prediction = values>=.5
        pixel_scores = binary_segmentation(prediction,reference>0)
        if any(pixel_scores[k]!=row[k] for k in ('tp','fp','fn')):
            raise ValueError('模型缓存与原评价计数不一致')
        matches, total = match_reference(prediction,reference)
        if total!=row['objects']['predicted_components'] or sum(v is not None for v in matches.values())!=row['objects']['matched']:
            raise ValueError('无法复算原区域匹配')
        for sample in samples:
            match = matches[sample['reference_instance']]
            observations.append({'sample_id':sample['sample_id'], 'image_uri':uri, 'noaa':sample['noaa'],
                                 'target':sample['target'], 'target_state':sample['target_state'],
                                 'detector_match':match, 'reference_used_for_association':True})
    cohort = Counter(str(row['target']) for row in observations)
    matched = Counter(str(row['target']) for row in observations if row['detector_match'])
    output.mkdir(parents=True)
    raw = ''.join(json.dumps(row, ensure_ascii=False)+'\n' for row in observations).encode('utf-8')
    commit(output/'rows.jsonl',lambda stream:stream.write(raw))
    report = {'state':'completed', 'created_utc':now(), 'model':MODEL, 'images':len(by_image), 'events_by_target':dict(cohort),
              'regions_detected_by_target':{key:matched[key] for key in cohort}, 'regions_missed_by_target':{key:cohort[key]-matched[key] for key in cohort},
              'scope':'Retrospective validation only. Reference masks used to score detector coverage and link observations to NOAA identities.',
              'join_sha256':digest(join_rows), 'diagnostic_index_sha256':digest(index), 'rows_sha256':digest(output/'rows.jsonl'), 'code_sha256':digest(__file__),
              'operational_noaa_identity_verified':False, 'test_accessed':False, 'forecast_trained':False}
    atomic_json(output/'summary.json',report)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--dataset',required=True)
    parser.add_argument('--joined',required=True)
    parser.add_argument('--diagnostic',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    result=run(args.dataset,args.joined,args.diagnostic,args.output)
    print(json.dumps({k:result[k] for k in ('images','events_by_target','regions_detected_by_target','regions_missed_by_target')}))
