"""Check whether historical flare scores improve on simple past-flux persistence."""

from __future__ import annotations

import argparse
from pathlib import Path
import json

import numpy as np

from .durable import atomic_json
from .journal import read
from .runtime import digest, now, output_path
from .xrs_coverage import utc


BASELINES = {'past_24h_peak': 'log_max_24h', 'past_6h_peak': 'log_max_6h'}
MODELS = ('xrs', 'xrs_plus_raw', 'xrs_plus_raw_plus_regions')


def measure(truth, score, threshold):
    from sklearn.metrics import average_precision_score

    positive, alarm = truth == 1, score >= threshold
    return {'windows': len(truth), 'events': int(positive.sum()),
            'average_precision': float(average_precision_score(truth, score)) if positive.any() else None,
            'hits': int((positive & alarm).sum()), 'misses': int((positive & ~alarm).sum()),
            'false_alarms': int((~positive & alarm).sum())}


def run(dataset, previous, output):
    from sklearn.metrics import average_precision_score

    previous = Path(previous).resolve()
    output = output_path(output, Path(dataset).resolve())
    if output.exists() or output.is_relative_to(previous):
        raise ValueError('预报持续性分析需要新目录')
    report = read(previous / 'summary.json')
    if (report['state'] != 'completed' or report['test_accessed'] or report['calibration_accessed']
            or any(report[f'{name}_sha256'] != digest(previous / f'{name}.json') for name in ('rows', 'predictions'))):
        raise ValueError('历史拟合对照资料身份不符')
    sources = read(previous / 'rows.json')
    if {row['split'] for row in sources} != {'train', 'validation'}:
        raise ValueError('仅以训练与开发资料诊断')
    train = [r for r in sources if r['split'] == 'train']
    dev = [r for r in sources if r['split'] == 'validation']
    saved = read(previous / 'predictions.json')
    if [(r['uri'], r['target']) for r in dev] != [(r['uri'], r['target']) for r in saved]:
        raise ValueError('历史分数和事件时间窗不一致')
    if max(utc(r['target_end_utc']) for r in train) >= min(utc(r['observed_utc']) for r in dev):
        raise ValueError('训练与开发时间窗交叠')
    for row in (*train, *dev):
        if utc(row['xrs']['last_minute_end_utc']) > utc(row['xrs']['cutoff_utc']) or utc(row['xrs']['cutoff_utc']) >= utc(row['observed_utc']):
            raise ValueError('XRS时间窗未截止在图像观测前')

    policy = report['training_only_operating_points']
    years = {year for fold in policy['xrs_plus_raw']['folds']
             for year in range(fold['years'][0], fold['years'][1]+1)}
    negatives = [r for r in train if r['target'] == 0 and utc(r['observed_utc']).year in years]
    if len(negatives) != policy['xrs_plus_raw']['fold_negatives']:
        raise ValueError('持续性基线与拟合对照未使用同一阈值队列')
    thresholds = {name: float(np.nextafter(np.quantile([r['features'][key] for r in negatives], .9,
                                                        method='higher'), np.inf))
                  for name, key in BASELINES.items()}
    thresholds.update({name: policy[name]['threshold'] for name in MODELS})
    predictions = {name: np.asarray([r['features'][key] for r in dev]) for name, key in BASELINES.items()}
    predictions.update({name: np.asarray([r['outputs'][name] for r in saved]) for name in MODELS})
    truth = np.asarray([row['target'] for row in dev])
    prior_m = predictions['past_24h_peak'] >= -5
    filters = {'all': np.full(len(dev), True), 'prior_M_flux': prior_m, 'below_M_flux_in_prior_24h': ~prior_m}
    comparison = {name: {arm: measure(truth[subset], score[subset], thresholds[arm])
                         for arm, score in predictions.items()} for name, subset in filters.items()}
    blocks = np.array([int(utc(row['observed_utc']).timestamp() // (27*86400)) for row in dev])
    groups = [np.flatnonzero(blocks == value) for value in np.unique(blocks)]
    rng = np.random.default_rng(2026)
    differences = []
    for _ in range(2000):
        selected = np.concatenate([groups[index] for index in rng.integers(len(groups), size=len(groups))])
        if len(np.unique(truth[selected])) == 2:
            fitted, baseline = (average_precision_score(truth[selected], predictions[arm][selected])
                                for arm in ('xrs_plus_raw', 'past_24h_peak'))
            differences.append(fitted-baseline)
    result = {'state': 'completed', 'created_utc': now(), 'comparison': comparison, 'thresholds': thresholds,
              'trained_model_minus_24h_persistence_ap_ci95': np.quantile(differences, [.025, .975]).tolist(),
              'threshold_training_negative_windows': len(negatives), 'active_window_not_inferred_from_flux': True,
              'scope': 'Historical development only. Recent flux groups use the same pre-observation window; '
                       'below-M flux is not a claim of an inactive Sun.',
              'test_accessed': False, 'calibration_accessed': False, 'issued_forecast': False,
              'source_summary_sha256': digest(previous / 'summary.json'), 'code_sha256': digest(__file__)}
    atomic_json(output / 'summary.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('dataset', 'previous', 'output'):
        parser.add_argument('--' + key, required=True)
    result = run(**vars(parser.parse_args()))
    print(json.dumps(result['comparison']))


if __name__ == '__main__':
    main()
