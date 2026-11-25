"""Conservative detection consistency checks without reference annotations."""
import numpy as np

# Initial research policy. Validate review rates against reference labels before
# using any flag as a gate for unattended publication.
EXPOSURE_GAIN = 0.98
MIN_REGION_IOU = 0.65


def assess_stability(primary, repeated, *, threshold, polygon_skips):
    import cv2

    if (primary.ndim != 2 or primary.shape != repeated.shape
            or not np.isfinite(primary).all() or not np.isfinite(repeated).all()):
        raise ValueError('自检需要同尺寸、有效的模型输出')
    mask = primary >= threshold
    repeat_mask = repeated >= threshold
    union = int(np.count_nonzero(mask | repeat_mask))
    reasons = []
    if not mask.any():
        reasons.append('empty_candidate')
    if polygon_skips:
        reasons.append('raster_only_regions')

    # Check each region separately so a large stable region cannot hide the
    # disappearance of a small one. Compare in both directions to flag additions.
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype('uint8'), connectivity=8)
    rn, repeat_labels, repeat_stats, _ = cv2.connectedComponentsWithStats(repeat_mask.astype('uint8'), connectivity=8)
    primary_support = np.zeros(n)
    repeat_support = np.zeros(rn)
    both = mask & repeat_mask
    if both.any():
        pairs = labels[both].astype('int64') * rn + repeat_labels[both]
        keys, intersections = np.unique(pairs, return_counts=True)
        left, right = keys // rn, keys % rn
        iou = intersections / (stats[left, cv2.CC_STAT_AREA] + repeat_stats[right, cv2.CC_STAT_AREA] - intersections)
        np.maximum.at(primary_support, left, iou)
        np.maximum.at(repeat_support, right, iou)

    unstable = np.flatnonzero(primary_support[1:] < MIN_REGION_IOU) + 1
    new_count = int(np.count_nonzero(repeat_support[1:] < MIN_REGION_IOU))
    if unstable.size or new_count:
        reasons.append('regions_sensitive_to_exposure')
    unstable_examples = [{'bbox_xywh':stats[i,:4].tolist(), 'best_mask_iou':float(primary_support[i])}
                         for i in unstable[:25]]
    return {'schema':1, 'state':'needs_review' if reasons else 'consistent_candidate',
            'reason_codes':reasons, 'exposure_gain':EXPOSURE_GAIN, 'mask_threshold':threshold,
            'region_iou_floor':MIN_REGION_IOU, 'global_mask_iou':int(both.sum())/union if union else None,
            'primary_region_count':n-1, 'repeat_region_count':rn-1,
            'unstable_primary_count':int(unstable.size), 'unstable_repeat_count':new_count,
            'unstable_primary_examples':unstable_examples,
            'scope':'轻微亮度扰动下的重复推理一致性。稳定结果仍可能误标或漏标，未作自动验收。'}
