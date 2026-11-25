"""Measure loss of appearance detail between native GONG and 512-pixel inputs."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import distance_transform_edt, find_objects, label

from .durable import atomic_json
from .fl_candidate_review_2021 import publicly_paired
from .fl_ensemble_study import CONNECTIVITY
from .fl_magfilo_audit import DATA_SHA256, raster
from .fl_magfilo_transfer import LABEL_SHA256
from .journal import read
from .runtime import digest, now, output_path


SCALES = (2048, 1024, 512)


def native_raster(annotations):
    image = Image.new('1', (2048, 2048))
    draw = ImageDraw.Draw(image)
    for item in annotations:
        for ring in item['segmentation']:
            draw.polygon([tuple(xy) for xy in np.asarray(ring).reshape(-1, 2)], fill=1)
    return np.asarray(image, dtype=bool)


def contrast(image, foreground, excluded):
    """Local same-footprint comparison, with a fixed annulus in native pixels."""
    distance = distance_transform_edt(~foreground)
    ring = (distance >= 4) & (distance <= 12) & ~excluded & (image > 4) & (image < 250)
    if int(ring.sum()) < 16 or int(foreground.sum()) < 8 or (image[foreground] >= 250).mean() > .05:
        return None
    return float(image[ring].mean() - image[foreground].mean())


def measure(native, orig_labels, windows, image_planes, report):
    item = report['reference_id']
    window = windows[item - 1]
    core = orig_labels[window] == item
    y, x = window
    support = np.repeat(np.repeat(core, 4, axis=0), 4, axis=1)
    within = native[4 * y.start:4 * y.stop, 4 * x.start:4 * x.stop] & support
    if within.sum() < 8:
        return None
    padded = (slice(max(0, y.start * 4 - 16), min(2048, y.stop * 4 + 16)),
              slice(max(0, x.start * 4 - 16), min(2048, x.stop * 4 + 16)))
    local = np.zeros(native[padded].shape, bool)
    y0 = 4 * y.start - padded[0].start
    x0 = 4 * x.start - padded[1].start
    local[y0:y0 + within.shape[0], x0:x0 + within.shape[1]] = within
    values = {str(size): contrast(plane[padded], local, native[padded]) for size, plane in image_planes.items()}
    distances = distance_transform_edt(np.pad(local, 1))[1:-1, 1:-1][local]
    return {'native_pixels_compared': int(local.sum()), 'native_interior_diameter_p90_px': float(2 * np.quantile(distances, .90)),
            'display_gray_difference': values}


def run(dataset, verified, agreement, source, release, output):
    output = output_path(output, dataset)
    if output.exists() or any(output.is_relative_to(p.resolve()) for p in (verified, agreement)):
        raise ValueError('对比报告需要独立目录')
    prior, consensus = (read(p / 'summary.json') for p in (verified, agreement))
    if (prior['state'] != 'completed' or consensus['state'] != 'completed'
            or consensus['prior_sha256'] != digest(verified / 'summary.json')
            or consensus['images_sha256'] != digest(agreement / 'images.json')
            or digest(source) != DATA_SHA256 or digest(release) != LABEL_SHA256):
        raise ValueError('图像或跨标注诊断身份不符')
    dates, annotations = publicly_paired(source, release)
    cases = {r['image']: r for r in read(agreement / 'images.json')}
    photos = {r['file']: r for r in read(verified / 'inventory.json')}
    outputs = []
    for row in dates:
        name = row['file']
        grouped = defaultdict(list)
        for item in cases[name]['references']:
            if item['basis'] == 'corroborated_at_iou_50':
                grouped[item['variant']].append(item)
        if not grouped:
            continue
        file = verified / 'jpeg' / name
        if digest(file) != photos[name]['image_sha256']:
            raise ValueError('原始图像指纹不符')
        with Image.open(file) as photo:
            if photo.size != (2048, 2048) or photo.format != 'JPEG':
                raise ValueError('必须使用原始GONG 2048 JPEG')
            gray = photo.convert('L')
            planes = {s: np.asarray(gray.resize((s, s), Image.Resampling.BILINEAR)
                .resize((2048, 2048), Image.Resampling.NEAREST), dtype=np.uint8) for s in SCALES}
        for i, group in grouped.items():
            polygons = list(annotations[row['annotators'][i]['id']].values())
            native = native_raster(polygons)
            coarse, _ = label(raster(polygons), structure=CONNECTIVITY)
            regions = find_objects(coarse)
            for r in group:
                result = measure(native, coarse, regions, planes, r)
                if result is not None:
                    outputs.append({'file': name, 'station': cases[name]['station'], 'variant': i,
                                    'reference_id': r['reference_id'], 'old_model_any_overlap': r['best_original_candidate_iou'] > 0,
                                    'old_model_matched': r['review_matched'], **result})
    valid = [r for r in outputs if all(v is not None for v in r['display_gray_difference'].values())]
    arms = {}
    for category, group in (('all', valid), ('missed_completely', [r for r in valid if not r['old_model_any_overlap']]),
                            ('strict_matches', [r for r in valid if r['old_model_matched']])):
        if not group:
            continue
        measures = np.array([[r['display_gray_difference'][str(s)] for s in SCALES] for r in group])
        gain = measures[:, 0] - measures[:, -1]
        arms[category] = {'references': len(group), 'images': len({r['file'] for r in group}),
            'median_gray_difference': dict(zip(map(str, SCALES), np.median(measures, axis=0).tolist(), strict=True)),
            'native_minus_512_gain_median': float(np.median(gain)), 'native_minus_512_gain_ge_2': int((gain >= 2).sum()),
            'native_width_indicator_median_px': float(np.median([r['native_interior_diameter_p90_px'] for r in group]))}
    summary = {'state': 'completed', 'recorded_utc': now(), 'code_sha256': digest(__file__), 'agreement_sha256': digest(agreement / 'summary.json'),
               'references_passed_all_scales': len(valid), 'groups': arms, 'support': 'native polygons intersected with same 512 component footprint; keep local ring fixed across scales',
               'scope': 'display intensity, 8-bit gray; not a radiometric measurement or model performance',
               'trained': False, 'test_accessed': False, 'calibration_accessed': False}
    atomic_json(output / 'references.json', outputs)
    summary['references_sha256'] = digest(output / 'references.json')
    atomic_json(output / 'summary.json', summary)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ('dataset', 'verified', 'agreement', 'source', 'release', 'output'):
        parser.add_argument('--' + arg, type=Path, required=True)
    print(json.dumps(run(**vars(parser.parse_args())), ensure_ascii=False))
