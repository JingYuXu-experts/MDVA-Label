"""Pair author-published GONG images and measure a frozen MDVA filament model."""

from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import urlopen

import numpy as np
from PIL import Image
import torch

from .datasets import SegmentationDataset
from .durable import atomic_json, commit
from .fl_author_model import AuthorGraph
from .fl_ensemble_study import DATASET, PILOT, ROOT
from .fl_magfilo_audit import ARCHIVE_URL, DATA_SHA256, SIDE, compare, group_observations, raster
from .journal import read
from .reviewed_train import load_checkpoint
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path

RELEASE_URL = 'https://www.kaggle.com/datasets/esairlab/magfilo-v1-0-segmentation-of-solar-filaments'
LABEL_SHA256 = '1b16fd38409a005daea02aad18d1a47262aa3cf180b1a6f4a95b0ad85fecea67'
DOWNLOAD_URL = 'https://www.kaggle.com/api/v1/datasets/download/esairlab/magfilo-v1-0-segmentation-of-solar-filaments'
PREFIX = 'MAGFiLO_1.0_Images/'
LATE = datetime(2017, 1, 1)
EARLY_END = datetime(2016, 1, 1)
MAX_BYTES = 4 * 1024 * 1024


def observation_name(row):
    captured = datetime.fromisoformat(row['date_captured'])
    filename = row['file_name']
    if (len(filename) != 21 or not filename.startswith(captured.strftime('%Y%m%d%H%M%S'))
            or filename[14] not in 'BCLMPTU' or filename[15:] != 'h.jpeg'):
        raise ValueError(f'发布文件名与观测时间不符 {filename!r}')
    return filename


def release_observations(packaged):
    """Only join records with the exact observation filename and dimensions."""
    groups = defaultdict(list)
    ids = set()
    for row in packaged['images']:
        name = observation_name(row)
        if (row['width'], row['height']) != (2048, 2048) or row['id'] in ids:
            raise ValueError('作者归档的记录尺寸或身份冲突')
        groups[name].append(row)
        ids.add(row['id'])
    return groups


def same_annotation(a, b):
    return all(a[key] == b[key] for key in ('segmentation', 'iscrowd', 'bbox', 'category_id'))


def verify_inventory(original, packaged, filenames):
    groups, annotations = group_observations(original)
    packaged_groups = release_observations(packaged)
    by_identity = {row['id']: row for row in packaged['annotations']}
    release_labels = defaultdict(set)
    for item in packaged['annotations']:
        release_labels[item['image_id']].add(item['id'])
    if len(by_identity) != len(packaged['annotations']):
        raise ValueError('作者归档存在重复标注编号')
    observations = []
    for url, variants in sorted(groups.items()):
        name = ARCHIVE_URL.fullmatch(url)['name'] + '.jpeg'
        if name not in filenames or name not in packaged_groups:
            continue
        matching = {v['id'] for v in packaged_groups[name]}
        verified = []
        for variant in variants:
            identity = variant['id']
            if identity not in matching:
                continue
            labels = annotations[identity]
            if {a['id'] for a in labels} != release_labels[identity]:
                continue
            if any(
                (a['id'] not in by_identity or by_identity[a['id']]['image_id'] != identity
                 or not same_annotation(a, by_identity[a['id']]))
                for a in labels
            ):
                continue
            verified.append(variant)
        if not verified:
            continue
        captured = datetime.fromisoformat(verified[0]['date_captured'])
        role = 'train_available' if captured < EARLY_END else 'transfer_dev' if captured >= LATE else 'temporal_buffer'
        observations.append({
            'file': name, 'captured_utc': captured.isoformat(), 'source': url,
            'role': role, 'annotators': verified,
            'all_original_variants_retained': len(verified) == len(variants),
        })
    return observations, annotations


def load_gray(raw):
    with Image.open(BytesIO(raw)) as image:
        if image.format != 'JPEG' or image.size != (2048, 2048):
            raise ValueError('图像不是同名记录所声明的全尺寸 JPEG')
        gray = image.convert('L').resize((SIDE, SIDE), Image.Resampling.BILINEAR)
        return np.asarray(gray, dtype=np.float32) / 255


