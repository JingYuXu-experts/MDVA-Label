"""Postprocess frozen diagnostic scores; no model loading or pixel access."""
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import numpy as np

from .diagnostic_support import read, write
from .metrics import paired_cluster_interval
from .runtime import digest, now


def station_intervals(output):
    output = Path(output)
    report, cases = read(output/'report.json'), read(output/'case-index.json')
    by_model = defaultdict(dict)
    for row in cases:
        if row['split'] == 'validation':
            by_model[row['run_id']][row['sample_id']] = row
    results = []
    for group in report['groups']:
        runs = [m['id'] for m in report['models'] if m.get('state') == 'completed'
                and m.get('reproduction_passed') and not m.get('partial_training')
                and m['id'].rsplit('-s', 1)[0] == group['group']]
        common = sorted(set.intersection(*(set(by_model[r]) for r in runs)))
        stations = sorted({by_model[runs[0]][sid]['instrument'] for sid in common})
        for station in stations:
            ids = [sid for sid in common if by_model[runs[0]][sid]['instrument'] == station]
            values = [float(np.mean([by_model[r][sid]['dice'] for r in runs])) for sid in ids]
            blocks = [int(datetime.fromisoformat(by_model[runs[0]][sid]['time_utc']).timestamp()//(27*86400)) for sid in ids]
            count = len(set(blocks))
            ci = paired_cluster_interval(np.zeros(len(values)), values, blocks, repeats=2000, seed=2026)['ci95'] if count >= 3 else None
            results.append({'group': group['group'], 'station': station, 'images': len(ids), 'seeds': len(runs),
                            'time_blocks': count, 'mean_dice': float(np.mean(values)), 'ci95': ci,
                            'note': '开发集27日块bootstrap；三个种子先逐图平均；少于3块不报区间；单种子不能估计种子稳定性'})
    value = {'created_utc': now(), 'report_sha256': digest(output/'report.json'), 'code_sha256': digest(__file__),
             'repeats': 2000, 'seed': 2026, 'station_intervals': results, 'test_accessed': False}
    write(output/'station-intervals.json', value)
    return value


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(); p.add_argument('--report', type=Path, required=True)
    station_intervals(p.parse_args().report)
