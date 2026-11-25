"""Matched full-resolution crop training for weak H-alpha filament contrast."""

from __future__ import annotations

import argparse
from collections import defaultdict
from io import BytesIO
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from safetensors.torch import save as save_weights
from scipy.ndimage import label
import torch

from .ch_spatial_multichannel import loss
from .durable import atomic_json, commit
from .fl_author_model import AuthorGraph
from .fl_ensemble_study import CONNECTIVITY, DATASET, ROOT, overlap_profile
from .fl_gong_limb_gate import visible_disk
from .fl_gong_patch_training import image_bytes
from .fl_gong_resolution import resize, predict
from .fl_magfilo_adaptation import source_receipt, stratified
from .fl_magfilo_audit import DATA_SHA256, compare, raster
from .fl_magfilo_transfer import LABEL_SHA256, inventory, load_gray, metrics, release_observations, same_annotation
from .fl_native_detail_audit import native_raster
from .journal import read
from .models import UNet
from .reviewed_train import load_checkpoint
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


SIZE, TILE, STRIDE = 2048, 256, 192
MODELS = ('gray', 'gray_plus_contrast')
EPOCHS, BATCH, SEED = 8, 8, 2026
THRESHOLD = .5


def grid():
    edges = sorted(set(range(0, SIZE - TILE + 1, STRIDE)) | {SIZE - TILE})
    return [(slice(y, y + TILE), slice(x, x + TILE)) for y in edges for x in edges]


def view(raw):
    with Image.open(BytesIO(raw)) as image:
        if image.size != (SIZE, SIZE):
            raise ValueError('暗条研究需要原生2048图')
        gray = np.asarray(image.convert('L'), dtype=np.float32) / 255.
    disk, _ = visible_disk(load_gray(raw))
    if disk is None:
        raise ValueError('无法确认本次研究需要的太阳日面范围')
    valid = np.repeat(np.repeat(disk, 4, axis=0), 4, axis=1)
    w = valid.astype(np.float32)
    fields = [(gray - .5) * 2.]
    for sigma in (3, 12):
        norm = cv2.GaussianBlur(w, (0, 0), sigma)
        background = cv2.GaussianBlur(gray * w, (0, 0), sigma) / np.maximum(norm, .02)
        fields.append(np.clip((background - gray) * 12., -3., 3.))
    return np.where(valid[None], np.stack(fields), 0).astype(np.float32), valid


def pick_patches(features, target, valid):
    available = []
    for index, window in enumerate(grid()):
        mask = target[window] >= .5
        valid_share = valid[window].mean()
        if valid_share < .75:
            continue
        contrast = features[2][window]
        positive = float(np.median(contrast[mask])) if mask.any() else None
        available.append((index, int(mask.sum()), positive, float(np.quantile(contrast, .98))))
    positive = [x for x in available if x[1] >= 20]
    negative = [x for x in available if x[1] == 0]
    chosen = []
    if positive:
        weak = min(positive, key=lambda r: (r[2], -r[1]))
        largest = max(positive, key=lambda r: r[1])
        chosen.extend(dict.fromkeys((weak[0], largest[0])))
    if negative:
        chosen.append(max(negative, key=lambda r: r[3])[0])
    return chosen


def fit_data(training, folder, annotations):
    frames, mask, boundary = [], [], []
    picks, exclusions = [], []
    patches = grid()
    for row in training:
        raw = image_bytes(folder, row)
        try:
            arrays, disk = view(raw)
        except ValueError as exc:
            exclusions.append({'file': row['file'], 'reason': str(exc)})
            continue
        target = np.mean([native_raster(annotations[a['id']]) for a in row['annotators']], axis=0, dtype=np.float32)
        ids = pick_patches(arrays, target, disk)
        picks.extend({'file': row['file'], 'patch': i} for i in ids)
        for i in ids:
            region = patches[i]
            frames.append(arrays[:, region[0], region[1]].astype(np.float16))
            mask.append(target[region].astype(np.float16))
            boundary.append(disk[region])
    return np.stack(frames), np.stack(mask), np.stack(boundary), picks, exclusions


def model_for():
    model = UNet(width=8)
    model.encoders[0].layers[0] = torch.nn.Conv2d(3, 8, 3, padding=1)
    return model


def arm_data(x, arm):
    x = x.copy().astype(np.float32)
    if arm == 'gray':
        x[:, 1:] = 0
    return torch.from_numpy(x).cuda()


