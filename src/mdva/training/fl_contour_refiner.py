"""Learn complete H-alpha contours from partial frozen-model candidates."""

from __future__ import annotations

import argparse
from pathlib import Path
import json

import cv2
import numpy as np
from scipy.ndimage import binary_dilation, label
from torch import nn
import torch
from torch.nn import functional as F

from .durable import atomic_json, commit
from .ar_hmi_temporal_ranker import matching, object_scores
from .fl_author_model import AuthorGraph
from .fl_ensemble_study import CONNECTIVITY, DATASET, ROOT, overlap_profile
from .fl_gong_fused_verify import later_pairs
from .fl_gong_limb_gate import visible_disk
from .fl_gong_patch_training import image_bytes
from .fl_gong_resolution import predict
from .fl_magfilo_audit import DATA_SHA256, compare, raster
from .fl_magfilo_transfer import LABEL_SHA256, inventory, load_gray, metrics
from .fl_native_positive_weight import weighted_loss
from .fl_spine_context_study import architecture, context, full_score, locate
from .journal import read
from .models import UNet
from .reviewed_train import load_checkpoint
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


SIDE, PATCH, BATCH, EPOCHS, SEED = 512, 96, 16, 16, 2026
MIN_IOU = .08


def scene(raw, whole, axial):
    gray = load_gray(raw)
    disk, radius = visible_disk(gray)
    if disk is None:
        raise ValueError('日面半径核验失败')
    probability = predict(whole, gray[None])[0]
    features, support = context(raw)
    raw_mask, spine = full_score(axial, features, support)
    baseline = probability >= .5
    candidates, size = label(locate(raw_mask, spine, baseline), structure=CONNECTIVITY)
    weights = cv2.GaussianBlur(disk.astype(np.float32), (0, 0), 4)
    nearby = cv2.GaussianBlur(gray * disk, (0, 0), 4) / np.maximum(weights, .02)
    field = np.stack((2 * gray - 1, np.clip((nearby - gray) * 8, -2, 2), probability, spine))
    return field.astype(np.float32), baseline, disk, radius, candidates, size


