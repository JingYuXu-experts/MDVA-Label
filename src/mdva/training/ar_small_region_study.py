"""Fixed-budget test of size-aware foreground BCE for solar active regions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import torch
from torch.nn import functional as F

from . import reviewed_train as engine
from .ar_scale_review import estimate, native_objects
from .ar_scale_study import (BUDGET, ROOT, cohort_rows, configuration, load_plan as scale_plan,
                             warm_model)
from .datasets import SegmentationDataset
from .durable import atomic_json
from .journal import append, read
from .metrics import binary_segmentation
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


BASE_IDENTITY = engine.code_identity
BASE_LOSS = engine.loss_value
RULE = {'tiny_input_pixels': 64, 'small_input_pixels': 256, 'tiny_multiplier': 3., 'small_multiplier': 1.5}


def foreground_weights(instances):
    """Reallocate positive BCE mass while retaining each image's foreground total."""
    foreground = instances > 0
    sizes = torch.bincount(instances.flatten())
    factors = torch.ones_like(sizes, dtype=torch.float32)
    factors[1:] = torch.where(sizes[1:] < RULE['tiny_input_pixels'], RULE['tiny_multiplier'],
                             torch.where(sizes[1:] < RULE['small_input_pixels'], RULE['small_multiplier'], 1.))
    weight = factors[instances]
    if foreground.any():
        weight[foreground] *= foreground.sum() / weight[foreground].sum()
    return weight


def weighted_target(semantic, instances):
    if instances.ndim != 2 or semantic.shape != (1, *instances.shape):
        raise ValueError('活动区训练标签与二维实例掩膜的网格不一致')
    return torch.cat((semantic, foreground_weights(instances)[None]), dim=0)


def weighted_bce(logits, target, kind):
    if target.shape[1] == 1:
        return BASE_LOSS(logits, target, kind)
    semantic, weight = target.split(1, dim=1)
    return F.binary_cross_entropy_with_logits(logits.float(), semantic.float(), weight=weight.float())


def identity():
    names = ('ar_precision.py', 'ar_scale_study.py', 'ar_scale_review.py', 'ar_small_region_study.py',
             'ar_guard_train.py', 'run_locations.py', 'forecast_region_detector_audit.py')
    return {**BASE_IDENTITY(), **{name: digest(Path(__file__).with_name(name)) for name in names}}


def prepare(study, control, review):
    control, review = Path(control).resolve(), Path(review).resolve()
    origin = scale_plan(control)
    study = output_path(study, origin['dataset'])
    result = read(review / 'summary.json')
    if study.exists() or any(study.is_relative_to(path) for path in (control, review)):
        raise ValueError('损失对照使用独立新目录')
    if (
        result['state'] != 'completed' or result['test_accessed'] or result['calibration_accessed']
        or result['protocol_sha256'] != digest(control / 'protocol.json')
        or result['images_sha256'] != digest(review / 'images.json')
        or result['checkpoint_sha256']['trained512'] != digest(control / '512/run/last.pt')
    ):
        raise ValueError('控制组和逐图记录身份不符')
    data = SegmentationDataset(origin['dataset'], 'AR', 'train', 512)
    data.rows = cohort_rows(data, read(control / 'cohort.json'))
    example = data[0]
    target = weighted_target(example['semantic_mask'], example['instance_mask'])
    if target.shape != (2, 512, 512) or not torch.isfinite(target).all():
        raise ValueError('真实训练标签权重试跑失败')
    plan = {
        'created_utc': now(), 'dataset': origin['dataset'], 'data_identity': origin['data_identity'],
        'control': str(control), 'control_protocol_sha256': digest(control / 'protocol.json'),
        'control_review': str(review), 'control_review_sha256': digest(review / 'summary.json'),
        'code': identity(), 'rule': RULE, 'budget': BUDGET,
        'config': {**configuration(origin, 512), 'id': 'AR-small-weight-512-s2026',
                   'loss_rule': 'size_weighted_foreground_bce'},
        'scope': 'Same384 train and276 development images, warm start, fixed three epochs, logits threshold 0.5.',
        'decision': origin['decision'],
        'difference': 'Only foreground BCE weight redistribution. No validation weighting or architecture-size change.',
        'test_accessed': False, 'calibration_accessed': False,
    }
    study.mkdir(parents=True)
    atomic_json(study / 'protocol.json', plan)
    return plan


