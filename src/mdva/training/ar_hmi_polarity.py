"""Paired segmentation comparison of signed means and separate polarity peaks."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import torch
from torch import nn
from torch.nn import functional as F

from . import reviewed_train as engine
from .ar_hmi_segmentation import HmiDataset, identity as prior_identity, setup as prior_setup
from .ar_region_retention import filter_by_area
from .ar_scale_review import estimate, native_objects
from .ar_scale_study import BUDGET, ROOT, warm_model
from .durable import atomic_json
from .journal import append, read
from .metrics import binary_segmentation
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


ARMS = ('mean', 'peaks')
BASE_LOADER = engine.loader_for


def polarity_planes(field, arm):
    if field.shape != (1024, 1024) or arm not in ARMS:
        raise ValueError('需已配准1024磁图和已冻结池化方式')
    windows = field.reshape(512, 2, 512, 2)
    valid = np.isfinite(windows).all(axis=(1, 3))
    usable = np.where(valid[:, None, :, None], windows, 0.)
    if arm == 'mean':
        means = usable.mean(axis=(1, 3))
        positive, negative = np.maximum(means, 0), np.minimum(means, 0)
    else:
        positive = np.maximum(usable.max(axis=(1, 3)), 0)
        negative = np.minimum(usable.min(axis=(1, 3)), 0)
    signed = np.stack((positive, negative)).clip(-1000, 1000)
    return (np.arcsinh(signed/50) / np.arcsinh(20)).astype(np.float32)


def expand(model):
    encoder = model.encoders[0].layers
    old = encoder[0]
    layer = nn.Conv2d(3, old.out_channels, old.kernel_size, padding=old.padding, bias=old.bias is not None)
    with torch.no_grad():
        layer.weight.zero_()
        layer.weight[:, :1].copy_(old.weight)
        if old.bias is not None:
            layer.bias.copy_(old.bias)
    encoder[0] = layer

    def fallback(_layer, args):
        (x,) = args
        if x.shape[1] == 1:
            x = torch.cat((x, torch.zeros_like(x), torch.zeros_like(x)), dim=1)
        return (x,)

    layer.register_forward_pre_hook(fallback)
    return model


def identity():
    return {**prior_identity(), 'ar_hmi_polarity.py': digest(__file__)}


class PolarityDataset(HmiDataset):
    def __init__(self, directory, split, cohort, known, arm):
        super().__init__(directory, split, cohort, known, False)
        self.arm = arm

    def __getitem__(self, index):
        item = super().__getitem__(index)
        info = self.pairs[item['uri']]
        use = info['state'] == 'display_alignment_supported'
        if use and self.epoch is not None:
            seed = f'{item["uri"]}|{self.epoch}|2026'.encode()
            use = int.from_bytes(hashlib.sha256(seed).digest()[:4], 'little') % 4 != 0
        if use:
            path = self.cohort/'pairs'/info['id']/'aligned-los.npy'
            if path not in self.checked:
                if digest(path) != info['array_sha256']:
                    raise ValueError('磁场网格指纹改变')
                self.checked.add(path)
            auxiliary = torch.from_numpy(polarity_planes(np.load(path, allow_pickle=False), self.arm))
        else:
            auxiliary = torch.zeros_like(item['image'])
        item['image'] = torch.cat((item['image'][:1], auxiliary), dim=0)
        item['physical_used'] = use
        return item


def fit(origin, known, study, cohort, plan, arm):
    run = study/arm/'run'
    config = {**plan['config'], 'id': f'AR-HMI-polarity-{arm}-s2026', 'protocol_sha256': digest(study/'protocol.json')}
    status = read(run/'status.json', {})
    if status.get('state') == 'completed' and status.get('completed_epoch') == BUDGET:
        return config
    if (study/'PAUSE').exists() or (run/'PAUSE').exists():
        return None
    model, details = warm_model(origin, config)
    model = expand(model)

    def loader(dataset, spec, epoch, batch):
        dataset.epoch = epoch
        return BASE_LOADER(dataset, spec, epoch, batch)

    class TorchProfile:
        optim = SimpleNamespace(Adam=lambda params, **kw: torch.optim.Adam(params, **{**kw, 'lr': config['learning_rate']}),
                                lr_scheduler=torch.optim.lr_scheduler)

        def __getattr__(self, name):
            return getattr(torch, name)

    original = (engine.architecture, engine.code_identity, engine.SegmentationDataset, engine.loader_for, engine.torch)
    log = ROOT/'out/training-20261112-v2'
    append(log, 'development', '活动区正负磁场输入对照', f'{arm} 组开始训练，固定三轮。', run_id=config['id'])
    try:
        engine.architecture = lambda _: (model, {**details, 'input_channels': 3, 'polarity_pooling': arm})
        engine.code_identity = identity
        engine.SegmentationDataset = lambda path, category, split, size: PolarityDataset(path, split, cohort, known, arm)
        engine.loader_for = loader
        engine.torch = TorchProfile()
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        outcome = engine.train(config, origin['dataset'], run, resume=(run/'last.pt').exists())
        return config if outcome['state'] == 'completed' else None
    finally:
        engine.architecture, engine.code_identity, engine.SegmentationDataset, engine.loader_for, engine.torch = original


def review(origin, known, study, cohort, fresh, plan, configs):
    if not (fresh/'summary.json').is_file():
        return {'state': 'awaiting_fresh_development'}
    fresh_report = read(fresh/'summary.json')
    if (fresh_report['state'] != 'completed' or fresh_report['test_accessed'] or fresh_report['calibration_accessed']
            or fresh_report['protocol_sha256'] != plan['fresh_protocol_sha256']
            or len(fresh_report['results']) != 64):
        raise ValueError('新开发样本没有通过冻结检查')
    extra = {r['uri']: r for r in fresh_report['results']}
    if set(extra) & set(known['validation']):
        raise ValueError('新开发样本和原研究组重叠')
    models, historical, checkpoints = {}, {}, {}
    for arm, config in configs.items():
        path = study/arm/'run/last.pt'
        saved = engine.load_checkpoint(path)
        if (saved['epoch'] != BUDGET or saved['identity'] != read(path.parent/'identity.json')
                or saved['identity']['config'] != config or saved['identity']['code'] != plan['code']):
            raise ValueError('末轮检查点身份不符')
        models[arm] = expand(warm_model(origin, config)[0]).eval()
        models[arm].load_state_dict(saved['model'])
        historical[arm] = saved['history'][-1]['validation']['dice']
        checkpoints[arm] = digest(path)
    rows = []
    with exclusive(ROOT/'.mdva/training-gpu.lock'):
        engine.seed_all(2026)
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        resource = Resources()
        for model in models.values():
            model.cuda()
        with torch.inference_mode():
            groups = ((cohort, known['validation']), (fresh, extra))
            for folder, sources in groups:
                datasets = {arm: PolarityDataset(origin['dataset'], 'validation', folder, {'validation': sources}, arm) for arm in ARMS}
                ordering = datasets['mean']
                for index, record in enumerate(ordering.rows):
                    uri = record['uri']
                    truth = np.load(ordering.source(record['mask_uri']), allow_pickle=False)
                    row = {'uri': uri, 'lane': sources[uri]['lane'], 'time_utc': record['time_utc'], 'arms': {}}
                    for arm, dataset in datasets.items():
                        view = dataset[index]
                        with torch.autocast('cuda', dtype=torch.float16):
                            logits = models[arm](view['image'][None].cuda())
                        if not torch.isfinite(logits).all():
                            raise ValueError('极性分割出现非有限输出')
                        p = F.interpolate(logits.float().sigmoid(), size=truth.shape, mode='bilinear', align_corners=False)[0, 0].cpu().numpy()
                        row['arms'][arm] = {'pixels': binary_segmentation(p >= .5, truth > 0),
                                          'objects': native_objects(filter_by_area(p >= .4, 64), truth)}
                        row['physical_used'] = view['physical_used']
                    rows.append(row)
                    if index % 24 == 0:
                        resource.sample()
        measured = resource.sample()
    results = {lane: {arm: estimate([row for row in rows if row['lane'] == lane], arm) for arm in ARMS}
               for lane in ('development', 'small_region_stress', 'fresh_development')}
    old = [row for row in rows if row['lane'] != 'fresh_development']
    delta = {arm: estimate(old, arm)['pixels']['dice']-historical[arm] for arm in ARMS}
    if any(abs(x) > 1e-5 for x in delta.values()):
        raise ValueError(f'检查点评价不复现：{delta}')
    stress, fresh_scores = results['small_region_stress'], results['fresh_development']
    tiny = stress['peaks']['by_reference_size']['under256']['matched']-stress['mean']['by_reference_size']['under256']['matched']
    overall = {key: fresh_scores['peaks'][key]-fresh_scores['mean'][key] for key in ('region_precision', 'region_recall')}
    passed = tiny >= 2 and overall['region_precision'] >= -.01 and overall['region_recall'] >= -.01
    atomic_json(study/'images.json', rows)
    report = {'state': 'completed', 'created_utc': now(), 'results': results, 'replay_delta': delta, 'tiny_gain': tiny,
              'fresh_delta': overall, 'passed': passed, 'resource': measured, 'checkpoint_sha256': checkpoints,
              'images_sha256': digest(study/'images.json'), 'protocol_sha256': digest(study/'protocol.json'),
              'fresh_summary_sha256': digest(fresh/'summary.json'), 'test_accessed': False, 'calibration_accessed': False}
    atomic_json(study/'summary.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('study', 'source', 'cohort', 'prior', 'fresh'):
        parser.add_argument('--'+key, required=True, type=Path)
    args = parser.parse_args()
    configure_environment()
    origin, known, old, _, cohort = prior_setup(args.prior, args.source, args.cohort)
    study = output_path(args.study, origin['dataset'])
    if any(study.is_relative_to(p.resolve()) for p in (args.source, args.cohort, args.prior, args.fresh)):
        raise ValueError('正负极性研究使用独立目录')
    fresh_protocol = read(args.fresh/'protocol.json')
    if (fresh_protocol['previous_summary_sha256'] != digest(cohort/'summary.json') or fresh_protocol['test_accessed']
            or fresh_protocol['calibration_accessed'] or len(fresh_protocol['selection']) != 64):
        raise ValueError('新开发图像预登记不符合')
    plan = {'code': identity(), 'config': old['config'], 'cohort_summary_sha256': digest(cohort/'summary.json'),
            'prior_protocol_sha256': digest(args.prior/'protocol.json'), 'fresh_protocol_sha256': digest(args.fresh/'protocol.json'),
            'arms': list(ARMS), 'epochs': BUDGET, 'same_width': True, 'physical_dropout': .25,
            'primary_gate': {'tiny_count_gain': 2, 'fresh_max_precision_drop': .01, 'fresh_max_recall_drop': .01},
            'test_accessed': False, 'calibration_accessed': False}
    study.mkdir(parents=True, exist_ok=True)
    path = study/'protocol.json'
    if path.exists() and read(path) != plan:
        raise ValueError('本次对照的代码、来源或样本已改变')
    if not path.exists():
        atomic_json(path, plan)
    configs = {}
    for arm in ARMS:
        configs[arm] = fit(origin, known, study, cohort, plan, arm)
        if configs[arm] is None:
            return
    result = review(origin, known, study, cohort, args.fresh.resolve(), plan, configs)
    print(json.dumps({key: result[key] for key in ('state', 'tiny_gain', 'fresh_delta', 'passed') if key in result}))


if __name__ == '__main__':
    main()
