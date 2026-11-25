"""Native-grid segmentation, rare-event scores and paired cluster inference."""
from __future__ import annotations

import numpy as np


def binary_segmentation(prediction, reference, boundary=False):
    p, y = np.asarray(prediction, bool), np.asarray(reference, bool)
    if p.shape != y.shape:
        raise ValueError('评价必须在相同原始网格上进行')
    tp, fp, fn = int((p & y).sum()), int((p & ~y).sum()), int((~p & y).sum())
    result = {'tp': tp, 'fp': fp, 'fn': fn, 'foreground_pixels': int(y.sum()),
              'predicted_pixels': int(p.sum()), 'empty_reference': not y.any(),
              'dice': 2*tp/(2*tp+fp+fn) if y.any() else None,
              'iou': tp/(tp+fp+fn) if y.any() else None,
              'precision': tp/(tp+fp) if tp+fp else 0.,
              'recall': tp/(tp+fn) if tp+fn else None,
              'false_positive_image': bool(p.any() and not y.any())}
    if boundary:
        from scipy.ndimage import binary_erosion, distance_transform_edt
        pb, yb = p ^ binary_erosion(p), y ^ binary_erosion(y)
        tolerance = max(1, round(.002 * np.hypot(*y.shape)))
        precision = float((distance_transform_edt(~yb)[pb] <= tolerance).mean()) if pb.any() and yb.any() else 0.
        recall = float((distance_transform_edt(~pb)[yb] <= tolerance).mean()) if pb.any() and yb.any() else 0.
        result['boundary_f1'] = 2*precision*recall/(precision+recall) if precision+recall else 0.
        result['boundary_tolerance_pixels'] = tolerance
    return result


def segmentation_summary(rows):
    nonempty = [r for r in rows if not r['empty_reference']]
    empty = [r for r in rows if r['empty_reference']]
    result = {'images': len(rows), 'nonempty_images': len(nonempty), 'empty_images': len(empty),
              'empty_false_positive_rate': float(np.mean([r['false_positive_image'] for r in empty])) if empty else None}
    for key in ('dice', 'iou', 'precision', 'recall', 'boundary_f1'):
        values = [r[key] for r in nonempty if r.get(key) is not None]
        result[key] = float(np.mean(values)) if values else None
    return result


def object_scores(prediction, instance_mask, iou_threshold=.5):
    """Connected components versus immutable reference IDs, never NOAA-guided grouping."""
    from scipy.ndimage import label
    from scipy.optimize import linear_sum_assignment
    predicted, count = label(prediction)
    original = np.asarray(instance_mask)
    ids = np.unique(original[original > 0])
    intersections = np.zeros((count, len(ids)), dtype=np.int64)
    predicted_sizes = np.bincount(predicted.ravel(), minlength=count+1)[1:]
    reference_sizes = np.asarray([(original == identity).sum() for identity in ids])
    for j, identity in enumerate(ids):
        intersections[:, j] = np.bincount(predicted[original == identity], minlength=count+1)[1:]
    unions = predicted_sizes[:, None] + reference_sizes[None, :] - intersections
    iou = np.divide(intersections, unions, out=np.zeros_like(unions, dtype=float), where=unions > 0)
    if count and len(ids):
        p, y = linear_sum_assignment(-iou)
        matched = int((iou[p, y] >= iou_threshold).sum())
    else:
        matched = 0
    # A substantial overlap is >=10% of the smaller participating object.
    meaningful = intersections >= .1*np.minimum(predicted_sizes[:, None], reference_sizes[None, :])
    meaningful &= intersections > 0
    return {'reference_objects': len(ids), 'predicted_components': count, 'matched': matched,
            'false_positive_objects': count-matched, 'missed_objects': len(ids)-matched,
            'merge_candidates': int((meaningful.sum(axis=1) > 1).sum()),
            'split_candidates': int((meaningful.sum(axis=0) > 1).sum()),
            'count_absolute_error': abs(count-len(ids)), 'match_iou_threshold': iou_threshold,
            'grouping': 'connected_components_only; not NOAA identity or original-team grouping'}


def forecast_scores(y, probability, threshold=.5, climate=None):
    from sklearn.metrics import average_precision_score, roc_auc_score
    y, p = np.asarray(y, int), np.asarray(probability, float)
    if len(y) != len(p) or not np.isfinite(p).all() or ((p < 0) | (p > 1)).any():
        raise ValueError('预测概率非法')
    pred = p >= threshold
    tp, fp = int((pred & (y == 1)).sum()), int((pred & (y == 0)).sum())
    fn, tn = int((~pred & (y == 1)).sum()), int((~pred & (y == 0)).sum())
    tpr, fpr = tp/(tp+fn) if tp+fn else 0., fp/(fp+tn) if fp+tn else 0.
    denominator = (tp+fn)*(fn+tn) + (tp+fp)*(fp+tn)
    bs = float(np.mean((p-y)**2)) if len(y) else None
    climate_bs = float(np.mean((float(climate)-y)**2)) if climate is not None and len(y) else None
    return {'n': len(y), 'positives': int(y.sum()), 'pr_auc_ap': float(average_precision_score(y, p)) if y.sum() else None,
            'roc_auc': float(roc_auc_score(y, p)) if len(np.unique(y)) == 2 else None,
            'tss': tpr-fpr, 'hss': 2*(tp*tn-fp*fn)/denominator if denominator else 0.,
            'precision': tp/(tp+fp) if tp+fp else 0., 'recall': tpr,
            'false_alarm_ratio': fp/(tp+fp) if tp+fp else 0., 'threshold': float(threshold),
            'brier': bs, 'brier_skill': 1-bs/climate_bs if climate_bs else None,
            'tp': tp, 'fp': fp, 'fn': fn, 'tn': tn}


def paired_cluster_interval(a, b, groups, repeats=2000, seed=2026):
    """CI for B-A, sampling entire blocks (never individual pixels)."""
    a, b, groups = np.asarray(a), np.asarray(b), np.asarray(groups)
    if not (len(a) == len(b) == len(groups)) or not len(a):
        raise ValueError('配对样本不一致或为空')
    unique = np.unique(groups)
    indices = [np.flatnonzero(groups == g) for g in unique]
    rng = np.random.default_rng(seed)
    samples = []
    for _ in range(repeats):
        selected = np.concatenate([indices[i] for i in rng.integers(len(indices), size=len(indices))])
        samples.append(float(np.mean(b[selected]-a[selected])))
    return {'difference_b_minus_a': float(np.mean(b-a)),
            'ci95': np.quantile(samples, [.025, .975]).tolist(), 'repeats': repeats,
            'clusters': len(unique), 'seed': seed}


def holm(pvalues):
    values = np.asarray(pvalues, float)
    order = np.argsort(values)
    adjusted = np.minimum(1, np.maximum.accumulate(values[order]*(len(values)-np.arange(len(values)))))
    result = np.empty_like(values)
    result[order] = adjusted
    return result.tolist()