def fetch_image(output, row):
    name = row['file']
    path = output / 'jpeg' / name
    url = f'{DOWNLOAD_URL}/{quote(PREFIX + name, safe="")}?datasetVersionNumber=1'
    for attempt in range(2):
        try:
            if path.is_file():
                raw = path.read_bytes()
            else:
                with urlopen(url, timeout=20) as response:
                    raw = response.read(MAX_BYTES + 1)
                if not 0 < len(raw) <= MAX_BYTES:
                    raise ValueError('发布图像超出文件大小限制')
                load_gray(raw)
                commit(path, lambda stream: stream.write(raw))
            load_gray(raw)
            return {**row, 'image_sha256': sha256(raw).hexdigest(), 'bytes': len(raw)}
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            if attempt == 1:
                return {**row, 'download_error': type(exc).__name__}


def inventory(original, release):
    if digest(original) != DATA_SHA256 or digest(release) != LABEL_SHA256:
        raise ValueError('MAGFiLO 公开标注版本身份不符')
    replica = read(release)
    files = {item['file_name'] for item in replica['images']}
    if len(files) != 704:
        raise ValueError('公开图像版本目录不符')
    return verify_inventory(read(original), replica, files)


def metrics(rows):
    pairs = [pair for row in rows for pair in row['comparisons']]
    n = sum(r['regions_second'] for r in pairs)
    p = sum(r['regions_first'] for r in pairs)
    m = sum(r['matched_iou_050'] for r in pairs)
    by_observation = [
        [r['dice'] for r in row['comparisons'] if r['dice'] is not None]
        for row in rows
    ]
    macro = [np.mean(values) for values in by_observation if values]
    return {'images': len(rows), 'annotation_variants': len(pairs),
            'image_macro_dice': float(np.mean(macro)) if macro else None,
            'matched_iou_050': m, 'precision_iou_050': m / p if p else None,
            'recall_iou_050': m / n if n else None,
            'recall_iou_030': sum(r['matched_iou_030'] for r in pairs) / n if n else None,
            'predicted_regions_total_across_variants': p, 'reference_regions_total_across_variants': n}


