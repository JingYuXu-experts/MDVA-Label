"""Adapt to GONG while using the original model's local predictions as a training target."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch
from torch.nn import functional as F

from . import fl_magfilo_adaptation as paired
from .datasets import SegmentationDataset
from .durable import atomic_json, commit
from .fl_author_model import AuthorGraph, loss_value
from .fl_ensemble_study import DATASET, PILOT, ROOT
from .fl_magfilo_transfer import inventory
from .fl_magfilo_weight_mix import meets_gate
from .journal import read
from .reviewed_train import load_checkpoint
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


WEIGHT = .5
SUPPORT = .15
ARM = 'gong_local_replay'
BASE_IDENTITY = paired.identity


def identity():
    return {**BASE_IDENTITY(), **{
        name: digest(Path(__file__).with_name(name))
        for name in ('fl_magfilo_local_replay.py', 'fl_magfilo_weight_mix.py')
    }}


def preservation_loss(logits, previous):
    soft = previous.float().clamp(1e-5, 1 - 1e-5)
    current = F.binary_cross_entropy_with_logits(logits.float(), soft, reduction='none')
    negative_entropy = soft * soft.log() + (1 - soft) * torch.log1p(-soft)
    kl = current + negative_entropy
    # Prevent the large quiet background from overwhelming the old region evidence.
    supported = soft >= SUPPORT
    dimensions = (1, 2, 3)
    region = (kl * supported).sum(dimensions) / supported.sum(dimensions).clamp_min(1)
    background = (kl * ~supported).sum(dimensions) / (~supported).sum(dimensions).clamp_min(1)
    return .5 * (region + background).mean()


def replay_loss(logits, target, positive_weight):
    supervised = loss_value(logits, target[:, :1], positive_weight)
    if target.shape[1] == 1:
        return supervised
    if target.shape[1] != 2:
        raise ValueError('旧域监督必须包含人工掩膜及教师概率两个通道')
    return supervised + WEIGHT * preservation_loss(logits, target[:, 1:])


def teacher_cache(training, chosen, weights, output):
    indices = {r['uri']: i for i, r in enumerate(training.rows)}
    manifest = output / 'teacher.json'
    if manifest.is_file():
        record = read(manifest)
        if list(record) != [r['uri'] for r in chosen]:
            raise ValueError('本地教师缓存的输入次序不符')
        for item in record.values():
            if digest(output / item['path']) != item['sha256']:
                raise ValueError('本地教师预测缓存已改变')
        return record
    model = AuthorGraph('batch')
    model.load_state_dict(weights)
    record = {}
    with exclusive(ROOT / '.mdva/training-gpu.lock'), torch.inference_mode():
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        model.cuda().eval()
        resources = Resources()
        for index, row in enumerate(chosen):
            image = training[indices[row['uri']]]['image'][None].cuda()
            probabilities = model(image).float().sigmoid()[0].cpu().numpy()
            if not np.isfinite(probabilities).all():
                raise ValueError('本地教师输出非有限')
            path = output / 'teacher' / f'{index:03}.npy'
            quantized = probabilities.astype(np.float16)
            commit(path, lambda stream: np.save(stream, quantized, allow_pickle=False))
            record[row['uri']] = {'path': path.relative_to(output).as_posix(), 'sha256': digest(path)}
            if index % 32 == 0:
                resources.sample()
    atomic_json(manifest, record)
    return record


def with_replay(parent, cache, output):
    class WithReplay(parent):
        def __getitem__(self, index):
            item = super().__getitem__(index)
            cached = cache.get(self.rows[index].get('uri')) if self.split == 'train' else None
            if cached is not None:
                path = output / cached['path']
                probabilities = torch.from_numpy(np.load(path, allow_pickle=False).astype(np.float32))
                item['semantic_mask'] = torch.cat((item['semantic_mask'], probabilities))
            return item

        def __init__(self, path, category, split, size):
            super().__init__(path, category, split, size)
            self.split = split

    return WithReplay


def run(source, release, early, later, adapted, weight_mix, output):
    output = output_path(output, DATASET)
    if (output / 'summary.json').exists() or any(
            output.is_relative_to(p.resolve()) for p in (early, later, adapted, weight_mix)):
        raise ValueError('教师监督必须使用独立新结果目录')
    earlier = read(adapted / 'protocol.json')
    prior = read(adapted / 'summary.json')
    mix = read(weight_mix / 'summary.json')
    previous_mix_plan = read(weight_mix / 'protocol.json')
    if (prior['protocol_sha256'] != digest(adapted / 'protocol.json')
            or mix['protocol_sha256'] != digest(weight_mix / 'protocol.json')
            or previous_mix_plan['adapted_weights_sha256'] != prior['results']['mixed_gong']['weights_sha256']
            or any(r['state'] != 'completed' or r['test_accessed'] for r in (prior, mix))
            or earlier['early_inventory_sha256'] != digest(early / 'inventory.json')
            or earlier['later_inventory_sha256'] != digest(later / 'inventory.json')):
        raise ValueError('旧域监督的配对对照或输入不符')
    registered, annotations = inventory(source, release)
    index = {r['file']: r for r in registered}
    gong = {r['file']: r for r in paired.source_receipt(early, 'train_available', index)}
    external = paired.source_receipt(later, 'transfer_dev', index)
    training = SegmentationDataset(DATASET, 'FL', 'train', 512)
    local_index = {r['uri']: r for r in training.rows}
    chosen = [local_index[uri] for uri in earlier['local_training_uris'][::2]]
    added = [gong[name] for name in earlier['gong_training_images']]
    checkpoint = PILOT / 'checkpoint.pt'
    if digest(checkpoint) != earlier['warm_start_sha256']:
        raise ValueError('旧域教师身份改变')
    weights = load_checkpoint(checkpoint)['model']
    protocol = {
        'identity': identity(), 'local_training_uris': [r['uri'] for r in chosen],
        'gong_training_images': [r['file'] for r in added], 'warm_start_sha256': digest(checkpoint),
        'original_adaptation_protocol_sha256': digest(adapted / 'protocol.json'),
        'reference_summary_sha256': digest(weight_mix / 'summary.json'), 'threshold': .5,
        'epochs': paired.EPOCHS, 'teacher_loss_weight': WEIGHT, 'teacher_foreground_min': SUPPORT,
        'change': 'Add balanced local Bernoulli distillation against original predictions. Identical samples and budget.',
        'test_accessed': False, 'calibration_accessed': False,
    }
    marker = output / 'protocol.json'
    if marker.is_file() and read(marker) != protocol:
        raise ValueError('旧域监督协议改变，不能复用运行')
    atomic_json(marker, protocol)
    cache = teacher_cache(training, chosen, weights, output)
    data_class = with_replay(paired.dataset_for(chosen + added, early, annotations), cache, output)
    before_hooks = paired.identity, paired.loss_value
    try:
        paired.identity = identity
        paired.loss_value = replay_loss
        config = paired.train(output, ARM, data_class, weights, protocol)
        if config is None:
            return {'state': 'paused'}
        dev = SegmentationDataset(DATASET, 'FL', 'validation', 512)
        results, use = paired.evaluate(output, {ARM: config}, dev, external, later, annotations)
    finally:
        paired.identity, paired.loss_value = before_hooks
    result_path = output / ARM / 'development.json'
    case_data = read(result_path)
    local = results[ARM]['local']
    stations = {}
    for case, row in zip(case_data['local'], dev.rows, strict=True):
        if case['uri'] != row['uri']:
            raise ValueError('本地旧域样本顺序改变')
        if case['pixel']['dice'] is not None:
            stations.setdefault(row['instrument'], []).append(case['pixel']['dice'])
    local['station_dice'] = {name: float(np.mean(values)) for name, values in stations.items()}
    checks = meets_gate(results[ARM], mix['results']['0.0'], mix['results']['1.0'])
    receipt = {
        'state': 'completed', 'recorded_utc': now(), 'result': results[ARM], 'checks': checks,
        'passed': all(checks.values()), 'protocol_sha256': digest(marker),
        'teacher_manifest_sha256': digest(output / 'teacher.json'), 'development_sha256': digest(result_path),
        'resources': use, 'test_accessed': False, 'calibration_accessed': False, 'deployed': False,
    }
    atomic_json(output / 'summary.json', receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'release', 'early', 'later', 'adapted', 'weight_mix', 'output'):
        parser.add_argument('--' + name.replace('_', '-'), required=True, type=Path)
    configure_environment()
    print(json.dumps(run(**vars(parser.parse_args())), ensure_ascii=False))


if __name__ == '__main__':
    main()
