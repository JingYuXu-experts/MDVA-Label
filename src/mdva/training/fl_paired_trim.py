"""Learn when the frozen H-alpha candidate benefits from edge trimming."""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np
from scipy.ndimage import find_objects, label
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
import torch

from .ar_hmi_temporal_ranker import matching, object_scores
from .durable import atomic_json
from .fl_direct_centers import architecture, infer, input_planes
from .fl_direct_reviewer import evidence
from .fl_ensemble_study import CONNECTIVITY, DATASET, ROOT
from .fl_magfilo_audit import DATA_SHA256, raster
from .fl_shape_replay import trim_contours
from .fl_sparse_reviewer import excluded_days, parsed_day
from .journal import read
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


def describe(raw, model, references):
    fields, valid, _, gray = input_planes(raw)
    probability, center = infer(model, fields, valid)
    valid = valid[::2, ::2].astype(np.float32)
    local = cv2.GaussianBlur(gray * valid, (0, 0), 3.) / np.maximum(cv2.GaussianBlur(valid, (0, 0), 3.), .02)
    darkness = local - gray
    base = probability >= .5
    trimmed = trim_contours(base, probability, darkness)
    objects, count = label(base, structure=CONNECTIVITY)
    features = []
    for ident, box in enumerate(find_objects(objects), 1):
        part = objects[box] == ident
        kept = part & trimmed[box]
        removed = part & ~kept
        if not removed.any():
            features.append(None)
            continue
        area = int(part.sum())
        shape = np.argwhere(part).astype(float)
        centroid = shape.mean(axis=0)
        shape -= centroid
        narrow, long = np.linalg.eigvalsh(shape.T @ shape / area)
        y, x = centroid + [s.start for s in box]
        p, dark = probability[box], darkness[box]
        features.append([float(np.log1p(area)), float(removed.sum()/area),
            float(np.log((long+.5)/(narrow+.5))), float(np.hypot(y-255.5, x-255.5)/256),
            float(p[kept].mean()), float(p[removed].mean()),
            float(center[box][part].max()), float(np.median(dark[kept])),
            float(np.median(dark[removed])), float(np.std(dark[part])),
            float(area / part.size)])
    trimmed_objects = np.where(trimmed, objects, 0)
    areas = [{'area': int(a)} for a in np.bincount(trimmed_objects.ravel(), minlength=count+1)[1:]]
    if any(item['area'] <= 0 for item in areas):
        raise ValueError('修正轮廓必须保留原候选身份')
    after = [object_scores(trimmed_objects, areas, ref, [True]*count)[1] for ref in references]
    return {'features': features, 'original': evidence(base, references), 'trimmed': after}


def targets(row):
    """Estimate utility from IoU changes and strict threshold crossings per reference variant."""
    count = len(row['features'])
    delta, crossed = [], []
    for left, right in zip(row['original'], row['trimmed'], strict=True):
        n = len(left['reference_ids'])
        if not n:
            continue
        before = np.asarray(left['iou']).reshape(count, n).max(axis=1)
        after = np.asarray(right['iou']).reshape(count, n).max(axis=1)
        crossed.append((after >= .5).astype(float) - (before >= .5))
        delta.append(after - before)
    if not delta:
        return np.zeros(count), np.zeros(count)
    crossing = np.mean(crossed, axis=0)
    return crossing + .25 * np.mean(delta, axis=0), crossing


def compare(row, selected):
    count = len(selected)
    totals = np.zeros(4, dtype=int)
    for left, right in zip(row['original'], row['trimmed'], strict=True):
        before = set(matching(left, [True]*count)[1])
        n = len(left['reference_ids'])
        a = np.asarray(left['iou']).reshape(count, n)
        b = np.asarray(right['iou']).reshape(count, n)
        mixed = {**left, 'iou': np.where(selected[:, None], b, a)}
        after = set(matching(mixed, [True]*count)[1])
        totals += (len(before & after), len(after - before), len(before - after), n)
    return totals


def rates(rows, threshold):
    tallies = np.zeros(4, dtype=int)
    changed = 0
    for row in rows:
        selected = np.asarray([s is not None and s >= threshold for s in row['scores']])
        tallies += compare(row, selected)
        changed += int(selected.sum())
    kept, gained, lost, refs = tallies.tolist()
    return {'matched': kept+gained, 'gained': gained, 'lost': lost,
            'reference': refs, 'changed_candidates': changed}


def choose_threshold(rows):
    points = sorted({s for row in rows for s in row['scores'] if s is not None}, reverse=True)
    if not points:
        return None, None
    best, passing = None, None
    for point in points:
        value = rates(rows, point)
        gain, loss = value['gained'], value['lost']
        ranking = (gain-loss, -loss, -value['changed_candidates'], point)
        if best is None or ranking > best:
            best = ranking
        if gain-loss >= 3 and gain >= 2*loss and (passing is None or ranking > passing):
            passing = ranking
    return (None if passing is None else passing[-1]), best[-1]


