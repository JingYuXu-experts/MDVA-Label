"""Fit independent FL candidate reviewers on early labels and verify on later images."""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
import json

import numpy as np
from scipy.ndimage import binary_dilation, binary_erosion, find_objects, label
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
import torch

from .ar_hmi_temporal_ranker import build_metrics, matching, object_scores
from .datasets import SegmentationDataset
from .durable import atomic_json
from .fl_author_model import AuthorGraph
from .fl_ensemble_study import CONNECTIVITY, DATASET, ROOT
from .fl_magfilo_adaptation import source_receipt, stratified
from .fl_magfilo_audit import raster
from .fl_magfilo_transfer import inventory, load_gray
from .journal import read
from .reviewed_train import load_checkpoint
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path


FEATURES = (
    'log_area', 'fill', 'elongation', 'boundary_fraction', 'radial_position',
    'mean_score', 'lower_score', 'peak_score', 'score_above_07',
    'inside_brightness', 'rim_minus_inside', 'rim_contrast',
)
RETENTION = .98


def describe(gray, probabilities):
    if gray.shape != (512, 512) or probabilities.shape != gray.shape or not np.isfinite(probabilities).all():
        raise ValueError('暗条候选图像和概率网格无效')
    regions, _ = label(probabilities >= .5, structure=CONNECTIVITY)
    result = []
    for identity, (y, x) in enumerate(find_objects(regions), 1):
        selected = regions[y, x] == identity
        scores = probabilities[y, x][selected]
        xy = np.column_stack(np.nonzero(selected)).astype(float)
        centered = xy - xy.mean(axis=0)
        minor, major = np.linalg.eigvalsh(centered.T @ centered / len(xy))
        y2 = slice(max(0, y.start - 5), min(gray.shape[0], y.stop + 5))
        x2 = slice(max(0, x.start - 5), min(gray.shape[1], x.stop + 5))
        window = regions[y2, x2]
        extension = binary_dilation(window == identity, structure=CONNECTIVITY, iterations=5)
        edge = gray[y2, x2][extension & (window == 0)]
        inside = gray[y, x][selected]
        center = float(np.median(inside))
        surrounding = float(np.median(edge)) if edge.size else center
        result.append([
            float(np.log1p(len(xy))), len(xy) / selected.size,
            float(np.log((major + .25) / (minor + .25))),
            1 - np.count_nonzero(binary_erosion(selected)) / len(xy),
            float(np.hypot((x.start + x.stop - 1) / 2 - 255.5, (y.start + y.stop - 1) / 2 - 255.5) / 256),
            float(scores.mean()), float(np.quantile(scores, .2)), float(scores.max()), float((scores >= .7).mean()),
            center, surrounding - center, float(edge.std()) if edge.size else 0.,
        ])
    return regions, np.asarray(result, dtype=float).reshape(-1, len(FEATURES))


def collect(model, gray, references, name, time, station):
    logits = model(torch.from_numpy(gray.copy())[None, None].cuda())
    objects, inputs = describe(gray, logits.float().sigmoid()[0, 0].cpu().numpy())
    areas = [{'area': int(size)} for size in np.bincount(objects.ravel())[1:]]
    kept = np.ones(len(areas), dtype=bool)
    geometry = []
    for mask in references:
        ref = label(mask, structure=CONNECTIVITY)[0]
        geometry.append(object_scores(objects, areas, ref, kept)[1])
    return {
        'name': name, 'time': time, 'station': station, 'features': inputs.tolist(), 'geometry': geometry,
    }


def features(row):
    return np.asarray(row['features'], dtype=float).reshape(-1, len(FEATURES))


def votes(row):
    initial = np.ones(len(row['features']), dtype=bool)
    answers = np.zeros(len(initial), dtype=int)
    for geometry in row['geometry']:
        matched, _ = matching(geometry, initial)
        answers[matched] += 1
    return answers


def fit_model(rows):
    training, labels = [], []
    for row in rows:
        marks = votes(row)
        enough = (marks == 0) | (marks == len(row['geometry']))
        training.extend(features(row)[enough])
        labels.extend(marks[enough] > 0)
    if min(sum(labels), len(labels) - sum(labels)) < 40:
        raise ValueError('拟合需要至少40个一致正例和40个一致负例')
    scale = StandardScaler().fit(training)
    model = LogisticRegression(C=1, class_weight='balanced', max_iter=500).fit(scale.transform(training), labels)
    return {'mean': scale.mean_.tolist(), 'scale': scale.scale_.tolist(),
            'weights': model.coef_[0].tolist(), 'intercept': float(model.intercept_[0]),
            'fit_candidates': len(labels), 'fit_positive': int(sum(labels))}


