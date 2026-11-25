"""Test whether prior NOAA daily reports can identify detected AR candidates by position."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re

import numpy as np
from scipy.ndimage import find_objects, label

from .ar_hmi_registration import DISPLAY_RADIUS, SIZE, fits_field, source_grid
from .ar_region_retention import filter_by_area
from .datasets import SegmentationDataset
from .durable import atomic_json
from .forecast_region_detector_audit import MODEL
from .forecast_region_join import load_region_mapping
from .forecast_srs_availability import fetch, lookup_day
from .journal import read
from .runtime import digest, now, output_path
from .xrs_coverage import utc


MAX_PIXEL_DISTANCE = 12
MINIMUM_COS_ANGLE = .35
MAX_AGE_HOURS = 36
LOCATION = re.compile(r'^(\d{4})\s+[NS]\d{2}[EW]\d{2}\s+(\d{1,3})(?:\s|$)', re.M)


def project(latitude, longitude, header, registration):
    lat, delta, observer_latitude = np.deg2rad([latitude, longitude-float(header['CRLN_OBS']), float(header['CRLT_OBS'])])
    mu = np.sin(lat)*np.sin(observer_latitude) + np.cos(lat)*np.cos(delta)*np.cos(observer_latitude)
    if mu < MINIMUM_COS_ANGLE:
        return None
    u = np.cos(lat)*np.sin(delta)
    v = np.sin(lat)*np.cos(observer_latitude)-np.cos(lat)*np.cos(delta)*np.sin(observer_latitude)
    angular_radius = np.deg2rad(float(header['RSUN_OBS'])/3600)
    factor = np.cos(angular_radius) / (1 - mu*np.sin(angular_radius))
    # Source-grid rotation is inverted by the same transform used for display registration.
    # The resulting normalized east/west and north/south axes only need measured display signs.
    dx, dy, scale = registration['adjustment_xy_scale']
    sign_x, sign_y = registration['orientation']
    return ((SIZE-1)/2 + dx + sign_x*DISPLAY_RADIUS*u*factor/scale,
            (SIZE-1)/2 + dy + sign_y*DISPLAY_RADIUS*v*factor/scale)


def unique_links(projected, raster):
    by_noaa = defaultdict(set)
    by_component = defaultdict(set)
    radius = MAX_PIXEL_DISTANCE
    for number, (x, y) in projected.items():
        sx = slice(max(0, int(x)-radius), min(raster.shape[1], int(x)+radius+2))
        sy = slice(max(0, int(y)-radius), min(raster.shape[0], int(y)+radius+2))
        if sx.start >= sx.stop or sy.start >= sy.stop:
            continue
        yy, xx = np.ogrid[sy.start:sy.stop, sx.start:sx.stop]
        neighbors = set(map(int, np.unique(raster[sy, sx][(xx-x)**2+(yy-y)**2 <= radius**2]))) - {0}
        for identity in neighbors:
            by_noaa[number].add(identity)
            by_component[identity].add(number)
    links = {identity: next(iter(numbers)) for identity, numbers in by_component.items()
             if len(numbers) == 1 and len(by_noaa[next(iter(numbers))]) == 1}
    return links, len(by_component) - len(links)


def matched_ids(raster, reference):
    values, sizes = np.unique(reference[reference > 0], return_counts=True)
    area_by_id = dict(zip(values.tolist(), sizes.tolist(), strict=True))
    options = defaultdict(list)
    for identity, window in enumerate(find_objects(raster), 1):
        chosen = raster[window] == identity
        nearby, counts = np.unique(reference[window][chosen], return_counts=True)
        for number, overlap in zip(nearby, counts, strict=True):
            if number and overlap / (area_by_id[number]+int(chosen.sum())-overlap) >= .5:
                options[identity].append(int(number))
    repeated = Counter(number for labels in options.values() for number in labels)
    return {identity: numbers[0] for identity, numbers in options.items()
            if len(numbers) == 1 and repeated[numbers[0]] == 1}


def load_coordinates(raw, report, observed, header, registration):
    earlier = utc(report['issued_utc']) <= observed and not report['corrected_marker']
    if not earlier:
        return {}
    lines = raw.decode('ascii').replace('\r\n', '\n').split('\nII.', 1)[0]
    matches = defaultdict(list)
    for number, longitude in LOCATION.findall(lines):
        if int(longitude) <= 360:
            matches[int(number)].append(int(longitude))
    result = {}
    for row in report['visible_regions']:
        number = row['short_noaa']
        age = (observed - utc(row['location_valid_utc'])).total_seconds()/3600
        if not 0 <= age <= MAX_AGE_HOURS or len(matches[number]) != 1:
            continue
        xy = project(row['latitude_north_deg'], matches[number][0], header, registration)
        if xy is not None:
            result[number] = xy
    return result


def run(dataset, cohort, diagnostic, output):
    dataset, cohort, diagnostic = (Path(p).resolve() for p in (dataset, cohort, diagnostic))
    output = output_path(output, dataset)
    if output.exists() or output.is_relative_to(cohort) or output.is_relative_to(diagnostic):
        raise ValueError('历史位置审计需要独立版本')
    summary = read(cohort / 'summary.json')
    plan = read(cohort / 'protocol.json')
    if (summary['state'] != 'completed' or summary['test_accessed'] or summary['calibration_accessed']
            or summary['protocol_sha256'] != digest(cohort / 'protocol.json')
            or plan['data']['manifest-hashes.json'] != digest(dataset / 'manifest-hashes.json')
            or read(diagnostic / 'identity.json')['test_accessed']):
        raise ValueError('仅接受同一冻结队列和开发缓存')
    data = SegmentationDataset(dataset, 'AR', 'validation')
    by_uri = {row['uri']: row for row in data.rows}
    index_file = diagnostic / 'case-index.json'
    index = {row['uri']: row for row in read(index_file) if row['run_id'] == MODEL and row['diagnostic_subset'] == 'validation'}
    mapping_path = data.source('@ssafd/AR/MASK-NOAA.xlsx')
    mapping = load_region_mapping(mapping_path)
    records = [r for r in summary['results'] if r['split'] == 'validation' and r['state'] == 'display_alignment_supported']
    days = sorted({lookup_day(utc(r['observed_utc'])) for r in records})
    output.mkdir(parents=True)
    archive = output / 'srs'
    archive.mkdir()
    policy = {'root_cohort_sha256': digest(cohort / 'summary.json'), 'diagnostic_index_sha256': digest(index_file),
              'reference_identity_table_sha256': digest(mapping_path), 'code_sha256': digest(__file__),
              'max_distance_pixels': MAX_PIXEL_DISTANCE, 'min_cos_heliocentric_angle': MINIMUM_COS_ANGLE,
              'max_age_hours': MAX_AGE_HOURS, 'rule': 'one candidate and one prior SRS numbering within radius',
              'test_accessed': False, 'calibration_accessed': False}
    atomic_json(output / 'protocol.json', policy)

    def retrieve(day):
        try:
            report, sha = fetch(day, archive)
            return day, (report, sha, (archive / day.strftime('%Y%m%dSRS.txt')).read_bytes())
        except (OSError, ValueError) as exc:
            return day, {'reason': str(exc)}

    with ThreadPoolExecutor(max_workers=3) as pool:
        reports = dict(pool.map(retrieve, days))
    totals = defaultdict(Counter)
    rows = []
    for registered in records:
        uri = registered['uri']
        observed = utc(registered['observed_utc'])
        receipt = reports[lookup_day(observed)]
        lane = registered['lane']
        row = {'uri': uri, 'lane': lane, 'image_observed_utc': observed.isoformat()}
        if isinstance(receipt, dict):
            totals[lane]['missing_reports'] += 1
            rows.append({**row, 'result': 'srs_unavailable', 'reason': receipt['reason']})
            continue
        srs, source_sha, raw = receipt
        fits = cohort / 'pairs' / registered['id'] / registered['source']['filename']
        if digest(fits) != registered['source']['sha256']:
            raise ValueError('物理元数据身份改变')
        _, header = fits_field(fits)
        source_grid(header)
        predicted = index[uri]
        p = diagnostic / 'probabilities' / MODEL / (predicted['sample_id'] + '.npy')
        if (digest(p) != predicted['probability_sha256'] or by_uri[uri]['mask_uri'] != predicted['mask_uri']):
            raise ValueError('概率和参考绑定发生变化')
        raster, count = label(filter_by_area(np.load(p, allow_pickle=False) >= .4, 64))
        reference = np.load(data.source(by_uri[uri]['mask_uri']), allow_pickle=False)
        positions = load_coordinates(raw, srs, observed, header, registered['registration'])
        links, ambiguous = unique_links(positions, raster)
        matched = matched_ids(raster, reference)
        original = {candidate: mapping.get((Path(uri).stem, instance), set()) for candidate, instance in matched.items()}
        assessed = {candidate: next(iter(ids)) % 10000 for candidate, ids in original.items() if len(ids) == 1}
        eligible = {candidate for candidate, number in assessed.items() if number in positions}
        audit = []
        for candidate, number in links.items():
            scope = ('no_unique_reference' if candidate not in assessed else
                     ('agrees' if number == assessed[candidate] else 'disagrees'))
            totals[lane][scope] += 1
            audit.append({'candidate': candidate, 'srs_short_number': number, 'reference_instance': matched.get(candidate),
                          'reference_short_noaa': assessed.get(candidate), 'comparison': scope})
        totals[lane].update(images=1, candidates=count, projected_srs_regions=len(positions),
                            eligible_reference_candidates=len(eligible), linked=len(links), ambiguous=ambiguous)
        rows.append({**row, 'result': 'audited', 'srs_issued_utc': srs['issued_utc'], 'srs_source_sha256': source_sha,
                     'srs_position_pixels': positions, 'links': audit})
    atomic_json(output / 'images.json', rows)
    result = {'state': 'completed', 'created_utc': now(), 'results': totals, 'reports_requested': len(days),
              'scope': 'Earlier NOAA header time. Retrospective processed HMI geometry; not proof of prior file availability. '
                       'Reference identity table used only for scoring. No forecast issued.',
              'operational_identity_verified': False, 'forecast_issued': False, 'test_accessed': False,
              'calibration_accessed': False, 'protocol_sha256': digest(output / 'protocol.json'),
              'rows_sha256': digest(output / 'images.json')}
    atomic_json(output / 'summary.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('dataset', 'cohort', 'diagnostic', 'output'):
        parser.add_argument('--' + key, required=True)
    result = run(**vars(parser.parse_args()))
    print(json.dumps(result['results']))


if __name__ == '__main__':
    main()
