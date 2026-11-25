"""Controlled AR warm-start study at 512 and native 1024 resolution."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from types import SimpleNamespace

import torch
from safetensors.torch import load_file

from . import reviewed_train as engine
from .datasets import SegmentationDataset
from .durable import atomic_json
from .journal import append, read
from .run_locations import model_and_identity
from .runtime import configure_environment, digest, now, output_path


ROOT = Path(__file__).resolve().parents[3]
SIZES = (512, 1024)
STRATA = ('under256', '256to1023', '1024plus')
BUDGET = 3
BASE_IDENTITY = engine.code_identity
BASE_VALIDATE = engine.validate_config


def identity():
    names = ('ar_guard_train.py', 'ar_precision.py', 'ar_scale_study.py', 'run_locations.py')
    return {**BASE_IDENTITY(), **{name: digest(Path(__file__).with_name(name)) for name in names}}


def cohort_rows(data, cohort):
    available = {row['uri']: row for row in data.rows}
    if len(cohort) != 384 or len({row['uri'] for row in cohort}) != 384:
        raise ValueError('活动区队列应为384张不同的冻结训练图')
    if Counter(row['trial_stratum'] for row in cohort) != Counter(dict.fromkeys(STRATA, 128)):
        raise ValueError('活动区队列的三个分层应各包含128张图')
    selected = []
    for row in cohort:
        source = available.get(row['uri'])
        if not source or any(source[key] != row[key] for key in ('mask_uri', 'time_utc')):
            raise ValueError('队列存在非训练样本、时间错配或原生掩膜身份不符')
        selected.append(source)
    return selected


def package_info(package):
    path = package / 'model.json'
    weights = package / 'weights.safetensors'
    card = read(path)
    if (card['id'], card['category'], card['config']['size'], card['threshold']) != ('AR-A0-s42', 'AR', 512, .5):
        raise ValueError('只允许登记的共同起点')
    constructor, training_identity = model_and_identity(card['config'])
    if card['training_code'] != training_identity() or card['weights_sha256'] != digest(weights):
        raise ValueError('模型权重或训练代码身份不符')
    return card, constructor, weights


def prepare(study, dataset, package, inputs):
    dataset, package, inputs = (Path(path).resolve() for path in (dataset, package, inputs))
    study = output_path(study, dataset)
    if study.exists() or any(study.is_relative_to(path) for path in (package, inputs)):
        raise ValueError('配对试验必须使用新目录')
    card, _, weights = package_info(package)
    cohort_path = inputs / 'fixed-training-cohort-tiny-balanced-01.json'
    preflight_path = inputs / 'resolution-resource-preflight-01.json'
    preflight = read(preflight_path)
    if (
        preflight['state'] != 'completed' or preflight['test_accessed'] or preflight['model_updated']
        or preflight['cohort_sha256'] != digest(cohort_path)
        or preflight['weight_sha256'] != digest(weights)
        or preflight['model_card_sha256'] != digest(package / 'model.json')
        or any(len(preflight['measurements'][str(side)]) != 3
               or not all(row['gradient_finite'] for row in preflight['measurements'][str(side)])
               for side in SIZES)
    ):
        raise ValueError('显卡试跑和训练队列身份不匹配')
    cohort = read(cohort_path)
    cohort_rows(SegmentationDataset(dataset, 'AR', 'train'), cohort)
    validation = SegmentationDataset(dataset, 'AR', 'validation')
    if len(validation) != 276:
        raise ValueError('必须在完整开发验证队列比较')
    plan = {
        'state': 'frozen', 'created_utc': now(), 'code': identity(),
        'dataset': str(dataset), 'data_identity': engine.data_identity(dataset),
        'package': str(package), 'model_card_sha256': digest(package / 'model.json'),
        'weights_sha256': digest(weights), 'cohort_sha256': digest(cohort_path),
        'preflight_sha256': digest(preflight_path), 'sizes': list(SIZES), 'budget': BUDGET,
        'config': {**card['config'], 'seed': 2026, 'micro_batch': 1, 'effective_batch': 8,
                   'max_epochs': BUDGET, 'min_epochs': BUDGET, 'patience': 12,
                   'augment': False, 'station_balance': False, 'learning_rate': 1e-4},
        'scope': 'Same384 training and276 development validation images. Same optimizer, order and warm weights.',
        'decision': {'small_recall_gain': .05, 'overall_precision_drop': .02,
                     'overall_recall_drop': .01, 'dice_drop': .02},
        'matching': '4-connected candidate objects, original reference IDs, native-grid mask IoU >= 0.5',
        'test_accessed': False, 'calibration_accessed': False,
    }
    study.mkdir(parents=True)
    atomic_json(study / 'cohort.json', cohort)
    plan['stored_cohort_sha256'] = digest(study / 'cohort.json')
    atomic_json(study / 'protocol.json', plan)
    return plan


def load_plan(study):
    plan = read(study / 'protocol.json')
    if (
        plan['code'] != identity() or plan['data_identity'] != engine.data_identity(plan['dataset'])
        or plan['stored_cohort_sha256'] != digest(study / 'cohort.json')
        or plan['sizes'] != list(SIZES) or plan['budget'] != BUDGET
    ):
        raise ValueError('配对协议、数据或代码身份已改变')
    return plan


def configuration(plan, side):
    return {**plan['config'], 'size': side, 'id': f'AR-scale-{side}-s2026',
            'study_cohort_sha256': plan['stored_cohort_sha256']}


def warm_model(plan, config):
    package = Path(plan['package'])
    card, constructor, weights = package_info(package)
    if card['weights_sha256'] != plan['weights_sha256'] or digest(package / 'model.json') != plan['model_card_sha256']:
        raise ValueError('起点模型包发生变化')
    model, provenance = constructor(config)
    model.load_state_dict(load_file(str(weights)))
    if not all(torch.isfinite(parameter).all() for parameter in model.parameters()):
        raise ValueError('起点权重含非有限参数')
    return model, {**provenance, 'input_side': config['size'], 'warm_weights': card['weights_sha256']}


def train(study, side):
    study = Path(study).resolve()
    plan = load_plan(study)
    run = study / str(side) / 'run'
    if (run / 'PAUSE').exists():
        raise ValueError('暂停标记仍在，先处理显式恢复')
    if side == 1024:
        control = read(study / '512/run/status.json', {})
        if control.get('state') != 'completed' or control.get('completed_epoch') != BUDGET:
            raise ValueError('先完成512控制组')
    status = read(run / 'status.json', {})
    if status.get('state') == 'completed' and status.get('completed_epoch') == BUDGET:
        return status
    config = configuration(plan, side)
    cohort = read(study / 'cohort.json')
    # Resolve the historical package before temporarily adapting the shared trainer.
    model, provenance = warm_model(plan, config)

    def dataset_factory(dirname, category, split, size):
        dataset = SegmentationDataset(dirname, category, split, size)
        if split == 'train':
            dataset.rows = cohort_rows(dataset, cohort)
        return dataset

    def validate(spec):
        if spec != config or spec['size'] not in SIZES or spec['category'] != 'AR':
            raise ValueError('请使用已冻结的活动区配对配置')
        BASE_VALIDATE({**spec, 'size': 512})

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
                engine.validate_config, engine.torch)
    log = ROOT / 'out/training-20261112-v2'
    append(log, 'development', '活动区分辨率配对训练',
           f'{side} 像素组启动，固定3轮，同一训练队列和起点。', run_id=config['id'])
    try:
        engine.architecture = lambda spec: (model, provenance)
        engine.code_identity = identity
        engine.SegmentationDataset = dataset_factory
        engine.validate_config = validate
        engine.torch = TorchOptions()
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        result = engine.train(config, plan['dataset'], run, resume=(run / 'last.pt').exists())
        append(log, 'development', '活动区分辨率配对训练进度',
               f'{side} 像素组保存 {result["completed_epoch"]}/3 轮，状态 {result["state"]}。',
               run_id=config['id'], epoch=result['completed_epoch'])
        return result
    finally:
        (engine.architecture, engine.code_identity, engine.SegmentationDataset,
         engine.validate_config, engine.torch) = original


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'train'))
    parser.add_argument('--study', required=True)
    parser.add_argument('--dataset')
    parser.add_argument('--package')
    parser.add_argument('--inputs')
    parser.add_argument('--arm', type=int, choices=SIZES)
    args = parser.parse_args()
    configure_environment()
    if args.action == 'prepare':
        if not all((args.dataset, args.package, args.inputs)):
            parser.error('prepare 需要数据、模型包和输入审计')
        result = prepare(args.study, args.dataset, args.package, args.inputs)
    else:
        if args.arm is None:
            parser.error('train 需要指定分辨率')
        result = train(args.study, args.arm)
    print(json.dumps({key: result[key] for key in ('state', 'completed_epoch', 'budget') if key in result}))


if __name__ == '__main__':
    main()
