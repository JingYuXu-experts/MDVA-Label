"""Generate CH contours directly from 193 or 171/193/211 on matched historical grids."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter, label
from sklearn.ensemble import HistGradientBoostingClassifier
from threadpoolctl import threadpool_limits

from .ar_hmi_temporal_ranker import object_scores
from .ch_aux_signal_audit import map_channel
from .ch_boundary_growth import CONNECTIVITY, remove_small
from .ch_matched_tiles import CORE, RADIUS, SEED
from .datasets import SegmentationDataset
from .durable import atomic_json, commit
from .journal import read
from .runtime import digest, now, output_path


CHANNELS = ('193', '171', '211')
FIT_END = '2014'
LIMIT = 2_400
THRESHOLD = .5
ARMS = {'aia193': 4, 'aia171_193_211': 8}


def shared_features(arrays):
    if any(a.shape != (512, 512) for a in arrays.values()):
        raise ValueError('只允许已核验的原生显示网格')
    support = (RADIUS <= CORE) & np.isfinite(list(arrays.values())).all(axis=0)
    support &= arrays['193'] > 0
    weights = support.astype(np.float32)
    smooth = np.maximum(gaussian_filter(weights, 3), .001)
    broad = np.maximum(gaussian_filter(weights, 15), .001)
    logs = {name: np.log1p(np.maximum(np.where(support, field, 0), 0)).astype(np.float32)
            for name, field in arrays.items()}
    es193 = logs['193']
    local = gaussian_filter(es193, 3) / smooth
    context = gaussian_filter(es193, 15) / broad
    common = [es193, local, local - context, (RADIUS / CORE).astype(np.float32)]
    added = []
    for name in ('171', '211'):
        ratio = np.where(support, logs[name] - es193, 0)
        added.extend((ratio, gaussian_filter(ratio, 3) / smooth))
    return np.stack([*common, *added], axis=-1), support


def cards(folder, selection):
    inputs = {}
    for group in ('aia', 'aia171'):
        source = folder / group / 'summary.json'
        j = read(source)
        if (j['state'] != 'completed' or j['protocol_sha256'] != digest(source.with_name('protocol.json'))):
            raise ValueError('观测审计清单不匹配')
        inputs[group] = {r['uri']: r for r in j['results']}
    d = read(folder / 'aia' / 'protocol.json')
    if digest(selection) != d['selection_sha256'] or set(inputs['aia']) != {r['uri'] for r in d['rows']}:
        raise ValueError('官方观测与固定样本不匹配')
    for r in d['rows']:
        a, b = (inputs[n][r['uri']] for n in ('aia', 'aia171'))
        if not (a.get('alignment_review_gate_passed') and b.get('source_gate_passed')):
            continue
        yield r, a, b


def physical(folder, row, original, additional):
    pair = folder / 'aia' / 'pairs' / row['id']
    receipt = pair / 'source-audit.json'
    mapping = pair / 'mapping-review.json'
    j = read(mapping)
    if (digest(mapping) != original['mapping_sha256'] or j['source_audit_sha256'] != digest(receipt)
            or j['train_uri'] != row['uri'] or read(receipt)['train_sha256'] != row['sha256']):
        raise ValueError('观测显示映射来源改变')
    records = {r['channel']: r for r in read(receipt)['sources']}
    field = {c: map_channel(pair, record) for c, record in records.items()}
    extra = folder / 'aia171' / 'pairs' / row['id']
    field['171'] = map_channel(extra, {**additional, 'filename': next(extra.glob('*.fits')).name})
    return {c: field[c] for c in CHANNELS}


def components(prediction, truth):
    refs, count = label(truth, structure=CONNECTIVITY)
    candidates = label(prediction, structure=CONNECTIVITY)[0]
    sizes = [{'area': int(n)} for n in np.bincount(candidates.ravel())[1:]]
    if not sizes:
        return {'matched': 0, 'reference': count, 'predicted': 0}
    metrics = object_scores(candidates, sizes, refs, [True] * len(sizes))[0]
    return {k: metrics[k] for k in ('matched', 'reference', 'predicted')}


def phenotype(prediction, truth, support):
    clipped = truth & support
    tp = int((prediction & clipped).sum())
    fp = int((prediction & ~clipped).sum())
    fn = int((~prediction & clipped).sum())
    return {'pixels': {'tp': tp, 'fp': fp, 'fn': fn, 'dice': 2 * tp / max(2 * tp + fp + fn, 1)},
            'within_support': components(prediction, clipped), 'whole_disk': components(prediction, truth)}


def aggregate(rows):
    result = {}
    for name in ARMS:
        arm = [r['arms'][name] for r in rows]
        result[name] = {'mean_core_dice': float(np.mean([r['pixels']['dice'] for r in arm]))}
        for scope in ('within_support', 'whole_disk'):
            totals = {k: sum(r[scope][k] for r in arm) for k in ('matched', 'reference', 'predicted')}
            totals['precision'] = totals['matched'] / totals['predicted'] if totals['predicted'] else None
            totals['recall'] = totals['matched'] / totals['reference'] if totals['reference'] else None
            result[name][scope] = totals
    return result


def run(dataset, earlier, fresh, output):
    output = output_path(output, dataset)
    if output.exists():
        raise ValueError('整图试验必须使用未使用的目录')
    datasets = {'earlier': earlier, 'fresh': fresh}
    registry = SegmentationDataset(dataset, 'CH', 'train')
    manifest = digest(dataset / 'manifest-hashes.json')
    by_uri = {r['uri']: r for r in registry.rows}
    fixed = {}
    for name, folder in datasets.items():
        selection = folder / 'selection.json'
        if read(selection)['manifest_sha256'] != manifest:
            raise ValueError('固定训练划分改变')
        fixed[name] = list(cards(folder, selection))
    if any(r['uri'] not in by_uri for rows in fixed.values() for r, _, _ in rows):
        raise ValueError('禁止读取冻结训练清单之外的图像')
    earlier_days = {by_uri[r['uri']]['date'] for r, _, _ in fixed['earlier']}
    fresh_days = {by_uri[r['uri']]['date'] for r, _, _ in fixed['fresh']}
    if earlier_days & fresh_days:
        raise ValueError('拟合与新日期组存在交叠')
    provenance = {name: {p.as_posix(): digest(p) for p in (folder / 'selection.json', folder / 'aia/summary.json',
                   folder / 'aia171/summary.json')} for name, folder in datasets.items()}
    protocol = {'created_utc': now(), 'code_sha256': digest(__file__), 'manifest_sha256': manifest,
                'sources': provenance, 'seed': SEED, 'fit_before_year': FIT_END,
                'sample_limit_per_class_per_image': LIMIT, 'threshold': THRESHOLD,
                'model': 'HistGradientBoostingClassifier(max_iter=85,max_leaf_nodes=15,learning_rate=.08,l2_regularization=1)',
                'core_max_radius_px': CORE, 'evaluation': 'one-to-one IoU ≥.50 by connected region, core and full references',
                'alignment': {'minimum_jpg_render_correlation': .98, 'maximum_gray_mae': 5},
                'test_accessed': False, 'validation_accessed': False, 'calibration_accessed': False}
    atomic_json(output / 'protocol.json', protocol)
    rng = np.random.default_rng(SEED)
    training = []
    held = []
    rejected = []
    for name, files in fixed.items():
        for r, a, b in files:
            uri = r['uri']
            original = registry.source(uri)
            if digest(original) != r['sha256']:
                raise ValueError('原图与配准身份改变')
            rendered = datasets[name] / 'aia' / 'pairs' / r['id'] / 'fixed-mapping-193.png'
            with Image.open(original) as jpg, Image.open(rendered) as png:
                inputs = [np.asarray(im.convert('L'), dtype=float)[RADIUS <= CORE] for im in (jpg, png)]
            correlation = float(np.corrcoef(*inputs)[0, 1])
            mae = float(np.mean(abs(inputs[0] - inputs[1])))
            if not (correlation >= .98 and mae <= 5):
                rejected.append({'uri': uri, 'group': name, 'correlation': correlation if np.isfinite(correlation) else None,
                                 'mae': mae, 'reason': 'core_alignment'})
                continue
            observation = physical(datasets[name], r, a, b)
            x, support = shared_features(observation)
            truth = np.load(registry.source(by_uri[uri]['mask_uri']), allow_pickle=False) > 0
            if truth.shape != support.shape:
                raise ValueError('参考标注不是核验过的512网格')
            if name == 'earlier' and by_uri[uri]['time_utc'] < FIT_END:
                for target in (False, True):
                    indexes = np.flatnonzero(support.ravel() & (truth.ravel() == target))
                    chosen = rng.choice(indexes, min(LIMIT, len(indexes)), replace=False)
                    training.append((uri, x.reshape(-1, x.shape[-1])[chosen], np.full(len(chosen), target)))
            else:
                held.append((name, r, x, support, truth))
    train_x = np.vstack([x for _, x, _ in training])
    train_y = np.concatenate([y for _, _, y in training])
    all_dates = {by_uri[uri]['date'] for uri, _, _ in training}
    if len(all_dates) < 5 or not {False, True}.issubset(set(train_y)):
        raise ValueError('可用拟合日期或类别不足')
    output_cases = defaultdict(list)
    with threadpool_limits(limits=3):
        models = {arm: HistGradientBoostingClassifier(max_iter=85, max_leaf_nodes=15, learning_rate=.08,
                  l2_regularization=1, early_stopping=False, random_state=SEED).fit(train_x[:, :width], train_y)
                  for arm, width in ARMS.items()}
        for group, row, x, support, truth in held:
            predicted = {}
            metrics = {}
            for arm, width in ARMS.items():
                labelmap = np.zeros(support.shape, dtype=bool)
                labelmap[support] = models[arm].predict_proba(x[support, :width])[:, 1] >= THRESHOLD
                predicted[arm] = remove_small(labelmap)
                metrics[arm] = phenotype(predicted[arm], truth, support)
            masks = output / 'masks' / (row['id'] + '.npz')
            commit(masks, lambda stream: np.savez_compressed(stream, support=support, **predicted))
            output_cases[group].append({'uri': row['uri'], 'time_utc': by_uri[row['uri']]['time_utc'],
                'mask_artifact': masks.relative_to(output).as_posix(), 'mask_sha256': digest(masks),
                'reference_inside_support_share': int((support & truth).sum()) / max(int(truth.sum()), 1), 'arms': metrics})
    atomic_json(output / 'cases.json', dict(output_cases))
    report = {'state': 'completed', 'recorded_utc': now(), 'protocol_sha256': digest(output / 'protocol.json'),
        'cases_sha256': digest(output / 'cases.json'), 'fit_dates': len(all_dates), 'fit_pixels': len(train_y),
        'cohort_sizes': {g: len(r) for g, r in output_cases.items()}, 'rejected': rejected,
        'average_reference_support': {g: float(np.mean([row['reference_inside_support_share'] for row in r]))
                                      for g, r in output_cases.items()},
        'metrics': {g: aggregate(r) for g, r in output_cases.items()},
        'deployed': False, 'test_accessed': False, 'validation_accessed': False, 'calibration_accessed': False}
    atomic_json(output / 'summary.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ('dataset', 'earlier', 'fresh', 'output'):
        parser.add_argument('--' + argument, type=Path, required=True)
    print(json.dumps(run(**vars(parser.parse_args())), ensure_ascii=False))
