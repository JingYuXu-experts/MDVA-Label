"""Train matched local-crop models with and without added GONG image detail."""

from __future__ import annotations

import argparse
from collections import Counter
from functools import lru_cache
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path

import numpy as np
from PIL import Image
import torch

from . import fl_magfilo_adaptation as paired
from .durable import atomic_json
from .fl_author_model import AuthorGraph
from .fl_ensemble_study import DATASET, ROOT
from .fl_gong_resolution import GRID, predict, reference_at_1024, reference_counts, resize, tiles_for, windows
from .fl_magfilo_audit import compare, raster
from .fl_magfilo_transfer import inventory, load_gray, metrics
from .journal import read
from .reviewed_train import load_checkpoint
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path

ARMS = ('coarse_training', 'detail_training')
TRAIN_IMAGES = 96
BASE_IDENTITY = paired.identity


def identity():
    return {**BASE_IDENTITY(), **{
        name: digest(Path(__file__).with_name(name))
        for name in ('fl_gong_patch_training.py', 'fl_gong_resolution.py')
    }}


def make_input(raw, detail):
    if not detail:
        return resize(load_gray(raw), GRID)
    with Image.open(BytesIO(raw)) as image:
        if image.size != (2048, 2048):
            raise ValueError('局部训练原始网格不符')
        return np.asarray(image.convert('L').resize((GRID, GRID), Image.Resampling.BILINEAR), dtype=np.float32) / 255


def image_bytes(folder, row):
    raw = (folder / 'jpeg' / row['file']).read_bytes()
    if sha256(raw).hexdigest() != row['image_sha256']:
        raise ValueError('局部训练图像身份不符')
    return raw


def training_patches(eligible, annotations):
    picks = []
    images = paired.stratified(eligible, TRAIN_IMAGES, 'captured_utc')
    for row in images:
        targets = [reference_at_1024(annotations[a['id']]) for a in row['annotators']]
        votes = np.mean(targets, axis=0, dtype=np.float32)
        strengths = np.array([votes[window].sum() for window in windows()])
        rank = np.argsort(strengths, kind='stable')
        if strengths[rank[-1]] > 0:
            picks.append({**row, 'crop': int(rank[-1])})
            picks.append({**row, 'crop': int(rank[0])})
        else:
            picks.extend({**row, 'crop': crop} for crop in (0, 4))
    return picks


def crop_dataset(picks, early, annotations, detail):
    parent = paired.dataset_for(picks, early, annotations)

    @lru_cache(maxsize=8)
    def image_and_target(name, checksum, ids):
        raw = image_bytes(early, {'file': name, 'image_sha256': checksum})
        targets = [reference_at_1024(annotations[key]) for key in ids]
        target = np.mean(targets, axis=0, dtype=np.float32)
        return make_input(raw, detail), target

    class Patches(parent):
        def __getitem__(self, index):
            row = self.rows[index]
            if 'crop' not in row:
                return super().__getitem__(index)
            ys, xs = windows()[row['crop']]
            image, target = image_and_target(row['file'], row['image_sha256'], tuple(v['id'] for v in row['annotators']))
            return {'image': torch.from_numpy(image[ys, xs].copy())[None],
                    'semantic_mask': torch.from_numpy(target[ys, xs].copy())[None]}

    return Patches


def review(model, weights, early_model, rows, annotation_table, later, output):
    k = 'whole'
    all_names = (k, *ARMS)
    per_case = []
    with exclusive(ROOT / '.mdva/training-gpu.lock'), torch.inference_mode():
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cuda.matmul.allow_tf32 = False
        usage = Resources()
        early_model.cuda().eval()
        for m in model.values():
            m.cuda().eval()
        for index, row in enumerate(rows):
            raw = image_bytes(later, row)
            whole = predict(early_model, load_gray(raw)[None])[0]
            patched = {k: resize(whole, GRID)}
            for arm in ARMS:
                local = tiles_for(model[arm], make_input(raw, arm == 'detail_training'))
                patched[arm] = .65 * patched[k] + .35 * local
            masks = {k: whole >= .5, **{arm: resize(patched[arm], 512) >= .5 for arm in ARMS}}
            reference_list = [annotation_table[a['id']] for a in row['annotators']]
            refs_512 = [raster(ref) for ref in reference_list]
            refs_1024 = [reference_at_1024(ref) for ref in reference_list]
            per_case.append({
                'file': row['file'], 'time_utc': row['captured_utc'],
                'at_512': {arm:[compare(masks[arm], ref) for ref in refs_512] for arm in all_names},
                'at_1024': {arm:[reference_counts(patched[arm] >= .5, ref) for ref in refs_1024] for arm in all_names},
            })
            if index % 16 == 0:
                atomic_json(output / 'progress.json', {'state': 'evaluating', 'images': index + 1, 'total': len(rows)})
                usage.sample()
        resources = usage.sample()
    result = {}
    for arm in all_names:
        finer = Counter()
        for row in per_case:
            for result_item in row['at_1024'][arm]:
                finer.update(result_item)
        result[arm] = {'fixed_grid': metrics([{'comparisons': row['at_512'][arm]} for row in per_case]),
                       'detail_grid': dict(finer), 'checkpoint_sha256': weights[arm]}
    atomic_json(output / 'images.json', per_case)
    return result, resources