def train(data, arm, output, identity, resources, criterion=loss):
    x, y, v = data
    torch.manual_seed(SEED)
    model = model_for().cuda()
    optimizer = torch.optim.AdamW(model.parameters(), lr=.001)
    checkpoint = output / arm / 'last.pt'
    history = []
    if checkpoint.exists():
        payload = load_checkpoint(checkpoint)
        if payload['identity'] != [identity, arm]:
            raise ValueError('暗条局部模型的续训身份不同')
        model.load_state_dict(payload['model'])
        optimizer.load_state_dict(payload['optimizer'])
        history = payload['history']
    for epoch in range(len(history), EPOCHS):
        if (output / 'PAUSE').exists():
            return None
        rng = np.random.default_rng(SEED + epoch)
        order = rng.permutation(len(x))
        values = []
        for i in range(0, len(order), BATCH):
            batch = order[i:i + BATCH]
            target, valid = [torch.tensor(array[batch, None], device='cuda', dtype=torch.float32) for array in (y, v)]
            optimizer.zero_grad(set_to_none=True)
            value = criterion(model(arm_data(x[batch], arm)), target, valid)
            if not bool(torch.isfinite(value)):
                raise ValueError('训练损失非有限')
            value.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 4., error_if_nonfinite=True)
            optimizer.step()
            values.append(float(value.detach()))
        history.append(float(np.mean(values)))
        record = {'identity': [identity, arm], 'history': history, 'model': model.state_dict(), 'optimizer': optimizer.state_dict()}
        commit(checkpoint, lambda stream: torch.save(record, stream))
        state = {'state': 'fitting', 'arm': arm, 'epoch': epoch + 1, 'loss': history[-1], 'resources': resources.sample()}
        atomic_json(output / 'status.json', state)
        print(json.dumps(state), flush=True)
    target = output / arm / 'research.safetensors'
    commit(target, lambda stream: stream.write(save_weights({k: v.cpu().contiguous() for k, v in model.state_dict().items()})))
    return model.eval()


def local_probabilities(model, features, arm, disk):
    windows = [w for w in grid() if disk[w].any()]
    weight = np.maximum(np.outer(np.hanning(TILE), np.hanning(TILE)), .05).astype(np.float32)
    merged, mass = np.zeros((SIZE, SIZE), np.float32), np.zeros((SIZE, SIZE), np.float32)
    for index in range(0, len(windows), BATCH):
        batch = windows[index:index + BATCH]
        x = np.stack([features[:, y, x] for y, x in batch])
        out = model(arm_data(x, arm)).sigmoid()[:, 0].cpu().numpy()
        if not np.isfinite(out).all():
            raise ValueError('局部模型预测非有限')
        for win, tile in zip(batch, out, strict=True):
            merged[win] += tile * weight
            mass[win] += weight
    return np.divide(merged, mass, out=np.zeros_like(merged), where=(mass > 0) & disk)


def measure(folder, observations, refs, models, whole, resources, output):
    rows, unmodeled = [], []
    for row in observations:
        if (output / 'PAUSE').exists():
            return None, unmodeled
        raw = image_bytes(folder, row)
        basic = predict(whole, load_gray(raw)[None])[0]
        targets = [raster(refs[a['id']]) for a in row['annotators']]
        try:
            planes, disk = view(raw)
        except ValueError as exc:
            planes, disk = None, None
            unmodeled.append({'file': row['file'], 'reason': str(exc)})
        probabilities = {'whole': basic}
        for name in MODELS:
            score = resize(local_probabilities(models[name], planes, name, disk), 512) if planes is not None else None
            probabilities[name] = score if score is not None else basic
            probabilities[f'whole_plus_{name}'] = .65 * basic + .35 * score if score is not None else basic
        image = {'file': row['file'], 'station': row['file'][14:16], 'arms': {}}
        for name, score in probabilities.items():
            prediction = score >= THRESHOLD
            pairs = [compare(prediction, ref) for ref in targets]
            coverage = [overlap_profile(prediction, label(ref, structure=CONNECTIVITY)[0]) for ref in targets]
            for a, b in zip(pairs, coverage, strict=True):
                a['zero_overlap_references'] = b['zero_overlap_references']
            image['arms'][name] = pairs
        rows.append(image)
        resources.sample()
    return rows, unmodeled


def held_annotations(source, release, observations):
    originals, published = read(source), read(release)
    book = release_observations(published)
    selected = {a['id'] for r in observations for a in r['annotators']}
    tables = []
    for dataset in (originals, published):
        table = defaultdict(dict)
        for row in dataset['annotations']:
            if row['image_id'] in selected:
                table[row['image_id']][row['id']] = row
        tables.append(table)
    reference, released = tables
    for row in observations:
        identities = {r['id'] for r in book[row['file']]}
        for mark in row['annotators']:
            key = mark['id']
            if (key not in identities or not reference[key] or reference[key].keys() != released[key].keys()
                    or any(not same_annotation(value, released[key][id]) for id, value in reference[key].items())):
                raise ValueError('2022参考与官方及公开版本的对应不符')
    return {key: list(value.values()) for key, value in reference.items()}


