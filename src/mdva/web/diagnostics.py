"""Read-only diagnostic reports. Web environment never imports torch."""
from __future__ import annotations

from collections import defaultdict
from functools import lru_cache
import json
from pathlib import Path
import re


def root(output):
    return Path(output)/'model-diagnostics'


def folder(output, identifier=''):
    base = root(output).resolve()
    if identifier and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,80}', identifier):
        raise ValueError('诊断报告编号不合法')
    if not identifier:
        candidates = [p for p in base.glob('*/identity.json') if (p.parent/'status.json').is_file()]
        if not candidates:
            raise FileNotFoundError('尚未生成效果诊断')
        result = max(candidates, key=lambda p: p.stat().st_mtime_ns).parent
    else:
        result = base/identifier
    if not result.resolve().is_relative_to(base) or not (result/'identity.json').is_file():
        raise FileNotFoundError('诊断报告不存在')
    return result


@lru_cache(maxsize=6)
def cached(path, stamp, size):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def load(path, default=None):
    if not path.is_file():
        return default
    st = path.stat()
    return cached(str(path), st.st_mtime_ns, st.st_size)


def summary(output, identifier=''):
    try:
        current = folder(output, identifier)
    except FileNotFoundError:
        if identifier:
            raise
        return {'available': False, 'notice': '效果诊断尚未生成；训练不会因此自动启动'}
    report = load(current/'report.json', {})
    return {'available': True, 'report_id': current.name, 'status': load(current/'status.json', {}),
            'report': report, 'interpretation': load(current/'interpretation.json'),
            'statistics': load(current/'station-intervals.json'),
            'reports': sorted(p.parent.name for p in root(output).glob('*/identity.json')
                                              if (p.parent/'status.json').is_file())}


def cases(output, identifier='', category='FL', run_id='', station='', error_type='',
          page=1, page_size=24, order='fixed', subset='validation', sample_id=''):
    if page < 1 or not 1 <= page_size <= 50:
        raise ValueError('页码或每页数量非法')
    if category not in ('AR', 'CH', 'FL') or subset not in ('validation', 'train_fit_sample', 'train_empty_reference'):
        raise ValueError('仅支持训练／验证诊断')
    if run_id and not re.fullmatch(r'(AR-A[01]|CH-C[012]|FL-F[0-8])-s(17|42|2026)', run_id):
        raise ValueError('模型编号不合法')
    if sample_id and not re.fullmatch(r'[a-f0-9]{24}', sample_id):
        raise ValueError('样本编号不合法')
    if order not in ('fixed', 'worst', 'middle', 'best'):
        raise ValueError('排序方式非法')
    current = folder(output, identifier)
    all_rows = load(current/'case-index.json', [])
    available = sorted({r['run_id'] for r in all_rows if r['category'] == category})
    partial = {m['id'] for m in load(current/'report.json', {}).get('models', []) if m.get('partial_training')}
    chosen = run_id or next((r for r in available if r.endswith('-s17') and r not in partial),
                            next((r for r in available if r not in partial), ''))
    rows = [r for r in all_rows if r['category'] == category and r['run_id'] == chosen and
            r['diagnostic_subset'] == subset and (not station or r['instrument'] == station) and
            (not error_type or error_type in r['error_types']) and (not sample_id or r['sample_id'] == sample_id)]
    if order == 'fixed':
        groups = defaultdict(list)
        for row in sorted(rows, key=lambda r: r['sample_id']):
            groups[(row['instrument'], row['area_bin'])].append(row)
        rows = []
        for i in range(max([len(v) for v in groups.values()] or [0])):
            rows.extend(v[i] for _, v in sorted(groups.items()) if i < len(v))
    else:
        values = sorted(r['dice'] for r in rows if r['dice'] is not None)
        middle = values[len(values)//2] if values else 0
        rows.sort(key=lambda r: abs((r['dice'] or 0)-middle) if order == 'middle' else (r['dice'] or 0),
                  reverse=order == 'best')
    items = [{k: v for k, v in row.items() if k not in ('thresholds', 'probability_sha256')}
             for row in rows[(page-1)*page_size:page*page_size]]
    return {'report_id': current.name, 'items': items, 'total': len(rows), 'page': page, 'page_size': page_size,
            'run_id': chosen, 'models': available,
            'stations': sorted({r['instrument'] for r in all_rows if r['category'] == category}),
            'error_types': sorted({k for r in all_rows if r['category'] == category for k in r['error_types']})}


def asset(output, identifier, asset_id):
    current = folder(output, identifier)
    if asset_id in ('report.json', 'report.md', 'interpretation.json', 'interpretation.md', 'next-experiment-proposal.json', 'station-intervals.json'):
        path = current/asset_id
        return path.read_bytes(), 'application/json; charset=utf-8' if path.suffix == '.json' else 'text/markdown; charset=utf-8'
    if not re.fullmatch(r'[a-f0-9]{24}/(original|reference|input-\d+|(AR-A[01]|CH-C[012]|FL-F[0-8])-s(17|42|2026)-(prediction|errors))\.png', asset_id):
        raise ValueError('资源编号不合法，不接受任意路径')
    registered = {value for r in load(current/'case-index.json', []) for value in r['assets'].values()}
    if asset_id not in registered:
        raise FileNotFoundError('资源尚未登记')
    path = (current/'assets'/asset_id).resolve()
    if not path.is_relative_to((current/'assets').resolve()):
        raise ValueError('资源越界')
    return path.read_bytes(), 'image/png'
