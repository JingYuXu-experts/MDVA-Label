"""Compare weaker station oversampling with the original eight-epoch FL pilot."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

import torch
from torch.utils.data import DataLoader, WeightedRandomSampler

from . import fl_main
from .durable import atomic_json
from .runtime import digest, now, output_path

BASE_IDENTITY = fl_main.identity
BASE_LOADER = fl_main.engine.loader_for


def identity():
    return {**BASE_IDENTITY(), Path(__file__).name:digest(__file__)}


def loader_for(dataset, config, epoch, micro_batch):
    counts = Counter(row['instrument'] for row in dataset.rows)
    weights = [counts[row['instrument']] ** -.5 for row in dataset.rows]
    generator = torch.Generator().manual_seed(config['seed'] + epoch * 100003)
    sampler = WeightedRandomSampler(weights, len(weights), replacement=True, generator=generator)
    return DataLoader(dataset, batch_size=micro_batch, sampler=sampler,
                      generator=generator, num_workers=0, pin_memory=torch.cuda.is_available())


def prepare(control, coverage, study):
    control = Path(control).resolve()
    control_file = control/'protocol.json'
    base = json.loads(control_file.read_text(encoding='utf-8'))
    coverage = Path(coverage).resolve()
    review = json.loads(coverage.read_text(encoding='utf-8'))
    if (base['code'] != BASE_IDENTITY() or base['first_budget_epochs'] != 8
            or review['protocol_sha256'] != digest(control_file) or review['state'] != 'completed'):
        raise ValueError('原试点与采样证据身份不符')
    study = output_path(study, base['dataset'])
    if study.exists() or study.is_relative_to(control):
        raise ValueError('采样对照必须保存在独立新目录')
    config = {**base['config'], 'id':'FL-author-BN-w10-half-sampler-s2026'}
    plan = {**base, 'created_utc':now(), 'config':config, 'code':identity(), 'sampling':'half_inverse',
            'control':str(control), 'control_protocol_sha256':digest(control_file), 'coverage_evidence_sha256':digest(coverage),
            'rationale':'Change only sampling weights per image from count^-1 to count^-0.5. Same from-scratch initialization, data, objective, schedule, epochs, and number of draws.',
            'comparison':'At fixed8 epochs, compare last and best with matched control on the same182 development images. Report per-station Dice and region precision/recall. No test or release.'}
    study.mkdir(parents=True)
    atomic_json(study/'protocol.json', plan)
    return plan


def train(study):
    plan = json.loads((Path(study)/'protocol.json').read_text(encoding='utf-8'))
    control = Path(plan['control'])
    if plan['sampling'] != 'half_inverse' or plan['control_protocol_sha256'] != digest(control/'protocol.json'):
        raise ValueError('采样对照身份改变')
    state = json.loads((control/'run/status.json').read_text(encoding='utf-8'))
    if state['completed_epoch'] != 8 or state['state'] not in ('paused','completed'):
        raise ValueError('等待对照组保存第8轮和释放GPU')
    fl_main.engine.loader_for = loader_for
    fl_main.identity = identity
    try:
        return fl_main.train(study)
    finally:
        fl_main.engine.loader_for = BASE_LOADER
        fl_main.identity = BASE_IDENTITY


if __name__ == '__main__':
    from .runtime import configure_environment
    configure_environment()
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('prepare','train'))
    parser.add_argument('--control')
    parser.add_argument('--coverage')
    parser.add_argument('--study', required=True)
    args = parser.parse_args()
    result = prepare(args.control,args.coverage,args.study) if args.action=='prepare' else train(args.study)
    print(json.dumps({'state':result.get('state','prepared')}))
