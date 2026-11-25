"""Matched AR control and optional HMI continuum experiments."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter
import torch
from torch import nn
from torch.nn import functional as F

from . import reviewed_train as engine
from .ar_scale_review import estimate, native_objects
from .ar_scale_study import ROOT, BUDGET, cohort_rows, configuration, load_plan as scale_plan, warm_model
from .datasets import SegmentationDataset
from .durable import atomic_json
from .journal import append, read
from .metrics import binary_segmentation
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


ARMS = ('control', 'continuum')
BASE_IDENTITY = engine.code_identity
BASE_LOADER = engine.loader_for
Y, X = np.ogrid[:512, :512]
CORE_TAPER = np.clip((210 - np.hypot(X - 255.5, Y - 255.5)) / 22, 0, 1).astype(np.float32)


def auxiliary_image(path):
    with Image.open(path) as image:
        gray = np.asarray(image.convert('L').resize((512, 512), Image.Resampling.BILINEAR), dtype=np.float32)
    return torch.from_numpy(np.clip((gaussian_filter(gray, 8) - gray) / 64, -1, 1) * CORE_TAPER)[None]


def identity():
    names = ('ar_continuum_study.py', 'ar_continuum_geometry.py', 'ar_scale_study.py', 'ar_scale_review.py',
             'ar_precision.py', 'ar_guard_train.py', 'run_locations.py', 'forecast_region_detector_audit.py')
    return {**BASE_IDENTITY(), **{name: digest(Path(__file__).with_name(name)) for name in names}}


def expand(model):
    encoder = model.encoders[0].layers
    old = encoder[0]
    new = nn.Conv2d(2, old.out_channels, old.kernel_size, padding=old.padding, bias=old.bias is not None)
    with torch.no_grad():
        new.weight.zero_()
        new.weight[:, :1].copy_(old.weight)
        if old.bias is not None:
            new.bias.copy_(old.bias)
    encoder[0] = new

    def add_missing(_layer, args):
        (image,) = args
        if image.shape[1] == 1:
            image = torch.cat((image, torch.zeros_like(image)), dim=1)
        return (image,)

    new.register_forward_pre_hook(add_missing)
    return model


class PairedDataset(SegmentationDataset):
    def __init__(self, directory, split, pairs, cohort, enabled):
        super().__init__(directory, 'AR', split, 512)
        if split == 'train':
            self.rows = cohort_rows(self, cohort)
        if set(pairs) != {row['uri'] for row in self.rows}:
            raise ValueError('双输入配对和冻结队列不一致')
        self.pairs = pairs
        self.enabled = enabled
        self.epoch = None

    def __getitem__(self, index):
        item = super().__getitem__(index)
        row = self.pairs[item['uri']]
        use = self.enabled and row['state'] == 'supported_display_pair'
        if use and self.epoch is not None:
            sample = f"{item['uri']}|2026|{self.epoch}".encode()
            use = int.from_bytes(hashlib.sha256(sample).digest()[:4], 'little') % 3 != 0
        aux = auxiliary_image(self.source(row['aux_uri'])) if use else torch.zeros_like(item['image'])
        item['image'] = torch.cat((item['image'], aux), dim=0)
        item['aux_used'] = use
        return item


def load_pairs(geometry):
    summary = read(geometry / 'summary.json')
    path = geometry / 'pairs.json'
    if (summary['state'] != 'completed' or summary['test_accessed'] or summary['pairs_sha256'] != digest(path)
            or summary['code_sha256'] != digest(Path(__file__).with_name('ar_continuum_geometry.py'))):
        raise ValueError('配准结果身份不符')
    pairs = {split: {} for split in ('train', 'validation')}
    for row in read(path):
        if row['uri'] in pairs[row['split']]:
            raise ValueError('配对重复')
        pairs[row['split']][row['uri']] = row
    return summary, pairs


def prepare(study, source, geometry):
    source, geometry = Path(source).resolve(), Path(geometry).resolve()
    origin = scale_plan(source)
    study = output_path(study, origin['dataset'])
    if study.exists() or any(study.is_relative_to(path) for path in (source, geometry)):
        raise ValueError('双输入对照使用独立输出目录')
    summary, pairs = load_pairs(geometry)
    if summary['manifest_sha256'] != origin['data_identity']['manifest-hashes.json']:
        raise ValueError('配对来源不是同一版数据')
    train = PairedDataset(origin['dataset'], 'train', pairs['train'], read(source / 'cohort.json'), True)
    PairedDataset(origin['dataset'], 'validation', pairs['validation'], [], True)
    indices = [i for i, row in enumerate(train.rows) if pairs['train'][row['uri']]['state'] == 'supported_display_pair']
    if not indices:
        raise ValueError('缺少可用的真配对')
    config = configuration(origin, 512)
    baseline, _ = warm_model(origin, config)
    model, _ = warm_model(origin, config)
    model = expand(model).eval()
    torch.set_num_threads(2)
    example = train[indices[len(indices) // 2]]['image'][None]
    with torch.inference_mode():
        expected = baseline.eval()(example[:, :1])
        actual = model(example)
        torch.testing.assert_close(actual, expected, rtol=1e-5, atol=2e-5)
        torch.testing.assert_close(model(example[:, :1]), actual, rtol=0, atol=0)
    plan = {
        'state': 'frozen', 'created_utc': now(), 'code': identity(), 'source': str(source), 'geometry': str(geometry),
        'source_sha256': digest(source / 'protocol.json'), 'geometry_sha256': digest(geometry / 'summary.json'),
        'origin_data_identity': origin['data_identity'], 'cohort_sha256': digest(source / 'cohort.json'),
        'arms': list(ARMS), 'budget': BUDGET, 'initial_difference': float((actual - expected).abs().max()),
        'scope': '384 common train and276 development images, the same expanded U-Net and common initialization.',
        'change': 'Control always sees zeros. Treatment sees aligned display texture, with one-third deterministic auxiliary dropout.',
        'decision': {'overall_recall_gain': .03, 'precision_drop': .01, 'small_recall_drop': .02,
                     'absent_recall_drop': .02, 'absent_precision_drop': .02},
        'auxiliary_support': summary['by_split'], 'test_accessed': False, 'calibration_accessed': False,
    }
    study.mkdir(parents=True)
    atomic_json(study / 'protocol.json', plan)
    return plan


def load_study(study):
    plan = read(study / 'protocol.json')
    source, geometry = (Path(plan[key]) for key in ('source', 'geometry'))
    origin = scale_plan(source)
    _, pairs = load_pairs(geometry)
    if (
        plan['code'] != identity() or plan['source_sha256'] != digest(source / 'protocol.json')
        or plan['geometry_sha256'] != digest(geometry / 'summary.json')
        or plan['cohort_sha256'] != digest(source / 'cohort.json') or plan['origin_data_identity'] != origin['data_identity']
        or plan['arms'] != list(ARMS) or plan['budget'] != BUDGET
    ):
        raise ValueError('双输入协议身份改变')
    return plan, origin, pairs, read(source / 'cohort.json')


def config_for(origin, arm, protocol_path):
    return {**configuration(origin, 512), 'id': f'AR-continuum-{arm}-s2026',
            'paired_protocol_sha256': digest(protocol_path)}


def train(study, arm):
    study = Path(study).resolve()
    _, origin, pairs, cohort = load_study(study)
    run = study / arm / 'run'
    if (run / 'PAUSE').exists():
        raise ValueError('暂停标记仍在')
    if arm == 'continuum':
        control = read(study / 'control/run/status.json', {})
        if control.get('state') != 'completed' or control.get('completed_epoch') != BUDGET:
            raise ValueError('等待完整控制组')
    status = read(run / 'status.json', {})
    if status.get('state') == 'completed' and status.get('completed_epoch') == BUDGET:
        return status
    config = config_for(origin, arm, study / 'protocol.json')
    model, provenance = warm_model(origin, config)
    model = expand(model)

    def data_factory(directory, category, split, size):
        if category != 'AR' or size != 512:
            raise ValueError('双输入实验只支持登记的活动区与512网格')
        return PairedDataset(directory, split, pairs[split], cohort, arm == 'continuum')

    def loader_for(dataset, spec, epoch, batch):
        dataset.epoch = epoch
        return BASE_LOADER(dataset, spec, epoch, batch)

    class TorchOptions:
        optim = SimpleNamespace(
            Adam=lambda parameters, **options: torch.optim.Adam(
                parameters, **{**options, 'lr': config['learning_rate']}),
            lr_scheduler=torch.optim.lr_scheduler,
        )

        def __getattr__(self, name):
            return getattr(torch, name)

    original = (engine.architecture, engine.code_identity, engine.SegmentationDataset, engine.loader_for, engine.torch)
    log = ROOT / 'out/training-20261112-v2'
    append(log, 'development', '活动区连续谱配对对照',
           f'{arm} 组启动，固定三轮。', run_id=config['id'])
    try:
        engine.architecture = lambda _: (model, {**provenance, 'input_channels': 2, 'arm': arm})
        engine.code_identity = identity
        engine.SegmentationDataset = data_factory
        engine.loader_for = loader_for
        engine.torch = TorchOptions()
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        result = engine.train(config, origin['dataset'], run, resume=(run / 'last.pt').exists())
        append(log, 'development', '活动区连续谱配对保存',
               f'{arm} 组保存 {result["completed_epoch"]}/3 轮，状态 {result["state"]}。',
               run_id=config['id'], epoch=result['completed_epoch'])
        return result
    finally:
        engine.architecture, engine.code_identity, engine.SegmentationDataset, engine.loader_for, engine.torch = original


def review(study, output):
    study = Path(study).resolve()
    plan, origin, pairs, _ = load_study(study)
    output = output_path(output, origin['dataset'])
    if output.exists() or output.is_relative_to(study):
        raise ValueError('双输入复算使用新输出目录')
    models, checkpoints, expected = {}, {}, {}
    for arm in ARMS:
        run = study / arm / 'run'
        path = run / 'last.pt'
        status = read(run / 'status.json', {})
        if status.get('state') != 'completed' or status.get('completed_epoch') != BUDGET:
            raise ValueError('需要两组固定第三轮')
        sha = digest(path)
        saved = engine.load_checkpoint(path)
        config = config_for(origin, arm, study / 'protocol.json')
        if (saved['epoch'] != BUDGET or saved['identity'] != read(run / 'identity.json')
                or saved['identity']['config'] != config or saved['identity']['code'] != plan['code']
                or digest(path) != sha):
            raise ValueError('检查点身份不符')
        model, _ = warm_model(origin, config)
        model = expand(model)
        model.load_state_dict(saved['model'])
        models[arm] = model.eval()
        checkpoints[arm] = sha
        expected[arm] = saved['history'][-1]['validation']['dice']
    validation = PairedDataset(origin['dataset'], 'validation', pairs['validation'], [], True)
    rows = []
    with exclusive(ROOT / '.mdva/training-gpu.lock'):
        engine.seed_all(2026)
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        resource = Resources()
        for model in models.values():
            model.cuda()
        output.mkdir(parents=True)
        with torch.inference_mode():
            for index, row in enumerate(validation.rows):
                item = validation[index]
                pair = item['image'][None].cuda()
                primary = pair[:, :1]
                native = np.load(validation.source(row['mask_uri']), allow_pickle=False)
                result = {'uri': row['uri'], 'aux_used': item['aux_used'], 'arms': {}}
                paths = {'control': (models['control'], primary),
                         'continuum': (models['continuum'], pair),
                         'without_auxiliary': (models['continuum'], primary)}
                for name, (model, image) in paths.items():
                    with torch.autocast('cuda', dtype=torch.float16):
                        logits = model(image)
                    mask = F.interpolate(logits.float().sigmoid(), size=native.shape,
                                         mode='bilinear', align_corners=False)[0, 0].cpu().numpy() >= .5
                    result['arms'][name] = {'pixels': binary_segmentation(mask, native > 0),
                                            'objects': native_objects(mask, native)}
                rows.append(result)
                if (index + 1) % 32 == 0 or index + 1 == len(validation):
                    atomic_json(output / 'progress.json', {'done': index + 1, 'total': len(validation),
                                                           'resource': resource.sample()})
        resources = resource.sample()
    names = ('control', 'continuum', 'without_auxiliary')
    all_scores = {name: estimate(rows, name) for name in names}
    supported = [row for row in rows if row['aux_used']]
    control, paired, alone = (all_scores[name] for name in names)
    deltas = {key: paired[key] - control[key] for key in ('region_precision', 'region_recall')}
    replay = {arm: all_scores[arm]['pixels']['dice'] - expected[arm] for arm in ARMS}
    rules = plan['decision']
    checks = {
        'replay': all(abs(value) <= 1e-5 for value in replay.values()),
        'recall': deltas['region_recall'] >= rules['overall_recall_gain'],
        'precision': deltas['region_precision'] >= -rules['precision_drop'],
        'small': (paired['by_reference_size']['under256']['recall']
                  - control['by_reference_size']['under256']['recall']) >= -rules['small_recall_drop'],
        'fallback_recall': alone['region_recall'] >= control['region_recall'] - rules['absent_recall_drop'],
        'fallback_precision': alone['region_precision'] >= control['region_precision'] - rules['absent_precision_drop'],
    }
    atomic_json(output / 'images.json', rows)
    result = {
        'state': 'completed' if checks['replay'] else 'metric_mismatch', 'created_utc': now(),
        'images': len(rows), 'supported_images': len(supported), 'all': all_scores,
        'on_supported': {name: estimate(supported, name) for name in names},
        'paired_delta': deltas, 'fixed_threshold': .5, 'checks': checks, 'practical_gate_passed': all(checks.values()),
        'scope': 'Matched development comparison. Weak registration falls back. Checkpoint fixed at third epoch.',
        'replay_delta': replay, 'checkpoint_sha256': checkpoints, 'protocol_sha256': digest(study / 'protocol.json'),
        'rows_sha256': digest(output / 'images.json'), 'resources': resources,
        'test_accessed': False, 'calibration_accessed': False, 'released': False,
    }
    atomic_json(output / 'summary.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'train', 'review'))
    parser.add_argument('--study', required=True)
    parser.add_argument('--source')
    parser.add_argument('--geometry')
    parser.add_argument('--output')
    parser.add_argument('--arm', choices=ARMS)
    args = parser.parse_args()
    configure_environment()
    if args.action == 'prepare' and args.source and args.geometry:
        result = prepare(args.study, args.source, args.geometry)
    elif args.action == 'train' and args.arm:
        result = train(args.study, args.arm)
    elif args.action == 'review' and args.output:
        result = review(args.study, args.output)
    else:
        parser.error('缺少对应阶段参数')
    print(json.dumps({key: result[key] for key in (
        'state', 'completed_epoch', 'auxiliary_support', 'paired_delta', 'checks') if key in result}))


if __name__ == '__main__':
    main()
