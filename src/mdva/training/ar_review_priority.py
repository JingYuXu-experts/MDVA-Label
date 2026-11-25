"""Evaluate ordering of unchanged AR candidates for human review."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from scipy.ndimage import find_objects

from .ar_component_verifier import FEATURES, describe, match_components
from .datasets import SegmentationDataset
from .durable import atomic_json
from .forecast_region_detector_audit import MODEL
from .journal import read
from .runtime import configure_environment, digest, now, output_path


BUDGETS = (.1, .2, .3, .4)


def review_curve(candidates, ranking):
    ordered = sorted(candidates, key=lambda row: (row[ranking], row['sample_id'], row['component']))
    errors = sum(not row['matched_reference'] for row in ordered)
    curve = []
    for fraction in BUDGETS:
        count = int(fraction * len(ordered))
        selected = ordered[:count]
        flags = sum(not row['matched_reference'] for row in selected)
        curve.append({'budget_fraction': fraction, 'reviewed_candidates': count,
                      'images_to_open': len({row['sample_id'] for row in selected}),
                      'unmatched_flagged': flags, 'matched_flagged': count - flags,
                      'unmatched_flagged_share': flags / errors if errors else None})
    return curve, ordered


def run(dataset, diagnostic, verifier, output):
    dataset, diagnostic, verifier = (Path(p).resolve() for p in (dataset, diagnostic, verifier))
    output = output_path(output, dataset)
    if output.exists() or any(output.is_relative_to(p) for p in (diagnostic, verifier)):
        raise ValueError('复核排序需要新的输出目录')
    report, protocol, ranker = (read(verifier / name) for name in ('summary.json', 'protocol.json', 'ranker.json'))
    index_path = diagnostic / 'case-index.json'
    if (report['state'] != 'completed' or report['test_accessed'] or report['calibration_accessed']
            or report['ranker_sha256'] != digest(verifier / 'ranker.json')
            or report['protocol_sha256'] != digest(verifier / 'protocol.json')
            or report['rows_sha256'] != digest(verifier / 'images.json')
            or protocol['index_sha256'] != digest(index_path) or tuple(ranker['features']) != FEATURES):
        raise ValueError('开发复核和排序器身份不一致')
    index = {r['uri']: r for r in read(index_path) if r['run_id'] == MODEL and r['diagnostic_subset'] == 'validation'}
    recorded = {r['uri']: r['arms']['control']['objects'] for r in read(verifier / 'images.json')}
    validation = SegmentationDataset(dataset, 'AR', 'validation')
    if len(validation.rows) != 276 or set(index) != set(recorded) or set(index) != {r['uri'] for r in validation.rows}:
        raise ValueError('复核排序样本与原开发复算不一致')
    mean, scale, weights = (np.asarray(ranker[key], dtype=float) for key in ('mean', 'scale', 'weights'))
    if any(item.shape != (len(FEATURES),) or not np.isfinite(item).all() for item in (mean, scale, weights)) or (scale <= 0).any():
        raise ValueError('排序器含无效参数')
    candidates = []
    for image in validation.rows:
        case = index[image['uri']]
        probability = diagnostic / 'probabilities' / MODEL / (case['sample_id'] + '.npy')
        if case['mask_uri'] != image['mask_uri'] or case['probability_sha256'] != digest(probability):
            raise ValueError('概率缓存或参考身份不符')
        _, raster, vectors = describe(np.load(probability, allow_pickle=False))
        reference = np.load(validation.source(image['mask_uri']), allow_pickle=False)
        matches = match_components(raster, reference, len(vectors))
        prior = recorded[image['uri']]
        if len(vectors) != prior['predicted'] or int(matches.sum()) != prior['matched']:
            raise ValueError('候选数量或匹配关系未复现')
        if not vectors:
            continue
        scores = (np.asarray(vectors) - mean) / scale @ weights + ranker['intercept']
        for component, ((sy, sx), features, score, match) in enumerate(
            zip(find_objects(raster), vectors, scores, matches, strict=True), 1
        ):
            candidates.append({'sample_id': case['sample_id'], 'component': component,
                               'bbox_xyxy': [sx.start, sy.start, sx.stop, sy.stop],
                               'matched_reference': bool(match), 'review_score': float(score),
                               'mean_segmentation_score': features[FEATURES.index('mean_probability')]})
    review, ordered = review_curve(candidates, 'review_score')
    baseline, _ = review_curve(candidates, 'mean_segmentation_score')
    output.mkdir(parents=True)
    atomic_json(output / 'review-order.json', ordered)
    result = {'state': 'completed', 'created_utc': now(), 'images': len(validation.rows),
              'candidates': len(candidates), 'matched': sum(row['matched_reference'] for row in candidates),
              'candidate_removals': 0, 'learned_ranking': review, 'segmentation_score_ranking': baseline,
              'ranking_score_is_calibrated_probability': False, 'human_review_time_measured': False,
              'comparison_scope': 'Development references only; unmatched can include annotation ambiguity.',
              'workflow_integration': False, 'test_accessed': False, 'calibration_accessed': False,
              'verifier_summary_sha256': digest(verifier / 'summary.json'),
              'order_sha256': digest(output / 'review-order.json'), 'code_sha256': digest(__file__)}
    atomic_json(output / 'summary.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('dataset', 'diagnostic', 'verifier', 'output'):
        parser.add_argument('--' + key, required=True)
    configure_environment()
    result = run(**vars(parser.parse_args()))
    print(json.dumps({key: result[key] for key in ('candidates', 'learned_ranking', 'segmentation_score_ranking')}))


if __name__ == '__main__':
    main()
