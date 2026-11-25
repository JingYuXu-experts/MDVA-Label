"""Fit and check H-alpha-only supplemental filament candidates near the limb."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import json

import cv2
import numpy as np
from scipy.ndimage import binary_dilation, find_objects, label
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
import torch

from .ar_hmi_temporal_ranker import matching, object_scores
from .durable import atomic_json
from .fl_author_model import AuthorGraph
from .fl_ensemble_study import CONNECTIVITY, DATASET, ROOT
from .fl_gong_fused_verify import later_pairs
from .fl_gong_limb_gate import DISTANCE, visible_disk
from .fl_gong_patch_training import image_bytes
from .fl_gong_resolution import predict
from .fl_magfilo_adaptation import source_receipt, stratified
from .fl_magfilo_audit import DATA_SHA256, raster
from .fl_magfilo_transfer import LABEL_SHA256, inventory, load_gray
from .fl_spine_candidate_review import candidate_data
from .fl_spine_context_study import EPOCHS, architecture, context, full_score, locate
from .journal import read
from .reviewed_train import load_checkpoint
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


INNER_RADIUS = .75
ARMS = ('shape_score', 'local_halpha')


def local_features(regions, gray, radius):
    """Compare each candidate with on-disk surroundings beyond its bounding box."""
    position = DISTANCE / radius
    features = []
    for ident, (y, x) in enumerate(find_objects(regions), 1):
        box = (slice(max(0, y.start - 8), min(512, y.stop + 8)),
               slice(max(0, x.start - 8), min(512, x.stop + 8)))
        local = regions[box]
        mask = local == ident
        radial = position[box]
        ring = binary_dilation(mask, iterations=7) & ~binary_dilation(mask, iterations=2)
        ring &= (local == 0) & (radial <= 1)
        field = gray[box]
        center = float(np.median(field[mask]))
        outside = field[ring]
        average = float(np.median(outside)) if outside.size else center
        spread = float(np.quantile(outside, .8) - np.quantile(outside, .2)) if outside.size else 0.
        mass_center = tuple(round(v) for v in np.argwhere(mask).mean(axis=0))
        r = float(radial[mass_center])
        features.append([r, float((radial[mask] <= 1).mean()), center, average - center, spread,
                         (average - center) / max(.035, spread), float((field[mask] < average).mean())])
    return features


def gather(raw, row, references, base, joint):
    gray = load_gray(raw)
    disk512, radius = visible_disk(gray)
    if disk512 is None:
        raise ValueError('本试验需核验每张 Hα 图的日面半径')
    initial = predict(base, gray[None])[0] >= .5
    data, disk = context(raw)
    contour, spine = full_score(joint, data, disk)
    regions, basic = candidate_data(gray, contour, spine, locate(contour, spine, initial))
    extra = local_features(regions, gray, radius)
    baseline, n = label(initial, structure=CONNECTIVITY)
    tagged = np.where(regions > 0, regions + n, baseline)
    parts = [{'area': int(v)} for v in np.bincount(tagged.ravel())[1:]]
    raw_targets, benign_negative = np.zeros(len(extra), bool), np.ones(len(extra), bool)
    all_refs = []
    for ref in references:
        answer, count = label(ref, structure=CONNECTIVITY)
        geometry = object_scores(tagged, parts, answer, [True] * n + [False] * len(extra))[1]
        score = np.asarray(geometry['iou']).reshape(n + len(extra), count)
        missed = score[:n].max(axis=0, initial=0.) == 0
        raw_targets |= (score[n:, missed] >= .50).any(axis=1)
        benign_negative &= ~(score[n:] > 0).any(axis=1)
        ref_centers = []
        for ident, (y, x) in enumerate(find_objects(answer), 1):
            point = np.argwhere(answer[y, x] == ident).mean(axis=0) + (y.start, x.start)
            ref_centers.append(float(np.hypot(*(point - 255.5)) / radius))
        all_refs.append({**geometry, 'radius': ref_centers})
    return {'file': row['file'], 'base_count': n, 'shape_score': basic,
            'local_halpha': [a + b for a, b in zip(basic, extra, strict=True)],
            'limb_candidates': [INNER_RADIUS <= geom[0] <= 1 and geom[1] >= .95 for geom in extra],
            'target_state': np.where(raw_targets, 1, np.where(benign_negative, 0, -1)).tolist(),
            'references': all_refs}


def measure(rows, choices):
    counts = Counter()
    for row, keep in zip(rows, choices, strict=True):
        use = [True] * row['base_count'] + [bool(v) for v in keep]
        for g in row['references']:
            detected, refs = matching(g, use)
            point = np.asarray(g['radius'])
            limb = (INNER_RADIUS <= point) & (point <= 1)
            matrix = np.asarray(g['iou']).reshape(len(use), len(point))
            overlap = (matrix[np.asarray(use, dtype=bool)] > 0).any(axis=0)
            counts.update(predicted=sum(use), matched=len(detected), reference=len(point),
                          limb_references=int(limb.sum()), limb_matches=int(limb[refs].sum()),
                          limb_complete_misses=int((limb & ~overlap).sum()),
                          limb_new_matched=int((limb[refs] & (detected >= row['base_count'])).sum()))
    return {**counts, 'precision': counts['matched'] / max(1, counts['predicted']),
            'limb_recall': counts['limb_matches'] / max(1, counts['limb_references']),
            'unmatched': counts['predicted'] - counts['matched']}


def keep_limb(row, scores=None, cutoff=None):
    limb = np.asarray(row['limb_candidates'], dtype=bool)
    return limb if scores is None else limb & (scores >= cutoff)


def choose_policy(arm, fit, develop):
    X, Y = [], []
    for row in fit:
        for features, status in zip(row[arm], row['target_state'], strict=True):
            if status >= 0:
                X.append(features)
                Y.append(status)
    brief = {'fit_candidates': len(Y), 'fit_positive': sum(Y)}
    if min(sum(Y), len(Y) - sum(Y)) < 12:
        return None, None, {**brief, 'state': 'fit_targets_insufficient'}
    estimator = make_pipeline(StandardScaler(), LogisticRegression(C=1, class_weight='balanced', max_iter=500))
    estimator.fit(X, Y)
    values = [estimator.decision_function(r[arm]) if r[arm] else np.empty(0) for r in develop]
    sample = np.concatenate([v[keep_limb(r)] for r, v in zip(develop, values, strict=True)])
    if not sample.size:
        return estimator, None, {**brief, 'state': 'no_limb_candidates'}
    thresholds = np.unique(np.quantile(sample, np.linspace(0, 1, min(100, len(sample)))))
    base = measure(develop, ([False] * len(r['target_state']) for r in develop))
    outcomes = []
    for threshold in thresholds:
        result = measure(develop, (keep_limb(r, v, threshold) for r, v in zip(develop, values, strict=True)))
        gain = result['matched'] - base['matched']
        extra = result['unmatched'] - base['unmatched']
        limb_gain = result['limb_matches'] - base['limb_matches']
        if limb_gain >= 3 and gain >= limb_gain and extra <= gain:
            outcomes.append((limb_gain, -extra, float(threshold)))
    if not outcomes:
        return estimator, None, {**brief, 'state': 'development_gate_failed'}
    count, noise, cutoff = max(outcomes)
    return estimator, cutoff, {**brief, 'state': 'qualified_for_held_years', 'cutoff': cutoff,
                               'limb_gain': count, 'extra_unmatched': -noise}


def finalize(output, resources, recovered=False):
    plan = read(output / 'protocol.json')
    cases = {k: read(output / f'{k}-cases.json') for k in plan['samples']}
    if any(rows is None or [r['file'] for r in rows] != plan['samples'][key] for key, rows in cases.items()):
        raise ValueError('缺少与原协议一致的逐图记录')
    models = {arm: choose_policy(arm, cases['fit'], cases['development']) for arm in ARMS}
    rules = {arm: m[2] for arm, m in models.items()}
    if read(output / 'rules.json', rules) != rules:
        raise ValueError('汇总复算的复核规则与冻结结果不一致')
    scores = {}
    for key, rows in cases.items():
        if key == 'fit':
            continue
        result = {'baseline': measure(rows, ([False] * len(r['target_state']) for r in rows)),
                  'all_limb': measure(rows, map(keep_limb, rows))}
        for arm, (model, cutoff, _) in models.items():
            if cutoff is not None:
                result[arm] = measure(rows, (keep_limb(r, model.decision_function(r[arm]) if r[arm] else np.empty(0), cutoff)
                                             for r in rows))
        scores[key] = result
    result = {'state': 'completed', 'recorded_utc': now(), 'policy': rules, 'scores': scores, 'resources': resources,
              'samples': {k: len(v) for k, v in cases.items()}, 'recovered_from_prediction_records': recovered,
              'resources_scope': 'last progress measurement' if recovered else 'entire run',
              'finalizer_sha256': digest(__file__), 'protocol_sha256': digest(output / 'protocol.json'),
              'cases_sha256': {k: digest(output / f'{k}-cases.json') for k in cases},
              'deployed': False, 'test_accessed': False}
    atomic_json(output / 'summary.json', result)
    atomic_json(output / 'status.json', {'state': 'completed', 'summary_sha256': digest(output / 'summary.json')})
    return result


def run(source, release, early, development, later, anchor, trained, output):
    output = output_path(output, DATASET)
    inputs = early, development, later, anchor, trained
    if output.exists() or any(output.is_relative_to(p.resolve()) for p in inputs):
        raise ValueError('边缘补检试验需使用新的目录')
    if digest(source) != DATA_SHA256 or digest(release) != LABEL_SHA256:
        raise ValueError('MAGFiLO 版本与登记来源不符')
    rows, annotations = inventory(source, release)
    catalog = {r['file']: r for r in rows}
    directory_rows = source_receipt(early, 'train_available', catalog)
    dev_rows = source_receipt(development, 'transfer_dev', catalog)
    hold_rows, other = later_pairs(source, release)
    legacy = read(later / 'summary.json')
    if legacy['inventory_sha256'] != digest(later / 'inventory.json') or legacy['test_accessed']:
        raise ValueError('历史验证图清单不符')
    receipt = {r['file']: r for r in read(later / 'inventory.json')}
    if set(receipt) != {r['file'] for r in hold_rows}:
        raise ValueError('历史验证图日期不符')
    hold_rows = [receipt[r['file']] for r in hold_rows]
    base_path = anchor / 'mixed_gong' / 'last.pt'
    trained_info = read(trained / 'summary.json')
    spine_path = trained / 'mask_and_spine' / 'last.pt'
    checkpoint = torch.load(spine_path, map_location='cpu', weights_only=True)
    if (digest(base_path) != read(anchor / 'summary.json')['results']['mixed_gong']['weights_sha256']
            or trained_info['test_accessed'] or trained_info['protocol_sha256'] != digest(trained / 'protocol.json')
            or checkpoint['identity'] != [trained_info['protocol_sha256'], 'mask_and_spine']
            or len(checkpoint['history']) != EPOCHS):
        raise ValueError('冻结模型或先前训练身份不符')
    used = {r['file'][:8] for r in read(trained / 'label-quality.json')}
    used.update(s[:8] for s in read(anchor / 'protocol.json')['gong_training_images'])
    eligible = [r for r in directory_rows if r['file'][:8] not in used]
    fit = []
    for station in 'BCLMTU':
        candidates = [r for r in eligible if r['file'][14] == station]
        if candidates:
            fit.extend(stratified(candidates, min(32, len(candidates)), 'captured_utc'))
    cohorts = {'fit': (early, fit, annotations), 'development': (development, [r for r in dev_rows if r['file'].startswith('2017')], annotations),
               '2018': (development, [r for r in dev_rows if r['file'].startswith('2018')], annotations),
               '2019_2020': (later, hold_rows, other)}
    files = [r['file'] for _, rows, _ in cohorts.values() for r in rows]
    if len(set(files)) != len(files) or any(not r['file'][:4] < '2017' for r in fit):
        raise ValueError('拟合与验证日期隔离失败')
    atomic_json(output / 'protocol.json', {'code_sha256': digest(__file__), 'weight_sha256': [digest(base_path), digest(spine_path)],
        'samples': {k: [r['file'] for r in rows] for k, (_, rows, _) in cohorts.items()}, 'limb_radius': [.75, 1.],
        'gate': 'minimum 3 strict new limb matches and added unmatched not exceeding matched gains on 2017 only',
        'scope': 'external H-alpha GONG study; 2018 and 2019–2020 previously used on separate models; official test untouched',
        'test_accessed': False})
    cv2.setNumThreads(2)
    torch.set_num_threads(2)
    cases = {}
    with exclusive(ROOT / '.mdva/training-gpu.lock'), torch.inference_mode():
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        torch.backends.cudnn.allow_tf32 = torch.backends.cuda.matmul.allow_tf32 = False
        whole, spine = AuthorGraph('batch').cuda().eval(), architecture().cuda().eval()
        whole.load_state_dict(load_checkpoint(base_path)['model'])
        spine.load_state_dict(checkpoint['model'])
        meter = Resources()
        for group, (folder, images, mapping) in cohorts.items():
            cases[group] = []
            for row in images:
                if (output / 'PAUSE').exists():
                    return {'state': 'paused', 'group': group, 'processed': len(cases[group])}
                raw = image_bytes(folder, row)
                targets = [raster(mapping[a['id']].values() if isinstance(mapping[a['id']], dict) else mapping[a['id']])
                           for a in row['annotators']]
                cases[group].append(gather(raw, row, targets, whole, spine))
                if len(cases[group]) % 16 == 0:
                    state = {'state': 'running', 'group': group, 'processed': len(cases[group]), 'total': len(images)}
                    atomic_json(output / 'status.json', {**state, 'resources': meter.sample()})
            atomic_json(output / f'{group}-cases.json', cases[group])
            if group == 'development':
                rules = {arm: choose_policy(arm, cases['fit'], cases[group])[2] for arm in ARMS}
                atomic_json(output / 'rules.json', rules)
        resources = meter.sample()
    return finalize(output, resources)


if __name__ == '__main__':
    configure_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    for parameter in ('source', 'release', 'early', 'development', 'later', 'anchor', 'trained', 'output'):
        parser.add_argument('--' + parameter, type=Path, required=True)
    print(json.dumps(run(**vars(parser.parse_args())), ensure_ascii=False))
