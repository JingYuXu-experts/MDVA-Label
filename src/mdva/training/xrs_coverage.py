"""Quality-minute coverage from pinned NOAA science products, without training.

Coverage describes valid one-minute averages, not continuous one-second sampling
or proof that the historical product was available to an operator at that time.
"""
from __future__ import annotations

from collections import Counter
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re

import numpy as np

from .durable import atomic_json, commit
from .runtime import digest, now

UTC = timezone.utc
NAME = re.compile(r'sci_xrsf-l2-avg1m_g(\d{2})_d(\d{8})_v\d+-\d+-\d+\.nc')
YEAR_NAME = re.compile(r'sci_xrsf-l2-avg1m_g(\d{2})_y(20\d{2})_v\d+-\d+-\d+\.nc')


def utc(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('时间必须含明确时区')
    return parsed.astimezone(UTC)


def iso(seconds):
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def product_time(value):
    """Legacy annual headers use unpadded fields with an explicit UTC suffix."""
    value = text(value)
    if re.fullmatch(r'\d{4}-\d{1,2}-\d{1,2} \d{1,2}:\d{1,2}:\d{1,2} UTC', value):
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S UTC').replace(tzinfo=UTC)
    return utc(value)


def snapshot(directory, satellite, day):
    """Reuse verified pinned bytes; an upstream revision needs a new directory."""
    if type(satellite) is not int or satellite not in range(8, 20):
        raise ValueError('未登记的GOES卫星')
    if not isinstance(day, str) or not re.fullmatch(r'20(?:1[0-9]|2[0-4])\d{4}', day):
        raise ValueError('历史日期无效')
    datetime.strptime(day, '%Y%m%d')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    cached = list(directory.glob(f'sci_xrsf-l2-avg1m_g{satellite:02d}_d{day}_v*.source.json'))
    if len(cached) > 1:
        raise ValueError('同一卫星日期存在多个来源版本，需明确隔离')
    if cached:
        meta = json.loads(cached[0].read_text(encoding='utf-8'))
        verify_source(directory, meta)
        if meta['satellite'] != satellite or meta['day'] != day:
            raise ValueError('缓存卫星日期身份不符')
        return meta
    from ..web.space_weather import download_xrs_coverage
    raw, meta = download_xrs_coverage(satellite, day)
    path = directory / meta['filename']
    if path.exists():
        raise ValueError('存在无来源清单的文件，请隔离后重新获取，禁止覆盖')
    commit(path, lambda stream: stream.write(raw))
    meta.update(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw), fetched_utc=now())
    atomic_json(path.with_suffix('.source.json'), meta)
    return meta


def snapshot_year(directory, satellite, year):
    """Pin an official annual aggregation, retaining partial-year coverage."""
    if type(satellite) is not int or satellite not in range(13, 20):
        raise ValueError('未核验的年度产品卫星')
    if type(year) is not int or not 2010 <= year <= 2024:
        raise ValueError('年度必须位于2010—2024')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    cached = list(directory.glob(f'sci_xrsf-l2-avg1m_g{satellite:02d}_y{year}_v*.source.json'))
    if len(cached) > 1:
        raise ValueError('同一卫星年度存在多个版本，需隔离')
    if cached:
        meta = json.loads(cached[0].read_text(encoding='utf-8'))
        verify_source(directory, meta)
        if (meta['satellite'], meta['year']) != (satellite, year):
            raise ValueError('年度来源身份不符')
        return meta
    from ..web.space_weather import download_xrs_coverage_year
    raw, meta = download_xrs_coverage_year(satellite, year)
    # A short HTTP response can have no Content-Length. Validate the container
    # and required arrays before either the bytes or their identity are pinned.
    import io
    import h5py
    with h5py.File(io.BytesIO(raw), 'r') as candidate:
        for variable in ('time', 'xrsb_flux', 'xrsb_flag', 'xrsb_num'):
            candidate[variable][:]
    expected = hashlib.sha256(raw).hexdigest()
    path = directory/meta['filename']
    if path.exists():
        # A crash between two atomic commits can leave an unpaired raw file.
        # Only a fresh, byte-identical official download may certify that file.
        if digest(path) != expected:
            raise ValueError('存在内容不同的未登记年度文件，禁止覆盖')
        meta['recovered_orphan_equal_official_bytes'] = True
    else:
        commit(path, lambda stream: stream.write(raw))
    meta.update(sha256=expected, bytes=len(raw), fetched_utc=now())
    atomic_json(path.with_suffix('.source.json'), meta)
    return meta


