"""Re-evaluate full-disk flare models with the forecast clock shifted to input availability."""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter
from datetime import timedelta
import json
from pathlib import Path

import numpy as np
from sklearn.metrics import average_precision_score

from .durable import atomic_json
from .forecast_disk_supervision import label_window
from .forecast_disk_xrs import ARMS, operating_point, scores, train_fit
from .journal import read
from .metrics import forecast_scores
from .runtime import digest, now, output_path
from .xrs_coverage import Coverage, utc


OFFSETS = (0, 70, 120)
PRIMARY = 70
HORIZON = timedelta(hours=24)
FEATURE_SETS = ('xrs', 'xrs_plus_raw', 'xrs_plus_raw_plus_regions')


def reanchor(row, delay, events, event_times, coverage):
    observed = utc(row['observed_utc'])
    if utc(row['target_end_utc']) != observed + HORIZON or row['split'] not in ('train', 'validation'):
        raise ValueError('输入不是固定的开发 24 小时监督窗口')
    issue = observed + timedelta(minutes=delay)
    end = issue + HORIZON
    start_idx = bisect_right(event_times, issue - timedelta(minutes=1))
    end_idx = bisect_right(event_times, end)
    minutes = coverage.window(issue.isoformat(), end.isoformat())
    value, reason, positive, uncertain = label_window(issue, end, events[start_idx:end_idx], minutes['complete'])
    return {
        **row, 'target': value, 'issue_utc': issue.isoformat(), 'target_end_utc': end.isoformat(),
        'state': reason, 'prior_target': row['target'], 'positive_event_ids': [event['id'] for event in positive],
        'uncertain_event_ids': [event['id'] for event in uncertain],
    }


def audit_events(path, first, last, receipt):
    catalogs = {row['name']: row['sha256'] for row in receipt['catalogue_snapshots']}
    events, times = [], []
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            event = json.loads(line)
            instant = utc(event['start'])
            if instant > last:
                break
            if instant < first:
                continue
            if catalogs.get(event['source_file']) != event['source_sha256']:
                raise ValueError('事件目录来源和摘要无法核验')
            if times and instant < times[-1]:
                raise ValueError('事件目录次序变化')
            events.append(event)
            times.append(instant)
    return events, times


def evaluate(rows, offset):
    training = [r for r in rows if r['split'] == 'train']
    dev = [r for r in rows if r['split'] == 'validation']
    truth = np.array([row['target'] for row in dev])
    prevalence = float(np.mean([r['target'] for r in training]))
    models, variants = {}, {}
    for name in FEATURE_SETS:
        keys = ARMS[name]
        model = train_fit(training, keys)
        prediction = scores(model, dev, keys)
        policy = operating_point(training, keys)
        variants[name] = {
            'research_metrics_at_0_5': forecast_scores(truth, prediction, climate=prevalence),
            'train_only_policy': (None if policy is None else {
                **policy, 'metrics': forecast_scores(truth, prediction, threshold=policy['threshold'], climate=prevalence)
            }),
        }
        models[name] = prediction
    return variants, models, truth, {'offset_minutes': offset, 'train': len(training), 'development': len(dev),
                                     'train_positive': sum(r['target'] for r in training), 'development_positive': int(truth.sum())}


