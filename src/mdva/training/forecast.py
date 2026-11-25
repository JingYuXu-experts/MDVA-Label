"""AR-catalogue research baselines, with purged rolling CV and held-out calibration.

No formal test rows are read here. Every pipeline is fitted afresh inside its fold.
Serialized CPU estimators are local run artifacts, never accepted from uploads.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import time

import numpy as np

from .datasets import read_frozen
from .metrics import forecast_scores
from .runtime import Resources, atomic_json, digest, exclusive, now, output_path

FEATURES = ('MGB_PIL', 'R', 'MGB', 'Total_Unsigned_Flux', 'Area')
FOLDS = ((2011, 2012, 2013), (2013, 2014, 2015), (2015, 2016, 2020))


def features(rows, names=FEATURES):
    return np.asarray([[r['features'].get(k, np.nan) for k in names] for r in rows], float)


def labels(rows):
    return np.asarray([r['target_m_plus'] for r in rows], int)


def sample_id(row):
    return row.get('sample_id') or f"{row['time_utc']}|{row['noaa']}|{row['instance_id']}"


def purged_fold(rows, last_train, start_val, end_val):
    boundary = datetime.fromisoformat(f'{last_train+1}-01-01T00:00:00+00:00')
    train = [r for r in rows if datetime.fromisoformat(r['time_utc']) < boundary-timedelta(days=4)]
    validation = [r for r in rows if start_val <= int(r['time_utc'][:4]) <= end_val
                  and datetime.fromisoformat(r['time_utc']) > boundary+timedelta(days=4)]
    identities = {r['noaa'] for r in validation}
    train = [r for r in train if r['noaa'] not in identities]
    return train, validation


def sequences(rows, steps=3):
    """Require unique same-region, same-split daily timestamps, tolerating leap seconds."""
    from collections import defaultdict
    groups = defaultdict(list)
    for row in rows:
        groups[(row['split'], row['noaa'])].append(row)
    x, y, selected, histories = [], [], [], []
    for group in groups.values():
        group.sort(key=lambda r: r['time_utc'])
        times = [datetime.fromisoformat(r['time_utc']) for r in group]
        duplicates = {t for t in times if times.count(t) > 1}
        for i in range(steps-1, len(group)):
            window = group[i-steps+1:i+1]
            window_times = times[i-steps+1:i+1]
            if any(t in duplicates for t in window_times):
                continue
            if any(abs((b-a).total_seconds()-86400) > 2 for a, b in zip(window_times, window_times[1:])):
                continue
            x.append(features(window)); y.append(group[i]['target_m_plus'])
            selected.append(group[i]); histories.append([sample_id(r) for r in window])
    return np.asarray(x, float).reshape(-1, steps, len(FEATURES)), np.asarray(y, int), selected, histories


def candidates(kind):
    if kind == 'logistic':
        return [{'C': c} for c in (.1, 1., 10.)]
    if kind == 'svm':
        return [{'C': c, 'gamma': g} for c in (1., 10.) for g in ('scale', .2)]
    if kind == 'hgb':
        return [{'max_leaf_nodes': leaves, 'l2_regularization': l2}
                for leaves in (7, 15) for l2 in (1., 10.)]
    raise ValueError('未知CPU预测模型')


def estimator(kind, params):
    from sklearn.pipeline import make_pipeline
    from sklearn.impute import SimpleImputer
    from sklearn.preprocessing import StandardScaler
    from sklearn.linear_model import LogisticRegression
    from sklearn.svm import SVC
    from sklearn.ensemble import HistGradientBoostingClassifier
    if kind == 'logistic':
        model = LogisticRegression(class_weight='balanced', max_iter=2000, random_state=17, **params)
    elif kind == 'svm':
        model = SVC(class_weight='balanced', probability=False, cache_size=512, **params)
    elif kind == 'hgb':
        model = HistGradientBoostingClassifier(class_weight='balanced', max_iter=150,
                    early_stopping=False, random_state=17, **params)
    else:
        raise ValueError('未知CPU预测模型')
    return make_pipeline(SimpleImputer(strategy='median', keep_empty_features=True), StandardScaler(), model)


def raw_scores(model, x):
    if hasattr(model, 'decision_function'):
        return model.decision_function(x)
    p = np.clip(model.predict_proba(x)[:, 1], 1e-7, 1-1e-7)
    return np.log(p/(1-p))


def sigmoid(raw):
    from scipy.special import expit
    return expit(raw)


def calibrate(raw, y):
    from sklearn.linear_model import LogisticRegression
    # All calibration fitting is confined to the held-out 2022 split.
    model = LogisticRegression(C=1e6, max_iter=2000).fit(np.asarray(raw)[:, None], y)
    return model


def threshold_on_calibration(y, p):
    # Grid fixed before formal test. Ties favor the more conservative threshold.
    results = [(forecast_scores(y, p, t)['tss'], t) for t in np.linspace(.01, .99, 99)]
    return float(max(results)[1])


def dump_model(path, model):
    import joblib
    temporary = Path(path).with_suffix('.tmp')
    joblib.dump(model, temporary)
    os.replace(temporary, path)


def run_baselines(dataset, experiment, cohort='static', feature_group='all'):
    from sklearn.metrics import average_precision_score
    from threadpoolctl import threadpool_limits
    dataset, experiment = Path(dataset), output_path(experiment, dataset)
    folder = experiment/'forecast'/f'{cohort}-{feature_group}'
    folder.mkdir(parents=True, exist_ok=True)
    groups = {'all': FEATURES, 'without_flux_area': FEATURES[:3], 'without_gradients_R': FEATURES[3:]}
    if feature_group not in groups or cohort not in ('static', 'sequence3', 'sequence5'):
        raise ValueError('未登记的预测消融')
    names = groups[feature_group]
    allrows = read_frozen(dataset, 'forecast-ready.jsonl')
    rows = [r for r in allrows if r['split'] in ('train', 'validation', 'calibration')]
    # Only metadata for filtering are used from test; no test labels or values are evaluated.
    del allrows
    if cohort != 'static':
        steps = 3 if cohort == 'sequence3' else 5
        _, _, rows, _ = sequences(rows, steps)
    splits = {split: [r for r in rows if r['split'] == split] for split in ('train', 'validation', 'calibration')}
    identity = {'data': digest(dataset/'manifest-hashes.json'), 'forecast_code': digest(__file__),
                'features': list(names), 'cohort': cohort,
                'mode': 'reference_catalogue_research_only', 'test_evaluated': False}
    with exclusive(folder/'forecast.lock'), threadpool_limits(limits=4):
        identityfile = folder/'identity.json'
        if identityfile.exists() and json.loads(identityfile.read_text(encoding='utf-8')) != identity:
            raise ValueError('预测实验身份已变化，请使用新实验目录')
        atomic_json(identityfile, identity)
        monitor = Resources('cpu')
        climate = float(labels(splits['train']).mean())
        atomic_json(folder/'climate.json', {'probability': climate, 'fit_split': 'train',
                    'validation': forecast_scores(labels(splits['validation']), np.full(len(splits['validation']), climate), climate=climate),
                    'cohort': cohort, 'test_evaluated': False})
        for kind in ('logistic', 'svm', 'hgb'):
            resultfile = folder/f'{kind}-result.json'
            if resultfile.exists():
                continue
            trials = []
            for index, params in enumerate(candidates(kind)):
                scores = []
                for fold_index, years in enumerate(FOLDS):
                    trialfile = folder/'search'/f'{kind}-c{index}-f{fold_index}.json'
                    if trialfile.exists():
                        trial = json.loads(trialfile.read_text(encoding='utf-8'))
                    else:
                        train, valid = purged_fold(splits['train'], *years)
                        trial = {'params': params, 'fold': list(years), 'train_n': len(train), 'validation_n': len(valid),
                                 'validation_positive': int(labels(valid).sum()), 'state': 'skipped'}
                        if labels(valid).sum() >= 10 and len(np.unique(labels(train))) == 2:
                            begin = time.perf_counter()
                            model = estimator(kind, params).fit(features(train, names), labels(train))
                            raw = raw_scores(model, features(valid, names))
                            trial.update(state='complete', pr_auc_ap=float(average_precision_score(labels(valid), raw)),
                                         seconds=time.perf_counter()-begin)
                        else:
                            trial['reason'] = '该折阳性不足10或训练缺少一类'
                        atomic_json(trialfile, trial)
                    if trial['state'] == 'complete':
                        scores.append(trial['pr_auc_ap'])
                    monitor.sample()
                trials.append({'params': params, 'mean_cv_ap': float(np.mean(scores)) if len(scores) >= 2 else None,
                               'valid_folds': len(scores)})
            valid_trials = [t for t in trials if t['mean_cv_ap'] is not None]
            if not valid_trials:
                raise ValueError(f'{kind}没有至少两个有效滚动验证折')
            choice = max(valid_trials, key=lambda t: t['mean_cv_ap'])
            model = estimator(kind, choice['params']).fit(features(splits['train'], names), labels(splits['train']))
            calibration_raw = raw_scores(model, features(splits['calibration'], names))
            calibrator = calibrate(calibration_raw, labels(splits['calibration']))
            calibration_p = calibrator.predict_proba(calibration_raw[:, None])[:, 1]
            threshold = threshold_on_calibration(labels(splits['calibration']), calibration_p)
            raw = raw_scores(model, features(splits['validation'], names))
            probability = calibrator.predict_proba(raw[:, None])[:, 1]
            result = {'model': kind, **identity, 'chosen': choice, 'search': trials, 'climate': climate,
                      'threshold': threshold, 'threshold_fit_split': 'calibration',
                      'calibration_fit_split': 'calibration',
                      'validation_raw': forecast_scores(labels(splits['validation']), sigmoid(raw), .5, climate),
                      'validation_calibrated': forecast_scores(labels(splits['validation']), probability, threshold, climate),
                      'validation_interpretation': '校准器使用2022资料，2021开发分数不是当时可实时获得的预报',
                      'samples': {k: len(v) for k, v in splits.items()}, 'resource': monitor.sample(),
                      'completed_utc': now()}
            dump_model(folder/f'{kind}.joblib', {'pipeline': model, 'calibrator': calibrator,
                       'threshold': threshold, 'features': list(names), 'identity': identity})
            atomic_json(folder/f'{kind}-validation-predictions.json', [
                {'id': sample_id(r), 'noaa': r['noaa'], 'time_utc': r['time_utc'],
                 'target': int(y), 'raw_score': float(s), 'probability': float(p)}
                for r, y, s, p in zip(splits['validation'], labels(splits['validation']), raw, probability)])
            atomic_json(resultfile, result)
            print(json.dumps({'model': kind, 'state': 'complete', 'validation': result['validation_calibrated']}, ensure_ascii=False), flush=True)
