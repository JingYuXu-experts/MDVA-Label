"""Separate the effect of GONG image detail from the effect of a smaller field of view."""

from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import binary_dilation, label
import torch
from torch.nn import functional as F

from .durable import atomic_json
from .fl_author_model import AuthorGraph
from .fl_ensemble_study import CONNECTIVITY, DATASET, ROOT, overlap_profile
from .fl_magfilo_adaptation import source_receipt
from .fl_magfilo_audit import compare, raster
from .fl_magfilo_transfer import inventory, load_gray, metrics
from .journal import read
from .reviewed_train import load_checkpoint
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path

GRID = 1024
TILE = 512
POSITIONS = (0, 256, 512)
NAMES = ('whole', 'smoothed_tiles', 'detail_tiles', 'detail_additions', 'detail_outline')


def windows():
    return tuple((slice(y, y + TILE), slice(x, x + TILE)) for y in POSITIONS for x in POSITIONS)


def stitch(tiles):
    taper = .1 + .9 * np.hanning(TILE).astype(np.float32)
    weights = taper[:, None] * taper[None, :]
    result = np.zeros((GRID, GRID), dtype=np.float32)
    count = np.zeros_like(result)
    for pane, window in zip(tiles, windows(), strict=True):
        if pane.shape != weights.shape or not np.isfinite(pane).all():
            raise ValueError('分块网格或概率非法')
        result[window] += pane * weights
        count[window] += weights
    if (count <= 0).any():
        raise ValueError('分块未覆盖完整图像')
    return result / count


def predict(model, batch):
    logits = model(torch.from_numpy(batch[:, None]).cuda())
    if not torch.isfinite(logits).all():
        raise ValueError('高分辨率分割出现非有限数值')
    return logits.float().sigmoid()[:, 0].cpu().numpy()


def tiles_for(model, image):
    batch = np.stack([image[window] for window in windows()])
    return stitch(np.concatenate([predict(model, batch[i:i + 2]) for i in range(0, len(batch), 2)]))


def resize(data, side):
    return F.interpolate(torch.from_numpy(data)[None, None], size=(side, side),
                         mode='bilinear', align_corners=False, antialias=True)[0, 0].numpy()


def add_isolated(primary, new):
    raster, n = label(new, structure=CONNECTIVITY)
    occupied = binary_dilation(primary, structure=CONNECTIVITY)
    touching = np.unique(raster[occupied])
    keep = np.ones(n + 1, dtype=bool)
    keep[0] = False
    keep[touching] = False
    return primary | keep[raster]


def reference_at_1024(annotations):
    reference = Image.new('1', (GRID, GRID))
    pen = ImageDraw.Draw(reference)
    for annotation in annotations:
        for ring in annotation['segmentation']:
            coordinates = np.asarray(ring, dtype=float).reshape(-1, 2) * (GRID / 2048)
            if not np.isfinite(coordinates).all() or len(coordinates) < 3:
                raise ValueError('高分辨率参考标注坐标不符')
            pen.polygon([tuple(point) for point in coordinates], fill=1)
    return np.asarray(reference, dtype=bool)


def reference_counts(mask, reference):
    objects, n_objects = label(reference, structure=CONNECTIVITY)
    return {**overlap_profile(mask, objects), 'reference': int(n_objects)}


