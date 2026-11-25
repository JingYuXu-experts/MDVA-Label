"""Matched temporal SVM comparison: identical three-day cohort and CV budget."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .datasets import read_frozen
from .forecast import (FEATURES, FOLDS, calibrate, candidates, dump_model, estimator, labels,
                       purged_fold, raw_scores, sample_id, sequences, sigmoid, threshold_on_calibration)
from .metrics import forecast_scores
from .runtime import Resources, atomic_json, digest, exclusive, now


def run(experiment, dataset):
    from sklearn.metrics import average_precision_score
    from threadpoolctl import threadpool_limits
    experiment, dataset = Path(experiment), Path(dataset)
    output = experiment/'forecast/sequence3-flat-all'
    output.mkdir(parents=True, exist_ok=True)
    identity = {'data': digest(dataset/'manifest-hashes.json'),
                'code': {f: digest(Path(__file__).parent/f) for f in ('temporal_baselines.py', 'forecast.py')},
                'mode': 'reference_catalogue_research_only', 'cohort': 'sequence3', 'input': 'three_daily_points_flattened',
                'test_evaluated': False}
    with exclusive(output/'forecast.lock'), threadpool_limits(limits=4):
        old = output/'identity.json'
        if old.exists() and json.loads(old.read_text(encoding='utf-8')) != identity:
            raise ValueError('展平SVM版本不符，请新建实验目录')
        atomic_json(old, identity)
        if (output/'svm-result.json').exists():
            return
        rows = [r for r in read_frozen(dataset, 'forecast-ready.jsonl') if r['split'] != 'test']
        x, _, selected, _ = sequences(rows, 3)
        inputs = {sample_id(r): a.flatten() for r, a in zip(selected, x)}
        def matrix(items):
            return np.asarray([inputs[sample_id(r)] for r in items])
        splits = {s: [r for r in selected if r['split'] == s] for s in ('train', 'validation', 'calibration')}
        trials = []
        resource = Resources('cpu')
        for index, params in enumerate(candidates('svm')):
            scores = []
            for fi, fold in enumerate(FOLDS):
                path = output/'search'/f'c{index}-f{fi}.json'
                if path.exists():
                    trial = json.loads(path.read_text(encoding='utf-8'))
                else:
                    train, validation = purged_fold(splits['train'], *fold)
                    trial = {'state': 'skipped', 'positive': int(labels(validation).sum()), 'params': params, 'fold': fold}
                    if labels(validation).sum() >= 10 and len(np.unique(labels(train))) == 2:
                        model = estimator('svm', params).fit(matrix(train), labels(train))
                        trial.update(state='complete', ap=float(average_precision_score(labels(validation), raw_scores(model, matrix(validation)))))
                    atomic_json(path, trial)
                if trial['state'] == 'complete':
                    scores.append(trial['ap'])
                resource.sample()
            trials.append({'params': params, 'ap': float(np.mean(scores)) if len(scores) >= 2 else None})
        choices = [r for r in trials if r['ap'] is not None]
        if not choices:
            raise ValueError('展平SVM没有至少两个有效折')
        choice = max(choices, key=lambda r: r['ap'])
        model = estimator('svm', choice['params']).fit(matrix(splits['train']), labels(splits['train']))
        cal_raw = raw_scores(model, matrix(splits['calibration']))
        calibrator = calibrate(cal_raw, labels(splits['calibration']))
        threshold = threshold_on_calibration(labels(splits['calibration']), calibrator.predict_proba(cal_raw[:, None])[:, 1])
        raw = raw_scores(model, matrix(splits['validation']))
        p = calibrator.predict_proba(raw[:, None])[:, 1]
        climate = float(labels(splits['train']).mean())
        dump_model(output/'svm.joblib', {'pipeline': model, 'calibrator': calibrator, 'threshold': threshold,
                                        'identity': identity, 'features': list(FEATURES), 'steps': 3})
        atomic_json(output/'svm-validation-predictions.json', [
            {'id': sample_id(r), 'noaa': r['noaa'], 'time_utc': r['time_utc'], 'target': int(y),
             'raw_score': float(s), 'probability': float(prob)}
            for r, y, s, prob in zip(splits['validation'], labels(splits['validation']), raw, p)])
        atomic_json(output/'svm-result.json', {**identity, 'model': 'svm_flatten3', 'chosen': choice,
            'climate': climate, 'threshold': threshold, 'search': trials, 'resource': resource.sample(),
            'samples': {s: len(v) for s, v in splits.items()},
            'validation_calibrated': forecast_scores(labels(splits['validation']), p, threshold, climate),
            'validation_raw': forecast_scores(labels(splits['validation']), sigmoid(raw), .5, climate),
            'completed_utc': now()})
