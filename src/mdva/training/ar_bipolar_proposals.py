"""Test independent magnetic-display proposals for missed activity regions."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import find_objects, gaussian_filter, label, maximum_filter

from .ar_scale_study import cohort_rows, load_plan
from .datasets import SegmentationDataset
from .durable import atomic_json
from .forecast_region_detector_audit import MODEL, match_reference
from .journal import read
from .runtime import Resources, configure_environment, digest, now, output_path


Y, X = np.ogrid[:1024, :1024]
DISTANCE = np.hypot(X - 511.5, Y - 511.5)
CORE = DISTANCE <= 400
INTERIOR = DISTANCE <= 445
SCALES = (8, 16, 28)
BUDGETS = (1, 3, 5)
FEATURES = ('response', 'radius', 'positive_peak', 'negative_peak', 'balance',
            'positive_fraction', 'negative_fraction', 'central_distance', 'local_abs_mean')


def proposals(gray):
    """Use opposing contrast peaks. Signal values are JPEG levels, not Gauss."""
    if gray.shape != (1024, 1024) or gray.dtype != np.uint8:
        raise ValueError('仅适用已验证的1024像素8位HMI显示域磁图')
    field = gaussian_filter(gray.astype(np.float32), 1.25) - np.median(gray[CORE])
    field[~INTERIOR] = 0
    peaks = []
    for radius in SCALES:
        span = 2 * radius + 1
        positive = maximum_filter(field, size=span)
        negative = maximum_filter(-field, size=span)
        strength = gaussian_filter(np.minimum(positive, negative), radius / 4)
        maxima = maximum_filter(strength, size=2 * radius + 1)
        location = (strength >= 16) & (strength == maxima) & INTERIOR
        components, _ = label(location)
        for number, area in enumerate(find_objects(components), 1):
            if area is None:
                continue
            yy, xx = np.where(components[area] == number)
            cy = int(np.rint(yy.mean())) + area[0].start
            cx = int(np.rint(xx.mean())) + area[1].start
            if not INTERIOR[cy, cx]:
                continue
            half = int(radius * 1.5)
            x0, x1 = max(0, cx - half), min(gray.shape[1], cx + half + 1)
            y0, y1 = max(0, cy - half), min(gray.shape[0], cy + half + 1)
            patch = field[y0:y1, x0:x1]
            p, n = max(float(patch.max()), 0.), max(float(-patch.min()), 0.)
            peak = float(strength[cy, cx])
            peaks.append({
                'center': [cx, cy], 'box': [x0, y0, x1, y1],
                'features': {
                    'response': peak, 'radius': radius, 'positive_peak': p, 'negative_peak': n,
                    'balance': min(p, n) / max(p, n, 1.),
                    'positive_fraction': float((patch >= 16).mean()), 'negative_fraction': float((patch <= -16).mean()),
                    'central_distance': float(DISTANCE[cy, cx]), 'local_abs_mean': float(np.abs(patch).mean()),
                },
            })
    ordered = sorted(peaks, key=lambda row: (-row['features']['response'], row['features']['radius'], row['center']))
    kept = []
    for candidate in ordered:
        x, y = candidate['center']
        if any(np.hypot(x - old['center'][0], y - old['center'][1])
               < max(candidate['features']['radius'], old['features']['radius']) for old in kept):
            continue
        kept.append(candidate)
        if len(kept) == 32:
            break
    return kept


def reference_assignment(candidate, native, areas):
    """A localization hit requires a center in the reference and half its pixels in the crop."""
    x, y = candidate['center']
    x0, y0, x1, y1 = candidate['box']
    crop = native[y0:y1, x0:x1]
    instance = int(native[y, x])
    if instance and np.count_nonzero(crop == instance) >= .5 * areas[instance]:
        return instance, 'matched_reference'
    if np.any(crop > 0):
        return None, 'ambiguous_reference_overlap'
    return None, 'no_reference_overlap'


def independent_of_mask(candidate, mask):
    x, y = candidate['center']
    x0, y0, x1, y1 = candidate['box']
    return not mask[y, x] and float(mask[y0:y1, x0:x1].mean()) < .25


def image_data(data, row):
    with Image.open(data.source(row['uri'])) as image:
        gray = np.array(image.convert('L'))
    native = np.load(data.source(row['mask_uri']), allow_pickle=False)
    if native.shape != gray.shape or not np.issubdtype(native.dtype, np.integer):
        raise ValueError('磁图和实例掩膜应使用同一1024网格')
    return gray, native


def features_for(candidate):
    return [candidate['features'][key] for key in FEATURES]


def run(study, diagnostic, missed, output):
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    study, diagnostic, missed = (Path(path).resolve() for path in (study, diagnostic, missed))
    origin = load_plan(study)
    dataset = origin['dataset']
    output = output_path(output, dataset)
    if output.exists() or any(output.is_relative_to(parent) for parent in (study, diagnostic, missed)):
        raise ValueError('候选实验使用新目录')
    audited = read(missed / 'summary.json')
    index_path = diagnostic / 'case-index.json'
    if (
        audited['state'] != 'completed' or audited['test_accessed']
        or audited['rows_sha256'] != digest(missed / 'rows.json')
        or audited['index_sha256'] != digest(index_path)
    ):
        raise ValueError('漏检审计和冻结模型缓存身份不符')
    by_uri = defaultdict(set)
    for row in read(missed / 'rows.json'):
        if row['description'] == 'no_overlap':
            by_uri[row['uri']].add(row['instance'])
    if sum(map(len, by_uri.values())) != 83:
        raise ValueError('漏检审计的无重叠数量不同')
    train = SegmentationDataset(dataset, 'AR', 'train')
    train.rows = cohort_rows(train, read(study / 'cohort.json'))
    validation = SegmentationDataset(dataset, 'AR', 'validation')
    index = {
        row['uri']: row for row in read(index_path)
        if row['run_id'] == MODEL and row['diagnostic_subset'] == 'validation'
    }
    if len(validation) != 276 or set(index) != {row['uri'] for row in validation.rows}:
        raise ValueError('只接受完整开发实验和同样的缓存输入')
    output.mkdir(parents=True)
    protocol = {
        'created_utc': now(), 'code_sha256': digest(__file__), 'checkpoint_index_sha256': digest(index_path),
        'scope': 'Independent proposals from 384 training and 276 development images. No deployment or forecast.',
        'source_study_sha256': digest(study / 'protocol.json'), 'miss_audit_sha256': digest(missed / 'summary.json'),
        'scales': list(SCALES), 'budgets_additional_per_image': list(BUDGETS), 'features': list(FEATURES),
        'ranking': 'Display response vs train-only class-balanced standardized logistic regression, C=1.',
        'label_rule': 'Center lies inside original instance and crop covers >=50%; ambiguous crop overlaps excluded from fitting.',
        'comparison': 'Count localization candidates only. Rectangular crop and center are not segmentation or NOAA matches.',
        'reference_method': 'Intensity threshold and morphology motivated by Cui et al. 2021; different algorithm/display units.',
        'literature': 'https://www.cjss.ac.cn/en/article/doi/10.11728/cjss2021.03.402',
        'test_accessed': False, 'calibration_accessed': False,
    }
    atomic_json(output / 'protocol.json', protocol)
    resource = Resources('cpu')
    inputs, labels = [], []
    fitted_from = Counter()
    for idx, row in enumerate(train.rows, 1):
        gray, native = image_data(train, row)
        ids, counts = np.unique(native[native > 0], return_counts=True)
        areas = dict(zip(ids.tolist(), counts.tolist()))
        for candidate in proposals(gray):
            _, state = reference_assignment(candidate, native, areas)
            fitted_from[state] += 1
            if state != 'ambiguous_reference_overlap':
                inputs.append(features_for(candidate))
                labels.append(int(state == 'matched_reference'))
        if idx % 32 == 0:
            atomic_json(output / 'progress.json', {'state': 'fitting', 'images': idx, 'total': len(train),
                                                   'resource': resource.sample()})
    if len(set(labels)) != 2:
        raise ValueError('候选排序缺少一类参照样本')
    model = make_pipeline(StandardScaler(), LogisticRegression(C=1., class_weight='balanced', random_state=2026))
    model.fit(inputs, labels)
    scale, classifier = model.steps[0][1], model.steps[1][1]
    atomic_json(output / 'ranker.json', {
        'features': list(FEATURES), 'mean': scale.mean_.tolist(), 'scale': scale.scale_.tolist(),
        'weights': classifier.coef_[0].tolist(), 'intercept': float(classifier.intercept_[0]),
        'raw_score_is_not_calibrated': True,
    })
    results = []
    totals = {f'{mode}/{limit}': Counter() for mode in ('contrast', 'learned') for limit in BUDGETS}
    oracle = Counter()
    for number, row in enumerate(validation.rows, 1):
        case = index[row['uri']]
        path = diagnostic / 'probabilities' / MODEL / (case['sample_id'] + '.npy')
        if digest(path) != case['probability_sha256'] or case['mask_uri'] != row['mask_uri']:
            raise ValueError('候选与冻结模型或掩膜身份不符')
        probability = np.load(path, allow_pickle=False)
        gray, native = image_data(validation, row)
        if probability.shape != native.shape or not np.isfinite(probability).all():
            raise ValueError('原模型概率必须可复算')
        mask = probability >= .5
        matched, _ = match_reference(mask, native)
        if sum(value is not None for value in matched.values()) != case['objects']['matched']:
            raise ValueError('原模型匹配结果不一致')
        ids, counts = np.unique(native[native > 0], return_counts=True)
        areas = dict(zip(ids.tolist(), counts.tolist()))
        missed_ids = {identity for identity, value in matched.items() if value is None}
        tiny = {identity for identity, area in areas.items() if area < 256}
        no_overlap = by_uri[row['uri']]
        candidates = []
        for candidate in proposals(gray):
            instance, status = reference_assignment(candidate, native, areas)
            candidates.append({**candidate, 'reference': instance, 'reference_state': status,
                               'independent': independent_of_mask(candidate, mask)})
        if candidates:
            scores = model.decision_function([features_for(candidate) for candidate in candidates])
            for candidate, score in zip(candidates, scores):
                candidate['learned_score'] = float(score)
        all_hits = {candidate['reference'] for candidate in candidates if candidate['reference'] is not None}
        total_independent = [candidate for candidate in candidates if candidate['independent']]
        raw_hits = {candidate['reference'] for candidate in total_independent if candidate['reference'] is not None}
        oracle.update({'all_reference': len(areas), 'all_proposals': len(candidates),
                       'all_hits': len(all_hits), 'independent_candidates': len(total_independent),
                       'independent_no_overlap_rescued': len(raw_hits & no_overlap),
                       'independent_tiny_rescued': len(raw_hits & missed_ids & tiny)})
        order = {
            'contrast': total_independent,
            'learned': sorted(total_independent, key=lambda candidate: -candidate['learned_score']),
        }
        for mode, entries in order.items():
            for limit in BUDGETS:
                subset = entries[:limit]
                found = {candidate['reference'] for candidate in subset if candidate['reference'] is not None}
                new = found & missed_ids
                totals[f'{mode}/{limit}'].update({
                    'candidates': len(subset), 'unique_new_missed_regions': len(new),
                    'unique_no_overlap_rescued': len(new & no_overlap), 'unique_tiny_rescued': len(new & tiny),
                    'no_reference_overlap': sum(row['reference_state'] == 'no_reference_overlap' for row in subset),
                    'ambiguous_overlap': sum(row['reference_state'] == 'ambiguous_reference_overlap' for row in subset),
                    'already_matched_objects': len(found - missed_ids),
                })
        results.append({'uri': row['uri'], 'missed': sorted(missed_ids), 'no_overlap': sorted(no_overlap),
                        'true_tiny': sorted(tiny), 'candidates': candidates})
        if number % 24 == 0 or number == len(validation):
            atomic_json(output / 'progress.json', {'state': 'evaluating', 'images': number, 'total': len(validation),
                                                   'resource': resource.sample()})
    atomic_json(output / 'images.json', results)
    report = {
        'state': 'completed', 'created_utc': now(), 'train_images': len(train), 'validation_images': len(results),
        'training_candidates': dict(fitted_from), 'oracle': dict(oracle),
        'results': {key: {**count, 'candidates_per_image': count['candidates'] / len(results)}
                    for key, count in totals.items()},
        'scope': 'Development localization only. Unmatched crops may include structures outside reference annotations.',
        'protocol_sha256': digest(output / 'protocol.json'), 'rows_sha256': digest(output / 'images.json'),
        'ranker_sha256': digest(output / 'ranker.json'), 'published': False,
        'test_accessed': False, 'calibration_accessed': False,
    }
    atomic_json(output / 'summary.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('study', 'diagnostic', 'missed', 'output'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    configure_environment()
    result = run(**vars(args))
    print(json.dumps({key: result[key] for key in ('training_candidates', 'oracle', 'results')}))


if __name__ == '__main__':
    main()
