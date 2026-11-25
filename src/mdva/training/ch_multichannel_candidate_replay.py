"""Replay frozen paired-tile scores on automatic AIA193 candidate regions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import label
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .ar_hmi_temporal_ranker import object_scores
from .ch_aux_signal_audit import features
from .ch_boundary_growth import CONNECTIVITY
from .ch_dark_proposals import dark_regions
from .ch_matched_tiles import CORE, FEATURES, RADIUS, SEED, channel_files
from .datasets import SegmentationDataset
from .durable import atomic_json
from .journal import read
from .runtime import digest, now, output_path


ARMS = {'193_only': ('193',), 'euv_triplet': ('193', *FEATURES[:2]), 'euv_and_hmi': ('193', *FEATURES)}


def replay(dataset, audit, paired, output):
    output = output_path(output, dataset)
    if output.exists():
        raise ValueError('使用新的候选回放目录')
    summary = read(paired / 'summary.json')
    selection_file = audit / 'selection.json'
    selection = read(selection_file)
    if (summary['selection_sha256'] != digest(selection_file)
            or selection['manifest_sha256'] != digest(dataset / 'manifest-hashes.json')
            or summary['pairs_sha256'] != digest(paired / 'pairs.json')
            or not summary['holdout_ranking']):
        raise ValueError('配对报告或训练输入身份改变')
    groups = {}
    for source, identity in summary['source_audits'].items():
        p = audit / source / 'summary.json'
        if digest(p) != identity:
            raise ValueError('原观测登记身份改变')
        groups[source] = {r['uri']: r for r in read(p)['results']}
    matched = read(paired / 'pairs.json')
    early = [p for r in matched if r['time'] < '2014' for p in r['pairs'] if p['state'] == 'eligible']
    training = [(p[key], target) for p in early for key, target in (('reference_features', 1), ('control_features', 0))]
    models = {arm: make_pipeline(StandardScaler(), LogisticRegression(C=1, random_state=SEED)).fit(
        [[row[k] for k in keys] for row, _ in training], [y for _, y in training]) for arm, keys in ARMS.items()}
    held = [r for r in matched if r['time'] >= '2014' and any(p['state'] == 'eligible' for p in r['pairs'])]
    data = SegmentationDataset(dataset, 'CH', 'train', 512)
    rows = {r['uri']: r for r in data.rows}
    selections = {r['uri']: r for r in selection['rows']}
    results = []
    for row in held:
        uri = row['uri']
        inputs = (groups['aia171'][uri], groups['angular'][uri])
        arrays = channel_files(audit, inputs, audit / 'aia' / 'pairs' / selections[uri]['id'])
        support = (RADIUS <= CORE) & (arrays['193'] > 0) & np.isfinite(list(arrays.values())).all(axis=0)
        with Image.open(data.source(uri)) as image:
            gray = np.asarray(image.convert('L'))
        ref = np.load(data.source(rows[uri]['mask_uri']), allow_pickle=False)
        mask, n_refs = label(ref > 0, structure=CONNECTIVITY)
        try:
            candidate, parts = dark_regions(gray, np.zeros(gray.shape, dtype=bool), np.zeros(gray.shape, dtype=float))
        except ValueError as exc:
            results.append({'uri': uri, 'utc': row['time'], 'reference_regions': n_refs, 'generated': 0,
                            'candidates': [], 'state': 'disk_rejected', 'reason': str(exc)})
            continue
        geometry = object_scores(candidate, parts, mask, [False] * len(parts))[1]
        iou = np.asarray(geometry['iou'], dtype=float).reshape(len(parts), len(geometry['reference_ids']))
        tested = []
        for index in range(len(parts)):
            y, x = np.where(candidate == index + 1)
            if not support[y, x].all():
                continue
            measurements = {'193': float(np.median(arrays['193'][y, x])), **features(arrays, y, x)}
            scores = {arm: float(model.decision_function([[measurements[k] for k in ARMS[arm]]])[0])
                      for arm, model in models.items()}
            maximum = float(iou[index].max(initial=0.))
            tested.append({'candidate': index + 1, 'area': len(y), 'reference_iou': maximum, 'scores': scores})
        results.append({'uri': uri, 'utc': row['time'], 'state': 'generated', 'reference_regions': n_refs, 'generated': len(parts),
                        'candidates_measured': len(tested), 'candidates': tested})
    all_candidates = [item for row in results for item in row['candidates']]
    target = [r['reference_iou'] >= .50 for r in all_candidates]
    matched_count = sum(target)
    scores = {key: float(average_precision_score(target, [r['scores'][key] for r in all_candidates])) for key in ARMS} if matched_count else {}
    summary = {'state': 'completed', 'recorded_utc': now(), 'code_sha256': digest(__file__),
               'paired_sha256': digest(paired / 'summary.json'), 'historical_fit_pairs': len(early),
               'held_dates': len(held), 'days_with_disk_fit': sum(r['state'] == 'generated' for r in results),
               'generated': sum(r['generated'] for r in results),
               'measured': len(all_candidates), 'candidate_iou_ge_50': matched_count,
               'dates_with_matches': sum(any(p['reference_iou'] >= .50 for p in r['candidates']) for r in results),
               'candidate_ap': scores, 'scope': 'historical training dates only; original fixed dark candidate generator; no threshold selection',
               'validation_accessed': False, 'test_accessed': False, 'calibration_accessed': False, 'deployed': False}
    atomic_json(output / 'candidates.json', results)
    summary['candidates_sha256'] = digest(output / 'candidates.json')
    atomic_json(output / 'summary.json', summary)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ('dataset', 'audit', 'paired', 'output'):
        parser.add_argument('--' + argument, type=Path, required=True)
    print(json.dumps(replay(**vars(parser.parse_args())), ensure_ascii=False))
