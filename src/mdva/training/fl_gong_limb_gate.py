"""Quantify how much frozen filament error comes from pixels outside the solar limb."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch

from .durable import atomic_json
from .fl_author_model import AuthorGraph
from .fl_ensemble_study import DATASET, PILOT, ROOT
from .fl_magfilo_adaptation import source_receipt
from .fl_magfilo_audit import compare, raster
from .fl_magfilo_transfer import inventory, load_gray, metrics
from .journal import read
from .reviewed_train import load_checkpoint
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path

SIDE = 512
Y, X = np.ogrid[:SIDE, :SIDE]
DISTANCE = np.hypot(Y - (SIDE - 1) / 2, X - (SIDE - 1) / 2)
RADII = np.arange(209, 241)
RING = np.rint(DISTANCE).astype(np.int16)
RING_PIXELS = [np.flatnonzero(RING.ravel() == radius) for radius in RADII]


def visible_disk(gray):
    if gray.shape != (SIDE, SIDE) or not np.isfinite(gray).all():
        return None, None
    profile = np.array([np.median(gray.ravel()[pixels]) for pixels in RING_PIXELS])
    contrasts = profile[:-2] - profile[2:]
    strongest = int(contrasts.argmax())
    if contrasts[strongest] < .08:
        return None, None
    radius = int(RADII[strongest + 1])
    return DISTANCE <= radius + 2, radius


def run(source, release, development, output):
    output = output_path(output, DATASET)
    if output.exists() or output.is_relative_to(development.resolve()):
        raise ValueError('日面裁切复核需要独立新目录')
    rows, annotations = inventory(source, release)
    selected = source_receipt(development, 'transfer_dev', {r['file']: r for r in rows})
    original = read(development / 'summary.json')['transfer_dev']
    weights = PILOT / 'checkpoint.pt'
    frozen = read(PILOT / 'summary.json')
    if frozen['checkpoint_sha256'] != digest(weights) or frozen['test_accessed']:
        raise ValueError('暗条基线身份变化')
    descriptor = {
        'checkpoint_sha256': digest(weights), 'development_receipt_sha256': digest(development / 'inventory.json'),
        'code_sha256': digest(__file__), 'inference_threshold': .5, 'radial_drop_min': .08,
        'limb_guard_px': 2, 'input_grid': SIDE,
        'decision': 'Require precision gain of at least 3pp, recall loss at most 1pp, and less than 0.1% reference pixels outside.',
        'test_accessed': False,
    }
    atomic_json(output / 'protocol.json', descriptor)
    model = AuthorGraph('batch')
    model.load_state_dict(load_checkpoint(weights)['model'])
    results = {'raw': [], 'disk': []}
    checks = []
    with exclusive(ROOT / '.mdva/training-gpu.lock'), torch.inference_mode():
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cuda.matmul.allow_tf32 = False
        model.cuda().eval()
        resource = Resources()
        for i, row in enumerate(selected):
            gray = load_gray((development / 'jpeg' / row['file']).read_bytes())
            disk, radius = visible_disk(gray)
            scores = model(torch.from_numpy(gray)[None, None].cuda()).sigmoid()[0, 0].cpu().numpy()
            if not np.isfinite(scores).all():
                raise ValueError('日面裁切复核预测出现非有限值')
            proposal = scores >= .5
            clipped = proposal if disk is None else proposal & disk
            refs = [raster(annotations[a['id']]) for a in row['annotators']]
            for name, mask in (('raw', proposal), ('disk', clipped)):
                results[name].append({'file': row['file'], 'comparisons': [compare(mask, ref) for ref in refs]})
            checks.append({
                'file': row['file'], 'radius': radius, 'prediction_pixels_removed': int((proposal & ~clipped).sum()),
                'reference_pixels': sum(int(ref.sum()) for ref in refs),
                'reference_pixels_outside': sum(int((ref & ~disk).sum()) for ref in refs) if disk is not None else 0,
            })
            if i % 24 == 0:
                resource.sample()
        resources = resource.sample()
    baseline, candidate = (metrics(results[key]) for key in ('raw', 'disk'))
    if baseline != original:
        raise ValueError('日面裁切的基线无法精确复算')
    pooled = {field: sum(c[field] for c in checks)
              for field in ('reference_pixels', 'reference_pixels_outside', 'prediction_pixels_removed')}
    gate = (candidate['precision_iou_050'] >= baseline['precision_iou_050'] + .03
            and candidate['recall_iou_050'] >= baseline['recall_iou_050'] - .01
            and pooled['reference_pixels_outside'] / pooled['reference_pixels'] < .001)
    atomic_json(output / 'images.json', {'scores': results, 'geometry': checks})
    value = {
        'state': 'completed', 'recorded_utc': now(), 'screening_gate_passed': gate,
        'raw': baseline, 'disk': candidate, 'limb_accepted': sum(c['radius'] is not None for c in checks),
        'pixels': pooled, 'images_sha256': digest(output / 'images.json'),
        'protocol_sha256': digest(output / 'protocol.json'), 'resources': resources,
        'test_accessed': False, 'deployed': False,
    }
    atomic_json(output / 'summary.json', value)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'release', 'development', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    configure_environment()
    print(json.dumps(run(**vars(parser.parse_args())), ensure_ascii=False))


if __name__ == '__main__':
    main()
