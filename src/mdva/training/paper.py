"""Paper artifacts generated from real run files, with no fabricated empty results."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
from pathlib import Path

import numpy as np
from .run_locations import run_folder

from .metrics import paired_cluster_interval, holm
from .runtime import atomic_json, now


def csv_write(path, rows, fields):
    with Path(path).open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def grouped_images(experiment, family):
    values = {}
    for seed in (17, 42, 2026):
        file = run_folder(experiment, f'{family}-s{seed}')/'evaluation/test/images.json'
        if not file.exists():
            return None
        rows = json.loads(file.read_text(encoding='utf-8'))
        for r in rows:
            if r['dice'] is not None:
                values.setdefault(r['uri'], {'dice': [], 'time': r['time_utc']})['dice'].append(r['dice'])
    if any(len(v['dice']) != 3 for v in values.values()):
        raise ValueError('三个种子的逐图结果不一致')
    return {k: {'dice': float(np.mean(v['dice'])), 'time': v['time']} for k, v in values.items()}


def generate(experiment):
    experiment = Path(experiment)
    folder = experiment/'reports'; folder.mkdir(exist_ok=True)
    protocol = json.loads((experiment/'protocol.json').read_text(encoding='utf-8'))
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 9})
    aggregate, resources = {}, []
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.5), constrained_layout=True)
    for config in protocol['recognition']:
        run = run_folder(experiment, config['id'])
        metrics = run/'metrics.json'
        if metrics.exists():
            history = json.loads(metrics.read_text(encoding='utf-8'))
            axes[0].plot([r['epoch'] for r in history], [r['validation']['dice'] for r in history], label=config['id'])
            axes[1].plot([r['epoch'] for r in history], [r['train_loss'] for r in history], label=config['id'])
            if history:
                resources.append({'id': config['id'], 'epochs': len(history),
                    'mean_epoch_seconds': float(np.mean([r['seconds'] for r in history])),
                    'peak_rss_gib': max(r['resource']['peak_rss_gib'] for r in history),
                    'peak_reserved_gib': max(r['resource'].get('peak_reserved_gib', 0) for r in history)})
        file = run/'evaluation/test/summary.json'
        if file.exists():
            result = json.loads(file.read_text(encoding='utf-8'))
            aggregate.setdefault(config['id'].rsplit('-s', 1)[0], []).append(result)
    for ax, title in zip(axes, ('Development Dice (native grid)', 'Training loss')):
        ax.set_title(title); ax.set_xlabel('Epoch'); ax.grid(alpha=.2)
        if ax.lines:
            ax.legend(fontsize=6)
    fig.savefig(folder/'learning-curves.png', dpi=180); plt.close(fig)
    csv_write(folder/'resource.csv', resources, ['id','epochs','mean_epoch_seconds','peak_rss_gib','peak_reserved_gib'])
    statistics = []
    for family, results in aggregate.items():
        row = {'configuration': family, 'seeds_evaluated': len(results)}
        for key in ('dice', 'iou', 'precision', 'recall', 'boundary_f1'):
            numbers = [r[key] for r in results if r.get(key) is not None]
            row[key+'_mean'] = float(np.mean(numbers)) if numbers else None
            row[key+'_sd'] = float(np.std(numbers, ddof=1)) if len(numbers) > 1 else None
        statistics.append(row)
    atomic_json(folder/'recognition-test-aggregate.json', statistics)
    comparisons = []
    for control, intervention in [('FL-F0','FL-F2'), ('FL-F1','FL-F2'), ('FL-F5','FL-F2'),
                                   ('AR-A0','AR-A1'), ('CH-C1','CH-C2')]:
        a, b = grouped_images(experiment, control), grouped_images(experiment, intervention)
        if a is None or b is None:
            continue
        if a.keys() != b.keys():
            raise ValueError('方法间测试样本不一致，不能配对比较')
        keys = sorted(a)
        av, bv = np.asarray([a[k]['dice'] for k in keys]), np.asarray([b[k]['dice'] for k in keys])
        groups = np.asarray([int(datetime.fromisoformat(a[k]['time']).timestamp()//(27*86400)) for k in keys])
        result = paired_cluster_interval(av, bv, groups)
        unique = np.unique(groups)
        rng = np.random.default_rng(2026)
        differences = bv-av
        observed = abs(float(differences.mean()))
        # Paired cluster sign randomization, preserving all images in each block.
        statistics_null = []
        for _ in range(2000):
            signs = dict(zip(unique, rng.choice([-1, 1], len(unique))))
            statistics_null.append(abs(float(np.mean(differences*np.asarray([signs[g] for g in groups])))))
        result.update(control=control, intervention=intervention,
                      p_randomization=(1+sum(v >= observed for v in statistics_null))/2001,
                      bootstrap_unit='UTC 27-day block', seed_aggregation='per-image mean of three seeds')
        comparisons.append(result)
    if len(comparisons) == 5:
        for row, p in zip(comparisons, holm([r['p_randomization'] for r in comparisons])):
            row['p_holm'] = p
    atomic_json(folder/'paired-recognition.json', {'comparisons': comparisons,
                'all_planned_contrasts_present': len(comparisons) == 5,
                'note': '未完成全部预注册对比时不报告校正后的显著性'})
    (folder/'REPORT.md').write_text(
        '# 实验材料（自动生成）\n\n生成时间 UTC：'+now()+'\n\n'
        '学习曲线为开发指标。recognition.csv 中空白表示未完成，不是零分。\n\n'
        f'已有正式测试配置：{len(statistics)}。全部预注册配置完成并锁定前，正式测试保持封存。\n\n'
        '识别表、资源表与聚类统计由逐样本结果自动生成。预测仍需标签科学覆盖验收。\n', encoding='utf-8')
    return {'reports': str(folder), 'test_configurations': len(statistics)}