def run(source, release, development, model_source, output):
    torch.set_num_threads(2)
    output = output_path(output, DATASET)
    if output.exists() or any(output.is_relative_to(p.resolve()) for p in (development, model_source)):
        raise ValueError('分辨率对照须使用新的独立输出目录')
    baseline, tuned = (read(p / 'summary.json') for p in (development, model_source))
    if (any(r['state'] != 'completed' or r['test_accessed'] or r['calibration_accessed'] for r in (baseline, tuned))
            or tuned['protocol_sha256'] != digest(model_source / 'protocol.json')
            or read(model_source / 'protocol.json')['later_inventory_sha256'] != digest(development / 'inventory.json')):
        raise ValueError('高分辨率验证的输入不符')
    path = model_source / 'mixed_gong' / 'last.pt'
    if digest(path) != tuned['results']['mixed_gong']['weights_sha256']:
        raise ValueError('GONG 适配权重身份不符')
    paired, ann = inventory(source, release)
    records = source_receipt(development, 'transfer_dev', {r['file']: r for r in paired})
    if len(records) != 90 or any(row['captured_utc'] >= '2019' for row in records):
        raise ValueError('普通开发集时间边界或样本量不符')
    protocol = {
        'code_sha256': digest(__file__), 'weights_sha256': digest(path),
        'input_images_sha256': digest(development / 'inventory.json'), 'images': len(records),
        'original_size': 2048, 'tile_size': TILE, 'detail_grid': GRID, 'tile_positions': POSITIONS,
        'threshold': .5, 'outline_mix': [.65, .35], 'test_accessed': False,
        'calibration_accessed': False, 'external_2021_accessed': False,
        'screening_gate': 'strict recall gain of at least 3 pp, precision loss at most 1 pp, fixed 512 comparison grid',
    }
    atomic_json(output / 'protocol.json', protocol)
    model = AuthorGraph('batch')
    model.load_state_dict(load_checkpoint(path)['model'])
    rows = []
    with exclusive(ROOT / '.mdva/training-gpu.lock'), torch.inference_mode():
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cuda.matmul.allow_tf32 = False
        model.cuda().eval()
        usage = Resources()
        for i, row in enumerate(records):
            raw = (development / 'jpeg' / row['file']).read_bytes()
            if sha256(raw).hexdigest() != row['image_sha256']:
                raise ValueError('分辨率对照的图像已改变')
            coarse = load_gray(raw)
            with Image.open(BytesIO(raw)) as scan:
                image = np.asarray(scan.convert('L').resize((GRID, GRID), Image.Resampling.BILINEAR), dtype=np.float32) / 255
            base = predict(model, coarse[None])[0]
            smooth = resize(coarse, GRID)
            smooth_tile = tiles_for(model, smooth)
            detail_tile = tiles_for(model, image)
            base_at_detail = resize(base, GRID)
            mixed = .65 * base_at_detail + .35 * detail_tile
            tile_levels = {'smoothed_tiles': smooth_tile, 'detail_tiles': detail_tile, 'detail_outline': mixed}
            common = {'whole': base >= .5, **{k: resize(q, 512) >= .5 for k, q in tile_levels.items()}}
            common['detail_additions'] = add_isolated(common['whole'], common['detail_tiles'])
            native = {'whole': base_at_detail >= .5, 'detail_outline': mixed >= .5, 'detail_tiles': detail_tile >= .5}
            annotations = [ann[a['id']] for a in row['annotators']]
            fixed_refs = [raster(mark) for mark in annotations]
            native_refs = [reference_at_1024(mark) for mark in annotations]
            rows.append({
                'file': row['file'], 'time_utc': row['captured_utc'],
                'common_grid': {k: [compare(mask, ref) for ref in fixed_refs] for k, mask in common.items()},
                'detail_grid': {k: [reference_counts(mask, ref) for ref in native_refs] for k, mask in native.items()},
            })
            if i % 10 == 0:
                atomic_json(output / 'progress.json', {'processed': i + 1, 'total': len(records), 'state': 'running'})
                usage.sample()
        resources = usage.sample()
    result = {arm: metrics([{'comparisons': row['common_grid'][arm]} for row in rows]) for arm in NAMES}
    expected = tuned['results']['mixed_gong']['gong']
    if result['whole'] != expected:
        raise ValueError('高分辨率试验未复现固定全图基线')
    full = result['whole']
    gate = {arm: (scores['recall_iou_050'] >= full['recall_iou_050'] + .03
                  and scores['precision_iou_050'] >= full['precision_iou_050'] - .01)
            for arm, scores in result.items() if arm != 'whole'}
    fine = {}
    for arm in rows[0]['detail_grid']:
        accum = Counter()
        for row in rows:
            for score in row['detail_grid'][arm]:
                accum.update(score)
        fine[arm] = dict(accum)
    atomic_json(output / 'images.json', rows)
    summary = {
        'state': 'completed', 'recorded_utc': now(), 'fixed_grid': result, 'detailed_grid_1024': fine,
        'gate': gate, 'protocol_sha256': digest(output / 'protocol.json'), 'images_sha256': digest(output / 'images.json'),
        'resources': resources, 'test_accessed': False, 'calibration_accessed': False,
        'external_2021_accessed': False, 'deployed': False,
    }
    atomic_json(output / 'summary.json', summary)
    atomic_json(output / 'progress.json', {'processed': len(rows), 'total': len(rows), 'state': 'completed'})
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'release', 'development', 'model_source', 'output'):
        parser.add_argument('--' + name.replace('_', '-'), required=True, type=Path)
    configure_environment()
    report = run(**vars(parser.parse_args()))
    print(json.dumps({k: report[k] for k in ('state', 'gate', 'fixed_grid', 'detailed_grid_1024')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
