"""Measure how much the AR candidate area floor accounts for very small misses."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

import numpy as np

from .ar_region_retention import filter_by_area
from .ar_scale_review import native_objects
from .ar_seed_consensus import MODELS, PIXEL_THRESHOLD
from .datasets import SegmentationDataset
from .durable import atomic_json
from .journal import read
from .runtime import digest, now, output_path


AREA_FLOORS = (256, 64)


def band(size):
    return 'under128' if size < 128 else '128to255' if size < 256 else '256to1023' if size < 1024 else '1024plus'


def run(dataset, diagnostic, consensus, output):
    dataset, diagnostic, consensus = (Path(p).resolve() for p in (dataset, diagnostic, consensus))
    output = output_path(output, dataset)
    if output.exists() or any(output.is_relative_to(path) for path in (consensus, diagnostic)):
        raise ValueError('需要独立的面积审计目录')
    prior = read(consensus / 'summary.json')
    index_path = diagnostic / 'case-index.json'
    protocol = read(consensus / 'protocol.json')
    if (prior['test_accessed'] or prior['calibration_accessed'] or prior['state'] != 'completed'
            or prior['images_sha256'] != digest(consensus / 'images.json')
            or prior['protocol_sha256'] != digest(consensus / 'protocol.json')
            or protocol['index_sha256'] != digest(index_path) or protocol['threshold'] != PIXEL_THRESHOLD):
        raise ValueError('同图投票或概率缓存身份不符')
    data = SegmentationDataset(dataset, 'AR', 'validation')
    index = {(r['uri'], r['run_id']): r for r in read(index_path)
             if r['diagnostic_subset'] == 'validation' and r['run_id'] in MODELS}
    previous = {r['uri']: r['arms'] for r in read(consensus / 'images.json')}
    if len(data) != 276 or len(index) != len(data) * len(MODELS) or set(previous) != {r['uri'] for r in data.rows}:
        raise ValueError('开发范围需与原复核一致')

    by_size = {floor: {} for floor in AREA_FLOORS}
    totals = {floor: Counter() for floor in AREA_FLOORS}
    rows = []
    for row in data.rows:
        reference = np.load(data.source(row['mask_uri']), allow_pickle=False)
        ids, areas = np.unique(reference[reference > 0], return_counts=True)
        votes = np.zeros_like(reference, dtype=np.uint8)
        for model in MODELS:
            case = index[row['uri'], model]
            path = diagnostic / 'probabilities' / model / (case['sample_id'] + '.npy')
            if (case['mask_uri'] != row['mask_uri'] or case['checkpoint_sha256'] != protocol['models'][model]
                    or case['probability_sha256'] != digest(path)):
                raise ValueError('概率缓存身份变化')
            probability = np.load(path, allow_pickle=False)
            if probability.shape != reference.shape or not np.isfinite(probability).all():
                raise ValueError('缓存网格或数值无效')
            votes += probability >= PIXEL_THRESHOLD
        results = {floor: native_objects(filter_by_area(votes >= 2, floor), reference) for floor in AREA_FLOORS}
        if results[256] != previous[row['uri']]['majority']['objects']:
            raise ValueError('原投票结果未复现')
        for floor, result in results.items():
            totals[floor].update(predicted=result['predicted'], matched=result['matched'],
                                 false_positive=result['predicted'] - result['matched'])
            matched = set(result['matched_reference_ids'])
            for identifier, area in zip(ids, areas, strict=True):
                counts = by_size[floor].setdefault(band(area), Counter())
                # A candidate at least this large has IoU <= reference area / candidate area.
                counts.update(reference=1, matched=int(identifier in matched), impossible=int(area * 2 < floor))
        rows.append({'uri': row['uri'], 'area_floor_results': results})
    output.mkdir(parents=True)
    atomic_json(output / 'images.json', rows)
    result = {'state': 'completed', 'created_utc': now(), 'images': len(rows), 'by_size': by_size, 'totals': totals,
              'scope': 'Existing development cache, two fixed candidate area floors and same IoU=0.5 matching.',
              'images_sha256': digest(output / 'images.json'), 'consensus_sha256': digest(consensus / 'summary.json'),
              'test_accessed': False, 'calibration_accessed': False, 'released': False}
    atomic_json(output / 'summary.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('dataset', 'diagnostic', 'consensus', 'output'):
        parser.add_argument('--' + key, required=True)
    result = run(**vars(parser.parse_args()))
    print(json.dumps({key: result[key] for key in ('by_size', 'totals')}))


if __name__ == '__main__':
    main()
