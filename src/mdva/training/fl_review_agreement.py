"""Measure a conservative two-signal review policy without changing frozen scores."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

import numpy as np

from .ar_hmi_temporal_ranker import build_metrics, matching
from .durable import atomic_json
from .fl_candidate_review import features, score_row
from .fl_ensemble_study import DATASET
from .journal import read
from .runtime import digest, now, output_path


def decision(row, rule):
    probability = features(row)[:, 5]
    morphology = score_row(row, rule['classifier'])
    probability_passes = probability >= rule['cutoffs']['probability']
    morphology_passes = morphology >= rule['cutoffs']['shape_and_brightness']
    return probability_passes, morphology_passes


def review(row, rule):
    probability, morphology = decision(row, rule)
    votes = np.zeros(len(probability), dtype=int)
    for geom in row['geometry']:
        assigned, _ = matching(geom, np.ones(len(probability), dtype=bool))
        votes[assigned] += 1
    policies = {
        'original': np.ones(len(probability), dtype=bool),
        'confidence': probability, 'single_reviewer': morphology,
        'both_signals': probability & morphology,
        'either_signal': probability | morphology,
    }
    result = {name: [build_metrics(geom, chosen)[0] for geom in row['geometry']]
              for name, chosen in policies.items()}
    for name, before in (('original', 'original'), ('confidence', 'probability'), ('single_reviewer', 'shape_and_brightness')):
        if result[name] != row['review'][before]:
            raise ValueError('原复核结果不能逐图复现')
    levels = {
        'both': probability & morphology, 'confidence_only': probability & ~morphology,
        'morphology_only': morphology & ~probability, 'neither': ~(probability | morphology),
    }
    breakdown = {
        name: {'candidates': int(selected.sum()),
               'matched_by_any_annotator': int(np.count_nonzero(selected & (votes > 0))),
               'matched_by_all_annotators': int(np.count_nonzero(selected & (votes == len(row['geometry']))))}
        for name, selected in levels.items()
    }
    return {'name': row['name'], 'time': row['time'], 'station': row['station'], 'results': result,
            'triage': breakdown}


def summarize(rows):
    statistics = {}
    for arm in rows[0]['results']:
        counts = Counter()
        for row in rows:
            for item in row['results'][arm]:
                counts.update({k:item[k] for k in ('reference', 'predicted', 'matched')})
        n, p, m = (counts[name] for name in ('reference', 'predicted', 'matched'))
        statistics[arm] = {**counts, 'precision': m / max(1, p), 'recall': m / max(1, n), 'unmatched': p - m}
    levels = {name:dict(sum((Counter(row['triage'][name]) for row in rows), Counter()))
              for name in rows[0]['triage']}
    return {'statistics': statistics, 'candidate_groups': levels}


def verified_input(path, digest_value):
    if digest(path) != digest_value:
        raise ValueError('候选复核逐图数据摘要不符')
    return read(path)


def run(base, fused, external, output):
    output = output_path(output, DATASET)
    if output.exists() or any(output.is_relative_to(p.resolve()) for p in (base, fused, external)):
        raise ValueError('双信号验证使用独立目录')
    previous, combined, held = (read(p / 'summary.json') for p in (base, fused, external))
    if any(r['state'] != 'completed' or r['test_accessed'] or r['calibration_accessed'] for r in (previous, combined, held)):
        raise ValueError('只能复核已有的开发和外部诊断结果')
    base_rules = verified_input(base / 'rules.json', previous['rule_sha256'])
    fusion_rules = verified_input(fused / 'rules.json', combined['rule_sha256'])
    protocol = {
        'code_sha256': digest(__file__), 'source_sha256': {p.name:digest(p / 'summary.json') for p in (base, fused, external)},
        'rule_sha256': {p.name:digest(p / 'rules.json') for p in (base, fused)},
        'rule': 'freeze thresholds; either-signal proposals preserved, both-signal suggestions higher priority',
        'external_role': '2019–2020 cases are already known and used solely for error reproduction',
        'test_accessed': False, 'calibration_accessed': False,
    }
    atomic_json(output / 'protocol.json', protocol)
    inputs = {
        'fused_development': (verified_input(fused / 'development.json', combined['development_sha256']), fusion_rules),
        'fused_external_error_audit': (verified_input(external / 'cases.json', held['cases_sha256'])['fused'], fusion_rules),
        'local_development': (verified_input(base / 'local-development.json', previous['development_sha256']['local']), base_rules['local']),
    }
    results, all_rows = {}, {}
    for group, (rows, rule) in inputs.items():
        reviewed = [review(row, rule) for row in rows]
        results[group] = summarize(reviewed)
        results[group]['images'] = len(rows)
        all_rows[group] = reviewed
    atomic_json(output / 'images.json', all_rows)
    value = {
        'state': 'completed', 'created_utc': now(), 'results': results, 'protocol_sha256': digest(output / 'protocol.json'),
        'images_sha256': digest(output / 'images.json'), 'test_accessed': False, 'calibration_accessed': False,
        'new_independent_evaluation': False, 'deployed': False,
    }
    atomic_json(output / 'summary.json', value)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('base', 'fused', 'external', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    print(json.dumps(run(**vars(parser.parse_args())), ensure_ascii=False))


if __name__ == '__main__':
    main()
