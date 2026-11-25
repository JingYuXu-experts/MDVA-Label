"""Quantify fragmentation and limited area filtering of the CH spatial pilot."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

import numpy as np
from scipy.ndimage import label

from .ar_hmi_temporal_ranker import build_metrics, matching, object_scores
from .ch_boundary_growth import CONNECTIVITY
from .datasets import SegmentationDataset
from .durable import atomic_json
from .journal import read
from .runtime import digest, now, output_path


MODEL = 'aia171_193_211'
POLICIES = (32, 128, 256, 512)


def regions(guess, reference):
    candidates, count = label(guess, structure=CONNECTIVITY)
    refs = label(reference, structure=CONNECTIVITY)[0]
    sizes = np.bincount(candidates.ravel(), minlength=count + 1)[1:]
    parts = [{'area': int(n)} for n in sizes]
    geometry = object_scores(candidates, parts, refs, [True] * count)[1]
    overlap = np.asarray(geometry['iou'], dtype=float).reshape(count, len(geometry['reference_ids']))
    is_match, found = matching(geometry, [True] * count)
    matched = set(is_match.tolist())
    had_coverage = overlap.max(axis=1, initial=0.)
    extents = overlap.max(axis=0, initial=0.)
    return {'size_histogram': {f'{a}-{b}': {'predicted': int(((sizes >= a) & (sizes < b)).sum()),
             'matched': sum(a <= int(sizes[i]) < b for i in matched)}
             for a, b in ((32, 128), (128, 256), (256, 512), (512, 262145))},
            'unmatched_without_overlap': int((had_coverage == 0).sum()),
            'unmatched_partial_overlap': int((had_coverage > 0).sum()) - len(matched),
            'missed_refs_without_overlap': int((extents == 0).sum()), 'matched_reference_count': len(found),
            'policies': {str(n): {k: v for k, v in build_metrics(geometry, (sizes >= n).tolist())[0].items()
                                if k in ('matched', 'reference', 'predicted')} for n in POLICIES}}


def summarize(rows):
    metrics = {}
    for threshold in POLICIES:
        totals = Counter()
        for row in rows:
            totals.update(row['policies'][str(threshold)])
        metrics[str(threshold)] = {**totals, 'unmatched': totals['predicted'] - totals['matched'],
                                   'precision': totals['matched'] / totals['predicted'] if totals['predicted'] else None,
                                   'recall': totals['matched'] / totals['reference'] if totals['reference'] else None}
    return metrics


def run(dataset, pilot, output):
    output = output_path(output, dataset)
    if output.exists() or output.is_relative_to(pilot.resolve()):
        raise ValueError('误差定位须使用独立报告目录')
    source = read(pilot / 'summary.json')
    input_path = pilot / 'cases.json'
    if (source['state'] != 'completed' or source['cases_sha256'] != digest(input_path)
            or any(source[k] for k in ('test_accessed', 'validation_accessed', 'calibration_accessed'))):
        raise ValueError('空间模型对照身份不符')
    data = SegmentationDataset(dataset, 'CH', 'train')
    uri_map = {r['uri']: r['mask_uri'] for r in data.rows}
    results = {}
    for name, cases in read(input_path).items():
        calculated = []
        for row in cases:
            card = row['arms'][MODEL]
            asset = (pilot / card['mask_artifact']).resolve()
            if not asset.is_relative_to(pilot.resolve()) or digest(asset) != card['mask_sha256']:
                raise ValueError('圈选掩膜指纹不符')
            with np.load(asset, allow_pickle=False) as archive:
                support, guess = archive['support'], archive['prediction']
            ref = np.load(data.source(uri_map[row['uri']]), allow_pickle=False) > 0
            new = regions(guess, ref & support)
            if new['policies']['32'] != card['within_support']:
                raise ValueError('原始区域结果未复现')
            calculated.append({'uri': row['uri'], **new})
        results[name] = calculated
    earlier = summarize(results['earlier'])
    control = earlier['32']
    chosen = max(int(n) for n, score in earlier.items() if score['matched'] == control['matched'])
    held = summarize(results['fresh'])
    sizes = {key: {m: sum(r['size_histogram'][key][m] for r in results['fresh']) for m in ('predicted', 'matched')}
             for key in results['fresh'][0]['size_histogram']}
    totals = {k: sum(r[k] for r in results['fresh']) for k in ('unmatched_without_overlap', 'unmatched_partial_overlap',
              'missed_refs_without_overlap', 'matched_reference_count')}
    summary = {'state': 'completed', 'recorded_utc': now(), 'code_sha256': digest(__file__),
               'spatial_summary_sha256': digest(pilot / 'summary.json'), 'rule': 'choose largest area without fewer matches on earlier 8 dates',
               'chosen_area': chosen, 'earlier_threshold_comparison': earlier, 'fresh_size_histogram': sizes,
               'fresh_error_counts': totals, 'fresh_baseline': held['32'], 'fresh_chosen_area': held[str(chosen)],
               'scope': 'date-held research cohort reused for diagnosis; no threshold chosen from it', 'deployed': False}
    atomic_json(output / 'images.json', results)
    summary['images_sha256'] = digest(output / 'images.json')
    atomic_json(output / 'summary.json', summary)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ('dataset', 'pilot', 'output'):
        parser.add_argument('--' + argument, type=Path, required=True)
    print(json.dumps(run(**vars(parser.parse_args())), ensure_ascii=False))
