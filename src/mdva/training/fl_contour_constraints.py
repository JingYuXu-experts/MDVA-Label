"""Replay one frozen H-alpha contour checkpoint with spatial constraints."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
from scipy.ndimage import binary_dilation, distance_transform_edt, label, maximum_filter, minimum_filter
import torch
from torch import nn

from .durable import atomic_json
from .fl_author_model import AuthorGraph
from .fl_contour_refiner import BATCH, aggregate, anchored, patches, scene, score, window
from .fl_ensemble_study import CONNECTIVITY, DATASET, ROOT
from .fl_gong_fused_verify import later_pairs
from .fl_gong_patch_training import image_bytes
from .fl_magfilo_audit import DATA_SHA256, raster
from .fl_magfilo_transfer import LABEL_SHA256, inventory
from .fl_spine_context_study import architecture
from .journal import read
from .models import UNet
from .reviewed_train import load_checkpoint
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


MAX_RADIUS = 4
DARK_MINIMUM = .1
ARMS = ('baseline', 'raw', 'refine_050', 'separate', 'bounded', 'bounded_dark')


def ownership(objects):
    """Provide an eight-neighbor gap so two separately expanded seeds stay separate."""
    distance, indices = distance_transform_edt(objects == 0, return_indices=True)
    owner = objects[tuple(indices)]
    unique_neighborhood = maximum_filter(owner, size=3) == minimum_filter(owner, size=3)
    return distance, owner, unique_neighborhood | (objects != 0)


def limited(probable, seed, valid, distance):
    size = int(seed.sum())
    maximum = max(2 * size, size + 8)
    for limit in (MAX_RADIUS, 2, 1, 0):
        result = anchored(probable, seed, valid & (distance <= limit))
        if result.sum() <= maximum:
            return result
    raise RuntimeError('轮廓外扩面积超过上限')


def replay(raw, whole, spine, model):
    field, base, disk, radius, seeds, count = scene(raw, whole, spine)
    prediction = {name: base.copy() for name in ARMS}
    prediction['raw'] |= seeds != 0
    if not count:
        return prediction, radius, seeds
    distance, owner, interior = ownership(seeds)
    allowed = disk & ~binary_dilation(base, structure=CONNECTIVITY, iterations=2)
    descriptors = []
    for identity in range(1, count + 1):
        part = seeds == identity
        box = window(part)
        descriptors.append((identity, box, part[box], patches(box, field, part)))
    for start in range(0, len(descriptors), BATCH):
        batch = descriptors[start:start + BATCH]
        x = torch.as_tensor(np.stack([d[3] for d in batch]), device='cuda', dtype=torch.float32)
        out = model(x).sigmoid()[:, 0].cpu().numpy()
        if not np.isfinite(out).all():
            raise ValueError('轮廓模型输出非有限值')
        for (identity, box, seed, _), probability in zip(batch, out, strict=True):
            above = probability >= .5
            valid = allowed[box]
            separate = valid & interior[box] & (owner[box] == identity)
            dark = separate & (seed | (field[1][box] >= DARK_MINIMUM))
            prediction['refine_050'][box] |= anchored(above, seed, valid)
            prediction['separate'][box] |= anchored(above, seed, separate)
            prediction['bounded'][box] |= limited(above, seed, separate, distance[box])
            prediction['bounded_dark'][box] |= limited(above, seed, dark, distance[box])
    return prediction, radius, seeds


def geometry(outputs, seeds):
    values = {}
    total = int(seeds.max())
    for arm, mask in outputs.items():
        linked, count = label(mask & ~outputs['baseline'], structure=CONNECTIVITY)
        overlaps = np.bincount((linked * (total + 1) + seeds).ravel(),
                               minlength=(count + 1) * (total + 1)).reshape(count + 1, total + 1)
        cross = (overlaps[1:, 1:] > 0).sum(axis=1) >= 2
        values[arm] = {'added_pixels': int(overlaps[1:].sum()), 'added_components': int(count),
                       'merged_components': int(cross.sum()),
                       'pixels_in_merged_components': int(overlaps[1:][cross].sum())}
    return values


def qualification(result):
    base = result['baseline']
    values = {}
    for name in ('separate', 'bounded', 'bounded_dark'):
        proposed = result[name]
        gain = proposed['matched_iou_050'] - base['matched_iou_050']
        extra = proposed['predicted_regions_total_across_variants'] - base['predicted_regions_total_across_variants'] - gain
        values[name] = {'gain': gain, 'extra_unmatched': extra, 'passed': gain >= 3 and extra <= gain}
    return values


def run(source, release, development, later, base, spine, balanced, output):
    output = output_path(output, DATASET)
    if output.exists() or digest(source) != DATA_SHA256 or digest(release) != LABEL_SHA256:
        raise ValueError('约束复算需要新目录和冻结公开标注')
    summary = read(balanced / 'summary.json')
    plan = read(balanced / 'protocol.json')
    refiner_weight = balanced / 'last.pt'
    g = [base / 'mixed_gong' / 'last.pt', spine / 'mask_and_spine' / 'last.pt']
    if (summary['test_accessed'] or summary['protocol_sha256'] != digest(balanced / 'protocol.json')
            or summary['checkpoint_sha256'] != digest(refiner_weight)
            or digest(g[0]) != read(base / 'summary.json')['results']['mixed_gong']['weights_sha256']):
        raise ValueError('轮廓复算权重身份不符')
    dates = {kind: plan['samples'][kind] for kind in ('development', '2019_2020')}
    _, original = inventory(source, release)
    _, later_refs = later_pairs(source, release)
    phases = {'development': (development, original), '2019_2020': (later, later_refs)}
    atomic_json(output / 'protocol.json', {'code_sha256': digest(__file__), 'weight_sha256': [digest(p) for p in (*g, refiner_weight)],
        'anchor_summary_sha256': digest(balanced / 'summary.json'), 'samples': dates, 'arms': ARMS,
        'threshold': .5, 'max_distance_px': MAX_RADIUS, 'area_limit': 'max(2 * seed_pixels, seed_pixels + 8)',
        'dark_minimum': DARK_MINIMUM, 'gate_on_2017_only': 'strict match gain >=3 and extra unmatched <= gain',
        'test_accessed': False, 'fresh_years_accessed': False})
    cv2.setNumThreads(2)
    torch.set_num_threads(2)
    whole, axial, model = AuthorGraph('batch'), architecture(), UNet(width=8)
    model.encoders[0].layers[0] = nn.Conv2d(5, 8, 3, padding=1)
    whole.load_state_dict(load_checkpoint(g[0])['model'])
    for network, weights in ((axial, g[1]), (model, refiner_weight)):
        network.load_state_dict(torch.load(weights, map_location='cpu', weights_only=True)['model'])
    values, totals = {}, {}
    with exclusive(ROOT / '.mdva/training-gpu.lock'), torch.inference_mode():
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        for network in (whole, axial, model):
            network.cuda().eval()
        meter = Resources()
        for cohort, (folder, reference) in phases.items():
            rows = {r['file']: r for r in read(folder / 'inventory.json')}
            cases = []
            for name in dates[cohort]:
                row = rows[name]
                masks, radius, candidates = replay(image_bytes(folder, row), whole, axial, model)
                refs = [reference[a['id']] for a in row['annotators']]
                refs = [raster(r.values() if isinstance(r, dict) else r) for r in refs]
                cases.append({'file': name, 'arms': score(masks, refs, radius), 'geometry': geometry(masks, candidates)})
                if len(cases) % 8 == 0:
                    atomic_json(output / 'status.json', {'state': 'evaluating', 'cohort': cohort, 'done': len(cases),
                                                         'total': len(dates[cohort]), 'resources': meter.sample()})
            atomic_json(output / f'{cohort}-cases.json', cases)
            values[cohort] = aggregate(cases)
            for arm in ('baseline', 'raw', 'refine_050'):
                if values[cohort][arm] != summary['scores'][cohort][arm]:
                    raise ValueError(f'{cohort} 的原始 {arm} 对照未复现')
            totals[cohort] = {
                arm: {key: sum(row['geometry'][arm][key] for row in cases) for key in cases[0]['geometry'][arm]}
                for arm in ARMS
            }
        resources = meter.sample()
    result = {'state': 'completed', 'created_utc': now(), 'scores': values, 'geometry': totals,
              'development_gate': qualification(values['development']), 'resources': resources,
              'cases_sha256': {k: digest(output / f'{k}-cases.json') for k in values},
              'protocol_sha256': digest(output / 'protocol.json'), 'test_accessed': False, 'deployed': False}
    atomic_json(output / 'summary.json', result)
    atomic_json(output / 'status.json', {'state': 'completed', 'summary_sha256': digest(output / 'summary.json')})
    return result


if __name__ == '__main__':
    configure_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('source', 'release', 'development', 'later', 'base', 'spine', 'balanced', 'output'):
        parser.add_argument('--' + key, type=Path, required=True)
    result = run(**vars(parser.parse_args()))
    print(json.dumps({'state': result['state'], 'development_gate': result['development_gate']}, ensure_ascii=False))
