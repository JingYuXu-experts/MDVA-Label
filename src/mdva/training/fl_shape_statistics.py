"""Quantify paired region changes and time-block uncertainty for a shape study."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

from .ar_hmi_temporal_ranker import matching
from .durable import atomic_json
from .journal import read
from .runtime import digest


def totals(frame):
    count = frame['count']
    refs = frame['geometry']
    matched = [set(matching(v, [True] * count)[1]) for v in refs]
    return np.array([sum(map(len, matched)), count * len(refs),
                     sum(len(v['reference_ids']) for v in refs)]), matched


def paired(cases, target, baseline='control', repeats=2000):
    blocks = defaultdict(lambda: np.zeros((2, 3), dtype=int))
    gains = losses = 0
    for row in cases:
        base, found = totals(row['arms'][baseline])
        changed, new = totals(row['arms'][target])
        for old, after in zip(found, new, strict=True):
            gains += len(after - old)
            losses += len(old - after)
        date = datetime.strptime(row['file'][:8], '%Y%m%d')
        blocks[date.toordinal() // 27] += np.stack((base, changed))
    population = np.stack(list(blocks.values()))
    rng = np.random.default_rng(2026)
    indices = rng.integers(len(population), size=(repeats, len(population)))
    counts = population[indices].sum(axis=1)
    intervals = {}
    for position, metric in ((1, 'precision'), (2, 'recall')):
        valid = (counts[:, :, position] > 0).all(axis=1)
        ratio = counts[valid, :, 0] / counts[valid, :, position]
        intervals[metric] = {
            'interval': np.percentile(ratio[:, 1] - ratio[:, 0], [2.5, 97.5]).tolist() if valid.any() else None,
            'valid_repeats': int(valid.sum()),
        }
    return {'images': len(cases), 'blocks': len(population), 'gained_reference_records': gains,
            'lost_reference_records': losses, 'matched_delta_intervals': intervals}


def run(study):
    summary = read(study/'summary.json')
    if summary['state'] != 'completed' or summary['test_accessed']:
        raise ValueError('需要独立研究阶段的完整结果')
    checksums = {}
    results = {}
    for phase, arms in summary['results'].items():
        path = study/f'{phase}-cases.json'
        cases = read(path)
        checksums[phase] = digest(path)
        results[phase] = {}
        for arm in arms:
            if arm == 'control':
                continue
            result = paired(cases, arm)
            if result['gained_reference_records'] - result['lost_reference_records'] != arms[arm]['matched'] - arms['control']['matched']:
                raise ValueError('逐病例的净增量不一致')
            results[phase][arm] = result
    atomic_json(study/'paired-statistics.json', {'study_sha256':digest(study/'summary.json'),
        'cases_sha256':checksums, 'code_sha256':digest(__file__), 'results':results})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study', required=True, type=Path)
    run(parser.parse_args().study)