def score_row(row, model):
    matrix = features(row)
    return (matrix - model['mean']) / model['scale'] @ model['weights'] + model['intercept']


def choose_cutoff(rows, score):
    values = [np.repeat(score(row), votes(row)) for row in rows]
    positive = np.sort(np.concatenate(values))
    if len(positive) < 25:
        raise ValueError('保留规则没有足够的已匹配样本')
    # Keep at least 98% of the matches independently observed in the policy period.
    limit = int(np.floor(len(positive) * (1 - RETENTION)))
    return float(positive[limit])


def apply_review(row, model, cuts):
    score = score_row(row, model)
    masks = {
        'original': np.ones(len(score), dtype=bool),
        'probability': features(row)[:, 5] >= cuts['probability'],
        'shape_and_brightness': score >= cuts['shape_and_brightness'],
    }
    return {
        **row, 'review': {
            key: [build_metrics(g, keep)[0] for g in row['geometry']] for key, keep in masks.items()
        }
    }


def aggregate(rows):
    metrics = {}
    for name in ('original', 'probability', 'shape_and_brightness'):
        groups = [g for row in rows for g in row['review'][name]]
        counters = {entry: sum(grp[entry] for grp in groups) for entry in ('reference', 'predicted', 'matched')}
        n, p, m = counters.values()
        metrics[name] = {**counters, 'unmatched': p - m, 'precision': m / max(1, p), 'recall': m / max(1, n)}
    return metrics


def pick(rows, limit, time, group):
    grouped = defaultdict(list)
    for row in rows:
        grouped[group(row)].append(row)
    return [row for key in sorted(grouped)
            for row in stratified(grouped[key], min(limit, len(grouped[key])), time)]


