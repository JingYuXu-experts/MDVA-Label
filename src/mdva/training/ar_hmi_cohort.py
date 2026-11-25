"""Acquire a bounded physical HMI cohort for train-only candidate fitting."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import numpy as np

from .ar_hmi_registration import review, snapshot
from .ar_scale_study import STRATA, load_plan
from .datasets import SegmentationDataset
from .durable import atomic_json, commit
from .journal import append, read
from .runtime import digest, now, output_path
from .xrs_coverage import utc


TRAIN_PER_STRATUM = 32
DEVELOPMENT_COUNT = 64
SEED = 2026


def choose(dataset, cohort, previous):
    train, dev = (SegmentationDataset(dataset, 'AR', split) for split in ('train', 'validation'))
    indexed = {split: {r['uri']: r for r in data.rows} for split, data in (('train', train), ('validation', dev))}
    rng = np.random.default_rng(SEED)
    original_train = {r['uri'] for r in previous if r['split'] == 'train'}
    selected = set()
    for stratum in STRATA:
        all_ids = {r['uri'] for r in cohort if r['trial_stratum'] == stratum}
        keep = all_ids & original_train
        selected.update(keep)
        selected.update(rng.choice(sorted(all_ids - keep), TRAIN_PER_STRATUM-len(keep), replace=False).tolist())
    archive_dev = {r['uri'] for r in previous if r['split'] == 'validation'}
    independent = rng.choice(sorted(set(indexed['validation']) - archive_dev), DEVELOPMENT_COUNT, replace=False)
    rows = [{'lane': 'training', **indexed['train'][uri]} for uri in selected]
    rows += [{'lane': 'development', **indexed['validation'][uri]} for uri in independent]
    rows += [{'lane': 'small_region_stress', **indexed['validation'][uri]} for uri in archive_dev]
    rows.sort(key=lambda r: (r['lane'], r['time_utc'], r['uri']))
    assert len(rows) == 3*TRAIN_PER_STRATUM + DEVELOPMENT_COUNT + len(archive_dev)
    if max(utc(r['time_utc']) for r in rows if r['lane'] == 'training') >= min(
        utc(r['time_utc']) for r in rows if r['lane'] != 'training'
    ):
        raise ValueError('物理输入训练与开发未按时间隔离')
    return rows, {'train': train, 'validation': dev}


def expand_small(dataset, previous):
    train, dev = (SegmentationDataset(dataset, 'AR', split) for split in ('train', 'validation'))
    sources = {'train': train, 'validation': dev}
    indexed = {split: {row['uri']: row for row in source.rows} for split, source in sources.items()}
    old_uris = {row['uri'] for row in previous}
    rows = [{**indexed[item['split']][item['uri']], 'lane': item['lane']} for item in previous]
    rng = np.random.default_rng(SEED)
    buckets = {}
    for row in train.rows:
        sizes = [area for identity, area in row['mask_statistics']['pixels'].items() if identity != '0']
        if row['uri'] in old_uris or not sizes or min(sizes) >= 512:
            continue
        key = row['time_utc'][:4], min(sizes) < 256
        buckets.setdefault(key, []).append(row)
    for _, items in sorted(buckets.items()):
        items.sort(key=lambda row: row['uri'])
        for index in rng.choice(len(items), size=min(8, len(items)), replace=False):
            rows.append({**items[int(index)], 'lane': 'training'})
    rows.sort(key=lambda row: (row['lane'], row['time_utc'], row['uri']))
    return rows, sources


def run(study, previous, output, small_expansion=False):
    study, previous = (Path(p).resolve() for p in (study, previous))
    origin = load_plan(study)
    output = output_path(output, origin['dataset'])
    if any(output.is_relative_to(path) for path in (study, previous)):
        raise ValueError('训练候选物理图源需独立版本')
    old = read(previous / 'summary.json')
    if (old['state'] != 'completed' or old['test_accessed'] or old['calibration_accessed']
            or old['protocol_sha256'] != digest(previous / 'protocol.json')
            or read(previous / 'protocol.json')['data'] != origin['data_identity']):
        raise ValueError('原配准审计和清单身份不符')
    if small_expansion:
        if any(row.get('lane') not in ('training', 'development', 'small_region_stress') for row in old['results']):
            raise ValueError('扩充必须以已冻结的三组队列为起点')
        rows, sources = expand_small(origin['dataset'], old['results'])
    else:
        rows, sources = choose(origin['dataset'], read(study / 'cohort.json'), old['results'])
    cached = {r['uri']: previous / 'pairs' / r['id'] for r in old['results']}
    reusable = {r['uri']: r for r in old['results']} if small_expansion else {}
    protocol = {'data': origin['data_identity'], 'study_sha256': digest(study / 'protocol.json'),
                'previous_sha256': digest(previous / 'summary.json'), 'source_code_sha256': digest(__file__),
                'registration_code_sha256': digest(Path(__file__).with_name('ar_hmi_registration.py')),
                'seed': SEED, 'small_expansion': small_expansion,
                'selection': [{'id': str(i), 'uri': r['uri'], 'lane': r['lane'],
                                             'time_utc': r['time_utc'], 'split': r['split']} for i, r in enumerate(rows)],
                'inference_available_then': False, 'test_accessed': False, 'calibration_accessed': False}
    output.mkdir(parents=True, exist_ok=True)
    plan = output / 'protocol.json'
    if plan.exists():
        if read(plan) != protocol:
            raise ValueError('缓存目录里的选择或程序版本发生改变')
    else:
        atomic_json(plan, protocol)
        append(study.parents[1]/'training-20261112-v2', 'development', '物理磁图队列扩充',
               '按已冻结名单获取带单位的活动区磁图，独立保留开发与尺寸压力样本。')

    fingerprint = digest(plan)
    records = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        downloads = {}
        for i, row in enumerate(rows):
            if (output / 'PAUSE').exists():
                return {'state': 'paused', 'complete': len(records)}
            # Bounded read-ahead. FITS inspection and registration remain serial.
            for next_id in range(i, min(i+6, len(rows))):
                folder = output / 'pairs' / str(next_id)
                next_row = rows[next_id]
                if (next_id not in downloads and not (folder / 'review.json').exists()
                        and next_row['uri'] not in reusable):
                    downloads[next_id] = pool.submit(snapshot, next_row, folder, cached.get(next_row['uri']))
            folder = output / 'pairs' / str(i)
            review_path = folder / 'review.json'
            record = read(review_path)
            if record:
                if record['protocol_sha256'] != fingerprint or record['uri'] != row['uri']:
                    raise ValueError('物理缓存不对应已冻结样本')
                if record['state'] == 'display_alignment_supported' and digest(folder/'aligned-los.npy') != record['array_sha256']:
                    raise ValueError('物理对齐缓存改变')
            else:
                result = reusable.get(row['uri'])
                if result is not None:
                    if result['state'] == 'display_alignment_supported':
                        original = cached[row['uri']]/'aligned-los.npy'
                        if digest(original) != result['array_sha256']:
                            raise ValueError('原物理对齐数组内容发生变化')
                        # Preserve the official FITS and aligned grid for downstream geometry checks.
                        snapshot(row, folder, cached[row['uri']])
                        commit(folder/'aligned-los.npy', lambda stream: stream.write(original.read_bytes()))
                else:
                    try:
                        downloads.pop(i).result()
                        result = review(sources[row['split']], row, row['split'], folder, cached.get(row['uri']))
                    except (OSError, ValueError, KeyError) as exc:
                        result = {'state': 'unavailable', 'uri': row['uri'], 'split': row['split'],
                                  'error': f'{type(exc).__name__}: {exc}'}
                record = {**result, 'id': str(i), 'lane': row['lane'], 'time_utc': row['time_utc'],
                          'protocol_sha256': fingerprint}
                atomic_json(review_path, record)
            records.append(record)
            if len(records) % 16 == 0:
                progress = {'state': 'registering', 'complete': len(records), 'total': len(rows),
                            'accepted': sum(r['state'] == 'display_alignment_supported' for r in records)}
                atomic_json(output / 'progress.json', progress)
                print(json.dumps(progress), flush=True)
    counts = {lane: dict(Counter(r['state'] for r in records if r['lane'] == lane)) for lane in ('training', 'development', 'small_region_stress')}
    result = {'state': 'completed', 'created_utc': now(), 'complete': len(rows), 'registered_by_lane': counts,
              'results': records, 'protocol_sha256': fingerprint, 'test_accessed': False, 'calibration_accessed': False}
    atomic_json(output / 'summary.json', result)
    atomic_json(output / 'progress.json', {'state': 'completed', 'complete': len(rows), 'total': len(rows)})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('study', 'previous', 'output'):
        parser.add_argument('--' + key, required=True)
    parser.add_argument('--small-expansion', action='store_true')
    result = run(**vars(parser.parse_args()))
    print(json.dumps({key: result[key] for key in ('state', 'complete', 'registered_by_lane') if key in result}))


if __name__ == '__main__':
    main()