def paired_gain(rows, predictions, y):
    timestamps = np.asarray([int(utc(r['issue_utc']).timestamp() // (86400 * 27)) for r in rows])
    blocks = [np.flatnonzero(timestamps == item) for item in np.unique(timestamps)]
    random = np.random.default_rng(2026)
    values = []
    for _ in range(2000):
        index = np.concatenate([blocks[i] for i in random.integers(0, len(blocks), len(blocks))])
        if np.unique(y[index]).size < 2:
            continue
        raw, regions = (predictions[k][index] for k in ('xrs_plus_raw', 'xrs_plus_raw_plus_regions'))
        values.append(average_precision_score(y[index], regions) - average_precision_score(y[index], raw))
    if not values:
        raise ValueError('可用的正负例不足以度量区域增量')
    return {'blocks': len(blocks), 'valid_resamples': len(values), 'ci95': np.quantile(values, [.025, .975]).tolist()}


def run(dataset, source, audit, receipt_path, output):
    output = output_path(output, Path(dataset))
    if output.exists() or any(output.is_relative_to(p.resolve()) for p in (source, audit)):
        raise ValueError('到齐时刻的标签实验须使用独立目录')
    input_file = source / 'rows.json'
    events_file, coverage_file = audit / 'events.jsonl', audit / 'coverage-index.json'
    xrs, official, receipt = read(source / 'summary.json'), read(audit / 'summary.json'), read(receipt_path)
    measured_minutes = receipt['delay_from_hmi_observation_seconds'] / 60
    if (xrs['state'] != 'completed' or xrs['test_accessed'] or xrs['calibration_accessed']
            or xrs['rows_sha256'] != digest(input_file) or official['artifacts']['events.jsonl'] != digest(events_file)
            or official['coverage_index_sha256'] != digest(coverage_file)
            or official['frozen_manifest_sha256'] != digest(Path(dataset) / 'manifest-hashes.json')
            or receipt['issued_forecast'] or not (PRIMARY - 1 <= measured_minutes <= PRIMARY)):
        raise ValueError('观测、事件目录或参考延迟身份不符')
    records = read(input_file)
    if any(r['split'] not in ('train', 'validation') or r['target'] not in (0, 1) for r in records):
        raise ValueError('只允许训练与开发的已知监督记录')
    first = min(utc(r['observed_utc']) for r in records) - timedelta(minutes=1)
    last = max(utc(r['target_end_utc']) for r in records) + timedelta(minutes=max(OFFSETS))
    events, times = audit_events(events_file, first, last, official)
    coverage = Coverage(read(coverage_file))
    alternatives = {delay: [reanchor(row, delay, events, times, coverage) for row in records] for delay in OFFSETS}
    if any(row['target'] != original['target'] for row, original in zip(alternatives[0], records, strict=True)):
        raise ValueError('零延迟监督无法复现原标签')
    earliest_dev = min(utc(r['issue_utc']) for r in alternatives[0] if r['split'] == 'validation')
    common = [
        index for index, versions in enumerate(zip(*(alternatives[k] for k in OFFSETS), strict=True))
        if all(r['target'] in (0, 1) for r in versions)
        and (versions[0]['split'] == 'validation' or utc(versions[-1]['target_end_utc']) < earliest_dev)
    ]
    label_changes = {}
    for delay, rows in alternatives.items():
        label_changes[delay] = {}
        for split in ('train', 'validation'):
            taken = [r for r in rows if r['split'] == split]
            label_changes[delay][split] = {
                'label_transitions': dict(Counter(f"{r['prior_target']}->{r['target']}" for r in taken)),
                'status': dict(Counter(r['state'] for r in taken)),
            }
    result, predictions = {}, {}
    for delay, rows in alternatives.items():
        matched = [rows[i] for i in common]
        arms, probabilities, truth, cohort = evaluate(matched, delay)
        dev = [r for r in matched if r['split'] == 'validation']
        result[delay] = {'cohort': cohort, 'comparison': arms, 'regions_added_ap_delta': paired_gain(dev, probabilities, truth)}
        predictions[delay] = [
            {'uri': r['uri'], 'issue_utc': r['issue_utc'], 'target': r['target'],
             'scores': {name: float(score[i]) for name, score in probabilities.items()}}
            for i, r in enumerate(dev)
        ]
    protocol = {
        'code_sha256': digest(__file__), 'xrs_rows_sha256': digest(input_file),
        'coverage_sha256': digest(coverage_file), 'catalog_sha256': digest(events_file),
        'joint_receipt_sha256': digest(receipt_path), 'minutes': OFFSETS, 'primary': PRIMARY,
        'comparison': 'Same eligible train/development windows after both hypothetical delays and split purge. '
                      'HMI and GOES history inputs stay anchored at the observation time.',
        'target': 'Full-disk M1.0+ onset in (issue, issue + 24h]', 'test_accessed': False, 'calibration_accessed': False,
    }
    atomic_json(output / 'protocol.json', protocol)
    atomic_json(output / 'windows.json', {
        delay:[{k:v for k,v in r.items() if k != 'features'} for r in rows] for delay, rows in alternatives.items()
    })
    atomic_json(output / 'predictions.json', predictions)
    summary = {
        'state': 'completed', 'created_utc': now(), 'zero_delay_reproduced': len(records),
        'label_changes': label_changes, 'result': result, 'protocol_sha256': digest(output / 'protocol.json'),
        'windows_sha256': digest(output / 'windows.json'), 'predictions_sha256': digest(output / 'predictions.json'),
        'operational_input_availability_verified': False, 'test_accessed': False,
        'calibration_accessed': False, 'issued_forecast': False,
    }
    atomic_json(output / 'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dataset', 'source', 'audit', 'receipt_path', 'output'):
        parser.add_argument('--' + name.replace('_', '-'), type=Path, required=True)
    result = run(**vars(parser.parse_args()))
    print(json.dumps({k: result[k] for k in ('state', 'zero_delay_reproduced', 'label_changes')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
