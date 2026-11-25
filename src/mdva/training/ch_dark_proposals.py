"""Evaluate independently generated dark AIA193 candidates for fully missed CH regions."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import binary_dilation, binary_propagation, find_objects, gaussian_filter, label
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
import torch

from .ar_hmi_temporal_ranker import build_metrics, matching, object_scores
from .ch_boundary_growth import CONNECTIVITY, candidate_masks
from .ch_region_complement_study import probabilities
from .ch_seed_ensemble import MODELS
from .datasets import SegmentationDataset
from .durable import atomic_json
from .journal import read
from .reviewed_train import load_checkpoint
from .run_locations import model_and_identity, run_folder
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path
from .spatial_audit import apparent_disk


SEED = 2026
TRAIN_IMAGES = 264
ROOT = Path(__file__).resolve().parents[3]
Y, X = np.ogrid[:512, :512]
FEATURES = ('log_area', 'fill', 'rho', 'radial_ratio', 'contrast_ratio', 'learned_mean',
            'learned_peak', 'radial_core_share', 'contrast_core_share')


def local_average(gray, support, width):
    normalizer = gaussian_filter(support.astype(np.float32), width)
    return gaussian_filter(gray * support, width) / np.maximum(normalizer, .05)


def dark_regions(gray, anchor, probability):
    d = apparent_disk(gray)
    rho = np.hypot(X - d['cx'], Y - d['cy']) / d['radius']
    disk = rho <= .98
    radial_edges = np.linspace(0, 1, 13)
    white_safe = gray.astype(np.float32) / 255
    fine = local_average(white_safe, disk, 2.)
    coarse = local_average(white_safe, disk, 16.)
    centers = (radial_edges[1:] + radial_edges[:-1]) / 2
    radial_ref = np.array([np.median(fine[(rho >= a) & (rho < b) & disk])
                           for a, b in zip(radial_edges, radial_edges[1:])])
    if (not np.isfinite(radial_ref).all() or min(radial_ref) <= 0):
        raise ValueError('原图无可用的参考亮度')
    radial = fine / np.maximum(np.interp(rho, centers, radial_ref), .03)
    local = fine / np.maximum(coarse, .03)
    core = disk & (radial <= .72) & (local <= .84)
    support = disk & (radial <= .88) & (local <= .96)
    candidates, _ = label(binary_propagation(core, structure=CONNECTIVITY, mask=support), structure=CONNECTIVITY)
    touching = set(np.unique(candidates[binary_dilation(anchor, structure=CONNECTIVITY)]))
    regions = np.zeros_like(candidates)
    parts = []
    for identity, window in enumerate(find_objects(candidates), 1):
        if identity in touching:
            continue
        owned = candidates[window] == identity
        n = int(owned.sum())
        if not 32 <= n <= 12000:
            continue
        p = probability[window][owned]
        r, l = radial[window][owned], local[window][owned]
        parts.append({'area': n, 'features': [
            float(np.log1p(n)), n / owned.size, float(np.mean(rho[window][owned])),
            float(np.mean(r)), float(np.mean(l)), float(p.mean()), float(p.max()),
            float((r <= .72).mean()), float((l <= .84).mean()),
        ]})
        regions[window][owned] = len(parts)
    return regions, parts


def describe(row, gray, prob, reference):
    anchor = candidate_masks(prob)['bounded_growth']
    original, n = label(anchor, structure=CONNECTIVITY)
    baseline_parts = [{'area': int(a)} for a in np.bincount(original.ravel(), minlength=n + 1)[1:]]
    try:
        proposal, parts = dark_regions(gray, anchor, prob)
        disk_supported = True
    except ValueError:
        proposal, parts = np.zeros_like(original), []
        disk_supported = False
    geometry = object_scores(np.where(proposal > 0, proposal + n, original), baseline_parts + parts, reference,
                             [True] * n + [False] * len(parts))[1]
    start_matches, truth = matching(geometry, [True] * n + [False] * len(parts))
    reference_set = set(truth.tolist())
    proposals = np.asarray(geometry['iou'], dtype=float).reshape(n + len(parts), len(geometry['reference_ids']))[n:]
    unclaimed = [i for i in range(proposals.shape[1]) if i not in reference_set]
    known = (proposals[:, unclaimed] >= .5).any(axis=1)
    return {'uri': row['uri'], 'time': row['time_utc'], 'disk_supported': disk_supported,
            'baseline_n': n, 'inputs': [p['features'] for p in parts], 'known': known.tolist(),
            'geometry': geometry}


def fit(rows):
    before = [r for r in rows if r['time'] < '2016']
    policy = [r for r in rows if '2017' <= r['time'] < '2019']
    inputs = [x for r in before for x in r['inputs']]
    positive = [truth for r in before for truth in r['known']]
    test = [x for r in policy for x in r['inputs']]
    known = np.asarray([truth for r in policy for truth in r['known']])
    info = {'images_fit': len(before), 'images_policy': len(policy), 'fit_candidates': len(inputs),
            'fit_positive': sum(positive), 'policy_candidates': len(test), 'policy_positive': int(known.sum())}
    if min(sum(positive), len(positive) - sum(positive)) < 15 or known.sum() < 6:
        return None, None, {**info, 'state': 'insufficient_positive_candidates'}
    model = make_pipeline(StandardScaler(), LogisticRegression(C=1., class_weight='balanced', random_state=SEED))
    model.fit(inputs, positive)
    scores = model.decision_function(test)
    candidates = []
    for point in np.unique(scores):
        keep = scores >= point
        count = int(keep.sum())
        if count >= 8 and known[keep].mean() >= .60:
            candidates.append((int(known[keep].sum()), -count, float(point)))
    estimator = model.steps[-1][1]
    scaler = model.steps[0][1]
    info.update(coefficients=estimator.coef_[0].tolist(), intercept=float(estimator.intercept_[0]),
                center=scaler.mean_.tolist(), scale=scaler.scale_.tolist())
    if not candidates:
        return model, None, {**info, 'state': 'policy_precision_unmet'}
    hits, count, threshold = max(candidates)
    return model, threshold, {**info, 'state': 'completed', 'policy_kept': -count, 'policy_matches': hits,
                              'threshold': threshold}


def score_row(row, model, threshold):
    known = np.asarray(row['known'], dtype=bool)
    learned = (model.decision_function(row['inputs']) >= threshold
               if model is not None and threshold is not None and len(known) else np.zeros_like(known))
    masks = {'anchor': np.zeros_like(known), 'all_dark': np.ones_like(known),
             'reference_audit_only': known, 'learned': learned}
    result = {name:build_metrics(row['geometry'], [True] * row['baseline_n'] + mask.tolist())[0]
              for name, mask in masks.items()}
    return {**row, 'results': result}


def aggregate(rows):
    scores = {}
    for name in rows[0]['results']:
        totals = Counter()
        for row in rows:
            totals.update({k:row['results'][name][k] for k in ('reference', 'predicted', 'matched')})
        scores[name] = {**totals, 'precision': totals['matched']/totals['predicted'],
                        'recall': totals['matched']/totals['reference'], 'unmatched': totals['predicted']-totals['matched']}
    return scores


def model_set(experiment, checksums):
    models = []
    for key in MODELS:
        run = run_folder(experiment, key)
        checkpoint = run / 'best.pt'
        if digest(checkpoint) != checksums[key]:
            raise ValueError(f'{key} 检查点身份不符')
        saved = load_checkpoint(checkpoint)
        config = saved['identity']['config']
        factory, identity = model_and_identity(config)
        if config['id'] != key or saved['identity']['code'] != identity():
            raise ValueError('冕洞训练架构身份不符')
        model, _ = factory(config)
        model.load_state_dict(saved['model'])
        models.append(model)
    return models


def run(dataset, experiment, diagnostics, prior, output):
    torch.set_num_threads(2)
    output = output_path(output, dataset)
    if output.exists() or any(output.is_relative_to(p.resolve()) for p in (experiment, diagnostics, prior)):
        raise ValueError('冕洞候选生成应在新的结果目录验证')
    report, previous = (read(prior / k) for k in ('summary.json', 'protocol.json'))
    index_file = diagnostics / 'case-index.json'
    if (report['state'] != 'completed' or report['test_accessed'] or report['calibration_accessed']
            or report['protocol_sha256'] != digest(prior / 'protocol.json')
            or previous['diagnostic_sha256'] != digest(index_file)):
        raise ValueError('冕洞固定对照身份不符')
    training = SegmentationDataset(dataset, 'CH', 'train', 512)
    val = SegmentationDataset(dataset, 'CH', 'validation', 512)
    candidates = sorted(training.rows, key=lambda r:(r['time_utc'],r['uri']))
    rng = np.random.default_rng(SEED)
    training.rows = [candidates[i] for strand in np.array_split(np.arange(len(candidates)), 24)
                     for i in sorted(rng.choice(strand, TRAIN_IMAGES // 24, replace=False))]
    model = model_set(experiment, previous['models'])
    protocol = {
        'code_sha256': digest(__file__), 'prior_sha256': digest(prior / 'summary.json'),
        'models': previous['models'], 'training_images': [r['uri'] for r in training.rows],
        'positive_rule': 'new isolated dark region must match a previously uncovered-by-matching reference with IoU >=.5',
        'fit_scope': 'before 2016; threshold 2017–2018 only; at least 8 policy candidates and >=60% precision',
        'features': FEATURES, 'test_accessed': False, 'calibration_accessed': False,
    }
    atomic_json(output / 'protocol.json', protocol)
    train_rows = []
    with exclusive(ROOT / '.mdva/training-gpu.lock'), torch.inference_mode():
        torch.cuda.reset_peak_memory_stats()
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cuda.matmul.allow_tf32 = False
        for net in model:
            net.cuda().eval()
        usage = Resources()
        for i, row in enumerate(training.rows):
            item = training[i]
            x = item['image'][None].cuda()
            fused = np.stack([probabilities(net, x) for net in model]).mean(axis=0, dtype=np.float32)
            image = np.rint(item['image'][0].numpy() * 255).astype(np.uint8)
            refs = label(item['semantic_mask'][0].numpy() > 0, structure=CONNECTIVITY)[0]
            train_rows.append(describe(row, image, fused, refs))
            if i % 32 == 0:
                usage.sample()
        resource = usage.sample()
    ranker, cutoff, policy = fit(train_rows)
    atomic_json(output / 'rules.json', policy)
    book = {(r['uri'], r['run_id']):r for r in read(index_file)
            if r['run_id'] in MODELS and r['diagnostic_subset'] == 'validation'}
    latest = []
    for item in val.rows:
        stack = []
        for mode in MODELS:
            card = book[item['uri'], mode]
            path = diagnostics / 'probabilities' / mode / (card['sample_id'] + '.npy')
            if digest(path) != card['probability_sha256'] or item['mask_uri'] != card['mask_uri']:
                raise ValueError('冕洞开发缓存已改变')
            stack.append(np.load(path, allow_pickle=False))
        fused = np.stack(stack).mean(axis=0, dtype=np.float32)
        with Image.open(val.source(item['uri'])) as image:
            gray = np.asarray(image.convert('L'))
        refs = label(np.load(val.source(item['mask_uri']), allow_pickle=False) > 0, structure=CONNECTIVITY)[0]
        latest.append(score_row(describe(item, gray, fused, refs), ranker, cutoff))
    result = aggregate(latest)
    legacy = report['arms']['mean_three']
    if any(legacy[k] != result['anchor'][k] for k in ('reference', 'predicted', 'matched')):
        raise ValueError('冕洞三种子开发基线无法复算')
    atomic_json(output / 'development.json', latest)
    summary = {
        'state': 'completed', 'recorded_utc': now(), 'development': result, 'fit': policy,
        'counts': {'train_images': len(train_rows), 'development_images': len(latest),
                   'geometry_supported': sum(r['disk_supported'] for r in latest),
                   'dark_candidates': sum(len(r['known']) for r in latest), 'strictly_improving_candidates': sum(sum(r['known']) for r in latest)},
        'protocol_sha256': digest(output / 'protocol.json'), 'development_sha256': digest(output / 'development.json'),
        'resources': resource, 'test_accessed': False, 'calibration_accessed': False, 'deployed': False,
    }
    atomic_json(output / 'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dataset', 'experiment', 'diagnostics', 'prior', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    configure_environment()
    r = run(**vars(parser.parse_args()))
    print(json.dumps({k:r[k] for k in ('state', 'counts', 'fit', 'development')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
