"""Replay edge trimming and short, image-supported bridges on one frozen detector."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import numpy as np
from scipy.ndimage import binary_dilation, find_objects, label
from scipy.spatial import cKDTree
import torch

from .durable import atomic_json
from .fl_direct_centers import architecture, infer, input_planes
from .fl_direct_reviewer import evidence
from .fl_ensemble_study import CONNECTIVITY, DATASET, ROOT
from .fl_magfilo_audit import DATA_SHA256, raster
from .fl_visual_candidate_review import measure
from .journal import read
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


ARMS = ('baseline', 'trim', 'bridge', 'trim_bridge')


def trim_contours(mask, probability, darkness):
    """Keep dark interiors and confident pixels while preserving the parent component."""
    objects, _ = label(mask, structure=CONNECTIVITY)
    result = mask.copy()
    for identity, box in enumerate(find_objects(objects), 1):
        part = objects[box] == identity
        signal = darkness[box]
        if part.sum() < 8:
            continue
        level = np.median(signal[part])
        if level <= 0:
            continue
        kept = part & ((probability[box] >= .62) | (signal >= .50 * level))
        pieces, count = label(kept, structure=CONNECTIVITY)
        if count > 1:
            sizes = np.bincount(pieces.ravel())
            kept = pieces == (sizes[1:].argmax() + 1)
        if kept.sum() >= .60 * part.sum():
            result[box] = (result[box] & ~part) | kept
    return result


def points_to_endpoint(points, end, direction):
    nearby = points[np.linalg.norm(points - end, axis=1) <= 6]
    if len(nearby) < 3:
        return False
    vector = end - nearby.mean(axis=0)
    return np.linalg.norm(vector) >= 1 and np.dot(vector, direction) >= .75 * np.linalg.norm(vector)


def bridge_contours(mask, probability, darkness, valid):
    """Join at most one short, aligned gap per part when supported by the image."""
    objects, _ = label(mask, structure=CONNECTIVITY)
    parts = []
    for identity, box in enumerate(find_objects(objects), 1):
        part = objects[box] == identity
        if part.sum() >= 5:
            points = np.argwhere(part) + [box[0].start, box[1].start]
            parts.append((identity, points, box))
    proposals = []
    for index, (ia, first, a) in enumerate(parts):
        tree = cKDTree(first)
        for ib, second, b in parts[index + 1:]:
            if any(max(x.start - y.stop, y.start - x.stop) > 4 for x, y in zip(a, b, strict=True)):
                continue
            dist, nearest = tree.query(second)
            target = dist.argmin()
            length = dist[target]
            if not 1.45 <= length <= 5.:
                continue
            end_a, end_b = first[nearest[target]], second[target]
            direction = (end_b - end_a) / length
            if not (points_to_endpoint(first, end_a, direction)
                    and points_to_endpoint(second, end_b, -direction)):
                continue
            points = np.unique(np.rint(np.linspace(end_a, end_b, 2 * int(np.ceil(length)) + 1)).astype(int), axis=0)
            y, x = points[objects[tuple(points.T)] == 0].T
            if not len(y) or not valid[y, x].all() or (probability[y, x] < .22).any():
                continue
            if np.median(probability[y, x]) < .33 or np.median(darkness[y, x]) <= 0:
                continue
            proposals.append((length, ia, ib, y, x))
    result = mask.copy()
    used = set()
    for _, ia, ib, y, x in sorted(proposals, key=lambda record: record[:3]):
        if ia in used or ib in used:
            continue
        # Inspect every neighboring pixel before insertion, including previously added bridges.
        box = (slice(max(0, y.min() - 1), y.max() + 2), slice(max(0, x.min() - 1), x.max() + 2))
        region = np.zeros_like(result[box])
        region[y - box[0].start, x - box[1].start] = True
        neighborhood = binary_dilation(region, structure=CONNECTIVITY)
        touched = {int(value) for value in np.unique(objects[box][neighborhood & mask[box]])}
        if touched != {ia, ib} or (neighborhood & (result[box] & ~mask[box])).any():
            continue
        result[y, x] = True
        used.update((ia, ib))
    return result, len(used) // 2


def replay(raw, model):
    fields, valid, _, gray = input_planes(raw)
    probability, _ = infer(model, fields, valid)
    valid = valid[::2, ::2]
    weighted = valid.astype(np.float32)
    local = cv2.GaussianBlur(gray * weighted, (0, 0), 3.) / np.maximum(
        cv2.GaussianBlur(weighted, (0, 0), 3.), .02)
    darkness = local - gray
    base = probability >= .5
    trimmed = trim_contours(base, probability, darkness)
    bridged, count = bridge_contours(base, probability, darkness, valid)
    joined, trim_count = bridge_contours(trimmed, probability, darkness, valid)
    return dict(zip(ARMS, (base, trimmed, bridged, joined), strict=True)), (count, trim_count)


def run(source, study, output):
    output = output_path(output, DATASET)
    annotation_path = source / 'magfilo_2024_v1.0.json'
    if output.exists() or digest(annotation_path) != DATA_SHA256:
        raise ValueError('需要新目录及固定标注')
    receipt = study / 'fl-sparse-reviewer-02'
    parent = read(receipt / 'summary.json')
    detector = study / 'fl-direct-centers-01'
    info = read(detector / 'summary.json')
    weights = detector / 'direct.pt'
    if (parent['state'] != 'completed' or parent['test_accessed'] or info['test_accessed']
            or digest(weights) != info['model_sha256']
            or digest(detector / 'protocol.json') != info['protocol_sha256']
            or digest(receipt / 'protocol.json') != parent['protocol_sha256']):
        raise ValueError('复算模型或样本身份不符')
    data = read(annotation_path)
    refs = defaultdict(list)
    images = {row['id']: row for row in data['images']}
    for row in data['annotations']:
        refs[row['image_id']].append(row)
    dev, old = source / 'transfer-dev-01', study / 'fl-gong-fused-2019-2020-01'
    sparse = source / 'archive-fresh-2019-2020-01'
    archives = {
        'development': (dev, read(dev / 'inventory.json')),
        '2019_2020': (old, read(old / 'inventory.json')),
        'new_2018': (receipt, read(receipt / 'download.json')),
        'sparse_replay': (sparse, read(sparse / 'image-label-identity.json')['files']),
    }
    cohorts = {}
    for phase, (folder, inventory) in archives.items():
        by_name = {r['file']: r for r in inventory}
        cases = read(receipt / f'{phase}-cases.json')
        cohorts[phase] = [(folder, by_name[row['file']]) for row in cases]
    atomic_json(output / 'protocol.json', {'recorded_utc': now(), 'code_sha256': digest(__file__),
        'detector_sha256': info['model_sha256'], 'parent_sha256': digest(receipt / 'summary.json'),
        'parameters': 'Fixed .62 confidence and half-median trim; bridges up to 5 pixels with aligned endpoints, probability and local darkness support.',
        'samples': {k: [row['file'] for _, row in v] for k, v in cohorts.items()}, 'test_accessed': False})
    state = torch.load(weights, map_location='cpu', weights_only=True)
    if state['protocol_sha256'] != info['protocol_sha256']:
        raise ValueError('模型内部训练协议不符')
    network = architecture()
    network.load_state_dict(state['model'])
    torch.set_num_threads(2)
    cv2.setNumThreads(2)
    scores = {}
    with exclusive(ROOT / '.mdva/training-gpu.lock'), torch.inference_mode():
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        network.cuda().eval()
        meter = Resources()
        for phase, records in cohorts.items():
            cases = []
            changes = Counter()
            for folder, row in records:
                name = row['file']
                path = folder / 'jpeg' / name
                recorded = row.get('image_sha256') or row.get('jpeg_sha256') or row.get('sha256')
                if digest(path) != recorded:
                    raise ValueError('图像指纹不符')
                ids = row['variants'] if 'variants' in row else [v['id'] for v in row['annotators']]
                if any(images[identity]['file_name'] != name for identity in ids):
                    raise ValueError('参考与推理图像身份不符')
                references = [label(raster(refs[identity]), structure=CONNECTIVITY)[0] for identity in ids]
                outputs, bridges = replay(path.read_bytes(), network)
                frame = {}
                for arm, mask in outputs.items():
                    _, count = label(mask, structure=CONNECTIVITY)
                    frame[arm] = {'base_count': 0, 'count': count, 'geometry': evidence(mask, references)}
                cases.append({'file': name, 'arms': frame})
                changes.update(trimmed_pixels=int((outputs['baseline'] & ~outputs['trim']).sum()),
                               bridges=bridges[0], bridges_after_trim=bridges[1])
                meter.sample()
            aggregate = {arm: measure([row['arms'][arm] for row in cases], 'all', 0.) for arm in ARMS}
            if aggregate['baseline'] != parent['scores'][phase]['scores']['raw']:
                raise ValueError(f'{phase} 原始定位结果未复现')
            scores[phase] = {'images': len(cases), 'scores': aggregate, 'changes': dict(changes)}
            atomic_json(output / f'{phase}-cases.json', cases)
        resources = meter.sample()
    atomic_json(output / 'summary.json', {'state': 'completed', 'recorded_utc': now(), 'scores': scores,
        'protocol_sha256': digest(output / 'protocol.json'), 'test_accessed': False, 'deployed': False,
        'resources': resources})


if __name__ == '__main__':
    configure_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'study', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    run(**vars(parser.parse_args()))