def run(source, release, early, later, adaptation, replay, output):
    output = output_path(output, DATASET)
    if output.exists() or any(output.is_relative_to(p.resolve()) for p in (early, later, adaptation, replay)):
        raise ValueError('候选复核须使用新的独立输出目录')
    original = read(adaptation / 'summary.json')
    teacher = read(replay / 'summary.json')
    if any(s['state'] != 'completed' or s['test_accessed'] or s['calibration_accessed'] for s in (original, teacher)):
        raise ValueError('暗条候选复核只接受已登记检查点')
    local_weights = replay / 'gong_local_replay' / 'last.pt'
    gong_weights = adaptation / 'mixed_gong' / 'last.pt'
    if (digest(local_weights) != teacher['result']['weights_sha256']
            or digest(gong_weights) != original['results']['mixed_gong']['weights_sha256']):
        raise ValueError('暗条复核的模型校验失败')
    plan = read(adaptation / 'protocol.json')
    if digest(adaptation / 'protocol.json') != original['protocol_sha256']:
        raise ValueError('适配清单摘要不符')
    registry, annotations = inventory(source, release)
    indexed = {row['file']: row for row in registry}
    initial = source_receipt(early, 'train_available', indexed)
    outside = source_receipt(later, 'transfer_dev', indexed)
    if len(outside) != 90:
        raise ValueError('跨来源开发图像不完整')
    candidates = [r for r in initial if r['file'] not in plan['gong_training_images']]
    local = SegmentationDataset(DATASET, 'FL', 'train', 512)
    val = SegmentationDataset(DATASET, 'FL', 'validation', 512)
    source_rows = [r for r in local.rows if r['uri'] not in plan['local_training_uris']]
    local_split = {
        role: pick([r for r in source_rows if start <= r['time_utc'] < stop], 64, 'time_utc', lambda r:r['instrument'])
        for role, start, stop in (('fit', '2008', '2015'), ('policy', '2016', '2019'))
    }
    local_split['dev'] = val.rows
    external_split = {
        'fit': [r for r in candidates if r['captured_utc'] < '2014'],
        'policy': [r for r in candidates if r['captured_utc'].startswith('2015')],
        'dev': outside,
    }
    collections = {'local':local_split, 'gong':external_split}
    scheme = {
        'created_utc': now(), 'code_sha256': digest(__file__), 'features': FEATURES, 'threshold': .5,
        'weights_sha256': {'local':digest(local_weights), 'gong':digest(gong_weights)},
        'fit_windows': 'local 2008–2014, GONG 2011–2013; policy local 2016–2018 and GONG 2015',
        'fit_limitations': 'Local original segmenter previously trained on the local training split; exclude explicitly adapted images in both groups.',
        'samples': {group:{role:[r.get('file', r.get('uri')) for r in values] for role,values in cohorts.items()}
                    for group,cohorts in collections.items()},
        'policy_retention': RETENTION, 'gate': 'verified precision +5 pp and recall loss ≤1 pp on each development group',
        'test_accessed': False, 'calibration_accessed': False,
    }
    atomic_json(output / 'protocol.json', scheme)
    local_indices = {r['uri']: i for i, r in enumerate(local.rows)}
    values = {}
    with exclusive(ROOT / '.mdva/training-gpu.lock'), torch.inference_mode():
        torch.cuda.reset_peak_memory_stats()
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.backends.cudnn.allow_tf32 = False
        torch.backends.cuda.matmul.allow_tf32 = False
        resources = Resources()
        for group, path in (('local', local_weights), ('gong', gong_weights)):
            model = AuthorGraph('batch').cuda().eval()
            model.load_state_dict(load_checkpoint(path)['model'])
            values[group] = {}
            for role, rows in collections[group].items():
                measured = []
                for i, row in enumerate(rows):
                    if group == 'local':
                        item = val[i] if role == 'dev' else local[local_indices[row['uri']]]
                        arguments = (item['image'][0].numpy(), [item['semantic_mask'][0].numpy() > 0],
                                     row['uri'], row['time_utc'], row['instrument'])
                    else:
                        root = later if role == 'dev' else early
                        image = load_gray((root / 'jpeg' / row['file']).read_bytes())
                        references = [raster(annotations[label_row['id']]) for label_row in row['annotators']]
                        arguments = (image, references, row['file'], row['captured_utc'], row['file'][14:16])
                    measured.append(collect(model, *arguments))
                    if i % 32 == 0:
                        resources.sample()
                values[group][role] = measured
        resource = resources.sample()
    results, rules = {}, {}
    for group, dataset in values.items():
        fitted = fit_model(dataset['fit'])
        cuts = {
            'probability': choose_cutoff(dataset['policy'], lambda row: features(row)[:, 5]),
            'shape_and_brightness': choose_cutoff(dataset['policy'], lambda row: score_row(row, fitted)),
        }
        roles = {name:[apply_review(row, fitted, cuts) for row in dataset[name]] for name in ('policy', 'dev')}
        results[group] = {name: aggregate(rows) for name, rows in roles.items()}
        primary = results[group]['dev']['original']
        expected = (teacher['result']['local']['regions']['matched'] if group == 'local'
                    else original['results']['mixed_gong']['gong']['matched_iou_050'])
        if expected != primary['matched']:
            raise ValueError('开发基线匹配数不能复算')
        results[group]['by_station'] = {
            station: aggregate([row for row in roles['dev'] if row['station'] == station])
            for station in sorted({row['station'] for row in roles['dev']})
        }
        rules[group] = {'classifier': fitted, 'cutoffs': cuts}
        atomic_json(output / f'{group}-development.json', roles['dev'])
    atomic_json(output / 'rules.json', rules)
    gate = {}
    for group, data in results.items():
        raw, scored = (data['dev'][k] for k in ('original','shape_and_brightness'))
        gate[group] = scored['precision'] >= raw['precision'] + .05 and scored['recall'] >= raw['recall'] - .01
    report = {
        'state': 'completed', 'created_utc': now(), 'domain_gate_passed': gate, 'results': results,
        'sample_counts': {g:{role:len(items) for role,items in dataset.items()} for g,dataset in values.items()},
        'protocol_sha256': digest(output / 'protocol.json'), 'rule_sha256': digest(output / 'rules.json'),
        'development_sha256': {g:digest(output / f'{g}-development.json') for g in values},
        'resources': resource, 'test_accessed': False, 'calibration_accessed': False, 'deployed': False,
    }
    atomic_json(output / 'summary.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'release', 'early', 'later', 'adaptation', 'replay', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    configure_environment()
    report = run(**vars(parser.parse_args()))
    print(json.dumps({k: report[k] for k in ('state','domain_gate_passed','sample_counts')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
