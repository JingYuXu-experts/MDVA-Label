"""Independent diagnostic I/O and metrics; never changes frozen training code."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

import numpy as np

SCHEMA = 1
THRESHOLDS = [round(x / 100, 2) for x in range(10, 91, 5)]


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        for attempt in range(8):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(.025 * 2 ** attempt)
    finally:
        temporary.unlink(missing_ok=True)


def sample_id(uri):
    return hashlib.sha256(uri.encode('utf-8')).hexdigest()[:24]


def cache_ready(previous, checkpoint, probability_file, asset_root):
    from PIL import Image
    from .runtime import digest
    if previous['checkpoint_sha256'] != checkpoint:
        raise ValueError('缓存权重版本不同，请创建新诊断目录')
    if not probability_file.is_file() or digest(probability_file) != previous['probability_sha256']:
        return False
    try:
        for name in previous['assets'].values():
            path = (asset_root/name).resolve()
            if not path.is_relative_to(asset_root.resolve()):
                return False
            with Image.open(path) as im:
                im.verify()
    except (OSError, ValueError, SyntaxError):
        return False
    return True


def allowed_rows(dataset):
    from .datasets import read_frozen
    # Metadata are shared, but no pixels from calibration/test can be opened.
    return [r for r in read_frozen(dataset, 'segmentation-ready.jsonl')
            if r['eligible'] and r['split'] in ('train', 'validation')]


def select_samples(rows):
    """Fixed station/area-stratified training sample, common to every model."""
    result = [r for r in rows if r['split'] == 'validation']
    rng = np.random.default_rng(2026)
    groups = defaultdict(list)
    empty = []
    for row in rows:
        if row['split'] != 'train':
            continue
        area = sum(int(n) for v, n in row['mask_statistics']['pixels'].items() if int(v) > 0)
        if area == 0:
            empty.append({**row, 'diagnostic_subset': 'train_empty_reference'})
        else:
            groups[(row['category'], row['instrument'])].append((area, row))
    for _, group in sorted(groups.items()):
        ordered = sorted(group, key=lambda a: (a[0], a[1]['uri']))
        bins = np.array_split(np.arange(len(ordered)), 3)
        for indices in bins:
            chosen = rng.choice(indices, min(10, len(indices)), replace=False)
            result.extend({**ordered[int(i)][1], 'diagnostic_subset': 'train_fit_sample'} for i in chosen)
    result.extend(empty)
    return [{**r, 'sample_id': sample_id(r['uri']),
             'diagnostic_subset': r.get('diagnostic_subset', 'validation')} for r in result]


def score(probability, mask, category):
    from scipy.ndimage import label
    from .metrics import binary_segmentation, object_scores
    prediction, reference = probability >= .5, mask > 0
    values = binary_segmentation(prediction, reference, boundary=True)
    values['area_ratio'] = float(prediction.sum() / reference.sum()) if reference.any() else None
    if category == 'AR':
        values['objects'] = object_scores(prediction, mask)
    else:
        ref_ids, _ = label(reference, structure=np.ones((3, 3)))
        # Reuse matching with an explicitly 8-connected prediction raster.
        values['objects'] = connected_scores(prediction, ref_ids)
    values['thresholds'] = []
    for threshold in THRESHOLDS:
        m = binary_segmentation(probability >= threshold, reference)
        values['thresholds'].append({'threshold': threshold, **{k: m[k] for k in ('dice', 'iou', 'precision', 'recall')}})
    errors = []
    if reference.any() and not prediction.any():
        errors.append('empty_prediction')
    if values['recall'] is not None and values['recall'] < .5:
        errors.append('undersegmentation')
    if values['area_ratio'] is not None and values['area_ratio'] > 1.5:
        errors.append('oversegmentation')
    if (reference.any() or prediction.any()) and values.get('boundary_f1', 0) < .5:
        errors.append('boundary_error')
    if values['false_positive_image']:
        errors.append('empty_reference_positive')
    values['error_types'] = errors or ['no_major_flag']
    values['foreground_fraction'] = float(reference.mean())
    values['area_bin'] = 'small' if reference.mean() < .005 else 'medium' if reference.mean() < .03 else 'large'
    return values


def connected_scores(prediction, ref_ids):
    from scipy.ndimage import label
    from scipy.optimize import linear_sum_assignment
    predicted, count = label(prediction, structure=np.ones((3, 3)))
    n = int(ref_ids.max())
    ps = np.bincount(predicted.ravel(), minlength=count + 1)[1:]
    ys = np.bincount(ref_ids.ravel(), minlength=n + 1)[1:]
    intersections = np.zeros((count, n), dtype=np.int64)
    for j in range(n):
        intersections[:, j] = np.bincount(predicted[ref_ids == j + 1], minlength=count + 1)[1:]
    unions = ps[:, None] + ys[None, :] - intersections
    ious = np.divide(intersections, unions, out=np.zeros_like(unions, dtype=float), where=unions > 0)
    matched = 0
    if count and n:
        a, b = linear_sum_assignment(-ious)
        matched = int((ious[a, b] >= .5).sum())
    overlaps = (intersections > 0) & (intersections >= .1 * np.minimum(ps[:, None], ys[None, :]))
    # Size bins use equivalent pixels on a 512x512 grid, not physical feature size.
    areas = ys * (512 ** 2 / prediction.size)
    return {'reference_regions': n, 'predicted_components': count, 'matched': matched,
            'missed_regions': n - matched, 'false_positive_regions': count - matched,
            'merge_candidates': int((overlaps.sum(1) > 1).sum()),
            'split_candidates': int((overlaps.sum(0) > 1).sum()),
            'reference_size_bins': {'small_lt64': int((areas < 64).sum()),
                                    'medium_64_511': int(((areas >= 64) & (areas < 512)).sum()),
                                    'large_ge512': int((areas >= 512).sum())},
            'connectivity': 8, 'match_iou': .5,
            'grouping': 'connected_regions_not_physical_instances'}


def summarize(rows):
    from .metrics import segmentation_summary
    values = segmentation_summary(rows)
    ratios = [r['area_ratio'] for r in rows if r['area_ratio'] is not None]
    values['mean_area_ratio'] = float(np.mean(ratios)) if ratios else None
    values['errors'] = {key: sum(key in r['error_types'] for r in rows)
                        for key in sorted({k for r in rows for k in r['error_types']})}
    return values


def stratify(rows):
    result = {}
    for key in ('instrument', 'year', 'area_bin', 'diagnostic_subset'):
        groups = defaultdict(list)
        for row in rows:
            groups[str(row[key])].append(row)
        result[key] = {name: summarize(items) for name, items in sorted(groups.items())}
    return result


def comparisons(models, cases):
    from .metrics import paired_cluster_interval
    grouped = defaultdict(list)
    by_run = defaultdict(dict)
    for model in models:
        if model.get('state') == 'completed' and model.get('category') == 'FL' and model.get('reproduction_passed') and not model.get('partial_training'):
            grouped[model['id'].rsplit('-s', 1)[0]].append(model['id'])
    for row in cases:
        if row['split'] == 'validation':
            by_run[row['run_id']][row['sample_id']] = row
    result = []
    for left, right in [('FL-F0', 'FL-F2'), *[('FL-F2', 'FL-F' + str(i)) for i in range(3, 9)]]:
        if len(grouped[left]) != 3 or len(grouped[right]) != 3:
            continue
        runs = grouped[left] + grouped[right]
        common = sorted(set.intersection(*(set(by_run[r]) for r in runs)))
        if not common:
            continue
        means = lambda name: [float(np.mean([by_run[r][key]['dice'] for r in grouped[name]])) for key in common]
        a, b = means(left), means(right)
        dates = [datetime.fromisoformat(by_run[runs[0]][key]['time_utc']).timestamp() for key in common]
        groups = [int(t // (27 * 86400)) for t in dates]
        ci = paired_cluster_interval(a, b, groups, repeats=2000, seed=2026)
        result.append({'baseline': left, 'candidate': right, 'images': len(common), **ci,
                       'scope': 'reused_development_validation_not_final_test',
                       'seed_handling': 'mean_each_image_over_three_seeds_before_time_block_resampling'})
    return result
