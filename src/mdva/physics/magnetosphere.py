"""T96 + IGRF-14 field-line tracing in GSM. No network, no invented storm state.

Tsyganenko (1995/1996), DOI 10.1029/95JA03590; upstream Geopack 1.0.13.
Shue et al. (1998), DOI 10.1029/98JA01103, is an independent boundary estimate,
not the boundary used internally by T96. Neither model is an MHD time evolution.
"""
from __future__ import annotations

import math
import threading
from datetime import datetime, timezone

import numpy as np

_lock = threading.Lock()  # Geopack and T96 contain Fortran-style shared globals.
REVISION = 't96-igrf14-rk2-v1'


def parameters(values):
    limits = {'pressure': (.1, 50), 'dst': (-300, 50), 'by': (-50, 50), 'bz': (-50, 50)}
    result = {}
    for key, (low, high) in limits.items():
        try: value = float(values[key])
        except (KeyError, ValueError, TypeError): raise ValueError(f'缺少有效参数：{key}') from None
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f'{key} 超出允许的实验范围 [{low}, {high}]')
        result[key] = value
    t = float(values['time'])
    if not math.isfinite(t) or not datetime(1900, 1, 1, tzinfo=timezone.utc).timestamp() <= t <= datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp():
        raise ValueError('时间超出已打包 IGRF-14 系数的1900—2030年范围')
    result['time'] = t
    return result


def shue98(pressure, bz):
    """Subsolar distance (Re) and tail flaring. Proton-only pressure noted by caller."""
    return ((10.22 + 1.29 * math.tanh(.184 * (bz + 8.14))) * pressure ** (-1 / 6.6),
            (.58 - .007 * bz) * (1 + .024 * math.log(pressure)))


def trace(values, *, latitudes=(50, 60, 68, 74, 80), azimuths=12, step_scale=1):
    p = parameters(values)
    with _lock:
        from .geopack import geopack as gp, t96
        tilt = gp.recalc(p['time'])  # Default radial upstream velocity: GSM, not aberrated GSW.
        par = [p['pressure'], p['dst'], p['by'], p['bz'], 0, 0, 0, 0, 0, 0]
        def field(pos):
            return np.array(gp.igrf_gsm(*pos)) + np.array(t96.t96(par, tilt, *pos))
        lines = []
        # Northern geographic/magnetic seeds in SM, converted with the actual tilt.
        # Integration follows B/|B| with midpoint RK2, adaptive spatial step, both signs.
        for latitude in latitudes:
            lat = math.radians(latitude)
            for i in range(azimuths):
                phi = 2 * math.pi * i / azimuths
                start = np.array(gp.smgsm(1.03 * math.cos(lat) * math.cos(phi),
                                         1.03 * math.cos(lat) * math.sin(phi), 1.03 * math.sin(lat), 1))
                paths = []
                for sign in (-1, 1):
                    pos = start.copy(); path = []; end = 'step_limit'
                    for _ in range(650):
                        radius = float(np.linalg.norm(pos))
                        if radius < 1: end = 'earth'; break
                        if pos[0] < -50 or pos[0] > 20 or abs(pos[1]) > 30 or abs(pos[2]) > 30:
                            end = 'domain'; break
                        b = field(pos); strength = float(np.linalg.norm(b))
                        if not np.all(np.isfinite(b)) or strength < 1e-8: end = 'invalid_field'; break
                        path.append([*np.round(pos, 5).tolist(), round(strength, 4)])
                        ds = min(.45, .035 * radius) * step_scale
                        mid = pos + b / strength * sign * ds / 2
                        bm = field(mid); norm = float(np.linalg.norm(bm))
                        if not np.all(np.isfinite(bm)) or norm < 1e-8: end = 'invalid_field'; break
                        pos = pos + bm / norm * sign * ds
                    paths.append((path, end))
                points = paths[0][0][::-1] + paths[1][0][1:]
                if len(points) > 2:
                    lines.append({'points': points, 'ends': [paths[0][1], paths[1][1]]})
        # Right-handed GSM basis expressed in geographic Cartesian coordinates.
        basis = [list(gp.geogsm(*axis, -1)) for axis in ((1, 0, 0), (0, 1, 0), (0, 0, 1))]
    r0, alpha = shue98(p['pressure'], p['bz'])
    warnings = []
    if not (.5 <= p['pressure'] <= 10 and -100 <= p['dst'] <= 20 and abs(p['by']) <= 10 and abs(p['bz']) <= 10):
        warnings.append('驱动参数超出常见中等扰动条件；经验模型可能外推，不能据此作业务风险结论。')
    return {'model': 'T96 + IGRF-14', 'revision':REVISION, 'kind': 'empirical_model', 'coordinate': 'GSM',
            'unit': {'position': 'Rₑ (6371.2 km)', 'field': 'nT'}, 'parameters': p,
            'tilt_radians': float(tilt), 'basis_geo': basis, 'lines': lines,
            'boundary': {'model': 'Shue 1998', 'r0': r0, 'alpha': alpha, 'independent_of_field_model': True},
            'domain': {'x': [-50, 20], 'y': [-30, 30], 'z': [-30, 30]},
            'warnings': warnings,
            'limits': '瞬时经验磁场；绘图范围外磁场并未终止。边界为独立经验估计，不代表T96磁场截面或MHD解；未计算重联、电流片动力学或CME冲击波。',
            'references': ['https://ccmc.gsfc.nasa.gov/models/Tsyganenko%20Magnetic%20Field~T96/',
                           'https://doi.org/10.1029/98JA01103', 'https://github.com/tsssss/geopack']}


if __name__ == '__main__':
    import json
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    print(json.dumps(trace(json.load(sys.stdin)),ensure_ascii=False))
