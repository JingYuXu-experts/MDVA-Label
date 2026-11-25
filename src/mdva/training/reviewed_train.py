# Engineering revision of train.py: durable writes and resume-before-epoch early-stop.
# Model equations, optimizer, sampling, RNG and epoch update order remain frozen.
# New code identity and independent revision directories are mandatory.
"""Resumable, bounded-memory solar segmentation training.

The checkpoint is the authoritative epoch transaction, including metric history.
An interrupted epoch is replayed from its previous complete checkpoint.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
from pathlib import Path
import random
import signal
import time

import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader, WeightedRandomSampler

from .datasets import SegmentationDataset
from .metrics import binary_segmentation, segmentation_summary, object_scores
from .stable_models import architecture as stable_architecture
from .runtime import (Resources, ResourceLimit, atomic_json, code_identity, digest,
                      exclusive, now, output_path)
from .durable import atomic_json, early_stop_met
from .runtime import code_identity as frozen_code_identity

def code_identity():
    return {**frozen_code_identity(), **{n: digest(Path(__file__).parent/n) for n in
        ("cnn193_fp32.py", "durable.py", "reviewed_train.py")}}

def architecture(config):
    if config.get("cnn193_compute") == "fp32":
        from .cnn193_fp32 import architecture as cnn_architecture
        return cnn_architecture(config)
    return stable_architecture(config)


def seed_all(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    torch.use_deterministic_algorithms(True)
    torch.set_num_threads(4)


def rng_state():
    return {'python': random.getstate(), 'numpy': np.random.get_state(),
            'torch': torch.get_rng_state(),
            'cuda': torch.cuda.get_rng_state_all() if torch.cuda.is_available() else []}


def restore_rng(state):
    random.setstate(state['python'])
    np.random.set_state(state['numpy'])
    torch.set_rng_state(state['torch'].cpu())
    if state['cuda'] and torch.cuda.is_available():
        torch.cuda.set_rng_state_all([s.cpu() for s in state['cuda']])


def loss_value(logits, target, kind):
    bce = nn.functional.binary_cross_entropy_with_logits(logits.float(), target.float())
    if kind == 'bce':
        return bce
    if kind != 'bce_dice':
        raise ValueError('损失仅支持 bce 或 bce_dice')
    p, y = logits.float().sigmoid().flatten(1), target.float().flatten(1)
    dice = 1 - ((2*(p*y).sum(1)+1)/(p.sum(1)+y.sum(1)+1)).mean()
    return .5*bce + .5*dice


def atomic_checkpoint(path, data):
    from .durable import atomic_checkpoint as commit
    commit(path, data)

def load_checkpoint(path):
    # Only local, self-produced run files are accepted. Public model packages use safetensors.
    return torch.load(path, map_location='cpu', weights_only=False)


def data_identity(dataset):
    return {name: digest(Path(dataset)/name) for name in
            ('manifest-hashes.json', 'sources.json', 'segmentation-ready.jsonl')}


def validate_config(config):
    required = {'id', 'category', 'model', 'width', 'size', 'loss', 'seed'}
    if not required <= config.keys():
        raise ValueError('训练配置缺少必要字段')
    if config['category'] not in ('AR', 'CH', 'FL'):
        raise ValueError('未知太阳任务')
    if config['size'] not in (256, 512) or config['width'] not in (16, 24, 32, 64):
        raise ValueError('不在预注册的分辨率或宽度范围')
    if config['category'] == 'AR' and config.get('augment'):
        raise ValueError('AR 首轮禁止光度增强')
    if config.get('station_balance') and config['category'] != 'FL':
        raise ValueError('台站均衡仅适用于FL协议')
    if config.get('effective_batch', 8) % config.get('micro_batch', 2):
        raise ValueError('有效 batch 必须整除 micro-batch')


def loader_for(dataset, config, epoch, micro_batch):
    generator = torch.Generator().manual_seed(config['seed'] + epoch * 100003)
    sampler = None
    if config.get('station_balance'):
        from collections import Counter
        counts = Counter(r['instrument'] for r in dataset.rows)
        weights = [1/counts[r['instrument']] for r in dataset.rows]
        sampler = WeightedRandomSampler(weights, len(weights), replacement=True, generator=generator)
    return DataLoader(dataset, batch_size=micro_batch, shuffle=sampler is None,
                      sampler=sampler, num_workers=0, generator=generator,
                      pin_memory=torch.cuda.is_available())


@torch.no_grad()
def evaluate(model, dataset, config, device='cuda', boundary=False, resource=None):
    model.eval()
    rows, losses = [], []
    for index, row in enumerate(dataset.rows):
        item = dataset[index]
        x, y = item['image'][None].to(device), item['semantic_mask'][None].to(device)
        with torch.autocast(device, dtype=torch.float16, enabled=device == 'cuda'):
            logits = model(x)
            loss = loss_value(logits, y, config['loss'])
        if not torch.isfinite(logits).all() or not torch.isfinite(loss):
            raise RuntimeError(f"验证出现非有限数值，禁止将NaN当背景或写为完整epoch：{row['uri']}")
        native_mask = np.load(dataset.source(row['mask_uri']), allow_pickle=False)
        reference = native_mask > 0
        probability = nn.functional.interpolate(logits.float().sigmoid(), size=reference.shape,
                                               mode='bilinear', align_corners=False)[0, 0].cpu().numpy()
        scores = binary_segmentation(probability >= .5, reference, boundary=boundary)
        if boundary and config['category'] == 'AR':
            scores['objects'] = object_scores(probability >= .5, native_mask)
        scores.update(uri=row['uri'], time_utc=row['time_utc'], instrument=row['instrument'],
                      split=row['split'])
        rows.append(scores)
        losses.append(float(loss))
        if resource and index % 32 == 0:
            resource.sample()
    summary = segmentation_summary(rows)
    summary['loss'] = float(np.mean(losses)) if losses else None
    return summary, rows


def train(config, dataset, run, resume=False, device='cuda', stop_after=None, diagnostic_limit=None):
    validate_config(config)
    if device != 'cuda' and diagnostic_limit is None:
        raise ValueError('正式识别训练使用已验收的 CUDA 环境')
    dataset, run = Path(dataset).resolve(), output_path(run, dataset)
    run.mkdir(parents=True, exist_ok=True)
    identity = {'config': config, 'data': data_identity(dataset), 'code': code_identity(),
                'diagnostic_limit': diagnostic_limit, 'device': device}
    lockroot = Path(__file__).resolve().parents[3] / '.mdva'
    with exclusive(lockroot/'training-gpu.lock' if device == 'cuda' else run/'run.lock'):
        if (run/'identity.json').exists():
            old = json.loads((run/'identity.json').read_text(encoding='utf-8'))
            if old != identity:
                raise ValueError('配置、代码或数据版本不同，请建立新实验编号')
            if not resume and (run/'last.pt').exists():
                raise ValueError('实验已存在；使用 resume，禁止覆盖原运行')
        else:
            if resume:
                raise ValueError('尚无可恢复实验')
            atomic_json(run/'identity.json', identity)
            snapshot = run/'source'
            snapshot.mkdir(exist_ok=True)
            for name in identity['code']:
                (snapshot/name).write_bytes((Path(__file__).parent/name).read_bytes())
        seed_all(config['seed'])
        if device == 'cuda':
            torch.cuda.set_per_process_memory_fraction(.75)
            torch.cuda.reset_peak_memory_stats()
        resource = Resources(device)
        resource.sample()
        training = SegmentationDataset(dataset, config['category'], 'train', config['size'])
        validation = SegmentationDataset(dataset, config['category'], 'validation', config['size'])
        if diagnostic_limit:
            training.rows = training.rows[:diagnostic_limit]
            # Overfit probes validate the same training images, always marked diagnostic.
            validation = training
        if not len(training) or not len(validation):
            raise ValueError('训练或验证划分为空')
        model, provenance = architecture(config)
        atomic_json(run/'architecture.json', provenance)
        model.to(device)
        optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
        scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, factor=.5, patience=4, min_lr=1e-5)
        scaler = torch.amp.GradScaler('cuda', enabled=device == 'cuda')
        epoch, best, best_epoch, bad_epochs, history = 0, -1., 0, 0, []
        micro_batch = config.get('micro_batch', 2)
        stopping = {'requested': False}
        def pause(*_):
            stopping['requested'] = True
        previous = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                previous[sig] = signal.signal(sig, pause)
            except ValueError:
                pass
        def checkpoint():
            return {'epoch': epoch, 'best': best, 'best_epoch': best_epoch,
                    'bad_epochs': bad_epochs, 'history': history, 'model': model.state_dict(),
                    'optimizer': optimizer.state_dict(), 'scheduler': scheduler.state_dict(),
                    'scaler': scaler.state_dict(), 'rng': rng_state(), 'micro_batch': micro_batch,
                    'identity': identity}
        def status(state, **extra):
            atomic_json(run/'status.json', {'id': config['id'], 'state': state, 'updated_utc': now(),
                        'completed_epoch': epoch, 'best_epoch': best_epoch, 'best_validation_dice': best,
                        'formal': diagnostic_limit is None, 'pid': os.getpid(),
                        'resource': resource.sample(), **extra})
        try:
            if resume:
                saved = load_checkpoint(run/'last.pt')
                if saved['identity'] != identity:
                    raise ValueError('检查点身份与当前配置不同')
                model.load_state_dict(saved['model'])
                optimizer.load_state_dict(saved['optimizer'])
                # Optimizer tensors loaded on CPU follow their parameters to the training device.
                for state in optimizer.state.values():
                    for key, value in state.items():
                        if torch.is_tensor(value) and key != 'step':
                            state[key] = value.to(device)
                scheduler.load_state_dict(saved['scheduler'])
                scaler.load_state_dict(saved['scaler'])
                epoch, best, best_epoch = saved['epoch'], saved['best'], saved['best_epoch']
                bad_epochs, history, micro_batch = saved['bad_epochs'], saved['history'], saved['micro_batch']
                restore_rng(saved['rng'])
                if saved.get('best_model') is not None:
                    current_best = load_checkpoint(run/'best.pt') if (run/'best.pt').exists() else {}
                    if current_best.get('epoch') != best_epoch:
                        atomic_checkpoint(run/'best.pt', {'model': saved['best_model'], 'epoch': best_epoch,
                            'identity': identity, 'validation': history[best_epoch-1]['validation']})
            else:
                # Preflight tests the actual architecture and optimizer, without changing run RNG/weights.
                initial = copy.deepcopy(model.state_dict())
                random_initial = rng_state()
                for batch in (micro_batch, 1) if micro_batch != 1 else (1,):
                    try:
                        probe = torch.zeros(batch, 1, config['size'], config['size'], device=device)
                        with torch.autocast(device, dtype=torch.float16, enabled=device == 'cuda'):
                            loss = loss_value(model(probe), torch.zeros_like(probe), config['loss'])
                        scaler.scale(loss).backward()
                        resource.sample()
                        micro_batch = batch
                        break
                    except torch.OutOfMemoryError:
                        if batch == 1:
                            raise ResourceLimit('batch1仍显存不足；不改变分辨率继续原实验')
                        optimizer.zero_grad(set_to_none=True)
                        torch.cuda.empty_cache()
                optimizer.zero_grad(set_to_none=True)
                del probe, loss
                model.load_state_dict(initial)
                del initial
                restore_rng(random_initial)
                atomic_checkpoint(run/'last.pt', checkpoint())
            atomic_json(run/'metrics.json', history)
            max_epochs = config.get('max_epochs', 80)
            if resume and early_stop_met(epoch, bad_epochs, config):
                status('completed', reason='validation_early_stop_on_audited_resume')
                return json.loads((run/'status.json').read_text(encoding='utf-8'))
            status('running', micro_batch=micro_batch, effective_batch=config.get('effective_batch', 8))
            started_epoch = epoch
            while epoch < max_epochs:
                if stopping['requested'] or (run/'PAUSE').exists():
                    status('paused', reason='用户暂停，检查点已保存')
                    break
                loader = loader_for(training, config, epoch, micro_batch)
                model.train()
                epoch_start = time.perf_counter()
                optimizer.zero_grad(set_to_none=True)
                running_loss, images = 0., 0
                accumulation = config.get('effective_batch', 8)//micro_batch
                for step, batch in enumerate(loader):
                    x, y = batch['image'].to(device), batch['semantic_mask'].to(device)
                    if config.get('augment') and diagnostic_limit is None:
                        gain = .9 + .2*torch.rand((len(x), 1, 1, 1), device=device)
                        gamma = .9 + .2*torch.rand((len(x), 1, 1, 1), device=device)
                        x = (x*gain).clamp(0, 1).pow(gamma)
                    # Correct weighting for the incomplete final accumulation window.
                    window_start = (step//accumulation)*accumulation*micro_batch
                    window_images = min(config.get('effective_batch', 8), len(training)-window_start)
                    with torch.autocast(device, dtype=torch.float16, enabled=device == 'cuda'):
                        logits = model(x)
                        loss = loss_value(logits, y, config['loss'])
                    if not torch.isfinite(loss):
                        raise RuntimeError('训练损失非有限值，停止并保留上一完整epoch')
                    scaler.scale(loss*len(x)/window_images).backward()
                    if (step+1) % accumulation == 0 or step+1 == len(loader):
                        scaler.step(optimizer)
                        scaler.update()
                        optimizer.zero_grad(set_to_none=True)
                    running_loss += float(loss.detach())*len(x)
                    images += len(x)
                    if step % 64 == 0:
                        status('running', active_epoch=epoch+1, images_processed=images,
                               train_images=len(training), micro_batch=micro_batch)
                summary, _ = evaluate(model, validation, config, device, resource=resource)
                if summary['dice'] is None or not math.isfinite(summary['dice']) or not math.isfinite(summary['loss']):
                    raise ValueError('没有非空验证参考，无法选择模型')
                scheduler.step(summary['loss'])
                epoch += 1
                improved = summary['dice'] > best
                if improved:
                    best, best_epoch, bad_epochs = summary['dice'], epoch, 0
                else:
                    bad_epochs += 1
                history.append({'epoch': epoch, 'train_loss': running_loss/images,
                                'validation': summary, 'learning_rate': optimizer.param_groups[0]['lr'],
                                'seconds': time.perf_counter()-epoch_start, 'resource': resource.sample()})
                saved = checkpoint()
                # Embed best weights in last: crash between last/best writes is recoverable.
                if improved:
                    saved['best_model'] = copy.deepcopy(model.state_dict())
                else:
                    saved['best_model'] = load_checkpoint(run/'last.pt')['best_model']
                atomic_checkpoint(run/'last.pt', saved)
                if improved:
                    atomic_checkpoint(run/'best.pt', {'model': saved['best_model'], 'epoch': epoch,
                                                     'identity': identity, 'validation': summary})
                atomic_json(run/'metrics.json', history)
                status('running', micro_batch=micro_batch)
                print(json.dumps({'id': config['id'], 'epoch': epoch, 'dice': summary['dice'],
                                  'seconds': history[-1]['seconds']}, ensure_ascii=False), flush=True)
                if epoch >= config.get('min_epochs', 20) and bad_epochs >= config.get('patience', 12):
                    status('completed', reason='validation_early_stop')
                    break
                if stop_after and epoch-started_epoch >= stop_after:
                    status('paused', reason='本次运行epoch预算用尽')
                    break
            else:
                status('completed', reason='max_epochs')
            return json.loads((run/'status.json').read_text(encoding='utf-8'))
        except Exception as exc:
            # Do not checkpoint partially updated epoch weights as completed.
            atomic_json(run/'status.json', {'id': config['id'], 'state': 'resource_failed' if isinstance(exc, (ResourceLimit, torch.OutOfMemoryError)) else 'failed',
                        'completed_epoch': epoch, 'updated_utc': now(), 'error': str(exc),
                        'recovery': '修复原因后核验身份，再从last.pt恢复；最多重算当前epoch'})
            raise
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def main():
    from .runtime import configure_environment
    configure_environment()
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--dataset', required=True)
    parser.add_argument('--run', required=True)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--stop-after', type=int)
    parser.add_argument('--overfit-samples', type=int)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding='utf-8'))
    print(json.dumps(train(config, args.dataset, args.run, args.resume, stop_after=args.stop_after,
                           diagnostic_limit=args.overfit_samples), ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
