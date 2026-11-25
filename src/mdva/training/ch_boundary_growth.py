"""Test whether conservative boundary growth recovers CH regions without new islands."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path

import numpy as np
from scipy.ndimage import binary_dilation, label

from .ch_region_filter_review import CONNECTIVITY, aggregate, region_replay
from .datasets import SegmentationDataset
from .durable import atomic_json
from .journal import read
from .metrics import binary_segmentation
from .runtime import digest, now, output_path


MODEL = 'CH-C2-s2026'
MINIMUM_AREA = 32
GROWTH_STEPS = 4


def remove_small(mask):
    components, _ = label(mask, structure=CONNECTIVITY)
    keep = np.bincount(components.ravel()) >= MINIMUM_AREA
    keep[0] = False
    return keep[components]


def candidate_masks(probability):
    if probability.shape != (512, 512) or not np.isfinite(probability).all():
        raise ValueError('需已核验的冕洞原生512网格')
    primary = remove_small(probability >= .5)
    support = probability >= .35
    grown = binary_dilation(primary, structure=CONNECTIVITY, iterations=GROWTH_STEPS, mask=support)
    return {'control': primary, 'bounded_growth': grown, 'lower_threshold': remove_small(support)}


def run(dataset, diagnostic, prior, output):
    dataset, diagnostic, prior = (Path(path).resolve() for path in (dataset, diagnostic, prior))
    output = output_path(output, dataset)
    if output.exists() or any(output.is_relative_to(path) for path in (diagnostic, prior)):
        raise ValueError('冕洞边界实验需要独立目录')
    previous = read(prior / 'summary.json')
    index_path = diagnostic / 'case-index.json'
    if (previous['test_accessed'] or previous['state'] != 'completed'
            or previous['protocol_sha256'] != digest(prior / 'protocol.json')
            or read(diagnostic / 'identity.json')['test_accessed']):
        raise ValueError('仅评估已有开发缓存与可复算的旧对照')
    data = SegmentationDataset(dataset, 'CH', 'validation')
    index = {row['uri']: row for row in read(index_path)
             if row['run_id'] == MODEL and row['diagnostic_subset'] == 'validation'}
    if len(data) != 526 or set(index) != {row['uri'] for row in data.rows}:
        raise ValueError('需要完整的526张冕洞开发样本')
    protocol = {'created_utc': now(), 'prior_sha256': digest(prior / 'summary.json'), 'index_sha256': digest(index_path),
                'manifest_sha256': digest(dataset / 'manifest-hashes.json'), 'code_sha256': digest(__file__),
                'minimum_pixels': MINIMUM_AREA, 'growth_radius_pixels': GROWTH_STEPS,
                'control_threshold': .5, 'growth_support_threshold': .35,
                'decision': {'min_region_recall_gain': .03, 'max_region_precision_loss': .01},
                'scope': 'All development images, 8-connected components, exact original reference mask IoU≥0.5.',
                'test_accessed': False, 'calibration_accessed': False}
    output.mkdir(parents=True)
    atomic_json(output / 'protocol.json', protocol)
    rows = []
    grouped = defaultdict(list)
    for row in data.rows:
        case = index[row['uri']]
        probability_file = diagnostic / 'probabilities' / MODEL / (case['sample_id'] + '.npy')
        if case['mask_uri'] != row['mask_uri'] or case['probability_sha256'] != digest(probability_file):
            raise ValueError('概率缓存或参考身份改变')
        probability = np.load(probability_file, allow_pickle=False)
        ground = np.load(data.source(row['mask_uri']), allow_pickle=False) > 0
        reference, _ = label(ground, structure=CONNECTIVITY)
        cases = {}
        baseline = None
        for name, mask in candidate_masks(probability).items():
            pixels = binary_segmentation(mask, ground)
            _, matched, stats = next(region_replay(mask, reference, pixels))
            if baseline is None:
                baseline = matched
                base_tp, base_fp = pixels['tp'], pixels['fp']
            stats.update(gained_matches=len(matched - baseline), lost_matches=len(baseline - matched),
                         reference_pixels_removed=base_tp - pixels['tp'], false_positive_pixels_removed=base_fp - pixels['fp'])
            cases[name] = stats
            grouped[('all', name)].append(stats)
            grouped[(row['time_utc'][:4], name)].append(stats)
        rows.append({'uri': row['uri'], 'time_utc': row['time_utc'], 'arms': cases})
    results = {f'{group}/{name}': aggregate(items) for (group, name), items in grouped.items()}
    base = results['all/control']
    old = previous['results'][str(MINIMUM_AREA)]
    fields = ('matched', 'predicted', 'precision', 'recall')
    if any(base[field] != old['regions'][field] for field in fields) or abs(base['dice']-old['metrics']['dice']) > 1e-12:
        raise ValueError('原冕洞区域对照未复现')
    after = results['all/bounded_growth']
    gate = (after['recall'] - base['recall'] >= protocol['decision']['min_region_recall_gain']
            and base['precision'] - after['precision'] <= protocol['decision']['max_region_precision_loss'])
    atomic_json(output / 'images.json', rows)
    summary = {'state': 'completed', 'created_utc': now(), 'results': results, 'pilot_gate_passed': gate,
               'no_topology_preservation_claim': True, 'test_accessed': False, 'calibration_accessed': False,
               'deployed': False, 'protocol_sha256': digest(output / 'protocol.json'),
               'images_sha256': digest(output / 'images.json')}
    atomic_json(output / 'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('dataset', 'diagnostic', 'prior', 'output'):
        parser.add_argument('--' + key, required=True)
    summary = run(**vars(parser.parse_args()))
    print(json.dumps({key: value for key, value in summary['results'].items() if key.startswith('all/')}))


if __name__ == '__main__':
    main()
