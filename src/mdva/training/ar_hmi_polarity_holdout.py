"""Freeze and acquire a fresh development-only sample for the AR polarity comparison."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import numpy as np

from .ar_hmi_registration import review, snapshot
from .datasets import SegmentationDataset
from .durable import atomic_json
from .journal import read
from .runtime import digest, now, output_path


COUNT = 64
SEED = 20261119


def run(dataset, previous, output, remaining_after=None):
    dataset, previous = (Path(p).resolve() for p in (dataset, previous))
    output = output_path(output, dataset)
    if output.is_relative_to(previous):
        raise ValueError('新开发样本必须使用独立输出目录')
    old = read(previous/'summary.json')
    previous_plan = read(previous/'protocol.json')
    if (old['state'] != 'completed' or old['test_accessed'] or old['calibration_accessed']
            or old['protocol_sha256'] != digest(previous/'protocol.json')
            or previous_plan['data']['manifest-hashes.json'] != digest(dataset/'manifest-hashes.json')):
        raise ValueError('原开发样本和冻结清单不符')
    validation = SegmentationDataset(dataset, 'AR', 'validation')
    existing = {row['uri'] for row in old['results']}
    excluded_sha = None
    if remaining_after is not None:
        excluded = Path(remaining_after).resolve()
        if output.is_relative_to(excluded):
            raise ValueError('剩余样本使用独立输出目录')
        former, plan = (read(excluded/name) for name in ('summary.json', 'protocol.json'))
        prior_uris = {row['uri'] for row in former['results']}
        if (former['state'] != 'completed' or former['test_accessed'] or former['calibration_accessed']
                or former['protocol_sha256'] != digest(excluded/'protocol.json')
                or plan['previous_summary_sha256'] != digest(previous/'summary.json')
                or set(plan['selection']) != prior_uris or existing & prior_uris):
            raise ValueError('已开过的开发样本身份不符')
        existing.update(prior_uris)
        excluded_sha = digest(excluded/'summary.json')
    remaining = sorted((row for row in validation.rows if row['uri'] not in existing), key=lambda row: row['uri'])
    if remaining_after is None:
        selected = sorted(np.random.default_rng(SEED).choice(len(remaining), size=COUNT, replace=False))
        rows = [remaining[index] for index in selected]
    else:
        rows = remaining
    if not rows:
        raise ValueError('没有剩余的开发样本')
    protocol = {'previous_summary_sha256': digest(previous/'summary.json'),
                'dataset_sha256': digest(dataset/'manifest-hashes.json'), 'seed': SEED,
                'selection': [r['uri'] for r in rows], 'selected_without_scores_or_mask_sizes': True,
                'code_sha256': digest(__file__), 'test_accessed': False, 'calibration_accessed': False}
    if excluded_sha:
        protocol['remaining_after_summary_sha256'] = excluded_sha
    output.mkdir(parents=True, exist_ok=True)
    protocol_path = output/'protocol.json'
    if protocol_path.exists():
        if read(protocol_path) != protocol:
            raise ValueError('新开发样本的冻结身份发生变化')
    else:
        atomic_json(protocol_path, protocol)
    protocol_hash = digest(protocol_path)
    result = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        downloads = {}
        for index, row in enumerate(rows):
            if (output/'PAUSE').exists():
                return {'state': 'paused', 'complete': len(result)}
            for upcoming in range(index, min(index+6, len(rows))):
                folder = output/'pairs'/str(upcoming)
                if upcoming not in downloads and not (folder/'review.json').exists():
                    downloads[upcoming] = pool.submit(snapshot, rows[upcoming], folder)
            folder = output/'pairs'/str(index)
            saved = folder/'review.json'
            item = read(saved)
            if item is not None:
                if (item['protocol_sha256'] != protocol_hash or item['uri'] != row['uri']
                        or item['state']=='display_alignment_supported' and item['array_sha256'] != digest(folder/'aligned-los.npy')):
                    raise ValueError('已有开发磁图记录发生变化')
            else:
                try:
                    downloads.pop(index).result()
                    item = review(validation, row, 'validation', folder, None)
                except (OSError, ValueError, KeyError) as exc:
                    item = {'state': 'unavailable', 'error': str(exc), 'uri': row['uri'], 'split': 'validation'}
                item.update(id=str(index), lane='fresh_development', time_utc=row['time_utc'], protocol_sha256=protocol_hash)
                atomic_json(saved, item)
            result.append(item)
            if len(result) % 16 == 0:
                atomic_json(output/'progress.json', {'state': 'registering', 'complete': len(result), 'total': len(rows)})
    report = {'state': 'completed', 'created_utc': now(), 'results': result, 'counts': dict(Counter(r['state'] for r in result)),
              'protocol_sha256': protocol_hash, 'test_accessed': False, 'calibration_accessed': False}
    atomic_json(output/'summary.json', report)
    atomic_json(output/'progress.json', {'state': 'completed', 'complete': len(result), 'total': len(rows)})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('dataset', 'previous', 'output'):
        parser.add_argument('--'+key, required=True)
    parser.add_argument('--remaining-after')
    report = run(**vars(parser.parse_args()))
    print(json.dumps({key: report[key] for key in ('state', 'counts') if key in report}))


if __name__ == '__main__':
    main()
