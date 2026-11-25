"""Versioned AR-day canonicalization; never change prior manifests or results."""
from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path

from .datasets import read_frozen
from .forecast import FEATURES
from .runtime import atomic_json, digest, now, output_path


def canonicalize(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[(row['split'], row['time_utc'], row['noaa'])].append(row)
    ready, evidence, conflicts = [], [], []
    for (split, time, noaa), members in groups.items():
        comparison = {json.dumps({'features': {k: r['features'][k] for k in FEATURES},
                                 'target': r['target_m_plus'], 'start': r['target_start'],
                                 'end': r['target_end'], 'horizon': r['forecast_horizon_hours']}, sort_keys=True)
                      for r in members}
        provenance = [{'parameters_uri': r['parameters_uri'], 'parameter_line': r['parameter_line'],
                       'instance_id': r['instance_id'], 'stem': r['stem']} for r in members]
        identity = f'ARday:{time}|NOAA:{noaa}'
        if len(comparison) != 1:
            conflicts.append({'sample_id': identity, 'associations': provenance,
                              'reason': '同区域同时间数值输入/目标不一致，不能猜选或求平均'})
            continue
        row = dict(sorted(members, key=lambda r: (r['parameters_uri'], r['parameter_line']))[0])
        row.update(sample_id=identity, sample_unit='NOAA_AR_day',
                   provenance_associations=provenance, source_record_count=len(members),
                   instance_id_is_feature=False, primary_record_is_provenance_only=True)
        ready.append(row)
        if len(members) > 1:
            evidence.append({'sample_id': identity, 'records': len(members), 'associations': provenance,
                             'numeric_features_identical': True, 'target_identical': True,
                             'action': '一个AR日数值样本，保留全部原实例关联；不合并或改写像素掩膜'})
    return ready, evidence, conflicts


def prepare(parent, source):
    parent, source = Path(parent), Path(source)
    experiment = parent/'revisions/forecast-v2'
    dataset = experiment/'dataset'
    output_path(experiment, source)
    if (experiment/'protocol.json').exists():
        raise ValueError('关联修订版本已存在，禁止覆盖')
    rows = read_frozen(source, 'forecast-ready.jsonl')
    ready, duplicates, conflicts = canonicalize(rows)
    dataset.mkdir(parents=True, exist_ok=True)
    raw = ''.join(json.dumps(r, ensure_ascii=False, allow_nan=False)+'\n' for r in ready).encode('utf-8')
    (dataset/'forecast-ready.jsonl').write_bytes(raw)
    (dataset/'sources.json').write_bytes((source/'sources.json').read_bytes())
    manifest = {'forecast-ready.jsonl': digest(dataset/'forecast-ready.jsonl')}
    atomic_json(dataset/'manifest-hashes.json', manifest)
    report = {'created_utc': now(), 'parent_dataset_manifest_sha256': digest(source/'manifest-hashes.json'),
              'task': 'NOAA_AR_day_numeric_reference_forecast', 'source_rows': len(rows), 'canonical_rows': len(ready),
              'duplicate_groups': duplicates, 'conflicts_quarantined': conflicts,
              'counts': dict(Counter(r['split'] for r in ready)),
              'positive_counts': {s: sum(r['target_m_plus'] for r in ready if r['split']==s)
                                  for s in ('train','validation','calibration','test')},
              'source_masks_modified': False, 'hyperparameter_matrix_changed': False,
              'test_policy': '修复数据关联后的同一时段复算，不是第二个独立测试集；禁止据首版测试分数改参',
              'prior_results': '原参数行级探索证据保留；不作为独立AR日论文结果'}
    atomic_json(dataset/'association-revision.json', report)
    protocol = json.loads((parent/'protocol.json').read_text(encoding='utf-8'))
    protocol.update(created_utc=now(), dataset=str(dataset.resolve()),
                    dataset_manifest_sha256=digest(dataset/'manifest-hashes.json'), recognition=[],
                    scope='forecast-v2-canonical-AR-days', parent_protocol_sha256=digest(parent/'protocol.json'),
                    revision_reason='按相同数值和目标证据折叠同一NOAA/同一T重复行；冲突隔离；保留所有原关联')
    atomic_json(experiment/'protocol.json', protocol)
    atomic_json(parent/'forecast-v1-disposition.json', {'state': 'superseded_association_row_exploration',
                'reason': '参数重复行不能当作独立AR日；不得引用首版分数作正式AR日结论',
                'replacement': 'revisions/forecast-v2', 'previous_artifacts_preserved': True})
    atomic_json(parent/'active-forecast.json', {'relative': 'revisions/forecast-v2',
                'name': 'AR日去重关联修订v2', 'status': 'running', 'updated_utc': now()})
    return experiment, dataset, report


def execute(experiment, dataset):
    from .sequence_train import suite
    from .temporal_cv import run
    from .forecast_delivery import lock, evaluate
    from .forecast_paper import report
    from .model_cards import forecast_cards
    suite(experiment, dataset)
    if not (Path(experiment)/'forecast/temporal-cv/summary.json').exists():
        run(experiment, dataset)
    if not (Path(experiment)/'forecast-selection-lock.json').exists():
        lock(experiment)
    evaluate(experiment, dataset)
    report(experiment)
    forecast_cards(experiment)
    atomic_json(Path(experiment)/'revision-status.json', {'state': 'research_experiments_completed',
                'scientific_operational_gate': 'blocked', 'updated_utc': now(),
                'note': '仅参考标签回顾研究；权威覆盖、外部验证、业务实时发布仍未通过'})


if __name__ == '__main__':
    from .runtime import configure_environment
    from .__main__ import DEFAULT_DATA, DEFAULT_EXPERIMENT
    configure_environment()
    from .context import active_forecast
    experiment = active_forecast(DEFAULT_EXPERIMENT)
    if experiment != DEFAULT_EXPERIMENT and (experiment/'protocol.json').exists():
        dataset = Path(json.loads((experiment/'protocol.json').read_text(encoding='utf-8'))['dataset'])
    else:
        experiment, dataset, _ = prepare(DEFAULT_EXPERIMENT, DEFAULT_DATA)
    execute(experiment, dataset)
