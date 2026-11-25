"""Development-only full-disk flare labels independent of active-region numbers."""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import timedelta
import json
from pathlib import Path

from .datasets import SegmentationDataset
from .durable import atomic_json
from .journal import read
from .runtime import configure_environment, digest, now, output_path
from .xrs_coverage import Coverage, utc


def label_window(start, end, candidates, complete_coverage):
    certain, uncertain = [], []
    for event in candidates:
        onset = utc(event['start'])
        minute = timedelta(seconds=event['time_resolution_seconds'])
        if onset > end or onset + minute <= start:
            continue
        if onset > start and onset + minute <= end and not event['catalogue_duplicate_candidate']:
            certain.append(event)
        else:
            uncertain.append(event)
    if certain:
        return 1, 'catalogue_positive', certain, uncertain
    if uncertain:
        return None, 'unresolved_event_or_boundary', certain, uncertain
    if complete_coverage:
        return 0, 'catalogue_negative_quality_minutes_complete', certain, uncertain
    return None, 'incomplete_quality_minute_coverage', certain, uncertain


def run(dataset, audit, output):
    dataset, audit = Path(dataset).resolve(), Path(audit).resolve()
    output = output_path(output, dataset)
    if output.exists() or output.is_relative_to(audit):
        raise ValueError('全日面标注研究使用独立输出目录')
    source = read(audit / 'summary.json')
    events_file, coverage_file = audit / 'events.jsonl', audit / 'coverage-index.json'
    manifest = digest(dataset / 'manifest-hashes.json')
    if (
        source['frozen_manifest_sha256'] != manifest
        or source['artifacts']['events.jsonl'] != digest(events_file)
        or source['coverage_index_sha256'] != digest(coverage_file)
    ):
        raise ValueError('目录、观测覆盖或冻结清单身份不符')
    by_time = defaultdict(list)
    for split in ('train', 'validation'):
        for row in SegmentationDataset(dataset, 'AR', split).rows:
            stamp = utc(row['time_utc'])
            by_time[stamp].append({'uri': row['uri'], 'split': split, 'instrument': row['instrument']})
    if any(len({row['split'] for row in images}) != 1 for images in by_time.values()):
        raise ValueError('同一全日面观测跨划分')
    split_start = min(stamp for stamp, images in by_time.items() if images[0]['split'] == 'validation')
    if any(stamp >= split_start for stamp, images in by_time.items() if images[0]['split'] == 'train'):
        raise ValueError('全日面开发样本未按时间隔离')
    start_at, end_at = min(by_time) - timedelta(minutes=1), max(by_time) + timedelta(hours=24)
    event_rows = []
    with events_file.open(encoding='utf-8') as stream:
        for line in stream:
            event = json.loads(line)
            stamp = utc(event['start'])
            if stamp > end_at:
                break
            if stamp >= start_at:
                event_rows.append(event)
    event_times = [utc(event['start']) for event in event_rows]
    if event_times != sorted(event_times):
        raise ValueError('目录事件缺少时间顺序')
    coverage = Coverage(read(coverage_file))
    results = []
    for instant, images in sorted(by_time.items()):
        end = instant + timedelta(hours=24)
        split_boundary = images[0]['split'] == 'train' and end >= split_start
        time_scope = coverage.window(instant.isoformat(), end.isoformat())
        indexes = (bisect_right(event_times, instant - timedelta(minutes=1)), bisect_right(event_times, end))
        target, status, positives, uncertain = label_window(
            instant, end, event_rows[indexes[0]:indexes[1]], time_scope['complete'],
        )
        results.append({
            'observed_utc': instant.isoformat(), 'target_end_utc': end.isoformat(),
            'split': images[0]['split'], 'images': images, 'target': target, 'state': status,
            'positive_event_ids': [event['id'] for event in positives],
            'positives_without_region_id': [event['id'] for event in positives if event['resolved_noaa'] is None],
            'uncertain_events': [event['id'] for event in uncertain],
            'xrs_coverage': time_scope, 'split_boundary': split_boundary,
            'operational_input_availability_verified': False,
        })
    output.mkdir(parents=True)
    atomic_json(output / 'windows.json', results)
    report = {
        'state': 'completed', 'created_utc': now(),
        'target': 'Disk-integrated GOES XRS catalogue M1.0+ onset during (T,T+24 h]',
        'windows': len(results), 'independent_noaa_association': True,
        'by_split': {
            split: dict(Counter(row['state'] for row in results if row['split'] == split))
            for split in ('train', 'validation')
        },
        'positive_windows_with_unresolved_region': {
            split: sum(row['split'] == split and bool(row['positives_without_region_id']) for row in results)
            for split in ('train', 'validation')
        },
        'positive_windows_with_only_unresolved_regions': {
            split: sum(row['split'] == split and bool(row['positive_event_ids'])
                       and row['positive_event_ids'] == row['positives_without_region_id'] for row in results)
            for split in ('train', 'validation')
        },
        'training_windows_reaching_validation': sum(row['split_boundary'] for row in results),
        'source_audit_sha256': digest(audit / 'summary.json'), 'windows_sha256': digest(output / 'windows.json'),
        'code_sha256': digest(__file__), 'frozen_manifest_sha256': manifest,
        'scope': 'Historical full-disk supervision audit. A whole-disk event does not localize its origin.',
        'remaining': ['input availability', 'per-observation deduplication', 'split contamination and cutoff audit',
                      'independent model and operational evaluation'],
        'test_accessed': False, 'calibration_accessed': False, 'trained': False, 'operational': False,
    }
    atomic_json(output / 'summary.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dataset', 'audit', 'output'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    configure_environment()
    summary = run(**vars(args))
    print(json.dumps({key: summary[key] for key in (
        'windows', 'by_split', 'positive_windows_with_only_unresolved_regions'
    )}))


if __name__ == '__main__':
    main()
