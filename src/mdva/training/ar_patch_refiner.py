"""Fit a compact, native-localization AR refiner against the frozen proposal audit."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import binary_dilation, label
import torch
from torch.nn import functional as F

from .ar_bipolar_proposals import image_data, proposals, reference_assignment
from .ar_scale_review import estimate, native_objects
from .ar_scale_study import ROOT, cohort_rows, load_plan
from .datasets import SegmentationDataset
from .durable import atomic_checkpoint, atomic_json, commit
from .forecast_region_detector_audit import MODEL
from .journal import append, read
from .metrics import binary_segmentation
from .models import UNet
from .reviewed_train import load_checkpoint, restore_rng, rng_state, seed_all
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


SIDE = 64
BUDGET = 8
BATCH = 32


def crop_array(image, box, target=False):
    x0, y0, x1, y1 = box
    patch = Image.fromarray(image[y0:y1, x0:x1])
    resized = patch.resize((SIDE, SIDE), Image.Resampling.NEAREST if target else Image.Resampling.BILINEAR)
    return np.array(resized)


def source_identity():
    names = ('ar_patch_refiner.py', 'ar_bipolar_proposals.py', 'ar_scale_review.py', 'models.py',
             'metrics.py', 'datasets.py', 'runtime.py', 'reviewed_train.py', 'durable.py',
             'ar_scale_study.py', 'forecast_region_detector_audit.py')
    return {name: digest(Path(__file__).with_name(name)) for name in names}


def prepare(study, source, audit):
    source, audit = Path(source).resolve(), Path(audit).resolve()
    origin = load_plan(source)
    study = output_path(study, origin['dataset'])
    if study.exists() or any(study.is_relative_to(path) for path in (source, audit)):
        raise ValueError('局部复核使用独立新目录')
    summary = read(audit / 'summary.json')
    protocol = read(audit / 'protocol.json')
    if (
        summary['state'] != 'completed' or summary['test_accessed'] or summary['calibration_accessed']
        or summary['protocol_sha256'] != digest(audit / 'protocol.json')
        or summary['rows_sha256'] != digest(audit / 'images.json')
        or protocol['source_study_sha256'] != digest(source / 'protocol.json')
        or protocol['code_sha256'] != digest(Path(__file__).with_name('ar_bipolar_proposals.py'))
    ):
        raise ValueError('旧候选审计身份不符')
    data = SegmentationDataset(origin['dataset'], 'AR', 'train')
    data.rows = cohort_rows(data, read(source / 'cohort.json'))
    study.mkdir(parents=True)
    patches, targets = [], []
    counts = Counter()
    resource = Resources('cpu')
    for index, row in enumerate(data.rows, 1):
        image, native = image_data(data, row)
        ids, areas = np.unique(native[native > 0], return_counts=True)
        objects = dict(zip(ids.tolist(), areas.tolist()))
        for candidate in proposals(image):
            instance, match = reference_assignment(candidate, native, objects)
            counts[match] += 1
            if match == 'ambiguous_reference_overlap':
                continue
            target = (native == instance).astype(np.uint8) if instance is not None else np.zeros_like(image)
            patches.append(crop_array(image, candidate['box']))
            targets.append(crop_array(target, candidate['box'], target=True))
        if index % 32 == 0 or index == len(data):
            atomic_json(study / 'prep-status.json', {'state': 'preparing', 'images': index, 'total': len(data),
                                                     'resource': resource.sample()})
    if dict(counts) != summary['training_candidates']:
        raise ValueError('训练候选构建未能复现同一批参照')
    cache = study / 'patches.npz'
    commit(cache, lambda stream: np.savez_compressed(stream, images=np.stack(patches), targets=np.stack(targets)))
    plan = {
        'created_utc': now(), 'source': str(source), 'dataset': origin['dataset'], 'audit': str(audit),
        'source_sha256': digest(source / 'protocol.json'), 'audit_sha256': digest(audit / 'summary.json'),
        'cache_sha256': digest(cache), 'code': source_identity(), 'training_sources': len(data),
        'target_counts': dict(counts), 'grid': SIDE, 'budget': BUDGET, 'batch': BATCH, 'seed': 2026,
        'target': 'One native reference object for accepted centers, blank where clean no-overlap, omit ambiguous crops.',
        'scope': 'Fixed8 epoch U-Net width8, natural candidate sampling, BCE + positive-only Dice. Research only.',
        'decision': {'recall_gain': .03, 'precision_drop': .01, 'main_budget_per_image': 3},
        'test_accessed': False, 'calibration_accessed': False,
    }
    atomic_json(study / 'protocol.json', plan)
    atomic_json(study / 'prep-status.json', {'state': 'completed', 'images': len(data), 'total': len(data)})
    return plan


def open_study(study):
    path = study / 'protocol.json'
    plan = read(path)
    source, audit = Path(plan['source']), Path(plan['audit'])
    origin = load_plan(source)
    if (
        plan['code'] != source_identity() or plan['dataset'] != origin['dataset']
        or plan['source_sha256'] != digest(source / 'protocol.json')
        or plan['audit_sha256'] != digest(audit / 'summary.json')
        or plan['cache_sha256'] != digest(study / 'patches.npz')
        or (plan['grid'], plan['budget'], plan['batch']) != (SIDE, BUDGET, BATCH)
    ):
        raise ValueError('局部复核协议或缓存变化')
    return plan, {'protocol_sha256': digest(path)}, audit


def loss_value(logits, target):
    logits = logits.float()
    bce = F.binary_cross_entropy_with_logits(logits, target)
    p, y = logits.sigmoid().flatten(1), target.flatten(1)
    dice_loss = 1 - (2 * (p*y).sum(1) + 1) / (p.sum(1) + y.sum(1) + 1)
    present = y.sum(1) > 0
    return bce + .5 * (dice_loss * present).sum() / present.sum().clamp_min(1)


def train(study):
    import os
    import time

    study = Path(study).resolve()
    plan, identity, _ = open_study(study)
    folder = study / 'run'
    last = folder / 'last.pt'
    if (folder / 'PAUSE').exists():
        raise ValueError('暂停标记仍在')
    samples = np.load(study / 'patches.npz', allow_pickle=False)
    with samples:
        x = torch.from_numpy(samples['images'])[:, None]
        y = torch.from_numpy(samples['targets'])[:, None]
    seed_all(plan['seed'])
    log = ROOT / 'out/training-20261112-v2'
    with exclusive(ROOT / '.mdva/training-gpu.lock'):
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        resource = Resources()
        resource.sample()
        model = UNet(width=8).cuda()
        optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3)
        scaler = torch.amp.GradScaler('cuda')
        completed, history = 0, []
        if last.exists():
            saved = load_checkpoint(last)
            if saved['identity'] != identity:
                raise ValueError('局部复核检查点和协议不同')
            model.load_state_dict(saved['model'])
            optimizer.load_state_dict(saved['optimizer'])
            scaler.load_state_dict(saved['scaler'])
            restore_rng(saved['rng'])
            completed, history = saved['epoch'], saved['history']

        def status(state, **details):
            record = {'state': state, 'updated_utc': now(), 'pid': os.getpid(),
                      'completed_epoch': completed, 'budget': BUDGET, **details}
            atomic_json(folder / 'status.json', record)
            return record

        append(log, 'development', '活动区局部复核训练', '8轮预算，使用训练集候选的实际掩膜。')
        try:
            for epoch in range(completed + 1, BUDGET + 1):
                if (folder / 'PAUSE').exists():
                    return status('paused', reason='已保存完整轮次')
                started = time.perf_counter()
                permutation = torch.randperm(len(x), generator=torch.Generator().manual_seed(plan['seed'] + epoch))
                total = 0.
                for index, selection in enumerate(permutation.split(BATCH)):
                    data, target = x[selection].cuda().float() / 255, y[selection].cuda().float()
                    optimizer.zero_grad(set_to_none=True)
                    with torch.autocast('cuda', dtype=torch.float16):
                        loss = loss_value(model(data), target)
                    if not torch.isfinite(loss):
                        raise ValueError('损失非有限')
                    scaler.scale(loss).backward()
                    scaler.step(optimizer)
                    scaler.update()
                    total += float(loss.detach()) * len(selection)
                    if index % 32 == 0:
                        status('running', active_epoch=epoch, patches=min((index+1)*BATCH, len(x)),
                               total_patches=len(x), resource=resource.sample())
                history.append({'epoch': epoch, 'loss': total/len(x), 'seconds': time.perf_counter()-started,
                                'resource': resource.sample()})
                atomic_checkpoint(last, {'identity': identity, 'epoch': epoch, 'history': history,
                                         'model': model.state_dict(), 'optimizer': optimizer.state_dict(),
                                         'scaler': scaler.state_dict(), 'rng': rng_state()})
                completed = epoch
                atomic_json(folder / 'metrics.json', history)
                status('running', resource=resource.sample())
                print(json.dumps(history[-1]), flush=True)
        except Exception as exc:
            status('failed', error=str(exc))
            raise
        return status('completed', resource=resource.sample())


def central_mask(logits, box, center):
    x0, y0, x1, y1 = box
    probability = F.interpolate(logits.float().sigmoid(), size=(y1-y0, x1-x0),
                                mode='bilinear', align_corners=False)[0, 0].cpu().numpy()
    if not np.isfinite(probability).all():
        raise ValueError('局部掩膜概率含非有限值')
    components, _ = label(probability >= .5)
    idx = components[center[1] - y0, center[0] - x0]
    return components == idx if idx else np.zeros_like(probability, dtype=bool)


def review(study, diagnostic, output):
    study, diagnostic = Path(study).resolve(), Path(diagnostic).resolve()
    plan, identity, audit = open_study(study)
    output = output_path(output, plan['dataset'])
    if output.exists() or any(output.is_relative_to(path) for path in (study, diagnostic, audit)):
        raise ValueError('局部复核复算用新目录')
    status = read(study / 'run/status.json', {})
    path = study / 'run/last.pt'
    if status.get('state') != 'completed' or status.get('completed_epoch') != BUDGET:
        raise ValueError('缺少固定第8轮权重')
    checkpoint_hash = digest(path)
    saved = load_checkpoint(path)
    if saved['identity'] != identity or saved['epoch'] != BUDGET or digest(path) != checkpoint_hash:
        raise ValueError('局部复核检查点身份改变')
    data = SegmentationDataset(plan['dataset'], 'AR', 'validation')
    index_path = diagnostic / 'case-index.json'
    audit_protocol, audit_summary = read(audit / 'protocol.json'), read(audit / 'summary.json')
    if audit_protocol['checkpoint_index_sha256'] != digest(index_path) or audit_summary['rows_sha256'] != digest(audit / 'images.json'):
        raise ValueError('冻结检测及候选数据改变')
    candidate_by_uri = {row['uri']: row for row in read(audit / 'images.json')}
    frozen = {row['uri']: row for row in read(index_path)
              if row['run_id'] == MODEL and row['diagnostic_subset'] == 'validation'}
    if len(data) != 276 or set(frozen) != set(candidate_by_uri) or set(frozen) != {row['uri'] for row in data.rows}:
        raise ValueError('局部复核必须覆盖完整相同开发集')
    model = UNet(width=8)
    model.load_state_dict(saved['model'])
    rows = []
    with exclusive(ROOT / '.mdva/training-gpu.lock'):
        seed_all(2026)
        torch.cuda.reset_peak_memory_stats()
        resource = Resources()
        model.cuda().eval()
        output.mkdir(parents=True)
        with torch.inference_mode():
            for index, row in enumerate(data.rows, 1):
                gray, reference = image_data(data, row)
                case = frozen[row['uri']]
                probability = diagnostic / 'probabilities' / MODEL / (case['sample_id'] + '.npy')
                if digest(probability) != case['probability_sha256'] or case['mask_uri'] != row['mask_uri']:
                    raise ValueError('原模型缓存改变')
                values = np.load(probability, allow_pickle=False)
                if values.shape != reference.shape or not np.isfinite(values).all():
                    raise ValueError('原模型概率不可用')
                base = values >= .5
                baseline = native_objects(base, reference)
                if baseline['matched'] != case['objects']['matched']:
                    raise ValueError('原模型匹配不能复算')
                augmented = base.copy()
                candidates = sorted((candidate for candidate in candidate_by_uri[row['uri']]['candidates']
                                     if candidate['independent']), key=lambda candidate: -candidate['learned_score'])
                added = 0
                for candidate in candidates[:3]:
                    box = candidate['box']
                    sample = torch.from_numpy(crop_array(gray, box))[None, None].cuda().float() / 255
                    with torch.autocast('cuda', dtype=torch.float16):
                        logits = model(sample)
                    patch = central_mask(logits, box, candidate['center'])
                    if patch.sum() < 4:
                        continue
                    x0, y0, x1, y1 = box
                    avoid = binary_dilation(augmented[max(0,y0-1):y1+1, max(0,x0-1):x1+1])
                    top, left = int(y0 > 0), int(x0 > 0)
                    if np.any(patch & avoid[top:top+y1-y0, left:left+x1-x0]):
                        continue
                    augmented[y0:y1, x0:x1] |= patch
                    added += 1
                refined = native_objects(augmented, reference)
                misses = set(candidate_by_uri[row['uri']]['no_overlap'])
                rows.append({'uri': row['uri'], 'patches_added': added,
                             'no_overlap_rescued': len(misses & set(refined['matched_reference_ids'])),
                             'arms': {
                                 'baseline': {'objects': baseline, 'pixels': binary_segmentation(base, reference > 0)},
                                 'refined': {'objects': refined, 'pixels': binary_segmentation(augmented, reference > 0)},
                             }})
                if index % 32 == 0:
                    resource.sample()
        resources = resource.sample()
    before, after = (estimate(rows, name) for name in ('baseline', 'refined'))
    deltas = {key: after[key] - before[key] for key in ('region_precision', 'region_recall')}
    limits = plan['decision']
    passed = deltas['region_precision'] >= -limits['precision_drop'] and deltas['region_recall'] >= limits['recall_gain']
    atomic_json(output / 'images.json', rows)
    report = {
        'state': 'completed', 'created_utc': now(), 'images': len(rows), 'baseline': before, 'refined': after,
        'change': deltas, 'practical_gate_passed': passed, 'additional_masks': sum(row['patches_added'] for row in rows),
        'no_overlap_rescued': sum(row['no_overlap_rescued'] for row in rows), 'resource': resources,
        'scope': 'Add independent local masks only. Same native-grid IoU 0.5, fixed third-ranked budget.',
        'checkpoint_sha256': checkpoint_hash, 'protocol_sha256': digest(study / 'protocol.json'),
        'rows_sha256': digest(output / 'images.json'), 'test_accessed': False, 'released': False,
    }
    atomic_json(output / 'summary.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'train', 'review'))
    parser.add_argument('--study', required=True)
    for name in ('source', 'audit', 'diagnostic', 'output'):
        parser.add_argument('--' + name)
    args = parser.parse_args()
    configure_environment()
    if args.action == 'prepare' and args.source and args.audit:
        result = prepare(args.study, args.source, args.audit)
    elif args.action == 'train':
        result = train(args.study)
    elif args.action == 'review' and args.diagnostic and args.output:
        result = review(args.study, args.diagnostic, args.output)
    else:
        parser.error('缺少所选阶段的来源或输出')
    print(json.dumps({key: result[key] for key in (
        'state', 'completed_epoch', 'target_counts', 'change', 'additional_masks', 'no_overlap_rescued',
        'practical_gate_passed',
    ) if key in result}))


if __name__ == '__main__':
    main()
