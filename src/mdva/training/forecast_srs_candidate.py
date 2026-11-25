"""Retrospective same-region forecast comparison with and without detector masks."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import find_objects, label
import torch

from .ar_hmi_candidate_ranker import infer
from .ar_hmi_registration import fits_field
from .ar_region_retention import filter_by_area
from .ar_scale_study import ROOT, configuration, load_plan, warm_model
from .ar_srs_geometry_audit import load_coordinates, unique_links
from .datasets import SegmentationDataset
from .durable import atomic_json
from .forecast_disk_xrs import XRS, scores, train_fit
from .forecast_region_detector_audit import MODEL
from .forecast_srs_availability import fetch, lookup_day
from .journal import read
from .metrics import forecast_scores
from .reviewed_train import seed_all
from .runtime import Resources, configure_environment, digest, exclusive, now, output_path
from .xrs_coverage import utc


APERTURE_RADIUS = 32
FIELD = ('abs_field_p95', 'opposite_sign_fraction')
APERTURE = tuple('fixed_aperture_' + key for key in FIELD)
CANDIDATE = tuple('detected_mask_' + key for key in FIELD) + ('candidate_log_area',)
ARMS = {'xrs': XRS, 'xrs_and_aperture': (*XRS, *APERTURE), 'xrs_aperture_and_mask': (*XRS, *APERTURE, *CANDIDATE)}


def fetch_reports(times, cache, previous=None):
    cache.mkdir(parents=True, exist_ok=True)

    def retrieve(day):
        try:
            name = day.strftime('%Y%m%dSRS.txt')
            archive = previous if previous is not None and (previous / name).is_file() else cache
            report, checksum = fetch(day, archive)
            path = archive / name
            return day, {'report': report, 'sha256': checksum, 'bytes': path.read_bytes()}
        except (ValueError, OSError) as exc:
            return day, {'error': f'{type(exc).__name__}: {exc}'}

    with ThreadPoolExecutor(max_workers=3) as pool:
        return dict(pool.map(retrieve, sorted({lookup_day(utc(stamp)) for stamp in times})))


def field_features(values):
    valid = values[np.isfinite(values)]
    if not values.size or len(valid) < .9*values.size:
        return None
    return float(np.percentile(np.abs(valid), 95)), float(min(np.mean(valid >= 50), np.mean(valid <= -50)))


def aperture_values(field, x, y):
    r = APERTURE_RADIUS
    xs = slice(max(0, int(x)-r), min(field.shape[1], int(x)+r+2))
    ys = slice(max(0, int(y)-r), min(field.shape[0], int(y)+r+2))
    yy, xx = np.ogrid[ys.start:ys.stop, xs.start:xs.stop]
    return field[ys, xs][((xx-x)**2+(yy-y)**2) <= r**2]


def linked_features(probability, field, positions):
    if field.shape != probability.shape or not np.isfinite(probability).all():
        raise ValueError('模型和物理输入网格有误或概率非有限')
    raster, _ = label(filter_by_area(probability >= .4, 64))
    links, ambiguous = unique_links(positions, raster)
    windows = find_objects(raster)
    rows = []
    for identifier, number in links.items():
        window = windows[identifier-1]
        values = field[window][raster[window] == identifier]
        area = field_features(aperture_values(field, *positions[number]))
        inside = field_features(values)
        if area is None or inside is None:
            continue
        rows.append({'short_number': number, 'component': identifier,
                     'features': dict(zip((*APERTURE, *CANDIDATE), (*area, *inside, float(np.log1p(values.size))), strict=True))})
    return rows, {'spatial_links': len(links), 'ambiguities': ambiguous, 'missing_physical_support': len(links)-len(rows)}


def attach_labels(features, joins):
    chosen = []
    states = Counter()
    for image in features:
        options = joins.get((image['split'], image['uri']), {})
        for region in image['regions']:
            labels = options.get(region['short_number'], [])
            if len(labels) != 1:
                states['unmatched_or_ambiguous_label'] += 1
                continue
            target = labels[0]
            if target['target'] not in (0, 1):
                states['unknown_outcome'] += 1
                continue
            if target['time_utc'] != image['observed_utc']:
                raise ValueError('活动区目标窗口与磁图名义时刻不同')
            chosen.append({'split': image['split'], 'uri': image['uri'], 'observed_utc': image['observed_utc'],
                           'target_end_utc': image['target_end_utc'], 'target': target['target'],
                           'noaa': target['noaa'], 'features': {**image['xrs'], **region['features']}})
    return chosen, dict(states)


def evaluate(rows):
    from sklearn.metrics import average_precision_score

    validation = [row for row in rows if row['split'] == 'validation']
    training = [row for row in rows if row['split'] == 'train']
    dev_noaa = {row['noaa'] for row in validation}
    first = min((utc(row['observed_utc']) for row in validation), default=None)
    fitting = [row for row in training if row['noaa'] not in dev_noaa and first is not None and utc(row['target_end_utc']) < first]
    sizes = {lane: {'windows': len(source), 'positives': sum(row['target'] for row in source),
                    'distinct_positive_noaa': len({row['noaa'] for row in source if row['target']})}
             for lane, source in (('train', fitting), ('validation', validation))}
    if (sizes['train']['positives'] < 15 or sizes['train']['windows']-sizes['train']['positives'] < 15
            or sizes['validation']['distinct_positive_noaa'] < 3 or sizes['validation']['windows'] == sizes['validation']['positives']):
        return {'state': 'insufficient_known_outcomes', 'sizes': sizes, 'purged_training_samples': len(training)-len(fitting)}
    y = np.asarray([row['target'] for row in validation])
    climate = float(np.mean([row['target'] for row in fitting]))
    predicted = {arm: scores(train_fit(fitting, keys), validation, keys) for arm, keys in ARMS.items()}
    results = {name: forecast_scores(y, values, climate=climate) for name, values in predicted.items()}
    groups = [np.flatnonzero([row['noaa'] == key for row in validation]) for key in sorted(dev_noaa)]
    rng = np.random.default_rng(2026)
    deltas = []
    for chosen in rng.integers(len(groups), size=(2000, len(groups))):
        sample = np.concatenate([groups[index] for index in chosen])
        if np.unique(y[sample]).size == 2:
            a, b = (average_precision_score(y[sample], predicted[name][sample]) for name in ('xrs_and_aperture', 'xrs_aperture_and_mask'))
            deltas.append(b-a)
    return {'state': 'evaluated', 'sizes': sizes, 'metrics': results, 'paired_mask_ap_delta_ci95': np.quantile(deltas, [.025, .975]).tolist(),
            'purged_training_samples': len(training)-len(fitting), 'scope': 'Paired conditional comparison on successfully associated historical regions.'}


def run(study, cohort, previous, joined, diagnostic, output, srs_cache=None):
    study, cohort, previous, joined, diagnostic = (Path(p).resolve() for p in (study, cohort, previous, joined, diagnostic))
    origin = load_plan(study)
    output = output_path(output, origin['dataset'])
    if output.exists() or any(output.is_relative_to(path) for path in (study, cohort, previous, joined, diagnostic)):
        raise ValueError('预报位置试验需要独立的新目录')
    audit = read(cohort / 'summary.json')
    earlier = read(previous / 'summary.json')
    joins_summary = read(joined / 'summary.json')
    if (audit['state'] != 'completed' or audit['test_accessed'] or earlier['test_accessed']
            or audit['protocol_sha256'] != digest(cohort / 'protocol.json')
            or earlier['rows_sha256'] != digest(previous / 'rows.json')
            or joins_summary['frozen_manifest_sha256'] != origin['data_identity']['manifest-hashes.json']
            or joins_summary['join_rows_sha256'] != digest(joined / 'development-joins.jsonl')
            or read(diagnostic / 'identity.json')['test_accessed']):
        raise ValueError('已冻结物理、XRS或位置来源身份不符合')
    xrs = {(row['split'], row['uri']): row for row in read(previous / 'rows.json') if row['split'] in ('train', 'validation')}
    rows = [row for row in audit['results'] if row['state'] == 'display_alignment_supported'
            and (row['split'], row['uri']) in xrs]
    output.mkdir(parents=True)
    protocol = {'source_summary_sha256': digest(cohort / 'summary.json'), 'xrs_summary_sha256': digest(previous / 'summary.json'),
                'joins_sha256': joins_summary['join_rows_sha256'], 'diagnostic_index_sha256': digest(diagnostic / 'case-index.json'),
                'code_sha256': digest(__file__), 'aperture_radius_pixels': APERTURE_RADIUS,
                'arms': ARMS, 'test_accessed': False, 'calibration_accessed': False}
    atomic_json(output / 'protocol.json', protocol)
    reports = fetch_reports((r['observed_utc'] for r in rows), output / 'srs', Path(srs_cache) if srs_cache else None)
    index = {row['uri']: row for row in read(diagnostic / 'case-index.json') if row['run_id'] == MODEL and row['diagnostic_subset']=='validation'}
    data = {split: SegmentationDataset(origin['dataset'], 'AR', split) for split in ('train', 'validation')}
    config = configuration(origin, 512)
    model, _ = warm_model(origin, config)
    features = []
    counts = Counter()
    with exclusive(ROOT / '.mdva/training-gpu.lock'):
        seed_all(2026)
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats()
        resource = Resources()
        model.cuda().eval()
        for number, row in enumerate(rows, 1):
            split, uri = row['split'], row['uri']
            source = xrs[split, uri]
            observed = utc(row['observed_utc'])
            if utc(source['xrs']['last_minute_end_utc']) > utc(source['xrs']['cutoff_utc']) or utc(source['xrs']['cutoff_utc']) >= observed:
                raise ValueError('XRS输入在观测时刻之后，不能用于预测')
            report = reports[lookup_day(observed)]
            if 'error' in report:
                counts['missing_srs'] += 1
                continue
            folder = cohort / 'pairs' / row['id']
            path = folder / row['source']['filename']
            aligned = folder / 'aligned-los.npy'
            if digest(path) != row['source']['sha256'] or digest(aligned) != row['array_sha256']:
                raise ValueError('物理输入发生改变')
            _, header = fits_field(path)
            positions = load_coordinates(report['bytes'], report['report'], observed, header, row['registration'])
            if not positions:
                counts['no_position'] += 1
                continue
            if split == 'train':
                with Image.open(data[split].source(uri)) as im:
                    gray = np.array(im.convert('L'))
                probability = infer(model, gray)
            else:
                case = index[uri]
                probability_path = diagnostic / 'probabilities' / MODEL / (case['sample_id'] + '.npy')
                if digest(probability_path) != case['probability_sha256']:
                    raise ValueError('开发缓存内容发生改变')
                probability = np.load(probability_path, allow_pickle=False)
            regions, stats = linked_features(probability, np.load(aligned, allow_pickle=False), positions)
            counts.update(stats)
            features.append({'split': split, 'uri': uri, 'observed_utc': source['observed_utc'], 'target_end_utc': source['target_end_utc'],
                             'xrs': {key: source['features'][key] for key in XRS}, 'regions': regions, 'srs_sha256': report['sha256']})
            if number % 32 == 0:
                resource.sample()
        measured = resource.sample()
        model.cpu()
    joins = defaultdict(lambda: defaultdict(list))
    with (joined / 'development-joins.jsonl').open(encoding='utf-8') as stream:
        for row in map(json.loads, stream):
            if row['split'] in ('train', 'validation') and row['join_state'] == 'exact_reference_join':
                joins[row['split'], row['image_uri']][row['noaa']%10000].append(row)
    selected, excluded = attach_labels(features, joins)
    evaluation = evaluate(selected)
    atomic_json(output / 'input-features.json', features)
    atomic_json(output / 'matched-outcomes.json', selected)
    result = {'state': 'completed', 'created_utc': now(), 'paired_frames': len(rows), 'source_counts': dict(counts),
              'label_exclusions': excluded, 'evaluation': evaluation, 'resource': measured,
              'operational_availability_verified': False, 'issued_forecast': False, 'test_accessed': False, 'calibration_accessed': False,
              'protocol_sha256': digest(output / 'protocol.json'), 'features_sha256': digest(output / 'input-features.json'),
              'outcomes_sha256': digest(output / 'matched-outcomes.json')}
    atomic_json(output / 'summary.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('study', 'cohort', 'previous', 'joined', 'diagnostic', 'output'):
        parser.add_argument('--' + key, required=True)
    parser.add_argument('--srs-cache')
    configure_environment()
    result = run(**vars(parser.parse_args()))
    print(json.dumps({key: result[key] for key in ('paired_frames', 'source_counts', 'label_exclusions', 'evaluation')}))


if __name__ == '__main__':
    main()
