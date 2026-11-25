"""Read-only test of native AR tile detail under identical local inference."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import binary_dilation, label
import torch
from torch.nn import functional as F

from .ar_region_retention import filter_by_area
from .ar_scale_review import estimate, native_objects
from .ar_scale_study import ROOT, configuration, load_plan, warm_model
from .datasets import SegmentationDataset
from .durable import atomic_json
from .journal import append, read
from .metrics import binary_segmentation
from .reviewed_train import seed_all
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


SIDE = 256
POSITIONS = (0, 192, 384, 576, 768)
PIXEL_THRESHOLD = .40
MINIMUM_AREA = 64


def fixed_grid():
    return tuple((slice(y, y+SIDE), slice(x, x+SIDE)) for y in POSITIONS for x in POSITIONS)


def blend_tiles(predictions, grid):
    taper = .1 + .9 * np.hanning(SIDE).astype(np.float32)
    weight = np.outer(taper, taper)
    result = np.zeros((1024, 1024), dtype=np.float32)
    coverage = np.zeros_like(result)
    for prediction, window in zip(predictions, grid, strict=True):
        result[window] += prediction * weight
        coverage[window] += weight
    if (coverage <= 0).any():
        raise ValueError('分块没有覆盖完整原图')
    return result / coverage


def tiled_probabilities(model, gray):
    if gray.shape != (1024, 1024) or gray.dtype != np.uint8:
        raise ValueError('活动区分块仅评估已核验1024网格磁图')
    windows = fixed_grid()
    stack = np.stack([gray[window] for window in windows]).astype(np.float32) / 255
    logits = []
    tensor = torch.from_numpy(stack[:, None]).cuda()
    for batch in tensor.split(4):
        with torch.autocast('cuda', dtype=torch.float16):
            predicted = model(batch)
        if not torch.isfinite(predicted).all():
            raise ValueError('分块推理存在非有限值')
        logits.append(predicted.float().sigmoid()[:, 0].cpu().numpy())
    return blend_tiles(np.concatenate(logits), windows)


def image_pair(image):
    if image.size != (1024, 1024):
        raise ValueError('原图大小与AR训练显示域不同')
    low = image.resize((512, 512), Image.Resampling.BILINEAR)
    return np.asarray(image), np.asarray(low.resize(image.size, Image.Resampling.BILINEAR)), np.asarray(low)


def add_isolated(primary, local):
    components, _ = label(local)
    touching = np.unique(components[binary_dilation(primary)])
    keep = np.full(components.max(initial=0)+1, True)
    keep[touching] = False
    keep[0] = False
    return primary | keep[components]


def run(study, diagnostic, prior, output):
    study, diagnostic, prior = (Path(path).resolve() for path in (study, diagnostic, prior))
    plan = load_plan(study)
    output = output_path(output, plan['dataset'])
    if output.exists() or any(output.is_relative_to(path) for path in (study, diagnostic, prior)):
        raise ValueError('分块对照必须写入新目录')
    previous = read(prior / 'summary.json')
    if (previous['test_accessed'] or previous['manifest_sha256'] != plan['data_identity']['manifest-hashes.json']
            or previous['index_sha256'] != digest(diagnostic / 'case-index.json')
            or previous['images_sha256'] != digest(prior / 'images.json')):
        raise ValueError('历史控制复算身份不匹配')
    control_rows = {row['uri']: row['options']['0.40/16'] for row in read(prior / 'images.json')}
    data = SegmentationDataset(plan['dataset'], 'AR', 'validation')
    if len(data) != 276 or {row['uri'] for row in data.rows} != set(control_rows):
        raise ValueError('必须使用完整同源开发图像')
    protocol = {'created_utc': now(), 'code_sha256': digest(__file__), 'study_sha256': digest(study / 'protocol.json'),
                'reference_sha256': digest(prior / 'summary.json'), 'weights_sha256': plan['weights_sha256'],
                'tile_size': SIDE, 'positions': POSITIONS, 'threshold': PIXEL_THRESHOLD, 'minimum_area': MINIMUM_AREA,
                'design': 'Same model, tile footprint, weighted blending and pixel policy, with native versus filtered texture.',
                'merge': 'Isolated local regions beyond one pixel of global candidate; global masks unchanged.',
                'proposal_gate': {'minimum_new_tiny_under256': 4, 'maximum_new_false_regions': 20},
                'test_accessed': False, 'calibration_accessed': False}
    output.mkdir(parents=True)
    atomic_json(output / 'protocol.json', protocol)
    rows = []
    config = configuration(plan, 512)
    model, _ = warm_model(plan, config)
    with exclusive(ROOT / '.mdva/training-gpu.lock'):
        seed_all(config['seed'])
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        resources = Resources()
        model.cuda().eval()
        with torch.inference_mode():
            for position, row in enumerate(data.rows, 1):
                with Image.open(data.source(row['uri'])) as image:
                    native, filtered, low = image_pair(image.convert('L'))
                reference = np.load(data.source(row['mask_uri']), allow_pickle=False)
                x = torch.from_numpy(low.astype(np.float32) / 255)[None, None].cuda()
                with torch.autocast('cuda', dtype=torch.float16):
                    logits = model(x)
                whole = F.interpolate(logits.float().sigmoid(), size=reference.shape, mode='bilinear',
                                      align_corners=False)[0, 0].cpu().numpy()
                if not np.isfinite(whole).all():
                    raise ValueError('全图控制出现非有限值')
                primary = filter_by_area(whole >= PIXEL_THRESHOLD, MINIMUM_AREA)
                baseline = native_objects(primary, reference)
                old = control_rows[row['uri']]
                if (baseline['reference'], baseline['predicted'], baseline['matched']) != tuple(
                    old[key] for key in ('references', 'predicted', 'matched')
                ):
                    raise ValueError('历史全图区域控制未复现')
                masks = {'whole': primary}
                for name, source in (('native', native), ('filtered', filtered)):
                    local = filter_by_area(tiled_probabilities(model, source) >= PIXEL_THRESHOLD, MINIMUM_AREA)
                    masks[f'{name}_only'] = local
                    masks[f'{name}_added'] = add_isolated(primary, local)
                scores = {name: {'objects': baseline if name == 'whole' else native_objects(mask, reference),
                                 'pixels': binary_segmentation(mask, reference > 0)} for name, mask in masks.items()}
                rows.append({'uri': row['uri'], 'time_utc': row['time_utc'], 'arms': scores})
                if position % 16 == 0:
                    atomic_json(output / 'progress.json', {'state': 'running', 'images': position, 'total': len(data),
                                                            'resource': resources.sample()})
        measured = resources.sample()
    result = {key: estimate(rows, key) for key in rows[0]['arms']}
    policy = protocol['proposal_gate']
    tiny_gain = result['native_added']['by_reference_size']['under256']['matched'] - result['whole']['by_reference_size']['under256']['matched']
    false_gain = result['native_added']['false_positive_regions'] - result['whole']['false_positive_regions']
    atomic_json(output / 'images.json', rows)
    report = {'state': 'completed', 'images': len(rows), 'results': result,
              'native_addition_tiny_gain': tiny_gain, 'native_addition_false_gain': false_gain,
              'proposal_gate_passed': tiny_gain >= policy['minimum_new_tiny_under256'] and false_gain <= policy['maximum_new_false_regions'],
              'scope': 'Geometry and input texture feasibility with fixed weights; no local training or product changes.',
              'resource': measured, 'fp32_retry_counts': model.precision_counters,
              'protocol_sha256': digest(output / 'protocol.json'), 'images_sha256': digest(output / 'images.json'),
              'test_accessed': False, 'calibration_accessed': False, 'released': False}
    atomic_json(output / 'progress.json', {'state': 'completed', 'images': len(rows), 'total': len(data)})
    atomic_json(output / 'summary.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('study', 'diagnostic', 'prior', 'output'):
        parser.add_argument('--' + key, required=True)
    args = parser.parse_args()
    configure_environment()
    append(ROOT / 'out/training-20261112-v2', 'development', '活动区原图分块推理',
           '使用相同权重检查全覆盖原图裁片和降采样裁片；本步骤为推理，不更新模型。')
    result = run(**vars(args))
    print(json.dumps({name: {field: item[field] for field in ('region_precision', 'region_recall', 'false_positive_regions',
                                                              'by_reference_size')}
                      for name, item in result['results'].items()}))


if __name__ == '__main__':
    main()
