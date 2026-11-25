"""Audit accepted development supplements against nearby HMI and NOAA records."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from statistics import median

import numpy as np
from PIL import Image, ImageDraw

from .ar_hmi_registration import DISPLAY_RADIUS, FILENAME, SIZE, fits_field, sample, snapshot, source_grid
from .ar_srs_geometry_audit import MAX_PIXEL_DISTANCE, MINIMUM_COS_ANGLE, load_coordinates
from .datasets import SegmentationDataset
from .durable import atomic_json, commit
from .forecast_srs_availability import fetch, lookup_day
from .hmi_source_audit import tai_utc
from .journal import read
from .runtime import digest, now, output_path
from .xrs_coverage import utc


PATCH_MARGIN = 15
GAUSS_CLIP = 250
MIN_OVERLAP = .90
CARRINGTON_RATE = np.deg2rad(14.1844) / 86400


def inverse_disk(x, y, header, registration):
    """Intersect display pixel rays with a spherical photosphere, near side only."""
    dx, dy, scale = registration['adjustment_xy_scale']
    sign_x, sign_y = registration['orientation']
    angular_radius = np.deg2rad(float(header['RSUN_OBS']) / 3600)
    distance = 1 / np.sin(angular_radius)
    norm = scale * np.tan(angular_radius) / DISPLAY_RADIUS
    tx = (x - (SIZE - 1) / 2 - dx) * sign_x * norm
    ty = (y - (SIZE - 1) / 2 - dy) * sign_y * norm
    square = tx**2 + ty**2
    root = 1 - (distance**2 - 1) * square
    z = (distance * square + np.sqrt(np.where(root >= 0, root, np.nan))) / (1 + square)
    u, v = (distance - z) * tx, (distance - z) * ty
    latitude = np.deg2rad(float(header['CRLT_OBS']))
    lon = np.arctan2(u, z * np.cos(latitude) - v * np.sin(latitude))
    lat = np.arcsin(np.clip(v * np.cos(latitude) + z * np.sin(latitude), -1, 1))
    return lat, lon + np.deg2rad(float(header['CRLN_OBS'])), z


def projected_disk(lat, lon, header, registration):
    """Project fixed Carrington coordinates into the selected display grid."""
    latitude, longitude, angular_radius = np.deg2rad([float(header['CRLT_OBS']), float(header['CRLN_OBS']),
                                                     float(header['RSUN_OBS']) / 3600])
    delta = lon - longitude
    mu = np.sin(lat) * np.sin(latitude) + np.cos(lat) * np.cos(delta) * np.cos(latitude)
    u = np.cos(lat) * np.sin(delta)
    v = np.sin(lat) * np.cos(latitude) - np.cos(lat) * np.cos(delta) * np.sin(latitude)
    dx, dy, scale = registration['adjustment_xy_scale']
    sign_x, sign_y = registration['orientation']
    factor = DISPLAY_RADIUS / scale * np.cos(angular_radius) / (1 - mu * np.sin(angular_radius))
    return (SIZE - 1) / 2 + dx + sign_x * u * factor, (SIZE - 1) / 2 + dy + sign_y * v * factor, mu


def carrington_drift(latitude, seconds):
    """Howard sidereal photospheric rate relative to the Carrington frame."""
    sine_squared = np.sin(latitude)**2
    rate = (2.894 - .428*sine_squared - .370*sine_squared**2) * 1e-6
    return (rate - CARRINGTON_RATE) * seconds


def compare_fields(first, second, support):
    eligible = np.isfinite(first) & support
    both = eligible & np.isfinite(second)
    n = int(both.sum())
    fraction = n / max(1, support.size)
    if n < 64 or fraction < MIN_OVERLAP:
        return {'state': 'insufficient_geometry', 'overlap_fraction': fraction}
    a, b = first[both], second[both]
    correlation = float(np.corrcoef(a, b)[0, 1]) if min(a.std(), b.std()) > 1e-6 else None
    return {'state': 'measured', 'overlap_fraction': fraction, 'signed_correlation': correlation,
            'current_p95_gauss': float(np.percentile(abs(a), 95)),
            'neighbor_p95_gauss': float(np.percentile(abs(b), 95))}


def nominal(when):
    return when.strftime('%Y%m%d_%H%M%S')


def checked_frame(path, receipt, stamp):
    if digest(path) != receipt['sha256']:
        raise ValueError('归档磁图指纹改变')
    field, header = fits_field(path)
    recorded = tai_utc(header['T_REC'])
    observed = tai_utc(header['T_OBS'])
    expected = tai_utc(stamp.strftime('%Y.%m.%d_%H:%M:%S_TAI'))
    coordinates = [float(header[key]) for key in ('CRLT_OBS', 'CRLN_OBS')]
    if (header['QUALITY'] != 0 or recorded != expected or abs((observed - recorded).total_seconds()) > 60
            or not np.isfinite(coordinates).all()):
        raise ValueError('磁图时刻、质量或观测几何不符合要求')
    return field, header, source_grid(header), observed


def neighbor(stamp, output, source_cache):
    key = nominal(stamp)
    try:
        path, receipt = snapshot({'uri': f'hmi.m_720s.{key}_TAI.3.magnetogram.jpg'}, output / 'sources/hmi' / key,
                                 source_cache / 'hmi' / key if source_cache else None)
        return checked_frame(path, receipt, stamp), receipt
    except (OSError, ValueError, KeyError) as exc:
        return None, {'reason': str(exc)}


def reports(day, archive, observed, header, registration, source_cache):
    result = {}
    for role, expected in (('prior', day), ('following', day + timedelta(days=1))):
        try:
            name = expected.strftime('%Y%m%dSRS.txt')
            old = source_cache / 'srs' if source_cache else archive
            folder = old if (old/name).is_file() else archive
            report, sha = fetch(expected, folder)
            issued = utc(report['issued_utc'])
            if report['corrected_marker'] or (issued <= observed) != (role == 'prior'):
                raise ValueError('归档发布时间或订正版本未通过校验')
            raw = (folder / name).read_bytes()
            # The following bulletin is retrospective evidence, evaluated at its own issue time.
            # Its Carrington coordinates are still projected onto the original magnetogram.
            positions = load_coordinates(raw, report, observed if role == 'prior' else issued, header, registration)
            result[role] = {'issued_utc': issued.isoformat(), 'sha256': sha, 'positions': positions}
        except (OSError, ValueError) as exc:
            result[role] = {'reason': str(exc)}
    return result


def nearby(points, box):
    x0, y0, x1, y1 = box
    output = []
    for number, (x, y) in points.items():
        distance = float(np.hypot(max(x0 - x, 0, x - x1 + 1), max(y0 - y, 0, y - y1 + 1)))
        if distance <= MAX_PIXEL_DISTANCE:
            output.append({'short_number': number, 'box_distance_pixels': distance})
    return sorted(output, key=lambda item: item['box_distance_pixels'])


def panel(array):
    value = np.clip(np.nan_to_num(array, nan=0, posinf=0, neginf=0) / GAUSS_CLIP, -1, 1)
    weight = abs(value)[..., None]
    color = np.where(value[..., None] >= 0, [255, 73, 49], [78, 160, 255])
    rgb = weight * color + (1 - weight) * np.array([37, 42, 50])
    rgb[~np.isfinite(array)] = [10, 10, 10]
    return Image.fromarray(rgb.astype(np.uint8))


def evaluate(case, image, aligned, header, registration, frames, bulletins, destination, current_time, rotation):
    box = case['bbox_xyxy']
    x0, y0, x1, y1 = box
    left, top = max(0, x0 - PATCH_MARGIN), max(0, y0 - PATCH_MARGIN)
    right, bottom = min(SIZE, x1 + PATCH_MARGIN), min(SIZE, y1 + PATCH_MARGIN)
    y, x = np.mgrid[top:bottom, left:right]
    lat, lon, mu = inverse_disk(x, y, header, registration)
    original = aligned[top:bottom, left:right]
    support = np.isfinite(mu) & (mu >= MINIMUM_COS_ANGLE)
    values = {}
    views = {}
    for offset, (frame, receipt) in frames.items():
        if frame is None:
            views[offset] = np.full_like(original, np.nan)
            values[str(offset)] = {'state': 'unavailable', **receipt}
            continue
        field, other, grid, observed = frame
        shifted = lon + carrington_drift(lat, (observed-current_time).total_seconds()) if rotation == 'howard' else lon
        px, py, visible = projected_disk(lat, shifted, other, registration)
        sampled = sample(field, grid, registration['orientation'], registration['adjustment_xy_scale'], px, py)
        sampled[~(support & (visible >= MINIMUM_COS_ANGLE))] = np.nan
        views[offset] = sampled
        region = np.s_[y0-top:y1-top, x0-left:x1-left]
        values[str(offset)] = {**compare_fields(original, sampled, support), 'observed_utc': observed.isoformat(),
                               'candidate_box': compare_fields(original[region], sampled[region], support[region]),
                               'source': receipt}
    prior_following = {}
    for role, bulletin in bulletins.items():
        prior_following[role] = ({**{k: v for k, v in bulletin.items() if k != 'positions'},
                                 'nearby_positions': nearby(bulletin['positions'], box)}
                                if 'positions' in bulletin else bulletin)
    strip = Image.new('RGB', (4 * (right-left), bottom-top))
    earlier, later = views.values()
    panels = [image.crop((left, top, right, bottom)).convert('RGB'), panel(earlier), panel(original), panel(later)]
    for i, view in enumerate(panels):
        ImageDraw.Draw(view).rectangle((x0-left, y0-top, x1-left-1, y1-top-1), outline=(252, 218, 78))
        strip.paste(view, (i*(right-left), 0))
    strip = strip.resize((strip.width*3, strip.height*3), Image.Resampling.NEAREST)
    commit(destination, lambda stream: strip.save(stream, format='PNG'))
    return {**case, 'neighbors': values, 'srs': prior_following, 'asset': destination.name,
            'asset_sha256': digest(destination)}


def read_cases(data, dataset_hash, pipelines, physical):
    remaining = {row['uri'] for row in data.rows}
    records = []
    for pipeline, cohort in zip(pipelines, physical, strict=True):
        report, observations, plan = (read(path) for path in
                                      (pipeline/'summary.json', cohort/'summary.json', cohort/'protocol.json'))
        if any(r['state'] != 'completed' or r['test_accessed'] or r['calibration_accessed'] for r in (report, observations)):
            raise ValueError('输入报告包含未完成或非开发资料')
        expected_cohort = plan.get('dataset_sha256') or plan['data']['manifest-hashes.json']
        if (report['images_sha256'] != digest(pipeline/'images.json')
                or observations['protocol_sha256'] != digest(cohort/'protocol.json')
                or report['fresh_cohort_sha256'] not in (None, digest(cohort/'summary.json'))
                or expected_cohort != dataset_hash):
            raise ValueError('输入报告与冻结数据指纹不符')
        pairs = {row['uri']: row for row in observations['results'] if row['split'] == 'validation'}
        for row in read(pipeline/'images.json'):
            if row['uri'] not in remaining or row['uri'] not in pairs:
                raise ValueError('开发样本重复或来源未登记')
            remaining.remove(row['uri'])
            if row['added_candidates']:
                records.append((row, cohort, pairs[row['uri']]))
    if remaining:
        raise ValueError('输入报告缺少冻结开发样本')
    return records


def case_view(cases, hours, rotation):
    css = "body{font:16px system-ui;margin:2rem;background:#121923;color:#e6eef8}article{padding:1rem;background:#202b38;margin:1rem 0} img{display:block;max-width:100%;height:auto;margin:.7rem 0} small{color:#a9c3de}"
    rows = ['<!doctype html><meta charset="utf-8"><title>活动区补检复核</title><style>'+css+'</style>',
            f'<h1>活动区补检复核</h1><p>从左到右为原图、提前 {hours} 小时、当时、推后 {hours} 小时。黄色框为候选外接框。红色为正向场，蓝色为负向场，显示范围为 ±250 高斯。</p>',
            '<p>磁场持续存在只能说明附近有可重复观测的磁结构。NOAA 位置邻近性和现有参考仍需分别核对。</p>']
    if rotation == 'howard':
        rows.append('<p>邻帧已按 Howard 差速自转模型投影至当时的日面坐标。</p>')
    for item in cases:
        scores = []
        for hour, view in item['neighbors'].items():
            correlation = view.get('candidate_box', {}).get('signed_correlation')
            scores.append(f'{hour} 小时 {correlation:.2f}' if correlation is not None else f'{hour} 小时缺测')
        identities = []
        for role, details in item['srs'].items():
            title = '前一份通报' if role == 'prior' else '后一份通报'
            if 'nearby_positions' in details:
                position = ', '.join(f"{r['short_number']:04d}（{r['box_distance_pixels']:.1f} 像素）"
                                     for r in details['nearby_positions']) or '无邻近记录'
            else:
                position = '资料未通过校验'
            identities.append(f'{title} {position}')
        state = {'recovered_reference': '找回参考区域', 'disjoint_from_reference': '与参考不重叠',
                 'overlaps_reference': '与参考重叠，未形成匹配'}[item['classification']]
        rows.append(f'<article><strong>{state} · {escape(item["observed_utc"])} · 候选 {item["component"]}</strong>'
                    f'<img src="{item["asset"]}" alt="原图和三个时刻的视向磁图">'
                    f'<small>候选框内有符号磁场相关 {escape("，".join(scores))}。{escape("；".join(identities))}。</small></article>')
    return '\n'.join(rows)


def run(dataset, pipelines, physical, output, hours=6, source_cache=None, rotation='carrington'):
    dataset = Path(dataset).resolve()
    pipelines, physical = ([Path(path).resolve() for path in paths] for paths in (pipelines, physical))
    output = output_path(output, dataset)
    source_cache = Path(source_cache).resolve() if source_cache else None
    if hours not in (6, 24) or rotation not in ('carrington', 'howard'):
        raise ValueError('邻帧时差或自转方案无效')
    offsets = (-hours, hours)
    if (output/'summary.json').exists() or any(output.is_relative_to(path) for path in [*pipelines, *physical]):
        raise ValueError('使用独立审计输出目录')
    data = SegmentationDataset(dataset, 'AR', 'validation')
    dataset_hash = digest(dataset/'manifest-hashes.json')
    records = read_cases(data, dataset_hash, pipelines, physical)
    cases = []
    for row, cohort, registered in records:
        stamps = FILENAME.fullmatch(Path(row['uri']).name)
        record_time = datetime.strptime(stamps[1], '%Y%m%d_%H%M%S')
        if registered['state'] != 'display_alignment_supported':
            raise ValueError('新增物理候选缺少已核验的同一时刻磁图')
        folder = cohort/'pairs'/registered['id']
        source = registered['source']
        _, header, _, observed = checked_frame(folder/source['filename'], source, record_time)
        cached = folder/'aligned-los.npy'
        if registered['array_sha256'] != digest(cached):
            raise ValueError('当前磁图配准缓存改变')
        aligned = np.load(cached, allow_pickle=False)
        registration = registered['registration']
        with Image.open(data.source(row['uri'])) as raw:
            image = raw.convert('L')
        # FITS checksum inspection toggles image compression handling during each open.
        frames = {offset: neighbor(record_time + timedelta(hours=offset), output, source_cache) for offset in offsets}
        bulletins = reports(lookup_day(observed), output/'sources/srs', observed, header, registration, source_cache)
        for case in row['added_candidates']:
            asset = output/f'{nominal(record_time)}-{case["component"]}.png'
            record = evaluate(case, image, aligned, header, registration, frames, bulletins, asset, observed, rotation)
            cases.append({**record, 'uri': row['uri'], 'observed_utc': observed.isoformat(), 'current_source': source})
    atomic_json(output/'cases.json', cases)
    commit(output/'复核图集.html', lambda stream: stream.write(case_view(cases, hours, rotation).encode('utf-8')))
    groups = defaultdict(list)
    for case in cases:
        groups[case['classification']].append(case)
    totals = {}
    for name, items in groups.items():
        stats = {'cases': len(items)}
        for offset in offsets:
            views = [item['neighbors'][str(offset)] for item in items]
            scores = [view.get('signed_correlation') for view in views]
            finite = [value for value in scores if value is not None]
            box = [view['candidate_box']['signed_correlation'] for view in views
                   if view.get('candidate_box', {}).get('signed_correlation') is not None]
            stats[str(offset)] = {'measured': len(finite), 'median_signed_correlation': median(finite) if finite else None,
                                  'box_measured': len(box), 'box_median_signed_correlation': median(box) if box else None}
        for role in ('prior', 'following'):
            checks = [item['srs'][role]['nearby_positions'] for item in items if 'nearby_positions' in item['srs'][role]]
            stats[role] = {'audited': len(checks), 'with_nearby_reported_position': sum(bool(item) for item in checks)}
        totals[name] = stats
    result = {'state': 'completed', 'created_utc': now(), 'groups': totals, 'cases_sha256': digest(output/'cases.json'),
              'code_sha256': digest(__file__), 'scope': 'Development candidate review only',
              'offset_hours': list(offsets), 'rotation': rotation,
              'dataset_sha256': dataset_hash, 'input_summaries': [{'path': str(path/'summary.json'),
              'sha256': digest(path/'summary.json')} for path in [*pipelines, *physical]],
              'later_frame_used_for_prediction': False, 'test_accessed': False, 'calibration_accessed': False, 'deployed': False}
    atomic_json(output/'summary.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', required=True)
    parser.add_argument('--pipelines', nargs='+', required=True)
    parser.add_argument('--physical', nargs='+', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--hours', choices=(6, 24), type=int, default=6)
    parser.add_argument('--rotation', choices=('carrington', 'howard'), default='carrington')
    parser.add_argument('--source-cache')
    result = run(**vars(parser.parse_args()))
    print(Counter({name: stats['cases'] for name, stats in result['groups'].items()}))


if __name__ == '__main__':
    main()
