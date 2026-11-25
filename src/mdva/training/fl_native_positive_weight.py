"""Measure whether mild positive weighting rescues FL outputs without adding noise."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path

import cv2
import numpy as np
from safetensors.torch import load_file
from scipy.ndimage import label
import torch
import torch.nn.functional as F

from .durable import atomic_json
from .fl_author_model import AuthorGraph
from .fl_ensemble_study import CONNECTIVITY, DATASET, ROOT, overlap_profile
from .fl_gong_patch_training import image_bytes
from .fl_gong_resolution import predict, resize
from .fl_magfilo_adaptation import source_receipt, stratified
from .fl_magfilo_audit import compare, raster
from .fl_magfilo_transfer import inventory, load_gray, metrics
from .fl_native_local_training import BATCH, EPOCHS, fit_data, local_probabilities, model_for, train, view
from .journal import read
from .reviewed_train import load_checkpoint
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


ARM = 'gray_plus_contrast'
POSITIVE_WEIGHT = 4.


def weighted_loss(logits, truth, valid):
    axes = (1, 2, 3)
    count = valid.sum(axes).clamp_min(1)
    positive_weight = logits.new_tensor(POSITIVE_WEIGHT)
    terms = F.binary_cross_entropy_with_logits(logits, truth, pos_weight=positive_weight, reduction='none')
    bce = ((terms * valid).sum(axes) / count).mean()
    p, y = logits.sigmoid() * valid, truth * valid
    dice = 1 - ((2 * (p * y).sum(axes) + 1) / ((p + y).sum(axes) + 1)).mean()
    return .5 * (bce + dice)


def scores(whole, before, current, images, refs, folder, usage):
    rows = []
    for entry in images:
        raw = image_bytes(folder, entry)
        local, disk = view(raw)
        prior = predict(whole, load_gray(raw)[None])[0]
        original = resize(local_probabilities(before, local, ARM, disk), 512)
        weighted = resize(local_probabilities(current, local, ARM, disk), 512)
        variants = [raster(refs[a['id']]) for a in entry['annotators']]
        cases = {'whole': prior, 'unweighted_local': original, 'weighted_local': weighted,
                 'whole_plus_unweighted': .65 * prior + .35 * original, 'whole_plus_weighted': .65 * prior + .35 * weighted}
        result = {'file': entry['file'], 'arms': {}}
        for name, probability in cases.items():
            mask = probability >= .5
            result['arms'][name] = [{**compare(mask, target), **overlap_profile(mask, label(target, structure=CONNECTIVITY)[0])}
                                    for target in variants]
        rows.append(result)
        usage.sample()
    return rows


def run(source, release, early, development, baseline, anchor, output):
    output = output_path(output, DATASET)
    if (output / 'summary.json').exists():
        raise ValueError('已有完整正例权重实验')
    prior, base_plan = (read(baseline / name) for name in ('summary.json', 'protocol.json'))
    checkpoint = anchor / 'mixed_gong' / 'last.pt'
    if (prior['state'] != 'completed' or prior['protocol_sha256'] != digest(baseline / 'protocol.json')
            or base_plan['anchor_sha256'] != digest(checkpoint) or base_plan['early_sha256'] != digest(early / 'inventory.json')):
        raise ValueError('原训练输入与全图模型身份不同')
    entries, annotations = inventory(source, release)
    registered = {r['file']:r for r in entries}
    available = source_receipt(early, 'train_available', registered)
    dev = [r for r in source_receipt(development, 'transfer_dev', registered) if r['file'].startswith('2018')]
    groups = defaultdict(list)
    for row in available:
        groups[row['file'][14:16]].append(row)
    chosen = [item for key in sorted(groups) for item in stratified(groups[key], 12, 'captured_utc')]
    cv2.setNumThreads(2)
    torch.set_num_threads(2)
    x, y, v, registered_crops, excluded = fit_data(chosen, early, annotations)
    if registered_crops != base_plan['patches'] or excluded != base_plan['fit_exclusions'] or len(dev) < 12:
        raise ValueError('与冻结对照条件不一致')
    marker = output / 'protocol.json'
    weights = baseline / ARM / 'research.safetensors'
    protocol = {'code_sha256': digest(__file__), 'trainer_sha256': digest(Path(__file__).with_name('fl_native_local_training.py')),
                'base_sha256': digest(baseline / 'summary.json'), 'control_weights_sha256': digest(weights),
                'fitting_and_review_years': '2011–2015 fit, 2018 development review only', 'evaluation_files': [r['file'] for r in dev],
                'positive_weight': POSITIVE_WEIGHT, 'epochs': EPOCHS, 'batch': BATCH, 'threshold': .5,
                'test_accessed': False, 'calibration_accessed': False, 'year_2022_accessed': False}
    if marker.exists() and read(marker) != protocol:
        raise ValueError('无法恢复已改变的正例权重对照')
    if not marker.exists():
        atomic_json(marker, protocol)
    with exclusive(ROOT / '.mdva/training-gpu.lock'):
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cuda.matmul.allow_tf32 = False
        usage = Resources()
        model = train((x, y, v), ARM, output, digest(marker), usage, criterion=weighted_loss)
        if model is None:
            return {'state': 'paused'}
        before = model_for().cuda().eval()
        before.load_state_dict(load_file(weights))
        whole = AuthorGraph('batch').cuda().eval()
        whole.load_state_dict(load_checkpoint(checkpoint)['model'])
        with torch.inference_mode():
            rows = scores(whole, before, model, dev, annotations, development, usage)
        resources = usage.sample()
    summaries = {}
    for arm in rows[0]['arms']:
        selected = [{'comparisons': row['arms'][arm]} for row in rows]
        summaries[arm] = {**metrics(selected), 'zero_overlap_references': sum(o['zero_overlap_references'] for p in selected for o in p['comparisons'])}
    atomic_json(output / 'cases.json', rows)
    report = {'state': 'completed', 'recorded_utc': now(), 'fit_patches': len(x), 'development_images': len(dev),
              'metrics': summaries, 'resources': resources, 'protocol_sha256': digest(marker),
              'cases_sha256': digest(output / 'cases.json'), 'test_accessed': False, 'year_2022_accessed': False}
    atomic_json(output / 'summary.json', report)
    atomic_json(output / 'status.json', {'state': 'completed', 'summary_sha256': digest(output / 'summary.json')})
    return report


if __name__ == '__main__':
    configure_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('source', 'release', 'early', 'development', 'baseline', 'anchor', 'output'):
        parser.add_argument('--' + key, type=Path, required=True)
    result = run(**vars(parser.parse_args()))
    print(json.dumps(result, ensure_ascii=False))
