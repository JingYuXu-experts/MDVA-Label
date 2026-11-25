"""Small temporal AR predictors with resumable epoch transactions (CPU by default)."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import random
import time

import numpy as np

from .forecast import (FEATURES, labels, sample_id, sequences, sigmoid, threshold_on_calibration)
from .datasets import read_frozen
from .metrics import forecast_scores
from .runtime import Resources, atomic_json, digest, exclusive, now


def temperature_fit(raw, y):
    from scipy.optimize import minimize_scalar
    from scipy.special import expit
    def objective(log_temperature):
        p = np.clip(expit(raw/np.exp(log_temperature)), 1e-8, 1-1e-8)
        return float(-np.mean(y*np.log(p)+(1-y)*np.log(1-p)))
    fit = minimize_scalar(objective, bounds=(-4, 4), method='bounded')
    return float(np.exp(fit.x))


def fit(experiment, dataset, kind='lstm', seed=17, steps=3, repeat_current=False,
        weighted=True, common_steps=None):
    import torch
    from sklearn.impute import SimpleImputer
    from sklearn.preprocessing import StandardScaler
    from .models import SequenceClassifier
    from .train import atomic_checkpoint, load_checkpoint
    experiment, dataset = Path(experiment), Path(dataset)
    name = f'{kind}-T{steps}-s{seed}' + ('-repeat' if repeat_current else '') + ('-unweighted' if not weighted else '')
    if common_steps:
        name += f'-commonT{common_steps}'
    output = experiment/'forecast'/name
    output.mkdir(parents=True, exist_ok=True)
    identity = {'id': name, 'kind': kind, 'seed': seed, 'steps': steps,
                'repeat_current': repeat_current, 'weighted': weighted, 'common_steps': common_steps,
                'dataset': digest(dataset/'manifest-hashes.json'),
                'code': {p: digest(Path(__file__).parent/p) for p in
                         ('sequence_train.py', 'models.py', 'forecast.py', 'metrics.py')},
                'mode': 'reference_catalogue_research_only', 'device': 'cpu', 'test_evaluated': False}
    with exclusive(output/'sequence.lock'):
        oldfile = output/'identity.json'
        if oldfile.exists() and json.loads(oldfile.read_text(encoding='utf-8')) != identity:
            raise ValueError('时序实验版本已变化，请建立新编号')
        atomic_json(oldfile, identity)
        if (output/'result.json').exists():
            return json.loads((output/'result.json').read_text(encoding='utf-8'))
        raw = [r for r in read_frozen(dataset, 'forecast-ready.jsonl')
               if r['split'] in ('train', 'validation', 'calibration')]
        x, y, rows, histories = sequences(raw, steps)
        if common_steps:
            _, _, common, _ = sequences(raw, common_steps)
            allowed = {sample_id(r) for r in common}
            keep = [i for i, r in enumerate(rows) if sample_id(r) in allowed]
            x, y = x[keep], y[keep]
            rows, histories = [rows[i] for i in keep], [histories[i] for i in keep]
        if repeat_current:
            x = np.repeat(x[:, -1:], steps, axis=1)
        masks = {s: np.asarray([r['split'] == s for r in rows]) for s in ('train', 'validation', 'calibration')}
        # Fit exclusively on training observations, even when histories contain repeated days.
        imputer = SimpleImputer(strategy='median', keep_empty_features=True)
        imputer.fit(x[masks['train']].reshape(-1, len(FEATURES)))
        scaler_x = StandardScaler().fit(imputer.transform(x[masks['train']].reshape(-1, len(FEATURES))))
        x = scaler_x.transform(imputer.transform(x.reshape(-1, len(FEATURES)))).reshape(x.shape).astype('float32')
        torch.set_num_threads(4)
        torch.use_deterministic_algorithms(True)
        random.seed(seed); np.random.seed(seed); torch.manual_seed(seed)
        model = SequenceClassifier(kind, steps)
        optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3)
        train_y = y[masks['train']]
        weight = float((len(train_y)-train_y.sum())/train_y.sum()) if weighted else 1.
        loss_fn = torch.nn.BCEWithLogitsLoss(pos_weight=torch.tensor(weight))
        epoch, best, bad, best_epoch, history = 0, -1., 0, 0, []
        best_model = copy.deepcopy(model.state_dict())
        if (output/'last.pt').exists():
            saved = load_checkpoint(output/'last.pt')
            if saved['identity'] != identity:
                raise ValueError('时序检查点身份不符')
            model.load_state_dict(saved['model']); optimizer.load_state_dict(saved['optimizer'])
            epoch, best, bad, best_epoch = saved['epoch'], saved['best'], saved['bad'], saved['best_epoch']
            history, best_model = saved['history'], saved['best_model']
            torch.set_rng_state(saved['rng'])
        tx, ty = torch.from_numpy(x[masks['train']]), torch.tensor(train_y, dtype=torch.float32)
        vx, vy = torch.from_numpy(x[masks['validation']]), y[masks['validation']]
        monitor = Resources('cpu')
        from sklearn.metrics import average_precision_score
        while epoch < 80 and not (epoch >= 20 and bad >= 12):
            if (experiment/'PAUSE-FORECAST').exists():
                atomic_json(output/'status.json', {'state': 'paused', 'epoch': epoch, 'id': name})
                return {'state': 'paused'}
            begin = time.perf_counter()
            model.train()
            indices = torch.randperm(len(tx), generator=torch.Generator().manual_seed(seed+epoch*100003))
            total = 0.
            for start in range(0, len(indices), 128):
                selection = indices[start:start+128]
                optimizer.zero_grad(set_to_none=True)
                loss = loss_fn(model(tx[selection]), ty[selection])
                if not torch.isfinite(loss):
                    raise RuntimeError('时序损失非有限')
                loss.backward(); torch.nn.utils.clip_grad_norm_(model.parameters(), 1.)
                optimizer.step(); total += float(loss.detach())*len(selection)
            model.eval()
            with torch.no_grad():
                scores = model(vx).numpy()
            ap = float(average_precision_score(vy, scores))
            epoch += 1
            if ap > best:
                best, best_epoch, bad, best_model = ap, epoch, 0, copy.deepcopy(model.state_dict())
            else:
                bad += 1
            history.append({'epoch': epoch, 'train_loss': total/len(tx), 'validation_ap': ap,
                            'seconds': time.perf_counter()-begin, 'resource': monitor.sample()})
            atomic_checkpoint(output/'last.pt', {'epoch': epoch, 'best': best, 'best_epoch': best_epoch,
                'bad': bad, 'model': model.state_dict(), 'best_model': best_model,
                'optimizer': optimizer.state_dict(), 'rng': torch.get_rng_state(), 'history': history, 'identity': identity})
            atomic_json(output/'metrics.json', history)
            atomic_json(output/'status.json', {'id': name, 'state': 'running', 'epoch': epoch,
                                               'best_validation_ap': best, 'updated_utc': now()})
        model.load_state_dict(best_model); model.eval()
        with torch.no_grad():
            cal_raw = model(torch.from_numpy(x[masks['calibration']])).numpy()
            val_raw = model(vx).numpy()
        temperature = temperature_fit(cal_raw, y[masks['calibration']])
        threshold = threshold_on_calibration(y[masks['calibration']], sigmoid(cal_raw/temperature))
        climate = float(train_y.mean())
        result = {**identity, 'state': 'completed', 'epochs': epoch, 'best_epoch': best_epoch,
                  'temperature': temperature, 'threshold': threshold, 'climate': climate,
                  'preprocessing': {'features': list(FEATURES), 'fit_split': 'train',
                     'median': imputer.statistics_.tolist(), 'mean': scaler_x.mean_.tolist(), 'scale': scaler_x.scale_.tolist()},
                  'validation_raw': forecast_scores(vy, sigmoid(val_raw), .5, climate),
                  'validation_calibrated': forecast_scores(vy, sigmoid(val_raw/temperature), threshold, climate),
                  'samples': {s: int(m.sum()) for s, m in masks.items()},
                  'validation_interpretation': '2022校准的2021回顾开发检查，不能称2021实时预报',
                  'selection_rule': '预注册固定结构；用开发AP早停，不进行测试选型',
                  'resource': monitor.sample(), 'completed_utc': now()}
        from safetensors.torch import save_file
        save_file({k: v.contiguous() for k, v in best_model.items()}, str(output/'weights.safetensors'))
        selected = [i for i, r in enumerate(rows) if r['split'] == 'validation']
        atomic_json(output/'validation-predictions.json', [
            {'id': sample_id(rows[i]), 'noaa': rows[i]['noaa'], 'time_utc': rows[i]['time_utc'],
             'target': int(y[i]), 'raw_score': float(s), 'probability': float(p), 'history': histories[i]}
            for i, s, p in zip(selected, val_raw, sigmoid(val_raw/temperature))])
        atomic_json(output/'result.json', result)
        atomic_json(output/'status.json', {'id': name, 'state': 'completed', 'epoch': epoch, 'updated_utc': now()})
        print(json.dumps({'id': name, 'validation': result['validation_calibrated']}, ensure_ascii=False), flush=True)
        return result


def suite(experiment, dataset):
    from .forecast import run_baselines
    with exclusive(Path(experiment)/'forecast-suite.lock'):
        for group in ('all', 'without_flux_area', 'without_gradients_R'):
            run_baselines(dataset, experiment, feature_group=group)
        run_baselines(dataset, experiment, cohort='sequence3')
        from .temporal_baselines import run
        run(experiment, dataset)
        for seed in (17, 42, 2026):
            for kind in ('lstm', 'mlp'):
                fit(experiment, dataset, kind, seed)
            fit(experiment, dataset, 'lstm', seed, repeat_current=True)
            fit(experiment, dataset, 'lstm', seed, weighted=False)
            fit(experiment, dataset, 'lstm', seed, steps=3, common_steps=5)
            fit(experiment, dataset, 'lstm', seed, steps=5, common_steps=5)


if __name__ == '__main__':
    from .runtime import configure_environment
    from .__main__ import DEFAULT_DATA, DEFAULT_EXPERIMENT
    configure_environment()
    suite(DEFAULT_EXPERIMENT, DEFAULT_DATA)