def window(part):
    y, x = np.nonzero(part)
    centers = ((y.min() + y.max()) // 2, (x.min() + x.max()) // 2)
    starts = np.clip(np.array(centers) - PATCH // 2, 0, SIDE - PATCH)
    return tuple(slice(int(start), int(start + PATCH)) for start in starts)


def patches(box, field, part):
    return np.concatenate((field[(slice(None), *box)], part[box][None])).astype(np.float16)


def supervising_candidates(regions, count, references):
    matches, parts = [], []
    for mask in references:
        targets, n = label(mask, structure=CONNECTIVITY)
        matrix = np.bincount((regions * (n + 1) + targets).ravel(),
                             minlength=(count + 1) * (n + 1)).reshape(count + 1, n + 1)
        overlap = matrix[1:, 1:]
        union = matrix[1:, :].sum(1)[:, None] + matrix[:, 1:].sum(0)[None, :] - overlap
        iou = overlap / np.maximum(1, union)
        selected = iou.argmax(axis=1) + 1 if n else np.zeros(count, dtype=int)
        matches.append((selected, iou.max(axis=1, initial=0)))
        parts.append(targets)
    scores = np.stack([m[1] for m in matches])
    positive = np.flatnonzero((scores >= MIN_IOU).all(axis=0))
    negative = np.flatnonzero((scores == 0).all(axis=0))
    return positive, negative, matches, parts


def gather(raw, references, whole, axial):
    field, _, disk, _, regions, count = scene(raw, whole, axial)
    positive, negative, matches, parts = supervising_candidates(regions, count, references)
    supplies = []
    for i in positive[:8]:
        part = regions == i + 1
        box = window(part)
        target = np.stack([area == choice[0][i] for area, choice in zip(parts, matches, strict=True)])
        present = target.sum(axis=(1, 2))
        if np.any(target[(slice(None), *box)].sum(axis=(1, 2)) < .95 * present):
            continue
        supplies.append((patches(box, field, part), target[(slice(None), *box)].mean(axis=0), disk[box], True))
    area = np.bincount(regions.ravel(), minlength=count + 1)[1:]
    hardest = negative[np.argsort(-area[negative], kind='stable')[:max(2, len(supplies))]]
    for i in hardest:
        part = regions == i + 1
        box = window(part)
        supplies.append((patches(box, field, part), np.zeros((PATCH, PATCH)), disk[box], False))
    return supplies


def balanced_loss(logits, truth, valid):
    insides = truth * valid
    outsides = (1 - truth) * valid
    positive = (-F.logsigmoid(logits) * insides).sum() / insides.sum().clamp_min(1)
    negative = (F.softplus(logits) * outsides).sum() / outsides.sum().clamp_min(1)
    dims = (1, 2, 3)
    has_target = insides.sum(dims) > 0
    probability = logits.sigmoid() * valid
    overlap = (2 * (probability * insides).sum(dims) + 1) / ((probability + insides).sum(dims) + 1)
    dice = ((1 - overlap) * has_target).sum() / has_target.sum().clamp_min(1)
    return .3 * positive + .3 * negative + .4 * dice


def train(data, output, meter, protocol, criterion):
    torch.manual_seed(SEED)
    model = UNet(width=8)
    model.encoders[0].layers[0] = nn.Conv2d(5, 8, 3, padding=1)
    model = model.cuda()
    optimizer = torch.optim.AdamW(model.parameters(), lr=.001)
    x, y, valid = data
    for epoch in range(EPOCHS):
        order = np.random.default_rng(SEED + epoch).permutation(len(x))
        means = []
        for offset in range(0, len(order), BATCH):
            idx = order[offset:offset + BATCH]
            frame, target, support = [torch.as_tensor(a[idx], device='cuda', dtype=torch.float32)
                                      for a in (x, y[:, None], valid[:, None])]
            optimizer.zero_grad(set_to_none=True)
            loss = criterion(model(frame), target, support)
            if not torch.isfinite(loss):
                raise ValueError('轮廓损失非有限')
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 4., error_if_nonfinite=True)
            optimizer.step()
            means.append(float(loss.detach()))
        model_state = {'model': model.state_dict(), 'optimizer': optimizer.state_dict(),
                       'epoch': epoch + 1, 'protocol_sha256': protocol}
        commit(output / 'last.pt', lambda stream: torch.save(model_state, stream))
        state = {'state': 'training_contour', 'epoch': epoch + 1, 'loss': float(np.mean(means)), 'resources': meter.sample()}
        atomic_json(output / 'status.json', state)
        print(json.dumps(state), flush=True)
        if (output / 'PAUSE').exists():
            return None
    return model.eval()


def anchored(mask, seed, permitted):
    regions, n = label(mask & permitted, structure=CONNECTIVITY)
    if not n:
        return np.zeros_like(seed)
    overlap = np.bincount(regions[seed], minlength=n + 1)
    overlap[0] = 0
    winner = int(overlap.argmax())
    if winner == 0 or overlap[winner] < min(3, int(seed.sum())):
        return np.zeros_like(seed)
    return regions == winner


def replay(raw, whole, axial, model):
    field, base, disk, radius, candidates, n = scene(raw, whole, axial)
    parts, descriptors = [], []
    fixed = disk & ~binary_dilation(base, structure=CONNECTIVITY, iterations=2)
    for i in range(1, n + 1):
        part = candidates == i
        box = window(part)
        parts.append(patches(box, field, part))
        descriptors.append((box, part[box]))
    masks = {name: base.copy() for name in ('raw', 'refine_050', 'refine_065', 'raw_limb', 'refine_limb_050')}
    masks['raw'] |= candidates > 0
    for box, seed in descriptors:
        point = np.argwhere(seed).mean(axis=0) + [s.start for s in box]
        if .75 <= np.hypot(*(point - 255.5)) / radius <= 1:
            masks['raw_limb'][box] |= seed & disk[box]
    for i in range(0, len(parts), BATCH):
        tensor = torch.as_tensor(np.stack(parts[i:i + BATCH]), device='cuda', dtype=torch.float32)
        out = model(tensor).sigmoid()[:, 0].cpu().numpy()
        if not np.isfinite(out).all():
            raise ValueError('轮廓模型输出异常')
        for probability, (box, seed) in zip(out, descriptors[i:i + BATCH], strict=True):
            point = np.argwhere(seed).mean(axis=0) + [s.start for s in box]
            middle = anchored(probability >= .5, seed, fixed[box])
            masks['refine_050'][box] |= middle
            masks['refine_065'][box] |= anchored(probability >= .65, seed, fixed[box])
            if .75 <= np.hypot(*(point - 255.5)) / radius <= 1:
                masks['refine_limb_050'][box] |= middle
    return {'baseline': base, **masks}, radius


def score(masks, references, radius):
    results = {}
    for arm, mask in masks.items():
        objects, count = label(mask, structure=CONNECTIVITY)
        candidates = [{'area': int(v)} for v in np.bincount(objects.ravel(), minlength=count + 1)[1:]]
        keep = [True] * count
        variants = []
        for reference in references:
            parts, n = label(reference, structure=CONNECTIVITY)
            limb = np.zeros(n + 1, dtype=bool)
            for i in range(1, n + 1):
                point = np.argwhere(parts == i).mean(axis=0)
                limb[i] = .75 <= np.hypot(*(point - 255.5)) / radius <= 1
            metrics_row = {**compare(mask, reference), **overlap_profile(mask, parts)}
            geometry = object_scores(objects, candidates, parts, keep)[1]
            _, matched = matching(geometry, keep)
            iou = np.asarray(geometry['iou']).reshape(count, n)
            no_overlap = iou.max(axis=0, initial=0.) == 0.
            variants.append({**metrics_row, 'limb_references': int(limb.sum()),
                             'limb_matched': int(limb[matched + 1].sum()),
                             'limb_completely_missed': int((no_overlap & limb[1:]).sum())})
        results[arm] = variants
    return results


def aggregate(rows):
    outcomes = {}
    for arm in rows[0]['arms']:
        comparison = [{'comparisons': row['arms'][arm]} for row in rows]
        flat = [v for row in comparison for v in row['comparisons']]
        outcomes[arm] = {**metrics(comparison), **{k: sum(v[k] for v in flat) for k in
                         ('zero_overlap_references', 'limb_references', 'limb_matched', 'limb_completely_missed')}}
    return outcomes


def run(source, release, early, development, later, prior, base, axial, output, loss_mode):
    output = output_path(output, DATASET)
    if output.exists() or digest(source) != DATA_SHA256 or digest(release) != LABEL_SHA256:
        raise ValueError('需要独立输出及登记的标注版本')
    guide = read(prior / 'summary.json')
    scheme = read(prior / 'protocol.json')
    w = [base / 'mixed_gong' / 'last.pt', axial / 'mask_and_spine' / 'last.pt']
    if (guide['test_accessed'] or guide['protocol_sha256'] != digest(prior / 'protocol.json')
            or scheme['weight_sha256'] != [digest(p) for p in w]):
        raise ValueError('候选权重或日期划分摘要不同')
    rows, annotations = inventory(source, release)
    earlier = {r['file']: r for r in read(early / 'inventory.json')}
    dev = {r['file']: r for r in read(development / 'inventory.json')}
    held, held_refs = later_pairs(source, release)
    verified = {r['file']: r for r in read(later / 'inventory.json')}
    fit_names = [name for station in 'BCLMTU' for name in scheme['samples']['fit'] if name[14] == station][::2]
    groups = {'fit': (early, [earlier[n] for n in fit_names], annotations),
              'development': (development, [dev[n] for n in scheme['samples']['development']], annotations),
              '2019_2020': (later, [verified[r['file']] for r in held], held_refs)}
    atomic_json(output / 'protocol.json', {'code_sha256': digest(__file__),
        'prior_sha256': digest(prior / 'summary.json'), 'sources_sha256': [digest(p / 'inventory.json') for p in (early, development, later)],
        'samples': {k: [r['file'] for r in rows] for k, (_, rows, _) in groups.items()}, 'epochs': EPOCHS,
        'patch': PATCH, 'loss_mode': loss_mode,
        'positive': 'each label variant IoU >= 0.08 to a single reference, crop includes at least 95% of target',
        'scope': 'GONG chronological development and 2019–2020 historical evaluation; no 2021/2022 or formal test'})
    cv2.setNumThreads(2)
    torch.set_num_threads(2)
    with exclusive(ROOT / '.mdva/training-gpu.lock'):
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cuda.matmul.allow_tf32 = False
        whole, spine = AuthorGraph('batch').cuda().eval(), architecture().cuda().eval()
        whole.load_state_dict(load_checkpoint(w[0])['model'])
        spine.load_state_dict(torch.load(w[1], weights_only=True, map_location='cpu')['model'])
        meter = Resources()
        pending, fitted = [], []
        def inputs(row, mapping):
            return [raster(mapping[a['id']].values() if isinstance(mapping[a['id']], dict) else mapping[a['id']])
                    for a in row['annotators']]
        with torch.inference_mode():
            folder, selected, source_masks = groups['fit']
            for row in selected:
                crops = gather(image_bytes(folder, row), inputs(row, source_masks), whole, spine)
                pending.extend(crops)
                fitted.append({'file': row['file'], 'positive': sum(c[-1] for c in crops), 'negative': sum(not c[-1] for c in crops)})
                if len(fitted) % 16 == 0:
                    atomic_json(output / 'status.json', {'state': 'preparing', 'completed': len(fitted), 'total': len(selected)})
        atomic_json(output / 'fitting.json', fitted)
        positive = sum(row['positive'] for row in fitted)
        if positive < 32:
            raise ValueError(f'配对候选训练正例不足 {positive}')
        x, y, v, _ = zip(*pending)
        criterion = balanced_loss if loss_mode == 'balanced' else weighted_loss
        model = train((np.stack(x), np.stack(y).astype(np.float16), np.stack(v)), output, meter,
                      digest(output / 'protocol.json'), criterion)
        if model is None:
            return {'state': 'paused'}
        results = {}
        with torch.inference_mode():
            for name in ('development', '2019_2020'):
                folder, selected, source_masks = groups[name]
                cases = []
                for row in selected:
                    masks, radius = replay(image_bytes(folder, row), whole, spine, model)
                    cases.append({'file': row['file'], 'arms': score(masks, inputs(row, source_masks), radius)})
                    if len(cases) % 8 == 0:
                        atomic_json(output / 'status.json', {'state': 'evaluating', 'group': name, 'completed': len(cases), 'total': len(selected)})
                atomic_json(output / f'{name}-cases.json', cases)
                results[name] = aggregate(cases)
        resource = meter.sample()
    summary = {'state': 'completed', 'created_utc': now(), 'fit_positive': positive, 'fit_negative': len(pending) - positive,
               'scores': results, 'resources': resource, 'protocol_sha256': digest(output / 'protocol.json'),
               'checkpoint_sha256': digest(output / 'last.pt'), 'cases_sha256': {k:digest(output / f'{k}-cases.json') for k in results},
               'loss_mode': loss_mode, 'deployed': False, 'test_accessed': False, 'year_2021_or_2022_accessed': False}
    atomic_json(output / 'summary.json', summary)
    atomic_json(output / 'status.json', {'state': 'completed'})
    return summary


if __name__ == '__main__':
    configure_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'release', 'early', 'development', 'later', 'prior', 'base', 'axial', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--loss-mode', choices=('weighted', 'balanced'), default='weighted')
    print(json.dumps(run(**vars(parser.parse_args())), ensure_ascii=False))