def run(original, release, output, role='transfer_dev'):
    output = output_path(output, DATASET)
    if (output / 'summary.json').exists() or output == original.resolve().parent:
        raise ValueError('跨台站评估需要新输出目录')
    report = read(PILOT / 'summary.json')
    checkpoint = PILOT / 'checkpoint.pt'
    if digest(checkpoint) != report['checkpoint_sha256'] or report['test_accessed'] or report['calibration_accessed']:
        raise ValueError('来源模型权重身份不符')
    rows, annotation_table = inventory(original, release)
    if rows:
        counts = defaultdict(int)
        for row in rows:
            counts[row['role']] += 1
        selected = [row for row in rows if row['role'] == role]
        if len(selected) < 40:
            raise ValueError('可核验的跨来源开发观测不足')
        local = SegmentationDataset(DATASET, 'FL', 'train', SIDE)
        old_times = np.asarray([datetime.fromisoformat(r['time_utc']).timestamp() for r in local.rows])
        protocol = {
            'release_annotations_sha256': LABEL_SHA256, 'source': RELEASE_URL, 'version': 1,
            'license': 'CC BY-NC 4.0', 'dataverse_annotation_sha256': DATA_SHA256,
            'checkpoint_sha256': digest(checkpoint), 'code_sha256': digest(__file__),
            'threshold': .5, 'image_side': SIDE,
            'dates': 'train available through 2015; buffer 2016; transfer development 2017–2018-10',
            'separation_check': 'extra report for observations over 48h away from any local FL training observation',
            'test_accessed': False, 'calibration_accessed': False, 'role': role,
        }
        marker = output / 'protocol.json'
        if marker.is_file() and read(marker) != protocol:
            raise ValueError('跨台站复测来源发生变化，需建立新目录')
        atomic_json(marker, protocol)
        with ThreadPoolExecutor(max_workers=8) as workers:
            received = []
            for item in workers.map(lambda row: fetch_image(output, row), selected):
                received.append(item)
                if len(received) % 24 == 0:
                    atomic_json(output / 'status.json', {'state': 'downloading', 'processed': len(received), 'total': len(selected)})
        good = [row for row in received if 'image_sha256' in row]
        atomic_json(output / 'inventory.json', received)
        if len({r['image_sha256'] for r in good}) != len(good):
            raise ValueError('公开图像包存在带不同时间戳的字节重复观测')
        if len(good) != len(selected):
            atomic_json(output / 'status.json', {'state': 'incomplete', 'verified': len(good), 'total': len(selected)})
            return {'state': 'incomplete', 'verified': len(good), 'total': len(selected)}
        if role == 'train_available':
            receipt = {'state': 'completed', 'verified': len(good), 'available_by_role': dict(counts),
                       'inventory_sha256': digest(output / 'inventory.json'), 'test_accessed': False}
            atomic_json(output / 'summary.json', receipt)
            return receipt
        saved = load_checkpoint(checkpoint)
        model = AuthorGraph('batch')
        model.load_state_dict(saved['model'])
        with exclusive(ROOT / '.mdva/training-gpu.lock'):
            torch.cuda.reset_peak_memory_stats()
            torch.cuda.set_per_process_memory_fraction(.75)
            torch.backends.cudnn.allow_tf32 = False
            torch.backends.cuda.matmul.allow_tf32 = False
            model.cuda().eval()
            use = Resources()
            measured = []
            with torch.inference_mode():
                for i, row in enumerate(good):
                    raw = (output / 'jpeg' / row['file']).read_bytes()
                    if sha256(raw).hexdigest() != row['image_sha256']:
                        raise ValueError('复核图像已发生改变')
                    pixels = load_gray(raw)
                    logits = model(torch.from_numpy(pixels)[None, None].cuda())
                    if not torch.isfinite(logits).all():
                        raise ValueError('跨台站推理产生非有限数值')
                    mask = logits.sigmoid()[0, 0].cpu().numpy() >= .5
                    stamp = datetime.fromisoformat(row['captured_utc'] + '+00:00').timestamp()
                    hours = float(np.abs(old_times - stamp).min() / 3600)
                    compare_rows = [
                        {'id': a['id'], **compare(mask, raster(annotation_table[a['id']]))}
                        for a in row['annotators']
                    ]
                    measured.append({
                        'file': row['file'], 'captured_utc': row['captured_utc'],
                        'instrument_code': row['file'][14:16], 'nearest_training_hours': hours,
                        'comparisons': compare_rows,
                    })
                    if i % 24 == 0:
                        use.sample()
            resource = use.sample()
        atomic_json(output / 'development.json', measured)
        result = {
            'state': 'completed', 'created_utc': now(), 'available_by_role': dict(counts),
            'confirmed_archive_images': len(good), 'all_variants_retained_images': sum(r['all_original_variants_retained'] for r in good),
            'transfer_dev': metrics(measured),
            'transfer_dev_over_48h_from_local_train': metrics([r for r in measured if r['nearest_training_hours'] > 48]),
            'by_instrument': {code: metrics([r for r in measured if r['instrument_code'] == code])
                              for code in sorted({r['instrument_code'] for r in measured})},
            'inventory_sha256': digest(output / 'inventory.json'), 'development_sha256': digest(output / 'development.json'),
            'protocol_sha256': digest(output / 'protocol.json'), 'resources': resource,
            'test_accessed': False, 'calibration_accessed': False, 'deployed': False,
        }
        atomic_json(output / 'summary.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original', type=Path, required=True)
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--role', choices=('transfer_dev', 'train_available'), default='transfer_dev')
    configure_environment()
    print(json.dumps(run(**vars(parser.parse_args())), ensure_ascii=False))


if __name__ == '__main__':
    main()
