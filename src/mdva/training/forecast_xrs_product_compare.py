"""Compare operational and science XRS flux for matching satellite minutes."""

from __future__ import annotations

import argparse
from datetime import timedelta
import hashlib
import json
from pathlib import Path

import numpy as np

from mdva.web.space_weather import download_xrs_coverage

from .durable import atomic_json, commit
from .forecast_disk_xrs import read_minutes
from .forecast_xrs_intake import FEEDS, usable_minutes
from .journal import read
from .runtime import digest, now, output_path
from .xrs_coverage import read_source, utc


def overlap(operational, science):
    stamps, operational_index, science_index = np.intersect1d(operational[0], science[0], return_indices=True)
    if not len(stamps):
        return None
    live, archive = operational[1][operational_index], science[1][science_index]
    relative = np.abs(live-archive) / archive
    return {'matched_minutes': len(stamps), 'operational_minutes': len(operational[0]),
            'science_minutes': len(science[0]), 'relative_abs_difference_p50': float(np.median(relative)),
            'relative_abs_difference_p95': float(np.quantile(relative, .95)),
            'operational_over_science_median_ratio': float(np.median(live / archive)),
            'operational_at_or_above_m_flux': int((live >= 1e-5).sum()),
            'science_at_or_above_m_flux': int((archive >= 1e-5).sum()),
            'm_flux_disagreements': int(np.count_nonzero((live >= 1e-5) != (archive >= 1e-5)))}


def run(dataset, snapshot, output):
    snapshot = Path(snapshot).resolve()
    output = output_path(output, Path(dataset).resolve())
    if output.is_relative_to(snapshot):
        raise ValueError('对照必须保存在独立目录')
    receipt = read(snapshot / 'summary.json')
    output.mkdir(parents=True, exist_ok=False)
    feeds = {}
    for label, source in receipt['feeds'].items():
        if source['state'] != 'received':
            continue
        file = snapshot / f'{label}.json'
        if source['url'] != FEEDS[label] or digest(file) != source['raw_sha256']:
            raise ValueError('实时快照身份不一致')
        received = utc(source['received_utc'])
        live, _, _ = usable_minutes(read(file), received)
        daily = []
        for satellite, operational in live.items():
            for offset in range(1, 6):
                end = (received - timedelta(days=offset-1)).replace(hour=0, minute=0, second=0, microsecond=0)
                day = end - timedelta(days=1)
                inside = (operational[0] >= day.timestamp()) & (operational[0] < end.timestamp())
                results = {'satellite': satellite, 'day_utc': day.date().isoformat()}
                if not inside.any():
                    daily.append({**results, 'state': 'missing_operational_minutes'})
                    continue
                try:
                    raw, meta = download_xrs_coverage(satellite, day.strftime('%Y%m%d'), compare_recent=True)
                    path = output / meta['filename']
                    checksum = hashlib.sha256(raw).hexdigest()
                    if path.exists():
                        if digest(path) != checksum:
                            raise ValueError('同版本科学产品在两路对照期间发生修改')
                    else:
                        commit(path, lambda file: file.write(raw))
                    meta.update(sha256=checksum, bytes=len(raw), fetched_utc=now())
                    atomic_json(path.with_suffix('.source.json'), meta)
                    coverage = read_source(output, meta)
                    times, flux = read_minutes(output, coverage)
                    positive = flux > 0
                    matched = overlap(operational[:, inside], (times[positive], flux[positive]))
                    daily.append({**results, 'state': 'compared' if matched else 'no_overlap',
                                  'comparison': matched, 'science_source': meta})
                except (KeyError, OSError, ValueError) as exc:
                    daily.append({**results, 'state': 'unavailable', 'reason': str(exc)})
        feeds[label] = daily
    report = {'state': 'completed', 'created_utc': now(), 'feeds': feeds,
              'scope': 'Five completed UTC days preceding receipt, exact satellite and minute timestamps. '
                       'Flux threshold crossings are minutes, not distinct flare events.',
              'same_scale_validated_for_deployment': False, 'issued_forecast': False,
              'snapshot_sha256': digest(snapshot / 'summary.json'), 'code_sha256': digest(__file__)}
    atomic_json(output / 'summary.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('dataset', 'snapshot', 'output'):
        parser.add_argument('--' + key, required=True)
    result = run(**vars(parser.parse_args()))
    print(json.dumps({name: [{k: v for k, v in row.items() if k != 'science_source'} for row in values]
                      for name, values in result['feeds'].items()}, ensure_ascii=False))


if __name__ == '__main__':
    main()
