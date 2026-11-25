"""Group public GONG observations by input, preserving independent annotation variants."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
import json
from pathlib import Path
import re

import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import label

from .durable import atomic_json
from .fl_ensemble_study import CONNECTIVITY, overlap_profile
from .journal import read
from .runtime import digest, now


DATA_SHA256 = '6f0cbe45e12d8e7a49847664fe9c9ab9d8ddd4efd9019af907af8e01db585bb6'
TRAIN_CUTOFF = datetime(2018, 11, 1)
SIDE = 512
ARCHIVE_URL = re.compile(
    r'https://gong2\.nso\.edu/HA/hag/(?P<month>\d{6})/(?P<day>\d{8})/(?P<name>\d{14}[A-Z]h)\.jpg'
)


def group_observations(source):
    images = defaultdict(list)
    eligible_ids = set()
    for image in source['images']:
        observed = datetime.fromisoformat(image['date_captured'])
        if observed >= TRAIN_CUTOFF:
            continue
        match = ARCHIVE_URL.fullmatch(image['url'])
        name = observed.strftime('%Y%m%d%H%M%S')
        if (not match or match['month'] != name[:6] or match['day'] != name[:8]
                or not match['name'].startswith(name)
                or image['id'] in eligible_ids or (image['height'], image['width']) != (2048, 2048)):
            raise ValueError('MAGFiLO 图像时间、尺寸或来源有冲突')
        eligible_ids.add(image['id'])
        images[image['url']].append(image)
    annotations = defaultdict(list)
    for item in source['annotations']:
        if item['image_id'] in eligible_ids:
            annotations[item['image_id']].append(item)
    return images, annotations


def raster(annotations, width=2048, height=2048):
    image = Image.new('1', (SIDE, SIDE))
    draw = ImageDraw.Draw(image)
    for annotation in annotations:
        if annotation['iscrowd'] != 0 or not annotation['segmentation']:
            raise ValueError('MAGFiLO 标注不是有效单体轮廓')
        for ring in annotation['segmentation']:
            coordinates = np.asarray(ring, dtype=np.float64).reshape(-1, 2)
            if (len(coordinates) < 3 or not np.isfinite(coordinates).all()
                    or (coordinates < 0).any() or (coordinates > (width, height)).any()):
                raise ValueError('MAGFiLO 标注坐标超界')
            scaled = coordinates * (SIDE / width, SIDE / height)
            draw.polygon([tuple(point) for point in scaled], fill=1)
    return np.asarray(image, dtype=bool)


def compare(first, second):
    labels, n = label(second, structure=CONNECTIVITY)
    p = label(first, structure=CONNECTIVITY)[1]
    overlap = overlap_profile(first, labels)
    size = int(first.sum() + second.sum())
    return {'dice': 2 * int(np.count_nonzero(first & second)) / size if size else None,
            'regions_first': p, 'regions_second': n,
            'matched_iou_030': overlap['matched_iou_030'], 'matched_iou_050': overlap['matched_iou_050']}


def run(source, output):
    if digest(source) != DATA_SHA256:
        raise ValueError('MAGFiLO v1.0 文件身份校验失败')
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('MAGFiLO 审计需要独立输出目录')
    body = read(source)
    groups, annotations = group_observations(body)
    if len(groups) != 824:
        raise ValueError('MAGFiLO 时间边界内的观测数不符')
    rows = []
    for url, variants in sorted(groups.items()):
        identities = sorted(item['id'] for item in variants)
        cases = {name: raster(annotations[name]) for name in identities}
        rows.append({
            'source': url, 'date_captured': variants[0]['date_captured'],
            'variants': [{'id': name, 'annotations': len(annotations[name])} for name in identities],
            'pairs': [{'first': a, 'second': b, **compare(cases[a], cases[b])}
                      for a, b in combinations(identities, 2)],
        })
    values = [pair for row in rows for pair in row['pairs']]
    denominators = sum(pair['regions_first'] + pair['regions_second'] for pair in values)
    report = {
        'state': 'completed', 'recorded_utc': now(),
        'dataset_doi': '10.7910/DVN/J6JNVK', 'annotation_license': 'CC BY-NC 4.0',
        'annotation_sha256': DATA_SHA256, 'annotation_cutoff_exclusive': TRAIN_CUTOFF.isoformat(),
        'unique_observations': len(rows), 'record_count': sum(len(row['variants']) for row in rows),
        'variants_per_observation': dict(Counter(len(row['variants']) for row in rows)),
        'paired_observations': sum(bool(row['pairs']) for row in rows), 'pairs': len(values),
        'paired_grid': SIDE, 'paired_valid_pixel_dice': sum(v['dice'] is not None for v in values),
        'both_empty_pairs': sum(not v['regions_first'] and not v['regions_second'] for v in values),
        'one_empty_pairs': sum(bool(v['regions_first']) != bool(v['regions_second']) for v in values),
        'macro_pair_dice': float(np.mean([v['dice'] for v in values if v['dice'] is not None])),
        'symmetric_pooled_region_f1_at_iou_030': 2 * sum(v['matched_iou_030'] for v in values) / denominators,
        'symmetric_pooled_region_f1_at_iou_050': 2 * sum(v['matched_iou_050'] for v in values) / denominators,
        'test_accessed': False, 'image_inputs_downloaded': 0, 'model_evaluated': False,
        'audit_code_sha256': digest(__file__),
    }
    atomic_json(output / 'observations.json', rows)
    report['observations_sha256'] = digest(output / 'observations.json')
    atomic_json(output / 'summary.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.source, args.output), ensure_ascii=False))


if __name__ == '__main__':
    main()
