"""Explicit time/identity rules and leakage-resistant research splits."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
import re

UTC = timezone.utc
BOUNDARIES = tuple(datetime(y, 1, 1, tzinfo=UTC) for y in (2021, 2022, 2023))


def observation(name: str) -> dict:
    """Parse complete instrument patterns, never an arbitrary eight-digit substring."""
    stem = Path(name).stem
    match = re.fullmatch(r'(hmi\.[^.]+|mdi\.[^.]+(?:\.[^.]+)?)\.(\d{8})_(\d{6})_TAI\..+', stem, re.I)
    scale = 'utc_source_convention'
    instrument = ''
    if match:
        instrument = 'HMI' if stem.lower().startswith('hmi.') else 'MDI'
        day, clock = match[2], match[3]
        scale = 'tai_explicit'
    else:
        match = re.fullmatch(r'AIA(\d{8})_(\d{4})_(\d{4})', stem, re.I)
        if match:
            day, clock, wavelength = match.groups()
            instrument = 'AIA' + str(int(wavelength))
        else:
            match = re.fullmatch(r'(bbso_halph_f[rl]|kanz_halph_f[id])_(\d{8})_(\d{6})', stem, re.I)
            if match:
                day, clock = match[2], match[3]
                instrument = 'BBSO_Halpha' if match[1].lower().startswith('bbso') else 'KSO_Halpha'
            else:
                match = re.fullmatch(r's?HrHa(\d{6})(\d{4}|\d{6})full[co]', stem, re.I)
                if match:
                    day = ('19' if int(match[1][:2]) >= 70 else '20') + match[1]
                    clock = match[2]
                    instrument = 'HSOS_Halpha'
                else:
                    match = re.fullmatch(r'(\d{8})_(\d{6})_(0195|0304)', stem)
                    if match:
                        day, clock = match[1], match[2]
                        instrument = 'EIT' + str(int(match[3]))
                    else:
                        raise ValueError('无法按仪器文件名规则核验时间')
    nominal = datetime.strptime(day + clock.ljust(6, '0'), '%Y%m%d%H%M%S').replace(tzinfo=UTC)
    if not 1990 <= nominal.year <= 2030:
        raise ValueError('观测年份超出本数据产品范围')
    if scale == 'tai_explicit':
        from astropy.time import Time
        actual = Time(nominal.replace(tzinfo=None), scale='tai').utc.to_datetime(timezone=UTC)
    else:
        actual = nominal
    return {'time_utc': actual.isoformat(), 'nominal_utc': nominal.isoformat(),
            'date': actual.date().isoformat(), 'instrument': instrument, 'time_basis': scale,
            'observation_group': f'{instrument}:{actual.isoformat()}'}


def integer(value):
    if value is None or str(value).strip() == '':
        return None
    try:
        number = float(value)
        return int(number) if math.isfinite(number) and number.is_integer() else None
    except (ValueError, TypeError):
        return None


def resolve_noaa(raw, candidates) -> tuple[int | None, str]:
    value = integer(raw)
    if value is None or value <= 0:
        return None, 'missing_or_invalid'
    if value >= 10000:
        return value, 'reported_full_id'
    matches = {n for n in candidates if n >= 10000 and n % 10000 == value}
    if len(matches) == 1:
        return matches.pop(), 'short_id_matched_nearby_reference'
    return None, 'ambiguous_short_id' if matches else 'short_id_without_reference'


def flare_level(value) -> tuple[str, float]:
    match = re.fullmatch(r'([ABCMX])\s*(\d+(?:\.\d+)?)', str(value).strip(), re.I)
    if not match:
        raise ValueError('耀斑等级无法解析')
    cls, magnitude = match[1].upper(), float(match[2])
    if not magnitude > 0:
        raise ValueError('耀斑幅度必须为正')
    return cls, magnitude


def event_time(day, hour, minute):
    h, m = integer(hour), integer(minute)
    if h is None or m is None or not 0 <= h <= 24 or not 0 <= m <= 59 or (h == 24 and m):
        raise ValueError('事件时刻非法或缺失')
    return day + timedelta(hours=h, minutes=m)


def split_for(time_utc: str, guard_days=4, boundaries=BOUNDARIES):
    time = datetime.fromisoformat(time_utc)
    if any(abs((time - edge).total_seconds()) <= guard_days * 86400 for edge in boundaries):
        return 'excluded_boundary'
    if time < boundaries[0]:
        return 'train'
    if time < boundaries[1]:
        return 'validation'
    if time < boundaries[2]:
        return 'calibration'
    return 'test'


def segmentation_split(time_utc, category):
    # CH/FL have sparse or absent 2021 labels; choose periods before any model fit.
    edges = BOUNDARIES if category == 'AR' else tuple(datetime(y,1,1,tzinfo=UTC) for y in (2019,2021,2023))
    return split_for(time_utc, guard_days=27, boundaries=edges)


def isolate_groups(rows, group_keys):
    """Quarantine cross-split groups; never move later observations into training."""
    ownership = {}
    for row in rows:
        if row['split'].startswith('excluded'):
            continue
        for key in group_keys(row):
            ownership.setdefault(key, set()).add(row['split'])
    conflicts = {key for key, splits in ownership.items() if len(splits) > 1}
    for row in rows:
        if any(key in conflicts for key in group_keys(row)):
            row.setdefault('exclusions', []).append('cross_split_source_or_region')
    return len(conflicts)