def verify_source(directory, meta):
    name = meta['filename']
    match = NAME.fullmatch(name) if isinstance(name, str) else None
    yearly = YEAR_NAME.fullmatch(name) if isinstance(name, str) else None
    daily_valid = match and (int(match[1]), match[2]) == (meta['satellite'], meta.get('day'))
    annual_valid = yearly and (int(yearly[1]), int(yearly[2])) == (meta['satellite'], meta.get('year'))
    if not (daily_valid or annual_valid):
        raise ValueError('历史观测文件名或来源身份不符')
    path = (Path(directory) / name).resolve()
    if not path.is_relative_to(Path(directory).resolve()):
        raise ValueError('来源文件越界')
    if path.stat().st_size != meta['bytes'] or digest(path) != meta['sha256']:
        raise ValueError('历史观测缓存指纹改变，不能用于原审计')
    return path


def text(value):
    return value.decode('utf-8') if isinstance(value, bytes) else str(value)


def scalar(value):
    array = np.asarray(value)
    if array.size != 1:
        raise ValueError('不支持的产品标量元数据')
    return array.item()


def merge(intervals):
    result = []
    for start, end in sorted(intervals):
        if not np.isfinite([start, end]).all() or end <= start:
            raise ValueError('观测覆盖区间无效')
        if result and start <= result[-1][1]:
            result[-1][1] = max(end, result[-1][1])
        else:
            result.append([float(start), float(end)])
    return result


def read_source(directory, meta):
    """Fail closed on unfamiliar NetCDF metadata; never interpolate missing bins."""
    import h5py
    path = verify_source(directory, meta)
    satellite = meta['satellite']
    if satellite not in range(13, 20):
        raise ValueError('GOES 8—12质量字段尚未核验，不沿用其他代际规则')
    annual = bool(YEAR_NAME.fullmatch(meta['filename']))
    if annual:
        day = datetime(meta['year'], 1, 1, tzinfo=UTC)
        period_end = datetime(meta['year']+1, 1, 1, tzinfo=UTC)
    else:
        day = datetime.strptime(meta['day'], '%Y%m%d').replace(tzinfo=UTC)
        period_end = day + timedelta(days=1)
    with h5py.File(path, 'r') as data:
        expected_platform = f'GOES-{satellite}' if satellite < 16 else f'g{satellite}'
        if text(data.attrs['platform']) != expected_platform:
            raise ValueError('NetCDF卫星身份不符')
        if text(data.attrs['time_coverage_resolution']) != 'PT1M':
            raise ValueError('仅支持一分钟平均产品')
        declared_start = product_time(data.attrs['time_coverage_start'])
        declared_end = product_time(data.attrs['time_coverage_end'])
        if not day <= declared_start < declared_end <= period_end:
            raise ValueError('NetCDF日期范围不符')
        if not annual and (declared_start != day or declared_end != period_end):
            raise ValueError('日产品日期范围不符')
        declared_id = text(data.attrs['id'])
        allowed_ids = {meta['filename']}
        if annual and satellite >= 16:
            # Official GOES-R aggregates retain the first daily file's id.
            # Require the SAME satellite, revision and declared first day.
            allowed_ids.add(meta['filename'].replace(f'_y{meta["year"]}_', f'_d{declared_start:%Y%m%d}_'))
        if declared_id not in allowed_ids:
            raise ValueError('NetCDF产品身份不符')
        time_var = data['time']
        if (text(time_var.attrs['units']) != 'seconds since 2000-01-01 12:00:00 UTC'
                or not text(time_var.attrs['long_name']).startswith('Record start time')):
            raise ValueError('时间编码未知，不猜测记录起止')
        base = datetime(2000, 1, 1, 12, tzinfo=UTC).timestamp()
        times = np.asarray(time_var[:], dtype=np.float64) + base
        flux_var, flag_var, count_var = (data[k] for k in ('xrsb_flux', 'xrsb_flag', 'xrsb_num'))
        flux, flags, counts = flux_var[:], flag_var[:], count_var[:]
        if not (times.ndim == 1 and times.shape == flux.shape == flags.shape == counts.shape):
            raise ValueError('时间和质量数组形状不符')
        if (not np.isfinite(times).all() or np.any(np.diff(times) <= 0) or
                np.any(times < declared_start.timestamp()) or np.any(times > declared_end.timestamp())
                or np.any(times + 60 > period_end.timestamp())
                or np.any((times-day.timestamp()) % 60 != 0)):
            raise ValueError('记录乱序、重复、越界或非分钟边界')
        meanings = text(flag_var.attrs['flag_meanings']).split()
        expected = (['good_data', 'bad_data', 'eclipsed_by_earth', 'temperature_recovery']
                    if satellite < 16 else ['good_data', 'eclipse', 'bad_data', 'interpolated'])
        if meanings != expected or list(flag_var.attrs['flag_values']) != [0, 1, 2, 4]:
            raise ValueError('质量标志定义发生变化，需重新核验')
        if text(flux_var.attrs['units']) not in ('W/m2', 'W m-2'):
            raise ValueError('XRS辐照度单位未知')
        good_flux = (np.isfinite(flux) & (flux != scalar(flux_var.attrs['_FillValue'])) &
                     (flux >= scalar(flux_var.attrs['valid_min'])) &
                     (flux <= scalar(flux_var.attrs['valid_max'])))
        # num is exposed, not interpreted as seconds: old sensors sample at a
        # different cadence. Good nonempty averages certify a quality MINUTE.
        good_count = ((counts != scalar(count_var.attrs['_FillValue'])) &
                      (counts > 0) & (counts <= 60))
        good = (flags == 0) & good_flux & good_count
        selected = times[good]
        # Annual products contain half a million bins. Detect contiguous runs
        # in arrays rather than constructing half a million Python intervals.
        if len(selected):
            breaks = np.flatnonzero(np.diff(selected) != 60) + 1
            first = np.r_[0, breaks]
            last = np.r_[breaks-1, len(selected)-1]
            intervals = [[float(selected[a]), float(selected[b]+60)] for a, b in zip(first, last)]
        else:
            intervals = []
        report = {'filename': meta['filename'], 'sha256': meta['sha256'], 'url': meta['url'],
                  'satellite': satellite, 'period': meta.get('day', str(meta.get('year'))),
                  'time_coverage_start': declared_start.isoformat(), 'time_coverage_end': declared_end.isoformat(),
                  'declared_product_id': declared_id,
                  'identity_basis': 'pinned URL/filename/hash, platform, interval; annual may retain first daily id',
                  'records': len(times), 'valid_minutes': int(good.sum()),
                  'missing_minute_records': int((period_end-day).total_seconds()/60)-len(times),
                  'flag_counts': dict(Counter(map(str, flags.tolist()))),
                  'sample_count_distribution': dict(Counter(map(str, counts[good].tolist()))),
                  'invalid_flux_minutes': int((~good_flux).sum()),
                  'invalid_sample_count_minutes': int((~good_count).sum()),
                  'quality_meanings': meanings, 'intervals': intervals,
                  'basis': 'flag zero + finite in-range XRS-B flux + nonempty average',
                  'resolution': 'valid one-minute average; not continuous high-cadence coverage'}
    verify_source(directory, meta)
    return report


