"""Separate missing contours from over-rejection in the dark-filament review."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import numpy as np

from .ar_hmi_temporal_ranker import matching
from .durable import atomic_json
from .journal import read
from .runtime import digest, now


def loss_audit(cases, arm, cutoff):
    totals = Counter()
    rows = []
    for case in cases:
        frame = case['frame']
        scores = np.asarray(frame['scores'][arm], dtype=float)
        retained = scores >= cutoff
        ranked = np.argsort(-scores, kind='stable')
        extras = ranked[~retained[ranked]]
        for variant, geometry in enumerate(frame['geometry']):
            count = len(geometry['reference_ids'])
            overlaps = np.asarray(geometry['iou'], dtype=float).reshape(len(scores), count)
            original = set(matching(geometry, np.ones(len(scores), dtype=bool))[1])
            matched = set(matching(geometry, retained)[1])
            totals.update(variants=1, reference=count, matched=len(matched))
            if not count:
                totals.update(blank_variants=1, blank_with_candidate=int(retained.any()))
            for budget in (1, 3, 5):
                # Keep the automatic matches fixed. Evaluate exposure of qualified alternatives
                # for each remaining reference separately, without treating exposure as a match.
                candidates = extras[:budget]
                exposed = (overlaps[candidates] >= .5).any(axis=0)
                totals.update({f'review_{budget}_suggestions': len(candidates),
                               f'review_{budget}_misses_exposed': sum(
                                   bool(exposed[k]) for k in range(count) if k not in matched)})
            for k, region in enumerate(geometry['reference_ids']):
                raw_best = float(overlaps[:, k].max(initial=0.))
                kept_best = float(overlaps[retained, k].max(initial=0.))
                if k in matched:
                    reason = 'matched'
                elif raw_best < .5:
                    reason = 'no_qualified_contour'
                elif kept_best < .5:
                    reason = 'qualified_contour_rejected'
                else:
                    reason = 'competing_assignment'
                totals[reason + '_references'] += 1
                if k not in matched:
                    rows.append({'file': case['file'], 'variant': variant, 'reference_region': region,
                                 'raw_best_iou': raw_best, 'retained_best_iou': kept_best,
                                 'matched_in_raw_one_to_one': k in original, 'cause': reason})
    return {'counts': dict(totals), 'missed': rows}


def contour_audit(rows):
    """Recover mask intersections from recorded sizes and IoUs at the fixed cutoff."""
    totals = Counter()
    sizes = {'under_256': Counter(), 'at_least_256': Counter()}
    for row in rows:
        features = np.asarray(row['features'], dtype=float).reshape(-1, 15)
        areas = np.rint(np.expm1(features[:, 3])).astype(int)
        for geometry in row['standalone']['geometry']:
            reference = np.asarray(geometry['reference_sizes'], dtype=int)
            iou = np.asarray(geometry['iou'], dtype=float).reshape(len(areas), len(reference))
            best = iou.max(axis=0, initial=0.)
            intersections = iou * (areas[:, None] + reference) / (1. + iou)
            hits = np.rint(intersections).astype(int)
            if (not np.allclose(hits, intersections, rtol=0., atol=1e-7)
                    or (hits > areas[:, None]).any() or (hits.sum(axis=0) > reference).any()):
                raise ValueError('轮廓面积无法复原逐区域交集')
            for col, required in enumerate(reference):
                group = sizes['under_256' if required < 256 else 'at_least_256']
                totals.update(reference=1)
                group.update(reference=1)
                if best[col] >= .5:
                    reason = 'qualified'
                elif best[col] == 0.:
                    reason = 'no_overlap'
                else:
                    k = iou[:, col].argmax()
                    recalls = hits[:, col] / required
                    coverage, precision = recalls[k], hits[k, col] / areas[k]
                    if recalls.sum() >= .5 and recalls.max() < .5:
                        reason = 'coverage_scattered_across_contours'
                    elif coverage >= .5 and precision < .5:
                        reason = 'best_contour_spills_outside'
                    elif coverage < .5 and precision >= .5:
                        reason = 'best_contour_small_or_partial'
                    else:
                        reason = 'other_insufficient_overlap'
                totals[reason] += 1
                group[reason] += 1
    return {'counts': dict(totals), 'by_size': {name: dict(values) for name, values in sizes.items()}}


def run(study):
    report = read(study / 'summary.json')
    if report['state'] != 'completed' or report['test_accessed'] or report['deployed']:
        raise ValueError('需要完成且未用于正式测试的暗条候选研究')
    if report['protocol_sha256'] != digest(study / 'protocol.json'):
        raise ValueError('复核实验协议指纹不符')
    thresholds = report['thresholds']
    output = {}
    inputs = {}
    contour = {}
    for cohort, aggregate in report['scores'].items():
        path = study / f'{cohort}-cases.json'
        cases = read(path)
        if len(cases) != aggregate['images']:
            raise ValueError('复核案例数不符')
        inputs[cohort] = digest(path)
        output[cohort] = {}
        for arm, cutoff in thresholds.items():
            audit = loss_audit(cases, arm, cutoff)
            for key in ('reference', 'matched'):
                if audit['counts'].get(key, 0) != aggregate['scores'][arm][key]:
                    raise ValueError('逐图匹配与原实验统计不一致')
            output[cohort][arm] = audit
        geometry = study / f'{cohort}-geometry.json'
        if not geometry.is_file():
            geometry = study.parent / 'fl-direct-reviewer-01' / geometry.name
            if digest(geometry) != read(study / 'protocol.json')['recorded_inputs'][geometry.name]:
                raise ValueError('原始候选几何身份不符')
        contour[cohort] = {'sha256': digest(geometry), **contour_audit(read(geometry))}
        if contour[cohort]['counts'].get('reference', 0) != aggregate['scores']['raw']['reference']:
            raise ValueError('原始候选参考数不符')
    atomic_json(study / 'missed-reference-audit.json', {'created_utc': now(),
        'code_sha256': digest(__file__), 'study_sha256': digest(study / 'summary.json'),
        'case_sha256': inputs, 'thresholds': thresholds, 'cohorts': output,
        'raw_contour_analysis': contour,
        'scope': 'Review budget is measured per reference version. Exposure can count one candidate for multiple references.'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study', type=Path, required=True)
    run(parser.parse_args().study)