def run(source, study, output):
    output = output_path(output, DATASET)
    annotation = source / 'magfilo_2024_v1.0.json'
    detector = study / 'fl-direct-centers-01'
    review = study / 'fl-direct-reviewer-01'
    replay = study / 'fl-shape-replay-01'
    sparse = study / 'fl-sparse-reviewer-02'
    info, ref = read(detector/'summary.json'), read(replay/'summary.json')
    config = read(replay/'protocol.json')
    review_protocol = read(review/'protocol.json')
    weight = detector / 'direct.pt'
    if (output.exists() or digest(annotation) != DATA_SHA256 or info['test_accessed'] or ref['test_accessed']
            or digest(weight) != info['model_sha256'] or digest(weight) != config['detector_sha256']
            or digest(replay/'protocol.json') != ref['protocol_sha256']
            or digest(review/'protocol.json') != read(review/'summary.json')['protocol_sha256']):
        raise ValueError('需要新目录、固定图像身份和原实验状态')
    names = {'fit': review_protocol['samples']['fit'], **config['samples']}
    base_fit = read(detector/'protocol.json')['samples']['fit']
    if set(map(parsed_day, names['fit'])) & excluded_days(base_fit):
        raise ValueError('轮廓选择的拟合日期与原检测训练过近')
    resolution = {'fit': (source/'train-pool-01', 'inventory.json'),
        'development': (source/'transfer-dev-01', 'inventory.json'),
        '2019_2020': (study/'fl-gong-fused-2019-2020-01', 'inventory.json'),
        'new_2018': (sparse, 'download.json'),
        'sparse_replay': (source/'archive-fresh-2019-2020-01', 'image-label-identity.json')}
    archives = {}
    for phase, (folder, filename) in resolution.items():
        inventory = read(folder/filename)
        if isinstance(inventory, dict):
            inventory = inventory['files']
        index = {row['file']: row for row in inventory}
        archives[phase] = (folder, [index[name] for name in names[phase]])
    atomic_json(output/'protocol.json', {'code_sha256': digest(__file__),
        'shape_replay_sha256': digest(replay/'summary.json'), 'detector_sha256': digest(weight),
        'sample_names': names, 'supervision': 'mean strict IoU crossing plus 0.25 mean IoU delta',
        'selector': 'scaled ridge alpha 12; dev point net gain >=3, gains at least twice losses',
        'test_accessed': False})
    data = read(annotation)
    refs = defaultdict(list)
    for item in data['annotations']:
        refs[item['image_id']].append(item)
    images = {row['id']: row for row in data['images']}
    state = torch.load(weight, map_location='cpu', weights_only=True)
    if state['protocol_sha256'] != digest(detector/'protocol.json'):
        raise ValueError('原检测检查点内部协议不符')
    network = architecture()
    network.load_state_dict(state['model'])
    torch.set_num_threads(2)
    cv2.setNumThreads(2)
    batches = {}
    with exclusive(ROOT/'.mdva/training-gpu.lock'), torch.inference_mode():
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        network.cuda().eval()
        meter = Resources()
        for phase, (folder, records) in archives.items():
            rows = []
            for record in records:
                name = record['file']
                path = folder/'jpeg'/name
                expected = record.get('image_sha256') or record.get('jpeg_sha256') or record.get('sha256')
                if digest(path) != expected:
                    raise ValueError('Hα图像指纹不符')
                ids = record['variants'] if 'variants' in record else [r['id'] for r in record['annotators']]
                if any(images[i]['file_name'] != name for i in ids):
                    raise ValueError('Hα标注身份不符')
                refs_for_image = [label(raster(refs[i]), structure=CONNECTIVITY)[0] for i in ids]
                rows.append({'file': name, **describe(path.read_bytes(), network, refs_for_image)})
                meter.sample()
            batches[phase] = rows
        resources = meter.sample()
    x, y, importance, changes = [], [], [], []
    for row in batches.pop('fit'):
        utility, crossing = targets(row)
        for feature, response, sign in zip(row['features'], utility, crossing, strict=True):
            if feature is not None:
                x.append(feature)
                y.append(response)
                importance.append(4. if sign else 1.)
                changes.append(float(sign))
    model = make_pipeline(StandardScaler(), Ridge(alpha=12.))
    model.fit(x, y, ridge__sample_weight=importance)
    results = {}
    for phase, rows in batches.items():
        for row in rows:
            usable = [f for f in row['features'] if f is not None]
            scores = iter(model.predict(usable).tolist() if usable else [])
            row['scores'] = [None if f is None else next(scores) for f in row['features']]
        if phase == 'development':
            threshold, research_point = choose_threshold(rows)
            atomic_json(output/'policy.json', {'candidate': threshold, 'exploratory': research_point})
        always = rates(rows, -np.inf)
        original = rates(rows, np.inf)
        expected = ref['scores'][phase]['scores']
        if always['matched'] != expected['trim']['matched'] or original['matched'] != expected['baseline']['matched']:
            raise ValueError('固定原轮廓与收紧对照未复现')
        results[phase] = {'always': always, 'original': original,
            'selected': None if threshold is None else rates(rows, threshold),
            'exploratory': None if research_point is None else rates(rows, research_point)}
        atomic_json(output/f'{phase}-pairs.json', rows)
    atomic_json(output/'summary.json', {'state':'completed', 'recorded_utc':now(),
        'fit_images': len(names['fit']), 'fit_pairs':len(x),
        'fit_crossings': {'beneficial': sum(v>0 for v in changes), 'harmful':sum(v<0 for v in changes)},
        'candidate_threshold': threshold, 'exploratory_threshold': research_point, 'results':results,
        'resources':resources, 'test_accessed':False, 'deployed':False,
        'protocol_sha256':digest(output/'protocol.json')})


if __name__ == '__main__':
    configure_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'study', 'output'):
        parser.add_argument('--'+name, required=True, type=Path)
    run(**vars(parser.parse_args()))
