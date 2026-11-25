"""Test whether frozen weak-contrast scores add isolated references to FL masks."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

import cv2
import numpy as np
from safetensors.torch import load_file
from scipy.ndimage import binary_dilation, binary_propagation, find_objects, label
import torch

from .ar_hmi_temporal_ranker import build_metrics, object_scores
from .durable import atomic_json
from .fl_author_model import AuthorGraph
from .fl_candidate_review import describe, score_row
from .fl_ensemble_study import CONNECTIVITY, DATASET, ROOT
from .fl_gong_patch_training import image_bytes
from .fl_gong_resolution import predict, resize
from .fl_magfilo_adaptation import source_receipt
from .fl_magfilo_audit import raster
from .fl_magfilo_transfer import inventory, load_gray
from .fl_native_local_training import local_probabilities, model_for, view
from .journal import read
from .reviewed_train import load_checkpoint
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


SEEDS = (.15, .25, .35)
LIMITS = (1, 2, None)
ARM = 'gray_plus_contrast'


def independent_regions(score, anchor, seed):
    initial = (score >= seed)
    grown = binary_propagation(initial, structure=CONNECTIVITY, mask=score >= .60 * seed)
    regions = label(grown, structure=CONNECTIVITY)[0]
    conflict = set(np.unique(regions[binary_dilation(anchor, structure=CONNECTIVITY, iterations=2)]))
    candidates = []
    for i, win in enumerate(find_objects(regions), 1):
        if i in conflict:
            continue
        inside = regions[win] == i
        area = int(inside.sum())
        if not 8 <= area <= 1024:
            continue
        tiles = score[win][inside]
        candidates.append((float(tiles.mean()), i, win, inside, area))
    ordered = sorted(candidates, key=lambda item: (-item[0], item[1]))
    output = np.zeros_like(regions)
    for new_id, (_, _, window, pixels, _) in enumerate(ordered, 1):
        output[window][pixels] = new_id
    return output, [{'area': item[-1]} for item in ordered]


def measure(gray, broad, local, masks, rule):
    raw, inputs = describe(gray, broad)
    scores = score_row({'features': inputs.tolist()}, rule['classifier'])
    keep = np.flatnonzero(scores >= rule['cutoffs']['shape_and_brightness']) + 1
    anchor = np.isin(raw, keep)
    previous, n = label(anchor, structure=CONNECTIVITY)
    fields = [{'area': int(a)} for a in np.bincount(previous.ravel(), minlength=n + 1)[1:]]
    refs = [label(mask, structure=CONNECTIVITY)[0] for mask in masks]
    digest = lambda metrics: {k: metrics[k] for k in ('reference', 'predicted', 'matched')}
    arms = {'whole_reviewed': [digest(object_scores(previous, fields, ref, [True] * n)[0]) for ref in refs]}
    limits_by_ref = []
    for seed in SEEDS:
        additions, extra = independent_regions(local, raw != 0, seed)
        composite = np.where(additions > 0, additions + n, previous)
        breakdown = [object_scores(composite, fields + extra, ref, [True] * n + [False] * len(extra))[1] for ref in refs]
        for cap in LIMITS:
            limit = len(extra) if cap is None else min(cap, len(extra))
            current = [True] * (n + limit) + [False] * (len(extra) - limit)
            arms[f'{seed}/' + (str(cap) if cap is not None else 'all')] = [digest(build_metrics(g, current)[0]) for g in breakdown]
        # Reference-assisted comparison only: which anchors have room for a disjoint additional region?
        potential = []
        for geom in breakdown:
            overlaps = np.asarray(geom['iou'], dtype=float).reshape(n + len(extra), len(geom['reference_ids']))
            isolated = overlaps[:n].max(axis=0, initial=0) == 0
            strong = overlaps[n:].max(axis=0, initial=0) >= .50
            potential.append(set((np.flatnonzero(isolated & strong) + 1).tolist()))
        limits_by_ref.append(potential)
    oracle = sum(len(set.union(*values)) for values in zip(*limits_by_ref, strict=True))
    return arms, oracle


def totals(rows):
    result = {}
    for key in rows[0]['arms']:
        sums = sum((Counter(g) for row in rows for g in row['arms'][key]), Counter())
        result[key] = {**sums, 'unmatched': sums['predicted']-sums['matched'],
                       'precision': sums['matched']/max(1, sums['predicted']), 'recall': sums['matched']/max(1, sums['reference'])}
    return result


def select_rule(fit):
    base = fit['whole_reviewed']
    viable = []
    for name, current in fit.items():
        found = current['matched'] - base['matched']
        extra = current['unmatched'] - base['unmatched']
        if found >= 3 and extra <= found:
            viable.append((found, -extra, name))
    return max(viable)[2] if viable else None


def run(source, release, development, local, anchor, review, output):
    output = output_path(output, DATASET)
    if output.exists() or any(output.is_relative_to(p.resolve()) for p in (development, local, anchor, review)):
        raise ValueError('需使用新的弱暗条补检目录')
    local_summary, review_summary = (read(p / 'summary.json') for p in (local, review))
    if (local_summary['state'] != 'completed' or local_summary['protocol_sha256'] != digest(local / 'protocol.json')
            or review_summary['rule_sha256'] != digest(review / 'rules.json')):
        raise ValueError('原始模型与复核规则身份不同')
    current = local / ARM / 'research.safetensors'
    original = anchor / 'mixed_gong' / 'last.pt'
    if (read(local / 'protocol.json')['anchor_sha256'] != digest(original)
            or read(review / 'protocol.json')['weights_sha256']['gong'] != digest(original)):
        raise ValueError('基线权重必须一致')
    data, refs = inventory(source, release)
    opened = source_receipt(development, 'transfer_dev', {r['file']:r for r in data})
    if any(not r['file'].startswith(('2017', '2018')) for r in opened):
        raise ValueError('只允许访问早期开发日期')
    rule = read(review / 'rules.json')['gong']
    cv2.setNumThreads(2)
    torch.set_num_threads(2)
    marker = output / 'protocol.json'
    atomic_json(marker, {'code_sha256': digest(__file__), 'local_checkpoint_sha256': digest(current), 'whole_sha256': digest(original),
                         'rules_sha256': digest(review / 'rules.json'), 'source_sha256': digest(development / 'inventory.json'),
                         'fit': 'all 2017 development images', 'check': 'all 2018 development images',
                         'role': 'research replay only; leave 2022 observations untouched', 'seed_thresholds': SEEDS,
                         'top_limits': LIMITS, 'added_eligibility': '8–1024 pixels, isolated by two pixels from any original candidate',
                         'selection': 'at least 3 additional strict matches, at most one extra unmatched per match',
                         'test_accessed': False, 'calibration_accessed': False})
    rows, exclusions = [], []
    with exclusive(ROOT / '.mdva/training-gpu.lock'), torch.inference_mode():
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cuda.matmul.allow_tf32 = False
        usage = Resources()
        base = AuthorGraph('batch').cuda().eval()
        base.load_state_dict(load_checkpoint(original)['model'])
        second = model_for().cuda().eval()
        second.load_state_dict(load_file(current))
        for r in opened:
            if (output / 'PAUSE').exists():
                state = {'state': 'paused', 'processed': len(rows), 'total': len(opened)}
                atomic_json(output / 'status.json', state)
                return state
            raw = image_bytes(development, r)
            try:
                arrays, disk = view(raw)
            except ValueError as exc:
                exclusions.append({'file': r['file'], 'reason': str(exc)})
                continue
            gray = load_gray(raw)
            whole = predict(base, gray[None])[0]
            local_scores = resize(local_probabilities(second, arrays, ARM, disk), 512)
            ground = [raster(refs[a['id']]) for a in r['annotators']]
            arms, oracle = measure(gray, whole, local_scores, ground, rule)
            rows.append({'file': r['file'], 'station': r['file'][14:16], 'arms': arms, 'oracle_references': oracle})
            if len(rows) % 15 == 0:
                atomic_json(output / 'status.json', {'state': 'evaluating', 'processed': len(rows), 'total': len(opened)})
                usage.sample()
        resources = usage.sample()
    earlier = [r for r in rows if r['file'].startswith('2017')]
    later = [r for r in rows if r['file'].startswith('2018')]
    fit = totals(earlier)
    chosen = select_rule(fit)
    check = totals(later)
    atomic_json(output / 'cases.json', rows)
    summary = {'state': 'completed', 'recorded_utc': now(), 'images': {'fit_2017': len(earlier), 'check_2018': len(later)},
               'fit': fit, 'check_audit': check, 'selected_from_2017': chosen, 'check_result': check.get(chosen),
               'reference_audit_oracle': {'confirmation_2018': sum(r['oracle_references'] for r in later)},
               'excluded': exclusions, 'resources': resources, 'protocol_sha256': digest(marker),
               'cases_sha256': digest(output / 'cases.json'), 'test_accessed': False, 'deployed': False}
    atomic_json(output / 'summary.json', summary)
    atomic_json(output / 'status.json', {'state': 'completed', 'processed': len(rows), 'total': len(opened)})
    return summary


if __name__ == '__main__':
    configure_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('source', 'release', 'development', 'local', 'anchor', 'review', 'output'):
        parser.add_argument('--' + key, type=Path, required=True)
    result = run(**vars(parser.parse_args()))
    print(json.dumps({k:result[k] for k in ('state', 'images', 'selected_from_2017', 'check_result', 'reference_audit_oracle') if k in result}, ensure_ascii=False))
