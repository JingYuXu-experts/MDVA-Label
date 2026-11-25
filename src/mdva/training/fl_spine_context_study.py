"""Learn FL contours with and without separately annotated spine supervision."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw
from safetensors.torch import save as save_weights
from scipy.ndimage import binary_dilation, binary_propagation, label
import torch
import torch.nn.functional as F

from .ch_boundary_growth import remove_small
from .durable import atomic_json, commit
from .fl_author_model import AuthorGraph
from .fl_ensemble_study import CONNECTIVITY, DATASET, ROOT, overlap_profile
from .fl_gong_patch_training import image_bytes
from .fl_gong_resolution import predict, resize
from .fl_magfilo_adaptation import source_receipt
from .fl_magfilo_audit import compare, raster
from .fl_magfilo_transfer import inventory, load_gray, metrics
from .fl_native_detail_audit import native_raster
from .fl_native_local_training import BATCH, SEED, SIZE, TILE, grid, view
from .fl_native_positive_weight import weighted_loss
from .journal import read
from .models import UNet
from .reviewed_train import load_checkpoint
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


ARMS = ('mask_only', 'mask_and_spine')
EPOCHS = 24


def spine_raster(items):
    """Rasterize author polylines, guarding invalid coordinates and off-mask strokes."""
    image = Image.new('1', (SIZE, SIZE))
    draw = ImageDraw.Draw(image)
    count = 0
    for row in items:
        curve = np.asarray(row['spine'], dtype=float).reshape(-1, 2)
        if len(curve) < 2 or not np.isfinite(curve).all() or (curve < 0).any() or (curve >= SIZE).any():
            raise ValueError('暗条原始主轴坐标非法')
        draw.line([tuple(point) for point in curve], fill=1, width=7)
        count += 1
    actual = np.asarray(image, dtype=bool)
    reference = native_raster(items)
    supported = cv2.dilate(reference.astype(np.uint8), np.ones((7, 7), dtype=np.uint8)) > 0
    return reference, actual & supported, count, int(actual.sum()), int((actual & supported).sum())


def context(raw):
    local, valid = view(raw)
    coarse = cv2.resize(local[0], (512, 512), interpolation=cv2.INTER_AREA)
    support = cv2.resize(valid.astype(np.float32), (512, 512), interpolation=cv2.INTER_AREA)
    weights = np.maximum(cv2.GaussianBlur(support, (0, 0), 10), .02)
    neighborhood = cv2.GaussianBlur(coarse, (0, 0), 10) / weights
    context_channels = np.stack([coarse, neighborhood], axis=-1)
    full = cv2.resize(context_channels, (SIZE, SIZE), interpolation=cv2.INTER_LINEAR).transpose(2, 0, 1)
    return np.where(valid[None], np.concatenate([local, full]), 0), valid


def training_data(rows, folder, annotations, patches):
    windows = grid()
    maps = defaultdict(list)
    for item in patches:
        maps[item['file']].append(item['patch'])
    known = {r['file']:r for r in rows}
    inp, targets, supported, quality = [], [], [], []
    for name, picks in maps.items():
        row = known[name]
        features, valid = context(image_bytes(folder, row))
        masks = [spine_raster(annotations[item['id']]) for item in row['annotators']]
        mask, spine = [np.mean([r[i] for r in masks], axis=0, dtype=np.float32) for i in (0, 1)]
        quality.append({'file': name, 'polylines': sum(m[2] for m in masks),
                        'drawn_spine_pixels': sum(m[3] for m in masks), 'in_mask_halo_pixels': sum(m[4] for m in masks)})
        for id in picks:
            y, x = windows[id]
            inp.append(features[:, y, x].astype(np.float16))
            targets.append(np.stack((mask[y, x], spine[y, x])).astype(np.float16))
            supported.append(valid[y, x])
    return np.stack(inp), np.stack(targets), np.stack(supported), quality


def architecture():
    model = UNet(width=8)
    model.encoders[0].layers[0] = torch.nn.Conv2d(5, 8, 3, padding=1)
    model.out = torch.nn.Conv2d(8, 2, 1)
    return model


def objective(output, values, valid, name):
    base = weighted_loss(output[:, :1], values[:, :1], valid)
    if name == 'mask_only':
        return base
    target = values[:, 1:]
    logits = output[:, 1:]
    axes = (1, 2, 3)
    # Balance faint/short lines against bright background so they influence localization.
    numerator = (F.binary_cross_entropy_with_logits(logits, target, reduction='none') * valid * (1 + 15 * target)).sum(axes)
    normalizer = (valid * (1 + 15 * target)).sum(axes).clamp_min(1)
    return base + .40 * (numerator / normalizer).mean()


def train(data, arm, output, protocol, meter):
    inputs, targets, valid = data
    torch.manual_seed(SEED)
    model = architecture().cuda()
    solver = torch.optim.AdamW(model.parameters(), lr=.001)
    state_file = output / arm / 'last.pt'
    history = []
    if state_file.exists():
        old = torch.load(state_file, map_location='cpu', weights_only=True)
        if old['identity'] != [protocol, arm]:
            raise ValueError('续跑模型身份不符')
        model.load_state_dict(old['model'])
        solver.load_state_dict(old['optimizer'])
        history = old['history']
    for i in range(len(history), EPOCHS):
        if (output / 'PAUSE').exists():
            return None
        shuffle = np.random.default_rng(SEED + i).permutation(len(inputs))
        batch_losses = []
        for start in range(0, len(shuffle), BATCH):
            batch = shuffle[start:start + BATCH]
            x, y, v = [torch.tensor(p[batch], dtype=torch.float32, device='cuda') for p in (inputs, targets, valid)]
            solver.zero_grad(set_to_none=True)
            cost = objective(model(x), y, v[:, None], arm)
            if not bool(torch.isfinite(cost)):
                raise ValueError('主轴模型出现无效损失')
            cost.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 4., error_if_nonfinite=True)
            solver.step()
            batch_losses.append(float(cost.detach()))
        history.append(float(np.mean(batch_losses)))
        checkpoint = {'identity': [protocol, arm], 'model': model.state_dict(), 'optimizer': solver.state_dict(), 'history': history}
        commit(state_file, lambda f: torch.save(checkpoint, f))
        report = {'state':'training', 'arm':arm, 'epoch':i+1, 'loss':history[-1], 'resources':meter.sample()}
        atomic_json(output / 'status.json', report)
        if i in (0, 7, 15, 23):
            print(json.dumps(report), flush=True)
    commit(output / arm / 'research.safetensors', lambda f: f.write(save_weights({k:v.cpu().contiguous() for k, v in model.state_dict().items()})))
    return model.eval()


def full_score(model, features, disk):
    windows = [w for w in grid() if disk[w].any()]
    weight = np.maximum(np.outer(np.hanning(TILE), np.hanning(TILE)), .05).astype(np.float32)
    image = np.zeros((2, SIZE, SIZE), np.float32)
    norm = np.zeros(disk.shape, np.float32)
    for index in range(0, len(windows), BATCH):
        batch = windows[index:index + BATCH]
        x = torch.from_numpy(np.stack([features[:, y, x] for y, x in batch])).cuda()
        pred = model(x).sigmoid().cpu().numpy()
        if not np.isfinite(pred).all():
            raise ValueError('主轴或掩膜模型输出无效')
        for (y, x), tile in zip(batch, pred, strict=True):
            image[:, y, x] += tile * weight
            norm[y, x] += weight
    np.divide(image, np.maximum(norm, .01), out=image)
    image *= disk
    return [resize(ch, 512) for ch in image]


def locate(mask, spine, baseline):
    strong = (spine >= .35) & (mask >= .22)
    support = mask >= .17
    grown = binary_propagation(strong, structure=CONNECTIVITY, mask=support)
    components = label(remove_small(grown), structure=CONNECTIVITY)[0]
    occupied = set(np.unique(components[binary_dilation(baseline, structure=CONNECTIVITY, iterations=2)]))
    keep = np.array([0 < i and i not in occupied and a <= 1024 for i, a in enumerate(np.bincount(components.ravel()))])
    return keep[components]


def evaluate(rows, folder, annotations, whole, models, output, meter):
    result = []
    for i, row in enumerate(rows, 1):
        raw = image_bytes(folder, row)
        features, disk = context(raw)
        whole_prediction = predict(whole, load_gray(raw)[None])[0] >= .5
        mask_base, _ = full_score(models['mask_only'], features, disk)
        mask_joint, spine = full_score(models['mask_and_spine'], features, disk)
        proposals = locate(mask_joint, spine, whole_prediction)
        targets = [raster(annotations[v['id']]) for v in row['annotators']]
        arms = {'whole':whole_prediction, 'local_mask_only':mask_base >= .5, 'local_joint_mask':mask_joint >= .5,
                'whole_plus_spine_proposals':whole_prediction | proposals}
        result.append({'file':row['file'], 'station':row['file'][14:16], 'spine_score_peak':float(spine.max()),
                       'arms':{name:[{**compare(mask, reference), **overlap_profile(mask, label(reference, structure=CONNECTIVITY)[0])}
                                     for reference in targets] for name, mask in arms.items()}})
        if i % 8 == 0:
            atomic_json(output / 'status.json', {'state':'evaluating', 'complete':i, 'total':len(rows)})
            meter.sample()
    return result


def run(source, release, early, development, prior, anchor, output):
    output = output_path(output, DATASET)
    if (output / 'summary.json').exists():
        raise ValueError('实验结果已经保存')
    old, original = (read(prior / k) for k in ('summary.json', 'protocol.json'))
    base = anchor / 'mixed_gong' / 'last.pt'
    if (old['protocol_sha256'] != digest(prior / 'protocol.json') or original['early_sha256'] != digest(early / 'inventory.json')
            or original['anchor_sha256'] != digest(base)):
        raise ValueError('旧实验身份不同')
    entries, reference = inventory(source, release)
    registry = {r['file']:r for r in entries}
    train_rows = source_receipt(early, 'train_available', registry)
    dev = source_receipt(development, 'transfer_dev', registry)
    dev = [r for r in dev if r['file'].startswith('2018')]
    if len(dev) < 12 or any(r['file'] >= '2016' for r in train_rows):
        raise ValueError('需要原约定的时间隔离')
    cv2.setNumThreads(2)
    torch.set_num_threads(2)
    x, masks, bound, quality = training_data(train_rows, early, reference, original['patches'])
    if int(np.count_nonzero(masks[:, 1])) < 1000:
        raise ValueError('主轴监督的有效样本不足')
    protocol = {'code_sha256':digest(__file__), 'original_annotations_sha256':digest(source), 'old_result_sha256':digest(prior/'summary.json'),
                'training_samples':len(x), 'epochs':EPOCHS, 'batch':BATCH, 'mask_threshold':.5,
                'spine_threshold':.35,'minimum_mask_for_seed':.22,'grow_support':.17,
                'review_files':[r['file'] for r in dev], 'test_accessed':False, 'year_2022_accessed':False, 'calibration_accessed':False}
    plan = output / 'protocol.json'
    if plan.exists() and read(plan) != protocol:
        raise ValueError('续跑协议不同')
    if not plan.exists():
        atomic_json(plan, protocol)
    atomic_json(output / 'label-quality.json', quality)
    models = {}
    with exclusive(ROOT / '.mdva/training-gpu.lock'):
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        meter = Resources()
        for arm in ARMS:
            model = train((x, masks, bound), arm, output, digest(plan), meter)
            if model is None:
                return {'state':'paused', 'arm':arm}
            models[arm] = model
        whole = AuthorGraph('batch').cuda().eval()
        whole.load_state_dict(load_checkpoint(base)['model'])
        with torch.inference_mode():
            cases = evaluate(dev, development, reference, whole, models, output, meter)
        resources = meter.sample()
    score = {}
    for name in cases[0]['arms']:
        comparisons = [{'comparisons':row['arms'][name]} for row in cases]
        score[name] = {**metrics(comparisons), 'zero_overlap_references':sum(o['zero_overlap_references'] for c in comparisons for o in c['comparisons'])}
    atomic_json(output / 'cases.json', cases)
    passed = score['whole_plus_spine_proposals']['matched_iou_050'] > score['whole']['matched_iou_050'] and score['whole_plus_spine_proposals']['precision_iou_050'] >= score['whole']['precision_iou_050'] - .01
    report = {'state':'completed', 'recorded_utc':now(), 'training_patches':len(x), 'annotated_spines':sum(r['polylines'] for r in quality),
              'spine_pixel_support':sum(r['in_mask_halo_pixels'] for r in quality) / sum(r['drawn_spine_pixels'] for r in quality),
              'metrics':score, 'resources':resources, 'pass_early_candidate_gate':passed, 'test_accessed':False, 'year_2022_accessed':False,
              'deployed':False, 'protocol_sha256':digest(plan), 'cases_sha256':digest(output / 'cases.json')}
    atomic_json(output / 'summary.json', report)
    atomic_json(output / 'status.json', {'state':'completed', 'summary_sha256':digest(output / 'summary.json')})
    return report


if __name__ == '__main__':
    configure_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'release', 'early', 'development', 'prior', 'anchor', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    print(json.dumps(run(**vars(parser.parse_args())), ensure_ascii=False))