def run(source, release, early, later, anchor, resolution, output):
    torch.set_num_threads(2)
    output = output_path(output, DATASET)
    if (output / 'summary.json').exists() or any(output.is_relative_to(p.resolve()) for p in (early, later, anchor, resolution)):
        raise ValueError('局部训练必须使用独立的新结果目录')
    start = read(anchor / 'summary.json')
    audit = read(resolution / 'summary.json')
    main = anchor / 'protocol.json'
    prior = read(main)
    weights = anchor / 'mixed_gong' / 'last.pt'
    checksum = digest(weights)
    if (any(r['state'] != 'completed' or r['test_accessed'] or r['calibration_accessed'] for r in (start, audit))
            or audit['external_2021_accessed'] or start['protocol_sha256'] != digest(main)
            or audit['protocol_sha256'] != digest(resolution / 'protocol.json')
            or checksum != start['results']['mixed_gong']['weights_sha256']
            or prior['later_inventory_sha256'] != digest(later / 'inventory.json')):
        raise ValueError('局部训练的锚点或已登记的分辨率诊断不符')
    registered, annotations = inventory(source, release)
    by_name = {row['file']: row for row in registered}
    train = paired.source_receipt(early, 'train_available', by_name)
    dev = paired.source_receipt(later, 'transfer_dev', by_name)
    never_adapted = [r for r in train if r['file'] not in prior['gong_training_images']]
    picks = training_patches(never_adapted, annotations)
    warm = load_checkpoint(weights)['model']
    protocol = {
        'identity': identity(), 'warm_start_sha256': checksum, 'early_images_sha256': digest(early / 'inventory.json'),
        'later_images_sha256': digest(later / 'inventory.json'), 'prior_summary_sha256': digest(resolution / 'summary.json'),
        'samples': [{'file': r['file'], 'crop': r['crop']} for r in picks], 'epochs': paired.EPOCHS,
        'fixed_blend': [.65, .35], 'threshold': .5, 'test_accessed': False, 'calibration_accessed': False,
        'external_2021_accessed': False,
    }
    marker = output / 'protocol.json'
    if marker.exists() and read(marker) != protocol:
        raise ValueError('局部训练方案已经改变')
    atomic_json(marker, protocol)
    base_identity = paired.identity
    configs = {}
    try:
        paired.identity = identity
        for arm in ARMS:
            config = paired.train(output, arm, crop_dataset(picks, early, annotations, arm == 'detail_training'), warm, protocol)
            if config is None:
                return {'state': 'paused'}
            configs[arm] = config
    finally:
        paired.identity = base_identity
    models, checksums = {}, {'whole': checksum}
    for arm in ARMS:
        last = output / arm / 'last.pt'
        checkpoint = load_checkpoint(last)
        if checkpoint['epoch'] != paired.EPOCHS or checkpoint['identity']['config'] != configs[arm]:
            raise ValueError('局部权重与预算不一致')
        models[arm] = AuthorGraph('batch')
        models[arm].load_state_dict(checkpoint['model'])
        checksums[arm] = digest(last)
    base = AuthorGraph('batch')
    base.load_state_dict(warm)
    result, use = review(models, checksums, base, dev, annotations, later, output)
    if result['whole']['fixed_grid'] != audit['fixed_grid']['whole']:
        raise ValueError('局部训练后的全图锚点无法复算')
    detail = result['detail_training']['fixed_grid']
    whole = result['whole']['fixed_grid']
    earlier = audit['fixed_grid']['detail_outline']
    gates = {
        'recall_gain_vs_whole_at_least_3pp': detail['recall_iou_050'] >= whole['recall_iou_050'] + .03,
        'precision_loss_vs_whole_at_most_1pp': detail['precision_iou_050'] >= whole['precision_iou_050'] - .01,
        'strict_match_gain_vs_untuned_outline': detail['matched_iou_050'] > earlier['matched_iou_050'],
    }
    report = {
        'state': 'completed', 'created_utc': now(), 'result': result, 'checks': gates, 'passed': all(gates.values()),
        'protocol_sha256': digest(marker), 'images_sha256': digest(output / 'images.json'), 'resources': use,
        'test_accessed': False, 'calibration_accessed': False, 'external_2021_accessed': False, 'deployed': False,
    }
    atomic_json(output / 'summary.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'release', 'early', 'later', 'anchor', 'resolution', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    configure_environment()
    print(json.dumps(run(**vars(parser.parse_args())), ensure_ascii=False))


if __name__ == '__main__':
    main()
