"""Reference-label prediction tables, reliability curves and paired uncertainty."""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime

import numpy as np

from .metrics import holm
from .paper import csv_write
from .runtime import atomic_json, digest, now


def load_family(root, pattern):
    ids = [pattern.format(seed=s) for s in (17, 42, 2026)] if '{seed}' in pattern else [pattern]
    predictions, summaries = [], []
    for identifier in ids:
        folder = root/identifier
        predictions.append(json.loads((folder/'predictions.json').read_text(encoding='utf-8')))
        summaries.append(json.loads((folder/'summary.json').read_text(encoding='utf-8')))
    canonical = {r['id'] for r in predictions[0]}
    if any({r['id'] for r in rows} != canonical for rows in predictions):
        raise ValueError('不同种子测试群体不一致')
    keys = sorted(canonical)
    maps = [{r['id']: r for r in rows} for rows in predictions]
    return {'keys': keys, 'y': np.asarray([maps[0][k]['target'] for k in keys]),
            'noaa': np.asarray([maps[0][k]['noaa'] for k in keys]),
            'time_blocks': np.asarray([int(datetime.fromisoformat(maps[0][k]['time_utc']).timestamp()//(27*86400)) for k in keys]),
            'p': np.asarray([[m[k]['probability'] for k in keys] for m in maps]),
            'summaries': summaries}


def compare(a, b, grouping='noaa', repeats=2000):
    from sklearn.metrics import average_precision_score
    if a['keys'] != b['keys'] or not np.array_equal(a['y'], b['y']):
        raise ValueError('比较必须使用同一批目标样本')
    y, group = a['y'], a[grouping]
    clusters = [np.flatnonzero(group == g) for g in np.unique(group)]
    pa, pb = a['p'], b['p']
    def ap(p, indices):
        return float(np.mean([average_precision_score(y[indices], r[indices]) for r in p]))
    def brier(p, indices):
        return float(np.mean((p[:, indices]-y[indices])**2))
    indices = np.arange(len(y))
    observed = ap(pb, indices)-ap(pa, indices)
    rng = np.random.default_rng(2026)
    ap_delta, bs_delta = [], []
    for _ in range(repeats):
        sampled = np.concatenate([clusters[i] for i in rng.integers(len(clusters), size=len(clusters))])
        if len(np.unique(y[sampled])) < 2:
            continue
        ap_delta.append(ap(pb, sampled)-ap(pa, sampled))
        bs_delta.append(brier(pb, sampled)-brier(pa, sampled))
    if not ap_delta:
        raise ValueError('聚类重采样没有有效的双类重复')
    result = {'grouping': grouping, 'clusters': len(clusters), 'requested_repeats': repeats,
              'valid_repeats': len(ap_delta), 'seed': 2026, 'ap_delta_b_minus_a': observed,
              'ap_ci95': np.quantile(ap_delta, [.025,.975]).tolist(),
              'brier_delta_b_minus_a': brier(pb, indices)-brier(pa, indices),
              'brier_ci95': np.quantile(bs_delta, [.025,.975]).tolist(),
              'seed_handling': 'mean of per-seed metrics; deterministic CPU model remains one fit, not a repeated experiment'}
    # Swap methods for whole AR clusters. A deterministic model is broadcast only
    # for the paired calculation, never counted as three independent trainings.
    if grouping == 'noaa':
        nseed = max(len(pa), len(pb))
        pa, pb = np.broadcast_to(pa, (nseed, len(y))), np.broadcast_to(pb, (nseed, len(y)))
        null = []
        for _ in range(repeats):
            mask = np.zeros(len(y), bool)
            for cluster, swap in zip(clusters, rng.integers(2, size=len(clusters))):
                if swap: mask[cluster] = True
            aa, bb = np.where(mask[None], pb, pa), np.where(mask[None], pa, pb)
            null.append(abs(ap(bb, indices)-ap(aa, indices)))
        result['p_ap_randomization'] = (1+sum(v >= abs(observed) for v in null))/(repeats+1)
    return result


def report(experiment):
    experiment = Path(experiment)
    frozen = json.loads((experiment/'forecast-selection-lock.json').read_text(encoding='utf-8'))
    root, output = experiment/'forecast-test', experiment/'reports/forecast'
    output.mkdir(parents=True, exist_ok=True)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from sklearn.metrics import precision_recall_curve
    rows = []
    for path in sorted(root.glob('*/summary.json')):
        summary = json.loads(path.read_text(encoding='utf-8'))
        score = summary['calibrated']
        rows.append({'id': summary['id'], **{k: score[k] for k in ('n','positives','pr_auc_ap','tss','hss','brier','brier_skill','precision','recall','false_alarm_ratio')}})
    csv_write(output/'test-metrics.csv', rows, ['id','n','positives','pr_auc_ap','tss','hss','brier','brier_skill','precision','recall','false_alarm_ratio'])
    comparisons = []
    for control, intervention in frozen['planned_comparisons']:
        cache = output/(control.replace('{seed}', '3seeds')+'__'+intervention.replace('{seed}', '3seeds')+'.json')
        identity = {'selection_sha256': digest(experiment/'forecast-selection-lock.json'),
                    'report_code_sha256': digest(__file__)}
        if cache.exists():
            item = json.loads(cache.read_text(encoding='utf-8'))
            if item['identity'] != identity:
                raise ValueError('统计协议已改变，请输出到新报告版本')
        else:
            a, b = load_family(root, control), load_family(root, intervention)
            item = {'control': control, 'intervention': intervention, 'identity': identity,
                    'primary_AR_clusters': compare(a, b),
                    'time_block_sensitivity': compare(a, b, 'time_blocks')}
            atomic_json(cache, item)
        comparisons.append(item)
    for row, p in zip(comparisons, holm([r['primary_AR_clusters']['p_ap_randomization'] for r in comparisons])):
        row['p_ap_holm'] = p
    atomic_json(output/'paired-statistics.json', {'scope': frozen['scope'], 'comparisons': comparisons,
                'completed_utc': now(), 'operational_release': False})
    fig, axes = plt.subplots(1, 2, figsize=(10, 4), constrained_layout=True)
    for label, pattern in [('CPU SVM, current', 'sequence3-all--svm-result'),
                            ('CPU SVM, three days', 'sequence3-flat-all--svm-result'),
                            ('LSTM seed17', 'lstm-T3-s17--result')]:
        family = load_family(root, pattern)
        p, y = family['p'][0], family['y']
        precision, recall, _ = precision_recall_curve(y, p)
        axes[0].plot(recall, precision, label=label)
        points = []
        for low in np.linspace(0, .9, 10):
            use = (p >= low) & (p < low+.1 if low < .9 else p <= 1)
            if use.any(): points.append((p[use].mean(), y[use].mean()))
        if points: axes[1].plot(*np.asarray(points).T, 'o-', label=label)
    axes[0].set(xlabel='Recall', ylabel='Precision', title='Frozen reference-AR test, common T3 cohort')
    axes[1].plot([0,1],[0,1], '--', color='grey')
    axes[1].set(xlabel='Predicted probability', ylabel='Observed fraction', title='Reliability (fixed 0.1-wide bins)')
    for ax in axes: ax.grid(alpha=.2); ax.legend(fontsize=7)
    fig.savefig(output/'pr-reliability.png', dpi=180); plt.close(fig)
    return {'tested_configs': len(rows), 'planned_comparisons': len(comparisons), 'scope': frozen['scope']}


if __name__ == '__main__':
    from .runtime import configure_environment
    from .__main__ import DEFAULT_EXPERIMENT
    configure_environment()
    print(json.dumps(report(DEFAULT_EXPERIMENT), ensure_ascii=False, indent=2))
