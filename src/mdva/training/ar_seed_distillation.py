"""Fixed-budget transfer of AR seed consensus into the existing single-model architecture."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import torch
from torch.nn import functional as F

from . import reviewed_train as engine
from .ar_region_retention import filter_by_area
from .ar_scale_review import estimate, native_objects
from .ar_scale_study import BUDGET, ROOT, cohort_rows, configuration, load_plan, warm_model
from .ar_seed_consensus import MODELS, PIXEL_THRESHOLD
from .datasets import SegmentationDataset
from .durable import atomic_json, commit
from .journal import append, read
from .metrics import binary_segmentation
from .run_locations import model_and_identity, run_folder
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


EXPERIMENT = ROOT / 'out/training-20261112-v2'
BASE_CODE = engine.code_identity
BASE_LOSS = engine.loss_value
MIN_TEACHER_DELTA = .10
LOSS_WEIGHT = .25


def identity():
    files = ('ar_scale_study.py', 'ar_seed_consensus.py', 'ar_seed_distillation.py', 'run_locations.py')
    return {**BASE_CODE(), **{name: digest(Path(__file__).with_name(name)) for name in files}}


def guide(probabilities, truth):
    """Keep a soft target only for material disagreements corroborated by the training reference."""
    vote = (probabilities >= PIXEL_THRESHOLD).sum(dim=0) >= 2
    mean = probabilities.mean(dim=0)
    changed = vote != (probabilities[0] >= PIXEL_THRESHOLD)
    supported = (vote == (truth > 0)) & (vote == (mean >= PIXEL_THRESHOLD))
    trust = changed & supported & ((mean - probabilities[0]).abs() >= MIN_TEACHER_DELTA)
    return torch.where(trust, mean, -1.)


def training_loss(logits, target, kind):
    if target.shape[1] == 1:
        return BASE_LOSS(logits, target, kind)
    truth, teacher = target.split(1, dim=1)
    supported = teacher >= 0
    penalty = F.binary_cross_entropy_with_logits(logits.float(), teacher.float().clamp_min(0), reduction='none')
    return BASE_LOSS(logits, truth, kind) + LOSS_WEIGHT * (penalty * supported).sum() / supported.sum().clamp_min(1)


def prepare(study, reference, consensus):
    reference, consensus = (Path(p).resolve() for p in (reference, consensus))
    origin = load_plan(reference)
    study = output_path(study, origin['dataset'])
    if study.exists() or any(study.is_relative_to(path) for path in (reference, consensus)):
        raise ValueError('知识迁移使用新的独立目录')
    evidence, teachers = (read(consensus / name) for name in ('summary.json', 'protocol.json'))
    if (evidence['state'] != 'completed' or evidence['test_accessed'] or evidence['calibration_accessed']
            or evidence['protocol_sha256'] != digest(consensus / 'protocol.json')
            or set(teachers['models']) != set(MODELS)):
        raise ValueError('投票和原模型身份未核验')
    control = reference / '512/run'
    status = read(control / 'status.json', {})
    if (status.get('state'), status.get('completed_epoch')) != ('completed', BUDGET):
        raise ValueError('原单模型控制未完成同样预算')
    checkpoints = {model: teachers['models'][model] for model in MODELS}
    for model, sha in checkpoints.items():
        if digest(run_folder(EXPERIMENT, model) / 'best.pt') != sha:
            raise ValueError('教师不对应已复核检查点')
    config = {**configuration(origin, 512), 'id': 'AR-A0-seed-transfer-s2026'}
    plan = {'state': 'frozen', 'created_utc': now(), 'code': identity(), 'data': origin['data_identity'],
            'dataset': origin['dataset'], 'reference': str(reference), 'reference_sha256': digest(reference / 'protocol.json'),
            'cohort_sha256': digest(reference / 'cohort.json'), 'control_sha256': digest(control / 'last.pt'),
            'consensus_sha256': digest(consensus / 'summary.json'), 'teachers': checkpoints, 'config': config,
            'guidance': {'loss_weight': LOSS_WEIGHT, 'min_probability_delta': MIN_TEACHER_DELTA,
                         'voting_threshold': PIXEL_THRESHOLD, 'target': 'reference-corroborated changes only'},
            'review': {'area_floors_native': [256, 64], 'primary': 256, 'secondary': 64,
                       'minimum_gain_regions': 10, 'maximum_additional_false_regions': 0},
            'test_accessed': False, 'calibration_accessed': False, 'teachers_saw_cohort_in_training': True}
    study.mkdir(parents=True)
    atomic_json(study / 'protocol.json', plan)
    return plan


def open_study(study):
    plan = read(study / 'protocol.json')
    reference = Path(plan['reference'])
    origin = load_plan(reference)
    if (plan['code'] != identity() or plan['data'] != origin['data_identity']
            or plan['reference_sha256'] != digest(reference / 'protocol.json')
            or plan['cohort_sha256'] != digest(reference / 'cohort.json')
            or plan['control_sha256'] != digest(reference / '512/run/last.pt')):
        raise ValueError('知识迁移的协议或控制模型已改变')
    return plan, origin, read(reference / 'cohort.json')


def cached_guide(folder, index, uri, protocol):
    path = folder / f'{index:04}.npy'
    meta = read(path.with_suffix('.json'), {})
    if meta:
        if meta['uri'] != uri or meta['protocol_sha256'] != protocol or meta['sha256'] != digest(path):
            raise ValueError('训练指导缓存身份发生变化')
        return path, meta
    return path, None


def setup_cuda(seed):
    engine.seed_all(seed)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    torch.cuda.set_per_process_memory_fraction(.75)
    torch.cuda.reset_peak_memory_stats()
    return Resources()


def cache(study):
    plan, _, cohort = open_study(study)
    protocol = digest(study / 'protocol.json')
    train_data = SegmentationDataset(plan['dataset'], 'AR', 'train', 512)
    train_data.rows = cohort_rows(train_data, cohort)
    folder = study / 'guide'
    folder.mkdir(exist_ok=True)
    teachers, records = [], []
    with exclusive(ROOT / '.mdva/training-gpu.lock'):
        resource = setup_cuda(plan['config']['seed'])
        for model in MODELS:
            path = run_folder(EXPERIMENT, model) / 'best.pt'
            if digest(path) != plan['teachers'][model]:
                raise ValueError('教师权重更改')
            saved = torch.load(path, map_location='cpu', weights_only=True)
            config = saved['identity']['config']
            constructor, version = model_and_identity(config)
            if ((config['id'], config['category'], config['size']) != (model, 'AR', 512)
                    or saved['identity']['data'] != plan['data'] or saved['identity']['code'] != version()):
                raise ValueError(f'教师架构或训练清单身份不符：{model}')
            net, _ = constructor(config)
            net.load_state_dict(saved['model'])
            teachers.append(net.cuda().eval())
        with torch.inference_mode():
            for index, row in enumerate(train_data.rows):
                if (study / 'PAUSE').exists():
                    return {'state': 'paused', 'cached_images': len(records)}
                path, existing = cached_guide(folder, index, row['uri'], protocol)
                if existing:
                    records.append(existing)
                    continue
                item = train_data[index]
                x = item['image'][None].cuda()
                with torch.autocast('cuda', dtype=torch.float16):
                    logits = torch.stack([teacher(x)[0] for teacher in teachers])
                if not torch.isfinite(logits).all():
                    raise ValueError('训练指导教师产生非有限输出')
                target = guide(logits.float().sigmoid(), item['semantic_mask'].cuda()).cpu().numpy().astype(np.float16)
                mask = target >= 0
                commit(path, lambda file: np.save(file, target, allow_pickle=False))
                record = {'uri': row['uri'], 'sha256': digest(path), 'protocol_sha256': protocol,
                          'supported_pixels': int(mask.sum()),
                          'supported_positive_pixels': int(np.count_nonzero(mask & (item['semantic_mask'].numpy() > 0)))}
                atomic_json(path.with_suffix('.json'), record)
                records.append(record)
                if not index % 32:
                    resource.sample()
        measured = resource.sample()
    counts = {'support_images': sum(record['supported_pixels'] > 0 for record in records),
              'positive_support_pixels': sum(record['supported_positive_pixels'] for record in records),
              'supported_pixels': sum(record['supported_pixels'] for record in records)}
    atomic_json(folder / 'rows.json', records)
    result = {'state': 'completed', 'cached_images': len(records), 'protocol_sha256': protocol,
              'rows_sha256': digest(folder / 'rows.json'), **counts, 'resource': measured}
    atomic_json(folder / 'summary.json', result)
    return result


def train(study):
    plan, origin, cohort = open_study(study)
    run = study / 'run'
    if (study / 'PAUSE').exists() or (run / 'PAUSE').exists():
        raise ValueError('显式暂停仍在')
    folder = study / 'guide'
    cached = read(folder / 'summary.json')
    sha = digest(study / 'protocol.json')
    if (cached['state'] != 'completed' or cached['protocol_sha256'] != sha
            or cached['rows_sha256'] != digest(folder / 'rows.json')
            or cached['cached_images'] != 384 or not cached['supported_pixels']):
        raise ValueError('先完成并核验训练指导缓存')
    targets = read(folder / 'rows.json')
    config = {**plan['config'], 'teacher_cache_sha256': cached['rows_sha256']}
    model, provenance = warm_model(origin, config)

    class GuidedDataset(SegmentationDataset):
        def __init__(self, directory, category, split, size):
            super().__init__(directory, category, split, size)
            self.guided = split == 'train'
            if self.guided:
                self.rows = cohort_rows(self, cohort)
                for index, row in enumerate(self.rows):
                    if cached_guide(folder, index, row['uri'], sha)[1] != targets[index]:
                        raise ValueError('训练目标无法复验')

        def __getitem__(self, index):
            item = super().__getitem__(index)
            if self.guided:
                target = np.load(folder / f'{index:04}.npy', allow_pickle=False)
                item['semantic_mask'] = torch.cat((item['semantic_mask'], torch.from_numpy(target.astype(np.float32))))
            return item

    class TorchOptions:
        optim = SimpleNamespace(
            Adam=lambda params, **kwargs: torch.optim.Adam(params, **{**kwargs, 'lr': config['learning_rate']}),
            lr_scheduler=torch.optim.lr_scheduler,
        )

        def __getattr__(self, name):
            return getattr(torch, name)

    original = (engine.architecture, engine.code_identity, engine.SegmentationDataset, engine.loss_value, engine.torch)
    append(EXPERIMENT, 'development', '活动区投票知识迁移', '固定384张训练图和3轮预算，仅增加教师引导项。', run_id=config['id'])
    try:
        engine.architecture = lambda _: (model, provenance)
        engine.code_identity = identity
        engine.SegmentationDataset = GuidedDataset
        engine.loss_value = training_loss
        engine.torch = TorchOptions()
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        state = engine.train(config, plan['dataset'], run, resume=(run / 'last.pt').exists())
        append(EXPERIMENT, 'development', '活动区投票迁移检查点',
               f'已保存 {state["completed_epoch"]}/{BUDGET} 轮，状态 {state["state"]}。',
               run_id=config['id'], epoch=state['completed_epoch'])
        return state
    finally:
        (engine.architecture, engine.code_identity, engine.SegmentationDataset, engine.loss_value, engine.torch) = original


def review(study, output):
    plan, origin, _ = open_study(study)
    output = output_path(output, plan['dataset'])
    if output.exists() or output.is_relative_to(study):
        raise ValueError('开发评价使用新目录')
    data = SegmentationDataset(plan['dataset'], 'AR', 'validation', 512)
    paths = {'control': Path(plan['reference']) / '512/run/last.pt', 'transfer': study / 'run/last.pt'}
    states, hashes = {}, {}
    for name, path in paths.items():
        hashes[name] = digest(path)
        states[name] = engine.load_checkpoint(path)
        if (states[name]['epoch'] != BUDGET or read(path.parent / 'status.json')['state'] != 'completed'
                or states[name]['identity'] != read(path.parent / 'identity.json') or digest(path) != hashes[name]):
            raise ValueError('需同等预算完整检查点')
    if states['transfer']['identity']['code'] != identity():
        raise ValueError('知识迁移的代码身份不符')
    models = {}
    rows = []
    with exclusive(ROOT / '.mdva/training-gpu.lock'):
        resource = setup_cuda(plan['config']['seed'])
        for name, saved in states.items():
            model, _ = warm_model(origin, saved['identity']['config'])
            model.load_state_dict(saved['model'])
            models[name] = model.cuda().eval()
        with torch.inference_mode():
            for index, source in enumerate(data.rows):
                item = data[index]['image'][None].cuda()
                reference = np.load(data.source(source['mask_uri']), allow_pickle=False)
                scores = {}
                for name, model in models.items():
                    with torch.autocast('cuda', dtype=torch.float16):
                        logits = model(item)
                    if not torch.isfinite(logits).all():
                        raise ValueError('开发评价存在非有限输出')
                    p = F.interpolate(logits.float().sigmoid(), size=reference.shape, mode='bilinear',
                                      align_corners=False)[0, 0].cpu().numpy()
                    scores[f'{name}-historical'] = {'pixels': binary_segmentation(p >= .5, reference > 0),
                                                    'objects': native_objects(p >= .5, reference)}
                    for floor in plan['review']['area_floors_native']:
                        mask = filter_by_area(p >= PIXEL_THRESHOLD, floor)
                        scores[f'{name}-{floor}'] = {'pixels': binary_segmentation(mask, reference > 0),
                                                     'objects': native_objects(mask, reference)}
                rows.append({'uri': source['uri'], 'time_utc': source['time_utc'], 'arms': scores})
                if not index % 32:
                    resource.sample()
        measured = resource.sample()
    estimates = {key: estimate(rows, key) for key in rows[0]['arms']}
    replay = {name: estimates[f'{name}-historical']['pixels']['dice'] - saved['history'][-1]['validation']['dice']
              for name, saved in states.items()}
    if any(abs(value) > 1e-5 for value in replay.values()):
        raise ValueError(f'原阈值评价未复现：{replay}')
    changes = {}
    for floor in plan['review']['area_floors_native']:
        control, transfer = (estimates[f'{name}-{floor}'] for name in ('control', 'transfer'))
        fields = ('region_precision', 'region_recall', 'matched_regions', 'false_positive_regions')
        changes[str(floor)] = {key: transfer[key] - control[key] for key in fields}
    primary = changes[str(plan['review']['primary'])]
    passes = (primary['matched_regions'] >= plan['review']['minimum_gain_regions']
              and primary['false_positive_regions'] <= plan['review']['maximum_additional_false_regions'])
    output.mkdir(parents=True)
    atomic_json(output / 'images.json', rows)
    result = {'state': 'completed', 'images': len(rows), 'results': estimates, 'changes': changes,
              'pilot_gate_passed': passes, 'historical_dice_replay_error': replay, 'resource': measured,
              'checkpoint_sha256': hashes, 'images_sha256': digest(output / 'images.json'),
              'protocol_sha256': digest(study / 'protocol.json'), 'released': False,
              'test_accessed': False, 'calibration_accessed': False}
    atomic_json(output / 'summary.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'cache', 'train', 'review'))
    parser.add_argument('--study', type=Path, required=True)
    parser.add_argument('--reference', type=Path)
    parser.add_argument('--consensus', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    configure_environment()
    if args.action == 'prepare':
        if args.reference is None or args.consensus is None:
            parser.error('prepare 需要 --reference 和 --consensus')
        result = prepare(args.study, args.reference, args.consensus)
    elif args.action == 'review':
        if args.output is None:
            parser.error('review 需要 --output')
        result = review(args.study, args.output)
    else:
        result = {'cache': cache, 'train': train}[args.action](args.study)
    print(json.dumps({key: result[key] for key in ('state', 'changes', 'cached_images', 'completed_epoch',
                                                    'pilot_gate_passed', 'supported_pixels') if key in result}))


if __name__ == '__main__':
    main()
