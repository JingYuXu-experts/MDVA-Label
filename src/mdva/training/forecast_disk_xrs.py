"""Historical disk-wide forecast comparison with strictly lagged XRS minutes."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path

import h5py
import numpy as np

from .durable import atomic_json
from .forecast_disk_baseline import RAW, REGIONS
from .journal import read
from .metrics import forecast_scores
from .runtime import configure_environment, digest, now, output_path
from .xrs_coverage import UTC, iso, utc, verify_source


MINUTES = 1440
MINUTE = 60
DELAY = 15 * MINUTE
XRS = ('log_last_flux', 'log_max_6h', 'log_max_24h', 'log_median_6h', 'm_level_minute_share')
ARMS = {
    'raw': RAW, 'raw_plus_regions': (*RAW, *REGIONS), 'xrs': XRS,
    'xrs_plus_raw': (*XRS, *RAW), 'xrs_plus_raw_plus_regions': (*XRS, *RAW, *REGIONS),
}
FOLDS = ((2012, 2013), (2014, 2015), (2016, 2020))


def read_minutes(directory, entry):
    file = Path(entry['filename'])
    if file.name != entry['filename']:
        raise ValueError('XRS质量清单资源路径越界')
    meta = read(directory / file.with_suffix('.source.json'))
    if (entry['sha256'], entry['satellite'], entry['url']) != (meta['sha256'], meta['satellite'], meta['url']):
        raise ValueError('档案原始字节与质量复核不一致')
    raw = verify_source(directory, meta)
    base = datetime(2000, 1, 1, 12, tzinfo=UTC).timestamp()
    with h5py.File(raw, 'r') as data:
        times = np.asarray(data['time'][:], dtype=np.float64) + base
        flux = data['xrsb_flux'][:]
        flags = data['xrsb_flag'][:]
        count = data['xrsb_num'][:]
    if times.shape != flux.shape or np.any(np.diff(times) <= 0):
        raise ValueError('GOES分钟数组时间无序或尺寸不符')
    intervals = np.asarray(entry['intervals'], dtype=np.float64).reshape((-1, 2))
    if not len(intervals):
        if entry['valid_minutes']:
            raise ValueError('缺失质量确认区间')
        return times[:0], flux[:0]
    index = np.searchsorted(intervals[:, 0], times, side='right') - 1
    good = (index >= 0) & (times + MINUTE <= intervals[np.maximum(index, 0), 1])
    if (int(good.sum()) != entry['valid_minutes'] or np.any(flags[good] != 0)
            or np.any(count[good] <= 0) or not np.isfinite(flux[good]).all()):
        raise ValueError('质量确认的分钟与原始产品不一致')
    return times[good], flux[good]


def series_for(index, directory, first, last):
    groups, inputs = defaultdict(list), []
    for entry in index:
        if (utc(entry['time_coverage_start']).timestamp() > last
                or utc(entry['time_coverage_end']).timestamp() + MINUTE < first):
            continue
        timestamps, flux = read_minutes(directory, entry)
        chosen = (timestamps >= first) & (timestamps < last)
        groups[entry['satellite']].append((timestamps[chosen], flux[chosen]))
        inputs.append({'file': entry['filename'], 'sha256': entry['sha256'], 'satellite': entry['satellite']})
    series = {}
    for satellite, parts in groups.items():
        time = np.concatenate([r[0] for r in parts])
        values = np.concatenate([r[1] for r in parts])
        order = np.argsort(time)
        time, values = time[order], values[order]
        if np.any(np.diff(time) <= 0):
            raise ValueError('同一卫星的分钟档案重复')
        series[satellite] = (time, values)
    return series, inputs


def history(observed, series):
    """Require full minute bins strictly before the observed time minus a margin."""
    cutoff = np.floor((utc(observed).timestamp() - DELAY) / MINUTE) * MINUTE
    start, recent = cutoff - MINUTES * MINUTE, cutoff - 360 * MINUTE
    best = None
    for satellite, (time, flux) in series.items():
        lo, hi, last6 = time.searchsorted([start, cutoff, recent])
        if hi-lo < .9*MINUTES or hi-last6 < .9*360 or cutoff-(time[hi-1] + MINUTE) > 20 * MINUTE:
            continue
        preference = (hi-lo, hi-last6, satellite)
        if best is None or preference > best[0]:
            best = (preference, time[hi-1], flux[lo:hi], flux[last6:hi])
    if best is None:
        return None
    preference, last, full, six = best
    log = lambda value: float(np.log10(max(float(value), 1e-12)))
    return {
        'satellite': int(preference[2]), 'valid_minutes_24h': int(preference[0]),
        'valid_minutes_6h': int(preference[1]),
        'cutoff_utc': iso(cutoff), 'last_minute_end_utc': iso(last + MINUTE),
        'features': {
            'log_last_flux': log(full[-1]), 'log_max_6h': log(six.max()), 'log_max_24h': log(full.max()),
            'log_median_6h': log(np.median(six)), 'm_level_minute_share': float((full >= 1e-5).mean()),
        },
    }


def train_fit(rows, keys):
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    model = make_pipeline(StandardScaler(), LogisticRegression(C=1., max_iter=500, random_state=2026))
    model.fit([[r['features'][key] for key in keys] for r in rows], [r['target'] for r in rows])
    return model


def scores(model, rows, keys):
    return model.predict_proba([[r['features'][key] for key in keys] for r in rows])[:, 1]


def operating_point(train, keys):
    negatives, used = [], []
    for low, high in FOLDS:
        held = [r for r in train if low <= utc(r['observed_utc']).year <= high]
        if len({r['target'] for r in held}) != 2:
            continue
        boundary = min(utc(r['observed_utc']) for r in held)
        fit = [r for r in train if utc(r['target_end_utc']) < boundary]
        if len({r['target'] for r in fit}) != 2:
            continue
        predictions = scores(train_fit(fit, keys), held, keys)
        negatives.extend(float(score) for score, row in zip(predictions, held) if not row['target'])
        used.append({'years': [low, high], 'train': len(fit), 'held': len(held),
                     'held_positives': sum(r['target'] for r in held)})
    if len(used) < 2 or not negatives:
        return None
    cutoff = float(np.nextafter(np.quantile(negatives, .9, method='higher'), 1.))
    return {'threshold': cutoff, 'folds': used, 'fold_negatives': len(negatives),
            'chosen_from': 'pooled training-only rolling negative scores, upper 10% empirical rate'}


def run(baseline, audit, archives, dataset, output):
    from sklearn.metrics import average_precision_score

    baseline, audit, archives, dataset = (Path(path).resolve() for path in (baseline, audit, archives, dataset))
    output = output_path(output, dataset)
    if output.exists() or any(output.is_relative_to(path) for path in (baseline, audit, archives)):
        raise ValueError('过去XRS审计需独立输出目录')
    base, prepared = read(baseline / 'summary.json'), read(audit / 'summary.json')
    features_path, index_path = baseline / 'features.json', audit / 'coverage-index.json'
    if (
        base['state'] != 'completed' or base['test_accessed'] or base['calibration_accessed']
        or base['features_sha256'] != digest(features_path)
        or prepared['frozen_manifest_sha256'] != digest(dataset / 'manifest-hashes.json')
        or prepared['coverage_index_sha256'] != digest(index_path)
    ):
        raise ValueError('全日面基线或XRS质量清单不符')
    original = read(features_path)
    if {r['split'] for r in original} != {'train', 'validation'}:
        raise ValueError('只允许训练及开发数据')
    times = [utc(row['observed_utc']).timestamp() for row in original]
    series, sources = series_for(read(index_path), archives, min(times) - (MINUTES+16)*MINUTE, max(times))
    rows, excluded = [], Counter()
    for row in original:
        observations = history(row['observed_utc'], series)
        if observations is None:
            excluded[f"{row['split']}/{row['target']}"] += 1
            continue
        if utc(observations['last_minute_end_utc']) > utc(observations['cutoff_utc']):
            raise ValueError('XRS使用了水印后的观测')
        rows.append({**row, 'features': {**row['features'], **observations['features']},
                     'xrs': {key: value for key, value in observations.items() if key != 'features'}})
    train = [r for r in rows if r['split'] == 'train']
    dev = [r for r in rows if r['split'] == 'validation']
    if max(utc(row['target_end_utc']) for row in train) >= min(utc(row['observed_utc']) for row in dev):
        raise ValueError('训练目标窗越过开发时段')
    climate = float(np.mean([row['target'] for row in train]))
    truth = np.array([row['target'] for row in dev])
    predictions = {'constant': np.full(len(dev), climate)}
    policies, details = {}, {}
    for arm, keys in ARMS.items():
        model = train_fit(train, keys)
        predictions[arm] = scores(model, dev, keys)
        scaler, fit = model.steps[0][1], model.steps[1][1]
        details[arm] = {'features': keys, 'mean': scaler.mean_.tolist(), 'scale': scaler.scale_.tolist(),
                        'coef': fit.coef_[0].tolist(), 'intercept': float(fit.intercept_[0])}
        policy = operating_point(train, keys)
        if policy is not None:
            policies[arm] = {**policy, 'development_metrics': forecast_scores(truth, predictions[arm],
                                                                              threshold=policy['threshold'], climate=climate)}
    metrics = {arm: forecast_scores(truth, p, climate=climate) for arm, p in predictions.items()}
    blocks = np.array([int(utc(row['observed_utc']).timestamp() // (27*86400)) for row in dev])
    groups = [np.flatnonzero(blocks == value) for value in np.unique(blocks)]
    rng = np.random.default_rng(2026)
    ap_changes = {'add_xrs_to_raw': [], 'add_regions_to_xrs_and_raw': []}
    for _ in range(2000):
        selection = np.concatenate([groups[index] for index in rng.integers(len(groups), size=len(groups))])
        y = truth[selection]
        if np.unique(y).size != 2:
            continue
        a, b, c = (average_precision_score(y, predictions[arm][selection])
                   for arm in ('raw', 'xrs_plus_raw', 'xrs_plus_raw_plus_regions'))
        ap_changes['add_xrs_to_raw'].append(b-a)
        ap_changes['add_regions_to_xrs_and_raw'].append(c-b)
    output.mkdir(parents=True)
    atomic_json(output / 'rows.json', rows)
    atomic_json(output / 'weights.json', details)
    atomic_json(output / 'predictions.json', [
        {'uri': row['uri'], 'target': row['target'], 'outputs': {arm: float(p[i]) for arm, p in predictions.items()}}
        for i, row in enumerate(dev)
    ])
    report = {
        'state': 'completed', 'created_utc': now(), 'sources': sources,
        'cohort': {'train_n': len(train), 'train_positives': sum(r['target'] for r in train), 'dev_n': len(dev),
                   'dev_positives': int(truth.sum()), 'excluded_for_xrs': dict(excluded)},
        'metrics_fixed_0_5': metrics, 'training_only_operating_points': policies,
        'paired_ap_change_ci95': {key: np.quantile(values, [.025, .975]).tolist() for key, values in ap_changes.items()},
        'scope': 'Historical archives, one satellite per history, no gap filling, 15-minute minimum lag and >=90% coverage.',
        'operational_availability_verified': False, 'test_accessed': False, 'calibration_accessed': False,
        'limitations': ['Historical reprocessed XRS available now, actual original dissemination times unverified.',
                        'Frozen detector is not out-of-fold on segmentation training observations.',
                        'Development evaluation only; alert policy is an experimental training-only threshold.'],
        'baseline_summary_sha256': digest(baseline / 'summary.json'),
        'xrs_quality_index_sha256': digest(index_path), 'code_sha256': digest(__file__),
        'rows_sha256': digest(output / 'rows.json'), 'weights_sha256': digest(output / 'weights.json'),
        'predictions_sha256': digest(output / 'predictions.json'),
    }
    atomic_json(output / 'summary.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('baseline', 'audit', 'archives', 'dataset', 'output'):
        parser.add_argument('--' + key, required=True)
    args = parser.parse_args()
    configure_environment()
    result = run(**vars(args))
    print(json.dumps({key: result[key] for key in ('cohort', 'paired_ap_change_ci95', 'metrics_fixed_0_5')}))


if __name__ == '__main__':
    main()
