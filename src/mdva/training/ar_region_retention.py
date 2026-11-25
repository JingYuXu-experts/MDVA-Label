"""Replay fixed AR postprocessing candidates and audit which regions remain."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path

import numpy as np
from scipy.ndimage import label

from .datasets import SegmentationDataset
from .durable import atomic_json
from .forecast_region_detector_audit import MODEL, match_reference
from .runtime import configure_environment, digest, now, output_path


OPERATING_POINTS = {
    '0.50/1': (.5, 1),
    '0.40/16': (.4, 16),
    '0.40/64': (.4, 64),
    '0.50/64': (.5, 64),
}
BASELINE = '0.50/1'


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def filter_by_area(prediction, minimum):
    if minimum <= 1:
        return prediction
    components, _ = label(prediction)
    keep = np.bincount(components.ravel()) >= minimum
    keep[0] = False
    return keep[components]


def run(dataset, diagnostic, points, joined, output):
    dataset, diagnostic, points, joined = (
        Path(path).resolve() for path in (dataset, diagnostic, points, joined)
    )
    output = output_path(output, dataset)
    if output.exists() or any(output.is_relative_to(path) for path in (diagnostic, points, joined)):
        raise ValueError('区域复核必须使用新输出目录')

    join_summary = read_json(joined / 'summary.json')
    join_path = joined / 'development-joins.jsonl'
    manifest_hash = digest(dataset / 'manifest-hashes.json')
    join_hash = digest(join_path)
    index_path = diagnostic / 'case-index.json'
    index_hash = digest(index_path)
    points_protocol = points / 'protocol.json'
    points_summary = points / 'summary.json'
    if (
        join_summary['frozen_manifest_sha256'] != manifest_hash
        or join_summary['join_rows_sha256'] != join_hash
        or read_json(points_protocol)['diagnostic_index_sha256'] != index_hash
        or read_json(points_summary)['protocol_sha256'] != digest(points_protocol)
    ):
        raise ValueError('输入快照身份不匹配')

    data = SegmentationDataset(dataset, 'AR', 'validation')
    expected = {row['uri']: row for row in data.rows}
    index = {
        row['uri']: row for row in read_json(index_path)
        if row['run_id'] == MODEL and row['diagnostic_subset'] == 'validation'
    }
    recorded = {
        key: {row['uri']: row for row in rows}
        for key, rows in read_json(points / 'rows.json').items()
        if key in OPERATING_POINTS
    }
    if set(index) != set(expected) or any(set(recorded[key]) != set(expected) for key in OPERATING_POINTS):
        raise ValueError('复核必须覆盖同一组完整验证图像')

    linked = defaultdict(list)
    with join_path.open(encoding='utf-8') as stream:
        for line in stream:
            row = json.loads(line)
            if row['join_state'] != 'exact_reference_join' or row['split'] != 'validation':
                continue
            uri = row['image_uri']
            if row['detector_split'] != 'validation' or row['mask_uri'] != expected.get(uri, {}).get('mask_uri'):
                raise ValueError('预测对应超出验证范围或掩膜不匹配')
            linked[uri].append(row)

    image_rows = []
    forecast_rows = []
    months = defaultdict(Counter)
    stratified = defaultdict(Counter)
    transitions = defaultdict(Counter)
    for uri, source in expected.items():
        case = index[uri]
        probability_path = diagnostic / 'probabilities' / MODEL / (case['sample_id'] + '.npy')
        if case['mask_uri'] != source['mask_uri'] or digest(probability_path) != case['probability_sha256']:
            raise ValueError('缓存或参考掩膜身份不符')
        probabilities = np.load(probability_path, allow_pickle=False)
        reference = np.load(data.source(source['mask_uri']), allow_pickle=False)
        if probabilities.shape != reference.shape or not np.isfinite(probabilities).all():
            raise ValueError('缓存概率网格不符合原生掩膜')

        matches_by_point = {}
        month = datetime.fromisoformat(recorded[BASELINE][uri]['time']).astimezone(timezone.utc).strftime('%Y-%m')
        report_row = {'uri': uri, 'utc_month': month, 'options': {}}
        for key, (threshold, floor) in OPERATING_POINTS.items():
            # Area settings refer to an equivalent 512-by-512 pixel grid.
            native_floor = int(np.ceil(floor * reference.size / 512**2)) if floor > 1 else 1
            prediction = filter_by_area(probabilities >= threshold, native_floor)
            matches, predicted = match_reference(prediction, reference)
            found = {instance for instance, match in matches.items() if match}
            matches_by_point[key] = found
            baseline = matches_by_point[BASELINE]
            counts = {
                'references': len(matches), 'predicted': predicted, 'matched': len(found),
                'gained': len(found - baseline), 'lost': len(baseline - found),
            }
            old = recorded[key][uri]
            if (counts['references'], predicted, len(found)) != (old['regions'], old['predicted'], old['matched']):
                raise ValueError(f'无法复算区域评价：{key} {uri}')
            months[(month, key)].update(counts)
            transitions[key].update(counts)
            report_row['options'][key] = counts
        image_rows.append(report_row)

        for row in linked[uri]:
            instance = row['reference_instance']
            if not np.any(reference == instance):
                raise ValueError('归档活动区实例不在原生参考掩膜中')
            results = {key: instance in found for key, found in matches_by_point.items()}
            baseline = results[BASELINE]
            for key, found in results.items():
                stratified[(str(row['target']), key)].update({
                    'observations': 1, 'matched': int(found),
                    'gained': int(found and not baseline), 'lost': int(baseline and not found),
                })
            forecast_rows.append({
                'sample_id': row['sample_id'], 'image_uri': uri, 'noaa': row['noaa'],
                'reference_instance': instance, 'target': row['target'], 'matched': results,
            })

    def rates(counts):
        return {
            **counts,
            'precision': counts['matched'] / counts['predicted'] if counts['predicted'] else None,
            'recall': counts['matched'] / counts['references'] if counts['references'] else None,
        }

    output.mkdir(parents=True)
    atomic_json(output / 'images.json', image_rows)
    atomic_json(output / 'forecast-retention.json', forecast_rows)
    report = {
        'state': 'completed', 'created_utc': now(), 'model': MODEL, 'images': len(image_rows),
        'by_point': {key: rates(counts) for key, counts in transitions.items()},
        'by_utc_month': {f'{month} {key}': rates(counts) for (month, key), counts in sorted(months.items())},
        'by_forecast_target': {f'{target} {key}': dict(counts) for (target, key), counts in sorted(stratified.items())},
        'manifest_sha256': manifest_hash, 'join_sha256': join_hash, 'index_sha256': index_hash,
        'operating_point_rows_sha256': digest(points / 'rows.json'),
        'forecast_retention_sha256': digest(output / 'forecast-retention.json'),
        'images_sha256': digest(output / 'images.json'), 'code_sha256': digest(__file__),
        'interpretation': 'Development replay only. NOAA identities supplied by references for scoring; no forecast or release validation.',
        'test_accessed': False, 'calibration_accessed': False, 'production_policy_changed': False,
    }
    atomic_json(output / 'summary.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dataset', 'diagnostic', 'points', 'joined', 'output'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    configure_environment()
    report = run(**vars(args))
    print(json.dumps({'images': report['images'], 'by_point': report['by_point'],
                      'positive_windows': {key: value for key, value in report['by_forecast_target'].items()
                                           if key.startswith('1 ')}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
