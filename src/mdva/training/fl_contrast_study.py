"""Paired FL fine-tuning with a trainable local-contrast input channel."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch
from torch import nn
from torch.nn import functional as F

from . import reviewed_train as engine
from .durable import atomic_json
from .fl_author_model import AuthorGraph, SOURCE, loss_value
from .fl_sampling_control import loader_for
from .journal import append, read
from .runtime import configure_environment, digest, now, output_path


ROOT = Path(__file__).resolve().parents[3]
ARMS = ('control', 'contrast')
BUDGET = 3
BASE_TRAIN_IDENTITY = engine.code_identity


def local_contrast(gray):
    """Use a fixed 33px local mean, suppressing residuals near missing background."""
    valid = gray > (8 / 255)

    def mean_filter(values):
        across = F.avg_pool2d(values, (1, 33), stride=1, padding=(0, 16))
        return F.avg_pool2d(across, (33, 1), stride=1, padding=(16, 0))

    density = mean_filter(valid.float())
    center = mean_filter(gray * valid) / density.clamp_min(.01)
    residual = ((gray - center) * 4).clamp(-1, 1)
    return torch.where(valid & (density >= .95), residual, 0.)


class ContrastGraph(AuthorGraph):
    def __init__(self, weights, enabled):
        super().__init__('batch')
        self.load_state_dict(weights)
        encoder = self.network.encoder1
        original = encoder.enc1conv1
        projection = nn.Conv2d(2, original.out_channels, original.kernel_size,
                               padding=original.padding, bias=False)
        with torch.no_grad():
            projection.weight.zero_()
            projection.weight[:, :1].copy_(original.weight)
        encoder.enc1conv1 = projection
        encoder.register_forward_pre_hook(self.append_contrast)
        self.enabled = enabled

    def append_contrast(self, _module, args):
        (image,) = args
        channel = local_contrast(image) if self.enabled else torch.zeros_like(image)
        return (torch.cat((image, channel), dim=1),)


def identity():
    names = ('fl_author_model.py', 'fl_sampling_control.py', 'fl_contrast_study.py', 'fl_main.py')
    return {**BASE_TRAIN_IDENTITY(), **{name: digest(Path(__file__).with_name(name)) for name in names}}


def prepare(study, source_study, archived_review, input_audit):
    source_study, archived_review, input_audit = map(lambda p: Path(p).resolve(), (source_study, archived_review, input_audit))
    source_path = source_study / 'protocol.json'
    review_path = archived_review / 'summary.json'
    source, review, audit = read(source_path), read(review_path), read(input_audit)
    weights = archived_review / 'checkpoint.pt'
    if (
        review['state'] != 'completed' or review['checkpoint_kind'] != 'best'
        or review['calibration_accessed'] or review['test_accessed']
        or review['plan_sha256'] != digest(source_path) or review['checkpoint_sha256'] != digest(weights)
        or audit['state'] != 'completed' or audit['images'] != 96 or audit['test_accessed']
    ):
        raise ValueError('配对试验的起点或输入审计身份不符')
    study = output_path(study, source['dataset'])
    if study.exists() or any(study.is_relative_to(parent) for parent in (source_study, archived_review)):
        raise ValueError('配对试验使用独立新目录')
    saved = engine.load_checkpoint(weights)
    if saved['epoch'] != review['epoch'] or saved['identity'] != read(source_study / 'run/identity.json'):
        raise ValueError('起点权重与复核记录不符')
    torch.set_num_threads(2)
    training = engine.SegmentationDataset(source['dataset'], 'FL', 'train', 512)
    samples = [training[i]['image'] for i in (0, len(training) // 2, len(training) - 1)]
    probe = torch.stack(samples)
    original = AuthorGraph('batch').eval()
    original.load_state_dict(saved['model'])
    with torch.inference_mode():
        expected = original(probe)
        preflight = {}
        for arm in ARMS:
            model = ContrastGraph(saved['model'], arm == 'contrast').eval()
            actual = model(probe)
            torch.testing.assert_close(actual, expected, rtol=1e-5, atol=2e-5)
            preflight[arm] = float((actual - expected).abs().max())
    config = {
        **source['config'], 'lr': 1e-4, 'micro_batch': 2, 'effective_batch': 8,
        'max_epochs': BUDGET, 'min_epochs': BUDGET, 'patience': 12,
        'warm_start_weights': str(weights), 'warm_start_sha256': digest(weights),
    }
    plan = {
        'created_utc': now(), 'arms': list(ARMS), 'budget_epochs_per_arm': BUDGET,
        'config': config, 'dataset': source['dataset'], 'data_identity': engine.data_identity(source['dataset']),
        'code': identity(), 'source_provenance_sha256': digest(SOURCE / 'provenance.json'),
        'initial_review_sha256': digest(review_path), 'input_audit_sha256': digest(input_audit),
        'preflight_max_logit_difference': preflight,
        'change': 'Trainable second feature channel. Same architecture and weights at initialization; control passes zeros.',
        'optimization': 'Adam 1e-4, FP32, w10 BCE+Dice, half-inverse station sampling, same stochastic augment and draw sequence.',
        'scope': 'Frozen train and development validation only. Compare fixed third epoch. Do not release or extend automatically.',
        'decision': 'Require region recall gain >=3pp, precision loss <=1pp, and no station Dice loss >3pp versus control.',
    }
    study.mkdir(parents=True)
    atomic_json(study / 'protocol.json', plan)
    return plan


def architecture(config):
    weights = Path(config['warm_start_weights'])
    if digest(weights) != config['warm_start_sha256']:
        raise ValueError('配对起点权重已改变')
    saved = engine.load_checkpoint(weights)
    if any(not torch.isfinite(value).all() for value in saved['model'].values()):
        raise ValueError('配对起点含非有限参数')
    return ContrastGraph(saved['model'], config['arm'] == 'contrast'), {
        'arm': config['arm'], 'trainable_input_channels': 2, 'source_sha256': config['warm_start_sha256'],
    }


def train(study, arm):
    study = Path(study).resolve()
    plan = read(study / 'protocol.json')
    if (
        plan['code'] != identity() or plan['data_identity'] != engine.data_identity(plan['dataset'])
        or plan['source_provenance_sha256'] != digest(SOURCE / 'provenance.json')
        or plan['arms'] != list(ARMS) or plan['budget_epochs_per_arm'] != BUDGET
    ):
        raise ValueError('配对方案、来源或工程身份发生变化')
    arm_folder = study / arm / 'run'
    if arm == 'contrast':
        control = read(study / 'control/run/status.json', {})
        if control.get('state') != 'completed' or control.get('completed_epoch') != BUDGET:
            raise ValueError('先完成配对控制组')
    if (arm_folder / 'PAUSE').exists():
        raise ValueError('暂停标记仍在')
    old = read(arm_folder / 'status.json', {})
    if old.get('state') == 'completed' and old.get('completed_epoch') == BUDGET:
        return old
    config = {**plan['config'], 'id': f'FL-local-contrast-{arm}-s2026', 'arm': arm}
    saved = arm_folder / 'last.pt'
    original = (engine.architecture, engine.code_identity, engine.loader_for, engine.loss_value, engine.torch)
    class Optim:
        def Adam(self, params, **kwargs):
            kwargs['lr'] = config['lr']
            return FiniteAdam(params, **kwargs)

        def __getattr__(self, name):
            return getattr(torch.optim, name)

    class FiniteAdam(torch.optim.Adam):
        def step(self, *args, **kwargs):
            if any(p.grad is not None and not torch.isfinite(p.grad).all()
                   for group in self.param_groups for p in group['params']):
                raise ValueError('非有限梯度，保留已保存轮次')
            return super().step(*args, **kwargs)

    class Amp:
        def GradScaler(self, *_args, **_kwargs):
            return torch.amp.GradScaler('cuda', enabled=False)

    class TrainingTorch:
        optim = Optim()
        amp = Amp()

        def __getattr__(self, name):
            return getattr(torch, name)

    append(ROOT / 'out/training-20261112-v2', 'development', '暗条局部对比配对试验',
           f'{arm} 开始，固定预算{BUDGET}轮；起点、采样和训练条件一致。', run_id=config['id'])
    try:
        engine.architecture = architecture
        engine.code_identity = identity
        engine.loader_for = loader_for
        engine.loss_value = lambda logits, target, _: loss_value(logits, target, 10)
        engine.torch = TrainingTorch()
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        result = engine.train(config, plan['dataset'], arm_folder, resume=saved.exists())
        append(ROOT / 'out/training-20261112-v2', 'development', '暗条局部对比配对试验进度',
               f'{arm} · {result["state"]} · {result["completed_epoch"]}/{BUDGET}轮',
               run_id=config['id'], epoch=result['completed_epoch'])
        return result
    finally:
        (engine.architecture, engine.code_identity, engine.loader_for, engine.loss_value, engine.torch) = original


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'train'))
    parser.add_argument('--study', required=True)
    parser.add_argument('--source-study')
    parser.add_argument('--archived-review')
    parser.add_argument('--input-audit')
    parser.add_argument('--arm', choices=ARMS)
    args = parser.parse_args()
    configure_environment()
    if args.action == 'prepare':
        if not all((args.source_study, args.archived_review, args.input_audit)):
            parser.error('prepare 需要来源试验、归档复核和输入审计')
        result = prepare(args.study, args.source_study, args.archived_review, args.input_audit)
        print(json.dumps({'state': 'prepared', 'preflight': result['preflight_max_logit_difference']}))
    else:
        if not args.arm:
            parser.error('train 需要指定配对组')
        result = train(args.study, args.arm)
        print(json.dumps({'state': result['state'], 'completed_epoch': result['completed_epoch']}))


if __name__ == '__main__':
    main()
