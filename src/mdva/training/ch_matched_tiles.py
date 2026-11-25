"""Compare extra solar channels on fixed brightness-matched CH training tiles."""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path

import numpy as np

from .ch_aux_signal_audit import features, map_channel
from .ch_channel_pairing import filename_time
from .datasets import SegmentationDataset
from .diagnostic_support import sample_id
from .durable import atomic_json
from .journal import read
from .runtime import digest, now, output_path


SIDE = 16
LIMIT = 4
BRIGHTNESS = .10
RADIUS_DELTA = 16
CORE = 195
SEED = 2026
FEATURES = ('171_over_193', '211_over_193', 'los_sign_imbalance')
YY, XX = np.ogrid[:512, :512]
RADIUS = np.hypot(YY - 255.5, XX - 255.5)


def tiles(image):
    return np.asarray(image).reshape(32, SIDE, 32, SIDE).transpose(0, 2, 1, 3).reshape(1024, SIDE * SIDE)


def matching_tiles(gray, reference):
    """Use only the annotation and AIA193 display tile to freeze each pair."""
    radius = tiles(RADIUS)
    inside = radius.max(axis=1) < CORE
    centers = radius.mean(axis=1)
    coverage = tiles(reference).mean(axis=1)
    value = np.median(tiles(gray), axis=1)
    core_background = np.median(gray[RADIUS < CORE])
    eligible = inside & (value > 0) & (value <= .9 * core_background)
    positive = np.flatnonzero(eligible & (coverage >= .90))
    negative = np.flatnonzero(eligible & (coverage == 0))
    delta = abs(value[positive, None] / value[negative] - 1)
    radial = abs(centers[positive, None] - centers[negative])
    options = []
    for i, j in np.argwhere((delta <= BRIGHTNESS) & (radial <= RADIUS_DELTA)):
        p, n = int(positive[i]), int(negative[j])
        x_sep, y_sep = abs(p % 32 - n % 32), abs(p // 32 - n // 32)
        if max(x_sep, y_sep) >= 2:
            options.append((float(delta[i, j] / BRIGHTNESS + radial[i, j] / RADIUS_DELTA), p, n))
    used = set()
    pairs = []
    for _, p, n in sorted(options):
        if p in used or n in used:
            continue
        used.update((p, n))
        pairs.append({'reference': p, 'control': n, 'jpg_193_delta': float(abs(value[p] / value[n] - 1)),
                      'radius_delta_px': float(abs(centers[p] - centers[n]))})
        if len(pairs) == LIMIT:
            break
    return pairs


def freeze(dataset, previous, output):
    from PIL import Image

    output = output_path(output, dataset)
    if output.exists():
        raise ValueError('请使用新的配对目录')
    data = SegmentationDataset(dataset, 'CH', 'train', 512)
    seen = {filename_time(Path(r['uri']).name, '193').date() for r in read(previous)['rows']}
    cohorts = defaultdict(list)
    for row in data.rows:
        stamp = filename_time(Path(row['uri']).name, '193')
        if stamp.date() not in seen and stamp.minute == 0:
            cohorts[stamp.year, stamp.month > 6].append((stamp, row))
    rng = np.random.default_rng(SEED)
    selected = []
    scanned = 0
    for _, candidates in sorted(cohorts.items()):
        candidates.sort(key=lambda r: (r[0], r[1]['uri']))
        options = []
        for index in rng.choice(len(candidates), min(32, len(candidates)), replace=False):
            stamp, row = candidates[index]
            with Image.open(data.source(row['uri'])) as image:
                gray = np.asarray(image.convert('L'))
            mask = np.load(data.source(row['mask_uri']), allow_pickle=False)
            scanned += 1
            if gray.shape != (512, 512) or mask.shape != gray.shape:
                continue
            pairs = matching_tiles(gray, mask > 0)
            if pairs:
                options.append((-len(pairs), sum(p['jpg_193_delta'] for p in pairs), stamp, row, pairs))
        if options:
            _, _, stamp, row, pairs = min(options, key=lambda r: r[:3])
            selected.append({'uri': row['uri'], 'id': sample_id(row['uri']), 'mask_uri': row['mask_uri'],
                             'time': stamp.isoformat(), 'pairs': pairs})
    if len(selected) != 16 or len({r['time'][:10] for r in selected}) != 16:
        raise ValueError(f'固定队列需要16个不同日期，当前筛得{len(selected)}个')
    protocol = {'created_utc': now(), 'code_sha256': digest(__file__), 'excluded_sha256': digest(previous),
                'manifest_sha256': digest(dataset / 'manifest-hashes.json'), 'selection': 'one paired date per half year across 2010–2017',
                'scanned': scanned, 'probe': {'uris': [r['uri'] for r in selected]}, 'rows': selected,
                'matching': {'tile': SIDE, 'pairs_per_date': LIMIT, 'jpg_brightness_tolerance': BRIGHTNESS,
                             'radius_delta_px': RADIUS_DELTA, 'max_radius_px': CORE,
                             'min_reference_coverage': .90, 'control': 'zero annotated coverage; only a proxy'} ,
                'test_accessed': False, 'calibration_accessed': False, 'validation_accessed': False}
    atomic_json(output / 'selection.json', protocol)
    return {'scanned': scanned, 'dates': len(selected), 'pairs': sum(len(r['pairs']) for r in selected)}


def channel_files(audit, identities, folder):
    receipt = folder / 'source-audit.json'
    source = read(receipt)
    if read(folder / 'mapping-review.json')['source_audit_sha256'] != digest(receipt):
        raise ValueError('AIA映射来源改变')
    records = {r['channel']: r for r in source['sources']}
    aia171, magnetic = identities
    if records['193']['sha256'] != magnetic['native_aia_sha256']:
        raise ValueError('AIA与HMI映射来源不同')
    extra = audit / 'aia171' / 'pairs' / folder.name
    hmi = audit / 'angular' / 'pairs' / folder.name / 'hmi-on-aia-local512.npy'
    if digest(hmi) != magnetic['array_sha256']:
        raise ValueError('磁图缓存改变')
    result = {k: map_channel(folder, r) for k, r in records.items()}
    result['171'] = map_channel(extra, {**aia171, 'filename': next(extra.glob('*.fits')).name})
    result['hmi'] = np.load(hmi, allow_pickle=False)
    return result


def compare_tile(arrays, tile):
    y0, x0 = divmod(tile, 32)
    y, x = np.ogrid[y0 * SIDE:(y0 + 1) * SIDE, x0 * SIDE:(x0 + 1) * SIDE]
    tile_support = all(np.isfinite(a[y, x]).all() for a in arrays.values())
    intensity = float(np.median(arrays['193'][y, x]))
    if not tile_support or not intensity > 0:
        return None
    return {'193': intensity, **features(arrays, y, x)}


def evaluate(dataset, audit, output):
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    output = output_path(output, dataset)
    protocol = read(audit / 'selection.json')
    if (protocol['code_sha256'] != digest(__file__) or protocol['manifest_sha256'] != digest(dataset / 'manifest-hashes.json')
            or (output / 'summary.json').exists()):
        raise ValueError('配对协议身份改变或结果已存在')
    group_names = ('aia', 'aia171', 'angular')
    summaries = [audit / n / 'summary.json' for n in group_names]
    source_index = []
    for path in summaries:
        book = read(path)
        if book['state'] != 'completed' or book['protocol_sha256'] != digest(path.with_name('protocol.json')):
            raise ValueError('观测审计状态无效')
        source_index.append({r['uri']: r for r in book['results']})
    results = []
    for row in protocol['rows']:
        a, b, c = (index[row['uri']] for index in source_index)
        result = {'uri': row['uri'], 'time': row['time'], 'pairs': [], 'state': 'source_rejected'}
        if (a.get('alignment_review_gate_passed') and b.get('source_gate_passed')
                and c['state'] == 'angular_sampled_not_physically_certified'):
            arrays = channel_files(audit, (b, c), audit / 'aia' / 'pairs' / row['id'])
            result['state'] = 'measured'
            for pair in row['pairs']:
                positive, control = [compare_tile(arrays, pair[key]) for key in ('reference', 'control')]
                if positive is None or control is None:
                    result['pairs'].append({**pair, 'state': 'missing_finite_support'})
                    continue
                delta = abs(positive['193'] / control['193'] - 1)
                result['pairs'].append({**pair, 'reference_features': positive, 'control_features': control,
                    'fits193_delta': delta, 'state': 'eligible' if delta <= .15 else 'fits193_difference'})
        results.append(result)
    by_time = {'earlier': [], 'later': []}
    for row in results:
        valid = [p for p in row['pairs'] if p['state'] == 'eligible']
        if valid:
            by_time['earlier' if row['time'] < '2014' else 'later'].append(valid)
    indexes = {'193_only': ('193',), 'euv_triplet': ('193', *FEATURES[:2]), 'euv_and_hmi': ('193', *FEATURES)}
    scores = {}
    if len(by_time['earlier']) >= 5 and len(by_time['later']) >= 5:
        for name, keys in indexes.items():
            training = [(list(p['reference_features'][k] for k in keys), 1) for group in by_time['earlier'] for p in group]
            training += [(list(p['control_features'][k] for k in keys), 0) for group in by_time['earlier'] for p in group]
            xf, yf = zip(*training)
            model = make_pipeline(StandardScaler(), LogisticRegression(C=1, random_state=SEED)).fit(xf, yf)
            hits = []
            for group in by_time['later']:
                for p in group:
                    potential = model.decision_function([[p[key][k] for k in keys]
                                                          for key in ('reference_features', 'control_features')])
                    hits.append(.5 if potential[0] == potential[1] else float(potential[0] > potential[1]))
            scores[name] = {'ranked_higher': sum(hits), 'pairs': len(hits), 'paired_win_rate': float(np.mean(hits))}
    summary = {'state': 'completed', 'recorded_utc': now(), 'selection_sha256': digest(audit / 'selection.json'),
        'source_audits': {p.parent.name: digest(p) for p in summaries}, 'days_measured': sum(r['state'] == 'measured' for r in results),
        'paired_dates': {k: len(v) for k, v in by_time.items()}, 'pairs': {k: sum(map(len, v)) for k, v in by_time.items()},
        'holdout_ranking': scores, 'scope': 'training-only case/control tiles; non-overlapping dates; outcomes are agreement with reference coverage',
        'test_accessed': False, 'calibration_accessed': False, 'validation_accessed': False}
    atomic_json(output / 'pairs.json', results)
    summary['pairs_sha256'] = digest(output / 'pairs.json')
    atomic_json(output / 'summary.json', summary)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('freeze', 'evaluate'))
    parser.add_argument('--dataset', required=True, type=Path)
    parser.add_argument('--previous', type=Path)
    parser.add_argument('--audit', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = (freeze(args.dataset, args.previous, args.output) if args.action == 'freeze'
              else evaluate(args.dataset, args.audit, args.output))
    print(json.dumps(result, ensure_ascii=False))
