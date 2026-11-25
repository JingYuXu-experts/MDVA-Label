"""Freeze, then evaluate the complete reference-label forecasting experiment family."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .datasets import read_frozen
from .forecast import features, labels, raw_scores, sample_id, sequences, sigmoid
from .metrics import forecast_scores
from .runtime import atomic_json, digest, exclusive, now


def registered_results():
    files = []
    for cohort in ('static-all', 'static-without_flux_area', 'static-without_gradients_R', 'sequence3-all'):
        files.extend(f'{cohort}/{kind}-result.json' for kind in ('logistic', 'svm', 'hgb'))
    files.append('sequence3-flat-all/svm-result.json')
    for seed in (17, 42, 2026):
        files.extend(f'{name}/result.json' for name in (
            f'lstm-T3-s{seed}', f'mlp-T3-s{seed}', f'lstm-T3-s{seed}-repeat',
            f'lstm-T3-s{seed}-unweighted', f'lstm-T3-s{seed}-commonT5', f'lstm-T5-s{seed}-commonT5'))
    return files


def lock(experiment):
    experiment = Path(experiment)
    file = experiment/'forecast-selection-lock.json'
    if file.exists():
        raise ValueError('预测测试锁已经存在，禁止重新选型或覆盖')
    folder = experiment/'forecast'
    protocol = json.loads((experiment/'protocol.json').read_text(encoding='utf-8'))
    cv = json.loads((folder/'temporal-cv/summary.json').read_text(encoding='utf-8'))
    if len([r for r in cv['fold_results'] if r['state'] == 'completed']) < 12:
        raise ValueError('神经网络内部滚动验证尚未达到每模型至少两个有效折/三个种子')
    evidence = {}
    for relative in registered_results():
        path = folder/relative
        result = json.loads(path.read_text(encoding='utf-8'))
        if result.get('data', result.get('dataset')) != protocol['dataset_manifest_sha256']:
            raise ValueError('预测运行的数据版本与协议不符')
        weights = path.parent/('weights.safetensors' if result.get('kind') else path.name.replace('-result.json', '.joblib'))
        evidence[relative] = {'result_sha256': digest(path), 'weights': weights.relative_to(folder).as_posix(),
                              'weights_sha256': digest(weights)}
    candidates = []
    for kind in ('logistic', 'svm', 'hgb'):
        path = f'sequence3-all/{kind}-result.json'
        record = json.loads((folder/path).read_text(encoding='utf-8'))
        candidates.append({'result': path, 'cv_ap': record['chosen']['mean_cv_ap']})
    record = json.loads((folder/'sequence3-flat-all/svm-result.json').read_text(encoding='utf-8'))
    candidates.append({'result': 'sequence3-flat-all/svm-result.json', 'cv_ap': record['chosen']['ap']})
    primary = max(candidates, key=lambda r: r['cv_ap'])
    value = {'locked_utc': now(), 'scope': 'original_reference_AR_catalogue_research_only',
             'operational_release': False, 'official_supervision_verified': False,
             'protocol_sha256': digest(experiment/'protocol.json'), 'results': evidence,
             'temporal_cv_sha256': digest(folder/'temporal-cv/summary.json'),
             'cpu_matched_cohort_candidates': candidates, 'selected_primary_cpu': primary,
             'neural_internal_cv': cv['mean_ap'],
             'planned_comparisons': [
                 ['sequence3-all--svm-result', 'sequence3-flat-all--svm-result'],
                 ['sequence3-flat-all--svm-result', 'lstm-T3-s{seed}--result'],
                 ['mlp-T3-s{seed}--result', 'lstm-T3-s{seed}--result'],
                 ['lstm-T3-s{seed}-repeat--result', 'lstm-T3-s{seed}--result'],
                 ['lstm-T3-s{seed}-unweighted--result', 'lstm-T3-s{seed}--result'],
                 ['lstm-T3-s{seed}-commonT5--result', 'lstm-T5-s{seed}-commonT5--result'],
                 ['static-without_flux_area--svm-result', 'static-all--svm-result'],
                 ['static-without_gradients_R--svm-result', 'static-all--svm-result']],
             'selection_rule': '预留可靠CPU主候选；时序网络作为已登记研究对照，不能凭测试分数换主模型',
             'test_selection_forbidden': True}
    atomic_json(file, value)
    return value


def evaluate(experiment, dataset):
    import joblib
    import torch
    from safetensors.torch import load_file
    from threadpoolctl import threadpool_limits
    from .models import SequenceClassifier
    experiment, dataset = Path(experiment), Path(dataset)
    frozen = json.loads((experiment/'forecast-selection-lock.json').read_text(encoding='utf-8'))
    if digest(experiment/'protocol.json') != frozen['protocol_sha256']:
        raise ValueError('预测协议已改变')
    protocol = json.loads((experiment/'protocol.json').read_text(encoding='utf-8'))
    if digest(dataset/'manifest-hashes.json') != protocol['dataset_manifest_sha256']:
        raise ValueError('预测测试数据版本已改变')
    root = experiment/'forecast'
    tests = [r for r in read_frozen(dataset, 'forecast-ready.jsonl') if r['split'] == 'test']
    output = experiment/'forecast-test'
    output.mkdir(exist_ok=True)
    with exclusive(output/'evaluate.lock'), threadpool_limits(limits=4):
        torch.set_num_threads(4)
        for relative, identity in frozen['results'].items():
            resultfile = root/relative
            if digest(resultfile) != identity['result_sha256'] or digest(root/identity['weights']) != identity['weights_sha256']:
                raise ValueError('测试解锁后模型或校准参数已改变')
            record = json.loads(resultfile.read_text(encoding='utf-8'))
            identifier = relative.replace('/', '--').replace('.json', '')
            destination = output/identifier
            if (destination/'summary.json').exists():
                continue
            if record.get('kind'):
                x, _, rows, histories = sequences(tests, record['steps'])
                if record.get('common_steps'):
                    _, _, common, _ = sequences(tests, record['common_steps'])
                    allowed = {sample_id(r) for r in common}
                    keep = [i for i, r in enumerate(rows) if sample_id(r) in allowed]
                    x, rows, histories = x[keep], [rows[i] for i in keep], [histories[i] for i in keep]
                if record['repeat_current']:
                    x = np.repeat(x[:, -1:], record['steps'], axis=1)
                config = record['preprocessing']
                x = (np.where(np.isnan(x), config['median'], x)-np.array(config['mean']))/np.array(config['scale'])
                model = SequenceClassifier(record['kind'], record['steps']).eval()
                model.load_state_dict(load_file(str(root/identity['weights'])))
                with torch.no_grad():
                    raw = model(torch.from_numpy(x.astype('float32'))).numpy()
                p = sigmoid(raw/record['temperature'])
            else:
                artifact = joblib.load(root/identity['weights'])
                rows = tests
                if record.get('cohort') == 'sequence3':
                    x, _, rows, histories = sequences(tests, 3)
                if artifact.get('steps'):
                    matrix = x.reshape(len(x), -1)
                else:
                    matrix = features(rows, artifact['features'])
                raw = raw_scores(artifact['pipeline'], matrix)
                p = artifact['calibrator'].predict_proba(raw[:, None])[:, 1]
            y = labels(rows)
            summary = {'id': identifier, 'scope': frozen['scope'], 'split': 'test',
                       'result_sha256': identity['result_sha256'], 'weights_sha256': identity['weights_sha256'],
                       'raw': forecast_scores(y, sigmoid(raw), .5, record['climate']),
                       'calibrated': forecast_scores(y, p, record['threshold'], record['climate']),
                       'operational_release': False, 'completed_utc': now()}
            atomic_json(destination/'predictions.json', [
                {'id': sample_id(r), 'noaa': r['noaa'], 'time_utc': r['time_utc'], 'target': int(target),
                 'raw_score': float(score), 'probability': float(prob)}
                for r, target, score, prob in zip(rows, y, raw, p)])
            atomic_json(destination/'summary.json', summary)
        return {'test_experiments': len(list(output.glob('*/summary.json'))), 'scope': frozen['scope']}


if __name__ == '__main__':
    import argparse
    from .runtime import configure_environment
    from .__main__ import DEFAULT_DATA, DEFAULT_EXPERIMENT
    configure_environment()
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['lock', 'evaluate'])
    args = parser.parse_args()
    result = lock(DEFAULT_EXPERIMENT) if args.action == 'lock' else evaluate(DEFAULT_EXPERIMENT, DEFAULT_DATA)
    print(json.dumps(result, ensure_ascii=False, indent=2))
