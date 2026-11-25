"""Paired segmentation fine-tuning with an optional aligned physical HMI image."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import torch
from torch.nn import functional as F

from . import reviewed_train as engine
from .ar_continuum_study import expand
from .ar_region_retention import filter_by_area
from .ar_scale_review import estimate, native_objects
from .ar_scale_study import BUDGET, ROOT, configuration, load_plan, warm_model
from .datasets import SegmentationDataset
from .durable import atomic_json
from .journal import append, read
from .metrics import binary_segmentation
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path
from .xrs_coverage import utc


ARMS = ('gray', 'hmi')
BASE_CODE = engine.code_identity
BASE_LOADER = engine.loader_for


def physical_plane(field):
    if field.shape != (1024, 1024):
        raise ValueError('物理磁图必须为已配准的1024网格')
    cells = field.reshape(512, 2, 512, 2)
    valid = np.isfinite(cells).all(axis=(1, 3))
    reduced = np.where(valid[:, None, :, None], cells, 0.).mean(axis=(1, 3))
    return (np.arcsinh(np.clip(reduced, -1000, 1000)/50) / np.arcsinh(20)).astype(np.float32)


def identity():
    names = ('ar_hmi_segmentation.py', 'ar_continuum_study.py', 'ar_scale_study.py', 'ar_scale_review.py',
             'ar_region_retention.py', 'ar_precision.py', 'ar_guard_train.py')
    return {**BASE_CODE(), **{name: digest(Path(__file__).with_name(name)) for name in names}}


class HmiDataset(SegmentationDataset):
    def __init__(self, directory, split, cohort, known, use_physical):
        super().__init__(directory, 'AR', split, 512)
        index = {row['uri']: row for row in self.rows}
        self.rows = [index[uri] for uri in known[split]]
        self.pairs = known[split]
        self.cohort = cohort
        self.use_physical = use_physical
        self.epoch = None
        self.checked = set()

    def __getitem__(self, index):
        item = super().__getitem__(index)
        record = self.pairs[item['uri']]
        use = self.use_physical and record['state'] == 'display_alignment_supported'
        if use and self.epoch is not None:
            key = f'{item["uri"]}|{self.epoch}|2026'.encode()
            use = int.from_bytes(hashlib.sha256(key).digest()[:4], 'little') % 4 != 0
        plane = torch.zeros_like(item['image'])
        if use:
            path = self.cohort / 'pairs' / record['id'] / 'aligned-los.npy'
            if path not in self.checked:
                if digest(path) != record['array_sha256']:
                    raise ValueError('配准磁图指纹不匹配')
                self.checked.add(path)
            plane = torch.from_numpy(physical_plane(np.load(path, allow_pickle=False)))[None]
        item['image'] = torch.cat((item['image'], plane), dim=0)
        item['physical_used'] = use
        return item


def setup(study, source, cohort):
    source, cohort = (Path(p).resolve() for p in (source, cohort))
    origin = load_plan(source)
    study = output_path(study, origin['dataset'])
    if any(study.is_relative_to(p) for p in (source, cohort)):
        raise ValueError('分割模型对照使用独立目录')
    report, index = (read(cohort / name) for name in ('summary.json', 'protocol.json'))
    if (report['state'] != 'completed' or report['test_accessed'] or report['calibration_accessed']
            or report['protocol_sha256'] != digest(cohort / 'protocol.json') or index['data'] != origin['data_identity']):
        raise ValueError('磁图队列与已冻结数据不符')
    known = {split: {} for split in ('train', 'validation')}
    for row in report['results']:
        if row['uri'] in known[row['split']]:
            raise ValueError('重复配对')
        known[row['split']][row['uri']] = row
    if [len(known[key]) for key in known] != [272, 102] or max(utc(r['time_utc']) for r in known['train'].values()) >= min(
        utc(r['time_utc']) for r in known['validation'].values()
    ):
        raise ValueError('正式试点必须保持同源队列和时间隔离')
    config = {**configuration(origin, 512), 'loss': 'bce_dice'}
    plan = {'source_sha256': digest(source / 'protocol.json'), 'cohort_sha256': digest(cohort / 'summary.json'),
            'code': identity(), 'epochs': BUDGET, 'input': 'Identical gray channel; clipped signed LOS transformed with asinh(B/50).',
            'missing_physical': 'Zero auxiliary, 25% deterministic training dropout on supported inputs.',
            'config': config, 'reference': 'Development only, paired ending checkpoint and native IoU≥0.5.',
            'gate': {'tiny_gain': 2, 'precision_max_drop': .01, 'recall_max_drop': .01},
            'test_accessed': False, 'calibration_accessed': False}
    study.mkdir(parents=True, exist_ok=True)
    protocol = study / 'protocol.json'
    if protocol.exists():
        if read(protocol) != plan:
            raise ValueError('分割对照协议或代码改变')
    else:
        atomic_json(protocol, plan)
    return origin, known, plan, study, cohort


def train(origin, known, plan, study, cohort, arm):
    config = {**plan['config'], 'id': f'AR-HMI-segmentation-{arm}-s2026', 'protocol_sha256': digest(study/'protocol.json')}
    run = study / arm / 'run'
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

    class TorchOptions:
        optim = SimpleNamespace(Adam=lambda p, **kw: torch.optim.Adam(p, **{**kw, 'lr': config['learning_rate']}),
                                lr_scheduler=torch.optim.lr_scheduler)

        def __getattr__(self, name):
            return getattr(torch, name)

    original = (engine.architecture, engine.code_identity, engine.SegmentationDataset, engine.loader_for, engine.torch)
    log = ROOT/'out/training-20261112-v2'
    append(log, 'development', '活动区物理磁图分割训练', f'{arm} 组开始，固定3轮同图对照。', run_id=config['id'])
    try:
        engine.architecture = lambda _: (model, {**details, 'input_channels': 2, 'arm': arm})
        engine.code_identity = identity
        engine.SegmentationDataset = lambda directory, category, split, size: HmiDataset(directory, split, cohort, known, arm == 'hmi')
        engine.loader_for = loader
        engine.torch = TorchOptions()
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        status = engine.train(config, origin['dataset'], run, resume=(run/'last.pt').exists())
        append(log, 'development', '活动区物理磁图分割保存', f'{arm} 组已保存 {status["completed_epoch"]}/3 轮。', run_id=config['id'])
        return config if status['state'] == 'completed' else None
    finally:
        engine.architecture, engine.code_identity, engine.SegmentationDataset, engine.loader_for, engine.torch = original


def review(origin, known, plan, study, cohort, configs):
    models, history, hashes = {}, {}, {}
    for arm, config in configs.items():
        path = study/arm/'run/last.pt'
        hashes[arm] = digest(path)
        state = engine.load_checkpoint(path)
        if (state['epoch'] != BUDGET or state['identity'] != read(path.parent/'identity.json')
                or state['identity']['config'] != config or state['identity']['code'] != plan['code']):
            raise ValueError('最后检查点不对应本次比较')
        model, _ = warm_model(origin, config)
        models[arm] = expand(model).eval()
        models[arm].load_state_dict(state['model'])
        history[arm] = state['history'][-1]['validation']['dice']
    starting, _ = warm_model(origin, plan['config'])
    models['starting'] = starting.eval()
    dataset = HmiDataset(origin['dataset'], 'validation', cohort, known, True)
    names = ('starting', 'gray', 'hmi', 'hmi_without_input')
    rows = []
    with exclusive(ROOT/'.mdva/training-gpu.lock'):
        engine.seed_all(2026)
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        resource = Resources()
        for model in models.values():
            model.cuda()
        with torch.inference_mode():
            for index, row in enumerate(dataset.rows):
                item = dataset[index]
                pair = item['image'][None].cuda()
                native = np.load(dataset.source(row['mask_uri']), allow_pickle=False)
                result = {'uri': row['uri'], 'lane': known['validation'][row['uri']]['lane'],
                          'physical_used': item['physical_used'], 'arms': {}}
                for name in names:
                    model = models['hmi' if name == 'hmi_without_input' else name]
                    with torch.autocast('cuda', dtype=torch.float16):
                        logits = model(pair if name == 'hmi' else pair[:, :1])
                    if not torch.isfinite(logits).all():
                        raise ValueError('分割推理结果非有限')
                    prob = F.interpolate(logits.float().sigmoid(), size=native.shape, mode='bilinear', align_corners=False)[0, 0].cpu().numpy()
                    mask = filter_by_area(prob >= .4, 64)
                    result['arms'][name] = {'objects': native_objects(mask, native), 'pixels': binary_segmentation(prob >= .5, native > 0)}
                rows.append(result)
                if index % 16 == 0:
                    resource.sample()
        measured = resource.sample()
    groups = {'all': rows, **{lane: [r for r in rows if r['lane'] == lane] for lane in ('development', 'small_region_stress')}}
    results = {group: {arm: estimate(records, arm) for arm in names} for group, records in groups.items()}
    full = results['all']
    replay = {arm: full[arm]['pixels']['dice']-history[arm] for arm in ARMS}
    if any(abs(error) > 1e-5 for error in replay.values()):
        raise ValueError(f'检查点重放指标不一致：{replay}')
    hmi, control = full['hmi'], full['gray']
    tiny_gain = hmi['by_reference_size']['under256']['matched']-control['by_reference_size']['under256']['matched']
    passed = (tiny_gain >= plan['gate']['tiny_gain'] and hmi['region_precision'] >= control['region_precision']-.01
              and hmi['region_recall'] >= control['region_recall']-.01)
    atomic_json(study/'images.json', rows)
    report = {'state': 'completed', 'created_utc': now(), 'results': results, 'tiny_gain_vs_control': tiny_gain,
              'pilot_gate_passed': passed, 'checkpoint_sha256': hashes, 'replay_delta': replay, 'resource': measured,
              'test_accessed': False, 'calibration_accessed': False, 'deployed': False,
              'images_sha256': digest(study/'images.json'), 'protocol_sha256': digest(study/'protocol.json')}
    atomic_json(study/'summary.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('study', 'source', 'cohort'):
        parser.add_argument('--'+key, required=True)
    args = parser.parse_args()
    configure_environment()
    origin, known, plan, study, cohort = setup(**vars(args))
    configs = {}
    for arm in ARMS:
        configs[arm] = train(origin, known, plan, study, cohort, arm)
        if configs[arm] is None:
            return
    result = review(origin, known, plan, study, cohort, configs)
    print(json.dumps({key: result[key] for key in ('state', 'tiny_gain_vs_control', 'pilot_gate_passed')}))


if __name__ == '__main__':
    main()