def run(source, release, early, holdout, anchor, output):
    output = output_path(output, DATASET)
    if (output / 'summary.json').exists() or (digest(source), digest(release)) != (DATA_SHA256, LABEL_SHA256):
        raise ValueError('输入身份不同或已有完成结果')
    entries, annotations = inventory(source, release)
    available = source_receipt(early, 'train_available', {r['file']: r for r in entries})
    by_station = defaultdict(list)
    for row in available:
        by_station[row['file'][14:16]].append(row)
    selected = [r for station in sorted(by_station) for r in stratified(by_station[station], 12, 'captured_utc')]
    holdout_input = holdout / 'inventory.json'
    frozen = read(holdout / 'protocol.json')
    candidates = read(holdout_input)
    if ((frozen['source_sha256'], frozen['release_sha256']) != (DATA_SHA256, LABEL_SHA256)
            or [r['file'] for r in candidates] != frozen['verified_annotated_files'] or frozen['training_allowed']
            or any(not r['file'].startswith('2022') or r['role'] != 'holdout_2022_read_only' for r in candidates)
            or any(r['file'] >= '2019' for r in selected)):
        raise ValueError('违反已登记的年份隔离')
    held = [r for r in candidates if 'image_sha256' in r]
    evaluation_labels = held_annotations(source, release, held)
    weight = anchor / 'mixed_gong' / 'last.pt'
    if digest(weight) != read(anchor / 'summary.json')['results']['mixed_gong']['weights_sha256']:
        raise ValueError('全图基线校验失败')
    cv2.setNumThreads(2)
    torch.set_num_threads(2)
    x, y, v, training, excluded = fit_data(selected, early, annotations)
    if len(held) < 10 or len(training) < 100:
        raise ValueError('训练或新年份对照数量不足')
    protocol_file = output / 'protocol.json'
    protocol = {'code_sha256': digest(__file__), 'early_sha256': digest(early / 'inventory.json'), 'patches': training,
                'holdout_sha256': digest(holdout_input), 'held_files': [r['file'] for r in held], 'fit_exclusions': excluded,
                'epochs': EPOCHS, 'tile': TILE, 'stride': STRIDE, 'threshold': THRESHOLD, 'anchor_sha256': digest(weight),
                'test_accessed': False, 'calibration_accessed': False, 'registered_2022_used_for_fit': False}
    if protocol_file.exists() and read(protocol_file) != protocol:
        raise ValueError('暗条专用实验协议改变')
    if not protocol_file.exists():
        atomic_json(protocol_file, protocol)
    with exclusive(ROOT / '.mdva/training-gpu.lock'):
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cuda.matmul.allow_tf32 = False
        resources = Resources()
        models = {}
        for arm in MODELS:
            model = train((x, y, v), arm, output, digest(protocol_file), resources)
            if model is None:
                return {'state': 'paused', 'arm': arm}
            models[arm] = model
        whole = AuthorGraph('batch').cuda().eval()
        whole.load_state_dict(load_checkpoint(weight)['model'])
        with torch.inference_mode():
            runs, fallbacks = measure(holdout, held, evaluation_labels, models, whole, resources, output)
        usage = resources.sample()
    if runs is None:
        return {'state': 'paused', 'arm': 'evaluation'}
    summary = {}
    for key in runs[0]['arms']:
        cases = [{'comparisons': r['arms'][key]} for r in runs]
        summary[key] = {**metrics(cases), 'zero_overlap_references': sum(a['zero_overlap_references'] for r in cases for a in r['comparisons'])}
    atomic_json(output / 'images.json', runs)
    report = {'state': 'completed', 'recorded_utc': now(), 'protocol_sha256': digest(protocol_file),
              'images_sha256': digest(output / 'images.json'), 'fit_images': len({r['file'] for r in training}),
              'fit_patches': len(training), 'held_images': len(held), 'metrics': summary, 'resources': usage,
              'fallback_to_whole': fallbacks, 'deployed': False, 'test_accessed': False, 'calibration_accessed': False}
    atomic_json(output / 'summary.json', report)
    atomic_json(output / 'status.json', {'state': 'completed', 'summary_sha256': digest(output / 'summary.json')})
    return report


if __name__ == '__main__':
    configure_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ('source', 'release', 'early', 'holdout', 'anchor', 'output'):
        parser.add_argument('--' + arg, type=Path, required=True)
    print(json.dumps(run(**vars(parser.parse_args())), ensure_ascii=False))
