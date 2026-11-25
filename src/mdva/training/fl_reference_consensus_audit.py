"""Identify FL misses that persist under independently corroborated references."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from itertools import combinations
import json
from pathlib import Path

import numpy as np
from scipy.ndimage import label

from .ar_hmi_temporal_ranker import object_scores
from .durable import atomic_json
from .fl_candidate_review_2021 import publicly_paired
from .fl_ensemble_study import CONNECTIVITY
from .fl_magfilo_audit import DATA_SHA256, raster
from .fl_magfilo_transfer import LABEL_SHA256
from .journal import read
from .runtime import digest, now, output_path


def reference_parts(mask):
    grid, count = label(mask, structure=CONNECTIVITY)
    sizes = np.bincount(grid.ravel(), minlength=count + 1)[1:]
    return grid, [{'area': int(pixels)} for pixels in sizes]


def corroboration(variants):
    parts = [reference_parts(v) for v in variants]
    best = [np.zeros(len(r), dtype=float) for _, r in parts]
    overlap_dice = []
    for i, j in combinations(range(len(parts)), 2):
        a, detections = parts[i]
        b, targets = parts[j]
        info = object_scores(a, detections, b, [True] * len(detections))[1]
        iou = np.asarray(info['iou'], dtype=float).reshape(len(detections), len(targets))
        best[i] = np.maximum(best[i], iou.max(axis=1, initial=0))
        best[j] = np.maximum(best[j], iou.max(axis=0, initial=0))
        total = int(variants[i].sum() + variants[j].sum())
        overlap_dice.append(2 * int((variants[i] & variants[j]).sum()) / total if total else None)
    return parts, best, overlap_dice


def run(dataset, verified, source, release, output):
    output = output_path(output, dataset)
    if output.exists() or output.is_relative_to(verified.resolve()):
        raise ValueError('标注一致性分析须使用新目录')
    prior = read(verified / 'summary.json')
    cached = verified / 'external-cases.json'
    if (prior['state'] != 'completed' or prior['cases_sha256'] != digest(cached)
            or prior['protocol_sha256'] != digest(verified / 'protocol.json')
            or digest(source) != DATA_SHA256 or digest(release) != LABEL_SHA256):
        raise ValueError('公开标注或已有模型结果身份不符')
    items, annotations = publicly_paired(source, release)
    cases = {r['name']: r for r in read(cached)}
    if len(cases) != len(items) or set(cases) != {r['file'] for r in items}:
        raise ValueError('图像与参考清单不同')
    scores = defaultdict(Counter)
    output_rows = []
    for row in items:
        case = cases[row['file']]
        refs = [raster(annotations[v['id']].values()) for v in row['annotators']]
        metadata, supported, overlaps = corroboration(refs)
        if len(refs) != len(case['geometry']) or 'shape_and_brightness' not in case['review']:
            raise ValueError('参考版本与已有模型评估不符')
        status = []
        for i, ((_, parts), corroborated) in enumerate(zip(metadata, supported, strict=True)):
            geometry = case['geometry'][i]
            sizes = [r['area'] for r in parts]
            ids = geometry['reference_ids']
            if geometry['reference_sizes'] != sizes or ids != list(range(1, len(sizes) + 1)):
                raise ValueError('参考连通区域未复现')
            maximum = np.asarray(geometry['iou'], dtype=float).reshape(len(case['features']), len(ids)).max(axis=0, initial=0)
            matched = set(case['review']['shape_and_brightness'][i]['matched_reference_ids'])
            for rid, area, concordance, max_iou in zip(ids, sizes, corroborated, maximum, strict=True):
                basis = ('single_annotator_only' if len(refs) == 1 else 'corroborated_at_iou_50' if concordance >= .5
                         else 'partial_annotator_overlap' if concordance > 0 else 'no_other_annotation_overlap')
                evidence = {'references': 1, 'review_matched': int(rid in matched), 'no_model_candidate_overlap': int(max_iou == 0)}
                scores[basis].update(evidence)
                status.append({'reference_id': rid, 'variant': i, 'area': area, 'basis': basis,
                               'best_alternative_iou': float(concordance), 'best_original_candidate_iou': float(max_iou),
                               'review_matched': rid in matched})
        output_rows.append({'image': row['file'], 'station': case['station'], 'variants': len(refs),
                            'paired_label_dice': overlaps, 'references': status})
    if (sum(v['references'] for v in scores.values()) != prior['metrics']['shape_and_brightness']['reference']
            or sum(v['review_matched'] for v in scores.values()) != prior['metrics']['shape_and_brightness']['matched']):
        raise ValueError('已有跨年评分未复现')
    medians = [np.median(r['paired_label_dice']) for r in output_rows if r['paired_label_dice'] and None not in r['paired_label_dice']]
    summary = {'state': 'completed', 'recorded_utc': now(), 'code_sha256': digest(__file__),
               'prior_sha256': digest(verified / 'summary.json'), 'images': len(output_rows), 'multi_annotated_images': len(medians),
               'median_human_pair_dice_per_image': float(np.median(medians)) if medians else None,
               'groups': {k: {**v, 'model_recall': v['review_matched'] / v['references']} for k, v in scores.items()},
               'scope': '2021 research images reused for diagnosis; counts per reference variant, not unique physical filaments',
               'test_accessed': False, 'calibration_accessed': False, 'training_started': False}
    atomic_json(output / 'images.json', output_rows)
    summary['images_sha256'] = digest(output / 'images.json')
    atomic_json(output / 'summary.json', summary)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ('dataset', 'verified', 'source', 'release', 'output'):
        parser.add_argument('--' + argument, type=Path, required=True)
    print(json.dumps(run(**vars(parser.parse_args())), ensure_ascii=False))