def open_study(study):
    plan = read(study / 'protocol.json')
    control = Path(plan['control'])
    review = Path(plan['control_review'])
    origin = scale_plan(control)
    if (
        plan['code'] != identity() or plan['data_identity'] != origin['data_identity']
        or plan['control_protocol_sha256'] != digest(control / 'protocol.json')
        or plan['control_review_sha256'] != digest(review / 'summary.json')
        or plan['budget'] != BUDGET or plan['rule'] != RULE
    ):
        raise ValueError('损失对照的身份已改变')
    return plan, origin, control, review


def train(study):
    study = Path(study).resolve()
    plan, origin, control, _ = open_study(study)
    run = study / 'run'
    if (run / 'PAUSE').exists():
        raise ValueError('暂停标记仍在')
    old = read(run / 'status.json', {})
    if old.get('state') == 'completed' and old.get('completed_epoch') == BUDGET:
        return old
    config = plan['config']
    model, provenance = warm_model(origin, config)
    cohort = read(control / 'cohort.json')

    class WeightedDataset(SegmentationDataset):
        def __init__(self, directory, category, split, size):
            super().__init__(directory, category, split, size)
            self.weighted = split == 'train'
            if self.weighted:
                self.rows = cohort_rows(self, cohort)

        def __getitem__(self, index):
            item = super().__getitem__(index)
            if self.weighted:
                item['semantic_mask'] = weighted_target(item['semantic_mask'], item['instance_mask'])
            return item

    class TorchOptions:
        optim = SimpleNamespace(
            Adam=lambda parameters, **options: torch.optim.Adam(
                parameters, **{**options, 'lr': config['learning_rate']}
            ),
            lr_scheduler=torch.optim.lr_scheduler,
        )

        def __getattr__(self, name):
            return getattr(torch, name)

    original = (engine.architecture, engine.code_identity, engine.SegmentationDataset,
                engine.loss_value, engine.torch)
    log = ROOT / 'out/training-20261112-v2'
    append(log, 'development', '活动区小目标损失试验',
           '从同一起点训练3轮，仅重分配前景的训练权重。', run_id=config['id'])
    try:
        engine.architecture = lambda _: (model, provenance)
        engine.code_identity = identity
        engine.SegmentationDataset = WeightedDataset
        engine.loss_value = weighted_bce
        engine.torch = TorchOptions()
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        status = engine.train(config, plan['dataset'], run, resume=(run / 'last.pt').exists())
        append(log, 'development', '活动区小目标损失训练进度',
               f'保存 {status["completed_epoch"]}/3 轮，状态 {status["state"]}。',
               run_id=config['id'], epoch=status['completed_epoch'])
        return status
    finally:
        (engine.architecture, engine.code_identity, engine.SegmentationDataset,
         engine.loss_value, engine.torch) = original


