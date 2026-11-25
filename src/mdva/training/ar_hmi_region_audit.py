"""Audit signed LOS support for detected, missed and unmatched regions on paired images."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import find_objects, label

from .ar_component_verifier import match_components
from .ar_hmi_registration import correlation
from .ar_region_retention import filter_by_area
from .ar_scale_review import native_objects
from .datasets import SegmentationDataset
from .durable import atomic_json
from .forecast_region_detector_audit import MODEL
from .journal import read
from .runtime import digest, now, output_path


def measure(field, gray, window, pixels):
    y, x = window
    ys = slice(max(0, y.start-20), min(field.shape[0], y.stop+20))
    xs = slice(max(0, x.start-20), min(field.shape[1], x.stop+20))
    aligned = field[window][pixels]
    valid = np.isfinite(aligned)
    fraction = float(valid.mean())
    local = correlation(np.clip(field[ys, xs], -500, 500), gray[ys, xs])
    if fraction < .9 or local is None or local < .6:
        return {'state': 'local_support_insufficient', 'finite_fraction': fraction, 'local_correlation': local}
    values = aligned[valid]
    rgb = gray[window][pixels][valid].astype(float)
    positive, negative = (float(np.mean(test)) for test in (values >= 50, values <= -50))
    return {'state': 'measured', 'finite_fraction': fraction, 'local_correlation': local,
            'absolute_p95_gauss': float(np.percentile(np.abs(values), 95)),
            'positive_ge_50g_fraction': positive, 'negative_le_minus_50g_fraction': negative,
            'minimum_polarity_fraction': min(positive, negative),
            'jpeg_absolute_p95_gray_levels': float(np.percentile(np.abs(rgb-127.5), 95))}


def comparable_scores(records):
    from sklearn.metrics import average_precision_score

    chosen = [x for x in records if x['state'] == 'measured' and x['area'] < 1024
              and x['group'] in ('matched_prediction', 'no_reference_overlap')]
    truth = np.array([int(x['group'] == 'matched_prediction') for x in chosen])
    result = {'candidate_count': len(chosen), 'matched_count': int(truth.sum()),
              'image_count': len({row['uri'] for row in chosen}), 'ranked_metrics': None}
    if len(set(truth)) == 2:
        fields = ('absolute_p95_gauss', 'minimum_polarity_fraction', 'jpeg_absolute_p95_gray_levels')
        result['ranked_metrics'] = {field: float(average_precision_score(truth, [x[field] for x in chosen]))
                                    for field in fields}
    return result


def run(dataset, registration, diagnostic, retention, output):
    dataset, registration, diagnostic, retention = (Path(p).resolve() for p in (dataset, registration, diagnostic, retention))
    output = output_path(output, dataset)
    if output.exists() or any(output.is_relative_to(path) for path in (registration, diagnostic, retention)):
        raise ValueError('磁场区域对照需要独立新目录')
    version, source = (read(registration / name) for name in ('protocol.json', 'summary.json'))
    old = read(retention / 'summary.json')
    index_path = diagnostic / 'case-index.json'
    manifest = digest(dataset / 'manifest-hashes.json')
    if (source['test_accessed'] or source['calibration_accessed'] or old['test_accessed']
            or source['protocol_sha256'] != digest(registration / 'protocol.json')
            or version['data']['manifest-hashes.json'] != manifest or old['manifest_sha256'] != manifest
            or old['index_sha256'] != digest(index_path) or old['images_sha256'] != digest(retention / 'images.json')):
        raise ValueError('磁图、候选缓存或冻结样本身份不对')
    data = SegmentationDataset(dataset, 'AR', 'validation')
    by_uri = {x['uri']: x for x in data.rows}
    index = {x['uri']: x for x in read(index_path) if x['run_id'] == MODEL and x['diagnostic_subset'] == 'validation'}
    expected = {x['uri']: x['options']['0.40/16'] for x in read(retention / 'images.json')}
    rows = []
    used = []
    for registered in source['results']:
        if registered['state'] != 'display_alignment_supported' or registered['split'] != 'validation':
            continue
        uri, identifier = registered['uri'], registered['id']
        row = by_uri[uri]
        field_path = registration / 'pairs' / identifier / 'aligned-los.npy'
        case = index[uri]
        p = diagnostic / 'probabilities' / MODEL / (case['sample_id'] + '.npy')
        if (digest(field_path) != registered['array_sha256'] or case['mask_uri'] != row['mask_uri']
                or digest(p) != case['probability_sha256']):
            raise ValueError('配准磁图或概率缓存内容改变')
        field = np.load(field_path, allow_pickle=False)
        with Image.open(data.source(uri)) as file:
            gray = np.array(file.convert('L'))
        probability = np.load(p, allow_pickle=False)
        reference = np.load(data.source(row['mask_uri']), allow_pickle=False)
        if (not field.shape == gray.shape == probability.shape == reference.shape
                or not np.isfinite(probability).all()):
            raise ValueError('磁图和候选必须是同一网格且概率有效')
        prediction = filter_by_area(probability >= .4, 64)
        objects = native_objects(prediction, reference)
        if (objects['reference'], objects['predicted'], objects['matched']) != tuple(
            expected[uri][key] for key in ('references', 'predicted', 'matched')
        ):
            raise ValueError('候选无法复现相同的原评价')
        labels, count = label(prediction)
        matches = match_components(labels, reference, count)
        used.append(uri)
        for index_id, window in enumerate(find_objects(labels), 1):
            pixels = labels[window] == index_id
            overlap = (reference[window][pixels] > 0).any()
            group = 'matched_prediction' if matches[index_id-1] else ('unmatched_overlap' if overlap else 'no_reference_overlap')
            results = measure(field, gray, window, pixels)
            rows.append({'uri': uri, 'group': group, 'area': int(pixels.sum()), 'component': index_id, **results})
        found = set(objects['matched_reference_ids'])
        for index_id, window in enumerate(find_objects(reference), 1):
            if window is None or index_id in found:
                continue
            pixels = reference[window] == index_id
            area = int(pixels.sum())
            if area < 256:
                rows.append({'uri': uri, 'group': 'missed_reference_under256', 'area': area, 'reference_id': index_id,
                             **measure(field, gray, window, pixels)})
    grouped = defaultdict(list)
    for row in rows:
        grouped[row['group']].append(row)
    counts = {group: dict(Counter(row['state'] for row in items)) for group, items in grouped.items()}
    stats = {}
    for group, items in grouped.items():
        selected = [row for row in items if row['state'] == 'measured' and row['area'] < 1024]
        if selected:
            stats[group] = {'small_samples': len(selected), 'median_absolute_p95_gauss':
                            float(np.median([row['absolute_p95_gauss'] for row in selected])),
                            'median_minimum_polarity_fraction':
                            float(np.median([row['minimum_polarity_fraction'] for row in selected]))}
    output.mkdir(parents=True)
    atomic_json(output / 'regions.json', rows)
    result = {'state': 'completed', 'created_utc': now(), 'images': len(used), 'counts': counts,
              'small_region_stats': stats, 'small_candidates_ranking': comparable_scores(rows),
              'scope': 'Targeted development sample, locally supported display matching, signed LOS fields in Gauss. '
                       'No-reference candidates are not certified negatives. A 50 G threshold is descriptive only.',
              'registration_sha256': digest(registration / 'summary.json'), 'rows_sha256': digest(output / 'regions.json'),
              'training_allowed': False, 'released': False, 'test_accessed': False, 'calibration_accessed': False}
    atomic_json(output / 'summary.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('dataset', 'registration', 'diagnostic', 'retention', 'output'):
        parser.add_argument('--' + key, required=True)
    result = run(**vars(parser.parse_args()))
    print(json.dumps({key: result[key] for key in ('images', 'counts', 'small_region_stats', 'small_candidates_ranking')}))


if __name__ == '__main__':
    main()
