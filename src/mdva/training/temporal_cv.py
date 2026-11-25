"""Purged training-period model selection for the fixed small temporal networks."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np

from .datasets import read_frozen
from .forecast import FOLDS, purged_fold, sample_id, sequences
from .runtime import Resources, atomic_json, digest, exclusive, now


def run(experiment, dataset):
    import torch
    from sklearn.impute import SimpleImputer
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import average_precision_score
    from .models import SequenceClassifier
    from .train import atomic_checkpoint, load_checkpoint
    experiment, dataset = Path(experiment), Path(dataset)
    output = experiment/'forecast/temporal-cv'
    output.mkdir(parents=True, exist_ok=True)
    identity = {'dataset': digest(dataset/'manifest-hashes.json'),
                'code': {p: digest(Path(__file__).parent/p) for p in ('temporal_cv.py', 'forecast.py', 'models.py')},
                'folds': [list(f) for f in FOLDS], 'steps': 3, 'state': 'reference_catalogue_research_only',
                'test_evaluated': False}
    with exclusive(output/'cv.lock'):
        old = output/'identity.json'
        if old.exists() and json.loads(old.read_text(encoding='utf-8')) != identity:
            raise ValueError('时序内部验证协议已变化，请新建实验版本')
        atomic_json(old, identity)
        raw = [r for r in read_frozen(dataset, 'forecast-ready.jsonl') if r['split'] == 'train']
        x, y, rows, _ = sequences(raw, 3)
        indices = {sample_id(r): i for i, r in enumerate(rows)}
        allresults = []
        torch.set_num_threads(4)
        torch.use_deterministic_algorithms(True)
        resources = Resources('cpu')
        for kind in ('lstm', 'mlp'):
            for seed in (17, 42, 2026):
                for fold_index, fold in enumerate(FOLDS):
                    folder = output/f'{kind}-s{seed}-f{fold_index}'
                    folder.mkdir(exist_ok=True)
                    result_file = folder/'result.json'
                    if result_file.exists():
                        allresults.append(json.loads(result_file.read_text(encoding='utf-8')))
                        continue
                    train, validation = purged_fold(rows, *fold)
                    ti = [indices[sample_id(r)] for r in train]
                    vi = [indices[sample_id(r)] for r in validation]
                    if y[vi].sum() < 10 or len(np.unique(y[ti])) < 2:
                        result = {'kind': kind, 'seed': seed, 'fold': list(fold), 'state': 'skipped',
                                  'reason': '正例不足10或训练缺类'}
                        atomic_json(result_file, result); allresults.append(result)
                        continue
                    imputer = SimpleImputer(strategy='median', keep_empty_features=True).fit(x[ti].reshape(-1, 5))
                    scale = StandardScaler().fit(imputer.transform(x[ti].reshape(-1, 5)))
                    transformed = scale.transform(imputer.transform(x.reshape(-1, 5))).reshape(x.shape).astype('float32')
                    tx, vx = torch.from_numpy(transformed[ti]), torch.from_numpy(transformed[vi])
                    ty = torch.tensor(y[ti], dtype=torch.float32)
                    torch.manual_seed(seed)
                    model = SequenceClassifier(kind, steps=3)
                    optimizer = torch.optim.AdamW(model.parameters(), lr=.001)
                    loss_fn = torch.nn.BCEWithLogitsLoss(pos_weight=torch.tensor(float((len(ti)-y[ti].sum())/y[ti].sum())))
                    epoch, best, bad, best_epoch = 0, -1., 0, 0
                    best_model = copy.deepcopy(model.state_dict())
                    if (folder/'last.pt').exists():
                        saved = load_checkpoint(folder/'last.pt')
                        if saved['identity'] != identity:
                            raise ValueError('内部验证检查点版本不同')
                        model.load_state_dict(saved['model']); optimizer.load_state_dict(saved['optimizer'])
                        epoch, best, bad, best_epoch = saved['epoch'], saved['best'], saved['bad'], saved['best_epoch']
                        best_model = saved['best_model']; torch.set_rng_state(saved['rng'])
                    while epoch < 80 and not (epoch >= 20 and bad >= 12):
                        if (experiment/'PAUSE-FORECAST').exists():
                            return {'state': 'paused', 'active': folder.name}
                        model.train()
                        order = torch.randperm(len(tx), generator=torch.Generator().manual_seed(seed+epoch*100003))
                        for start in range(0, len(order), 128):
                            batch = order[start:start+128]
                            optimizer.zero_grad(set_to_none=True)
                            loss = loss_fn(model(tx[batch]), ty[batch])
                            if not torch.isfinite(loss):
                                raise RuntimeError('内部验证出现非有限损失')
                            loss.backward(); torch.nn.utils.clip_grad_norm_(model.parameters(), 1.)
                            optimizer.step()
                        model.eval()
                        with torch.no_grad():
                            score = model(vx).numpy()
                        ap = float(average_precision_score(y[vi], score))
                        epoch += 1
                        if ap > best:
                            best, best_epoch, bad = ap, epoch, 0
                            best_model = copy.deepcopy(model.state_dict())
                        else:
                            bad += 1
                        atomic_checkpoint(folder/'last.pt', {'identity': identity, 'epoch': epoch,
                            'best': best, 'best_epoch': best_epoch, 'bad': bad, 'model': model.state_dict(),
                            'best_model': best_model, 'optimizer': optimizer.state_dict(), 'rng': torch.get_rng_state()})
                        atomic_json(folder/'status.json', {'state': 'running', 'epoch': epoch, 'best_ap': best,
                                                          'updated_utc': now(), 'resource': resources.sample()})
                    result = {'kind': kind, 'seed': seed, 'fold': list(fold), 'state': 'completed',
                              'best_selection_ap': best, 'best_epoch': best_epoch, 'epochs': epoch,
                              'train_n': len(ti), 'validation_n': len(vi), 'validation_positive': int(y[vi].sum()),
                              'preprocessing_fit_split': 'current_fold_train_only',
                              'interpretation': '内部模型选择分数，未经独立测试，不作无偏泛化精度',
                              'test_evaluated': False}
                    atomic_json(result_file, result); allresults.append(result)
                    atomic_json(folder/'status.json', {'state': 'completed', 'epoch': epoch, 'updated_utc': now()})
        summary = {'identity': identity, 'fold_results': allresults,
                   'mean_ap': {k: float(np.mean([r['best_selection_ap'] for r in allresults
                                if r['kind'] == k and r['state'] == 'completed'])) for k in ('lstm', 'mlp')},
                   'resource': resources.sample(), 'completed_utc': now()}
        atomic_json(output/'summary.json', summary)
        return summary


if __name__ == '__main__':
    from .runtime import configure_environment
    from .__main__ import DEFAULT_DATA, DEFAULT_EXPERIMENT
    configure_environment()
    print(json.dumps(run(DEFAULT_EXPERIMENT, DEFAULT_DATA), ensure_ascii=False, indent=2))