def review(study, output):
    study = Path(study).resolve()
    plan, origin, _, control = open_study(study)
    output = output_path(output, plan['dataset'])
    if output.exists() or output.is_relative_to(study):
        raise ValueError('复算需要独立目录')
    run = study / 'run'
    status = read(run / 'status.json', {})
    if status.get('state') != 'completed' or status.get('completed_epoch') != BUDGET:
        raise ValueError('尚无完整第三轮检查点')
    path = run / 'last.pt'
    sha = digest(path)
    saved = engine.load_checkpoint(path)
    if (
        saved['epoch'] != BUDGET or saved['identity'] != read(run / 'identity.json')
        or saved['identity']['config'] != plan['config'] or saved['identity']['code'] != plan['code']
        or digest(path) != sha
    ):
        raise ValueError('新权重不符合复算身份')
    past = read(control / 'summary.json')
    previous = control / 'images.json'
    if past['images_sha256'] != digest(previous):
        raise ValueError('原512控制组逐图记录变化')
    rows = read(previous)
    data = SegmentationDataset(plan['dataset'], 'AR', 'validation', 512)
    if len(rows) != 276 or [row['uri'] for row in data.rows] != [row['uri'] for row in rows]:
        raise ValueError('验证样本不一致')
    model, _ = warm_model(origin, plan['config'])
    model.load_state_dict(saved['model'])
    with exclusive(ROOT / '.mdva/training-gpu.lock'):
        engine.seed_all(plan['config']['seed'])
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        resource = Resources()
        model.cuda().eval()
        output.mkdir(parents=True)
        with torch.inference_mode():
            for index, (source, record) in enumerate(zip(data.rows, rows)):
                reference = np.load(data.source(source['mask_uri']), allow_pickle=False)
                with torch.autocast('cuda', dtype=torch.float16):
                    logits = model(data[index]['image'][None].cuda())
                prediction = F.interpolate(logits.float().sigmoid(), size=reference.shape,
                                           mode='bilinear', align_corners=False)[0, 0].cpu().numpy() >= .5
                record['arms']['weighted512'] = {
                    'pixels': binary_segmentation(prediction, reference > 0),
                    'objects': native_objects(prediction, reference),
                }
                if (index + 1) % 32 == 0:
                    resource.sample()
        resources = resource.sample()
    before, after = (estimate(rows, key) for key in ('trained512', 'weighted512'))
    start = estimate(rows, 'warm512')
    if before != past['results']['trained512']:
        raise ValueError('控制组无法复算')
    pixel_replay = after['pixels']['dice'] - saved['history'][-1]['validation']['dice']
    deltas = {name: after[name] - before[name] for name in ('region_precision', 'region_recall')}
    deltas['dice'] = after['pixels']['dice'] - before['pixels']['dice']
    deltas['small_recall'] = (after['by_reference_size']['under256']['recall']
                              - before['by_reference_size']['under256']['recall'])
    limits = plan['decision']
    checks = {
        'replay': abs(pixel_replay) <= 1e-5, 'small_recall': deltas['small_recall'] >= limits['small_recall_gain'],
        'precision': deltas['region_precision'] >= -limits['overall_precision_drop'],
        'recall': deltas['region_recall'] >= -limits['overall_recall_drop'],
        'dice': deltas['dice'] >= -limits['dice_drop'],
    }
    atomic_json(output / 'images.json', rows)
    result = {
        'state': 'completed' if checks['replay'] else 'metric_mismatch', 'created_utc': now(),
        'images': len(rows), 'scope': 'Development only, fixed final epoch and native matching.',
        'results': {'warm512': start, 'trained512': before, 'weighted512': after},
        'vs_control': deltas, 'checks': checks, 'practical_gate_passed': all(checks.values()),
        'checkpoint_sha256': sha, 'protocol_sha256': digest(study / 'protocol.json'),
        'control_review_sha256': plan['control_review_sha256'], 'pixel_replay': pixel_replay,
        'rows_sha256': digest(output / 'images.json'), 'resources': resources,
        'test_accessed': False, 'calibration_accessed': False, 'weights_replaced': False,
    }
    atomic_json(output / 'summary.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'train', 'review'))
    parser.add_argument('--study', required=True)
    parser.add_argument('--control')
    parser.add_argument('--control-review')
    parser.add_argument('--output')
    args = parser.parse_args()
    configure_environment()
    if args.action == 'prepare' and args.control and args.control_review:
        result = prepare(args.study, args.control, args.control_review)
    elif args.action == 'train':
        result = train(args.study)
    elif args.action == 'review' and args.output:
        result = review(args.study, args.output)
    else:
        parser.error('缺少所选阶段的来源或输出目录')
    print(json.dumps({key: result[key] for key in ('state', 'budget', 'completed_epoch', 'vs_control', 'checks')
                      if key in result}, ensure_ascii=False))


if __name__ == '__main__':
    main()