class Coverage:
    def __init__(self, sources):
        self.sources = [{**source, 'intervals': merge(source['intervals'])} for source in sources]
        self.intervals = merge(interval for source in self.sources for interval in source['intervals'])
        self.starts = [a for a, _ in self.intervals]
        self.ends = [b for _, b in self.intervals]
        self.source_indices = [([a for a, _ in s['intervals']], [b for _, b in s['intervals']])
                               for s in self.sources]

    def window(self, start, end):
        start, end = utc(start).timestamp(), utc(end).timestamp()
        if end <= start:
            raise ValueError('目标时间窗必须向未来延伸')
        valid, gaps, cursor = 0., [], start
        selected = self.intervals[bisect_right(self.ends, start):bisect_left(self.starts, end)]
        for left, right in selected:
            left, right = max(left, start), min(right, end)
            if right <= left:
                continue
            if left > cursor:
                gaps.append([iso(cursor), iso(left)])
            valid += right-left
            cursor = right
        if cursor < end:
            gaps.append([iso(cursor), iso(end)])
        source_ids = [s['filename'] for s, (starts, ends) in zip(self.sources, self.source_indices)
                      if bisect_right(ends, start) < bisect_left(starts, end)]
        return {'valid_seconds': valid, 'window_seconds': end-start,
                'fraction': valid/(end-start), 'complete': not gaps,
                'gaps': gaps, 'source_files': source_ids,
                'basis': 'union of valid one-minute averages; no gap filling',
                'operational_availability_verified': False}


def load(directory):
    directory = Path(directory)
    metadata = [json.loads(p.read_text(encoding='utf-8')) for p in sorted(directory.glob('*.source.json'))]
    annual = {}
    daily = set()
    for meta in metadata:
        verify_source(directory, meta)
        if YEAR_NAME.fullmatch(meta['filename']):
            key = (meta['satellite'], meta['year'])
            if key in annual:
                raise ValueError('同一卫星年度存在多个固定版本，不能混用')
            annual[key] = meta
        else:
            key = (meta['satellite'], meta['day'])
            if key in daily:
                raise ValueError('同一卫星日期存在多个固定版本，不能混用')
            daily.add(key)
    sources, ignored = [], []
    for meta in metadata:
        if 'day' in meta and (meta['satellite'], int(meta['day'][:4])) in annual:
            # Do not fill a reprocessed annual product's bad minutes using an
            # older daily snapshot. Keep the raw daily source but supersede it.
            ignored.append({**meta, 'reason': 'pinned annual product used for this satellite/year'})
        else:
            sources.append(read_source(directory, meta))
    result = Coverage(sources)
    result.ignored_sources = ignored
    return result
