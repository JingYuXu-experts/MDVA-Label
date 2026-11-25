"""Check the frozen regional forecast comparison on newly acquired development images."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

import numpy as np

from .ar_hmi_registration import fits_field
from .ar_srs_geometry_audit import load_coordinates
from .durable import atomic_json
from .forecast_disk_xrs import XRS
from .forecast_region_detector_audit import MODEL
from .forecast_srs_availability import lookup_day
from .forecast_srs_candidate import attach_labels, evaluate, fetch_reports, linked_features
from .journal import read
from .runtime import digest, now, output_path
from .xrs_coverage import utc


def run(dataset, fresh, prior, xrs, joined, diagnostic, output, earlier_fresh=None):
    dataset, fresh, prior, xrs, joined, diagnostic = (Path(p).resolve() for p in (dataset, fresh, prior, xrs, joined, diagnostic))
    output = output_path(output, dataset)
    if output.exists() or any(output.is_relative_to(p) for p in (fresh, prior, xrs, joined, diagnostic)):
        raise ValueError('新区域预报开发对照使用独立目录')
    fresh_result, old, archive = (read(folder/'summary.json') for folder in (fresh, prior, xrs))
    if any(r['state'] != 'completed' or r['test_accessed'] or r['calibration_accessed'] for r in (fresh_result, old, archive)):
        raise ValueError('预报数据不是已经完成的开发范围')
    old_plan, join_report = read(prior/'protocol.json'), read(joined/'summary.json')
    index_path = diagnostic/'case-index.json'
    if (fresh_result['protocol_sha256'] != digest(fresh/'protocol.json') or old['protocol_sha256'] != digest(prior/'protocol.json')
            or read(fresh/'protocol.json')['dataset_sha256'] != digest(dataset/'manifest-hashes.json')
            or old_plan['xrs_summary_sha256'] != digest(xrs/'summary.json') or archive['rows_sha256'] != digest(xrs/'rows.json')
            or old_plan['joins_sha256'] != digest(joined/'development-joins.jsonl')
            or join_report['frozen_manifest_sha256'] != digest(dataset/'manifest-hashes.json')
            or old_plan['diagnostic_index_sha256'] != digest(index_path)
            or old['outcomes_sha256'] != digest(prior/'matched-outcomes.json') or old['features_sha256'] != digest(prior/'input-features.json')):
        raise ValueError('前次预报实验或输入指纹发生改变')
    old_rows = read(prior/'matched-outcomes.json')
    prior_images = {r['uri'] for r in read(prior/'input-features.json') if r['split'] == 'validation'}
    selected = fresh_result['results']
    selected_uris = {r['uri'] for r in selected}
    selection_plan = read(fresh/'protocol.json')
    if (not selected or len(selected_uris) != len(selected) or selected_uris != set(selection_plan['selection'])
            or any(r['split'] != 'validation' for r in selected) or prior_images & selected_uris):
        raise ValueError('历史与新开发范围重叠或数量不符')
    earlier_sha = None
    if earlier_fresh is not None:
        earlier_fresh = Path(earlier_fresh).resolve()
        former = read(earlier_fresh/'summary.json')
        former_rows = earlier_fresh/'new-outcomes.json'
        if (output.is_relative_to(earlier_fresh) or former['state'] != 'completed' or former['test_accessed']
                or former['calibration_accessed'] or former['new_outcomes_sha256'] != digest(former_rows)
                or former['prior_summary_sha256'] != digest(prior/'summary.json')
                or former['fresh_input_summary_sha256'] != selection_plan.get('remaining_after_summary_sha256')):
            raise ValueError('上一批开发预报未能通过冻结身份检查')
        previous_fresh_outcomes = read(former_rows)
        if any(r['split'] != 'validation' or r['uri'] in selected_uris or r['uri'] in prior_images
               for r in previous_fresh_outcomes):
            raise ValueError('预测复验的开发样本重叠')
        old_rows += previous_fresh_outcomes
        earlier_sha = digest(earlier_fresh/'summary.json')
    input_xrs = {r['uri']: r for r in read(xrs/'rows.json') if r['split'] == 'validation'}
    index = {r['uri']: r for r in read(index_path) if r['run_id'] == MODEL and r['diagnostic_subset'] == 'validation'}
    ready = [r for r in selected if r['state']=='display_alignment_supported' and r['uri'] in input_xrs]
    output.mkdir(parents=True)
    srs = fetch_reports((r['observed_utc'] for r in ready), output/'srs', prior/'srs')
    features = []
    reasons = Counter()
    for row in ready:
        uri = row['uri']
        source = input_xrs[uri]
        time = utc(row['observed_utc'])
        if utc(source['xrs']['last_minute_end_utc']) > utc(source['xrs']['cutoff_utc']) or utc(source['xrs']['cutoff_utc']) >= time:
            raise ValueError('预测输入产生于观测以后')
        report = srs[lookup_day(time)]
        if 'error' in report:
            reasons['missing_srs'] += 1
            continue
        folder = fresh/'pairs'/row['id']
        fits, field = folder/row['source']['filename'], folder/'aligned-los.npy'
        case = index[uri]
        probability = diagnostic/'probabilities'/MODEL/(case['sample_id']+'.npy')
        checks = ((fits, row['source']['sha256']), (field, row['array_sha256']), (probability, case['probability_sha256']))
        if any(digest(file) != sha for file, sha in checks):
            raise ValueError('物理、模型或事件输入内容发生改变')
        _, header = fits_field(fits)
        locations = load_coordinates(report['bytes'], report['report'], time, header, row['registration'])
        if not locations:
            reasons['no_position'] += 1
            continue
        candidates, counts = linked_features(np.load(probability, allow_pickle=False), np.load(field, allow_pickle=False), locations)
        reasons.update(counts)
        features.append({'split':'validation', 'uri':uri, 'observed_utc':source['observed_utc'],
                         'target_end_utc':source['target_end_utc'], 'xrs':{k:source['features'][k] for k in XRS},
                         'regions':candidates, 'srs_sha256':report['sha256']})
    label_index = defaultdict(lambda: defaultdict(list))
    with (joined/'development-joins.jsonl').open(encoding='utf-8') as stream:
        for label in map(json.loads, stream):
            if label['split']=='validation' and label['join_state']=='exact_reference_join' and label['image_uri'] in input_xrs:
                label_index[('validation', label['image_uri'])][label['noaa']%10000].append(label)
    new_rows, exclusions = attach_labels(features, label_index)
    # Keep the original fitted population across comparisons. If a fresh NOAA
    # identifier overlaps an old training identifier, evaluate() purges it.
    train = [r for r in old_rows if r['split']=='train']
    combined = evaluate([*old_rows, *new_rows])
    independent = evaluate([*train, *new_rows])
    atomic_json(output/'features.json', features)
    atomic_json(output/'new-outcomes.json', new_rows)
    result = {'state':'completed', 'created_utc':now(), 'fresh_images':len(selected), 'xrs_hmi_pairs':len(ready),
              'source_counts':dict(reasons), 'label_exclusions':exclusions, 'fresh_only':independent, 'combined':combined,
              'scope':'Retrospective AR-level comparison on available inputs. No prospective forecast or threshold fitting on development.',
              'prior_summary_sha256':digest(prior/'summary.json'), 'fresh_input_summary_sha256':digest(fresh/'summary.json'),
              'earlier_fresh_forecast_sha256':earlier_sha,
              'new_outcomes_sha256':digest(output/'new-outcomes.json'), 'code_sha256':digest(__file__),
              'test_accessed':False, 'calibration_accessed':False, 'issued_forecast':False}
    atomic_json(output/'summary.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('dataset', 'fresh', 'prior', 'xrs', 'joined', 'diagnostic', 'output'):
        parser.add_argument('--'+key, required=True)
    parser.add_argument('--earlier-fresh')
    result = run(**vars(parser.parse_args()))
    print(json.dumps({key: result[key] for key in ('xrs_hmi_pairs', 'fresh_only', 'combined')}))


if __name__=='__main__':
    main()
