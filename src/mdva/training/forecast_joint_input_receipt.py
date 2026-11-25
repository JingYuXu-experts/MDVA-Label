"""Record when the HMI and GOES inputs for a flare forecast were actually available."""

from __future__ import annotations

import argparse
from datetime import timedelta
import json
from pathlib import Path
from urllib.parse import urlsplit

from .durable import atomic_json
from .forecast_disk_xrs import history
from .forecast_xrs_intake import FEEDS, usable_minutes
from .hmi_nrt_intake import POINTER, data_url
from .journal import read
from .runtime import digest, now, output_path
from .xrs_coverage import utc


def join(hmi, hmi_receipt, feeds, xrs_receipt):
    observed = utc(hmi['observation_utc'])
    received = utc(hmi['fits_received_utc'])
    if received < observed:
        raise ValueError('HMI 观测晚于收到时间')
    station_histories = {}
    newest_receipt = received
    for feed, item in xrs_receipt['feeds'].items():
        if item['state'] != 'received':
            continue
        finish = utc(item['received_utc'])
        newest_receipt = max(newest_receipt, finish)
        # Preserve the time origin used by the historical comparison.
        series, _, newest_minute = usable_minutes(feeds[feed], finish)
        features = history(observed.isoformat(), series)
        station_histories[feed] = {
            'received_utc': finish.isoformat(), 'latest_observation_utc': newest_minute.isoformat() if newest_minute else None,
            'hmi_observation_anchored_features': features, 'raw_sha256': item['raw_sha256'],
        }
    blockers = ['operational_vs_science_flux_not_validated', 'historical_model_target_starts_at_observation']
    if hmi['state'] != 'usable_snapshot_for_research' or hmi['quality'] != 0:
        blockers.append('HMI_quality_review_required')
    if not any(row['hmi_observation_anchored_features'] is not None for row in station_histories.values()):
        blockers.append('no_usable_GOES_history_at_HMI_time')
    return {
        'state': 'recorded_for_research', 'created_utc': now(),
        'hmi_observed_utc': observed.isoformat(), 'hmi_received_utc': received.isoformat(),
        'hmi_quality': hmi['quality'], 'hmi_sha256': hmi['fits_sha256'],
        'hmi_receipt_sha256': hmi_receipt,
        'paired_available_no_earlier_than_utc': newest_receipt.isoformat(),
        'delay_from_hmi_observation_seconds': (newest_receipt - observed).total_seconds(),
        'prospective_24h_outcome_can_be_checked_after_utc': (newest_receipt + timedelta(hours=24)).isoformat(),
        'observed_based_forecast_target_end_utc': (observed + timedelta(hours=24)).isoformat(),
        'feeds': station_histories, 'remaining_gates': blockers,
        'issued_forecast': False, 'model_executed': False, 'test_accessed': False,
    }


def run(dataset, hmi, xrs, output):
    output = output_path(output, Path(dataset))
    if output.exists() or any(output.is_relative_to(p.resolve()) for p in (hmi, xrs)):
        raise ValueError('联合输入留证必须使用独立目录')
    hmi_summary, xrs_summary = read(hmi / 'summary.json'), read(xrs / 'summary.json')
    pointer = hmi / 'latest_fits_time.txt'
    url, _ = data_url(pointer.read_bytes())
    fits = hmi / urlsplit(url).path.rsplit('/', 1)[-1]
    if (url != hmi_summary['data_url'] or hmi_summary['pointer_url'] != POINTER
            or digest(pointer) != hmi_summary['pointer_sha256'] or digest(fits) != hmi_summary['fits_sha256']
            or xrs_summary['issued_forecast']):
        raise ValueError('HMI 或 GOES 实时输入身份不符')
    feeds = {}
    for feed, entry in xrs_summary['feeds'].items():
        if entry['url'] != FEEDS.get(feed):
            raise ValueError('GOES 地址未登记')
        if entry['state'] == 'received':
            file = xrs / f'{feed}.json'
            if digest(file) != entry['raw_sha256']:
                raise ValueError('GOES 观测快照内容改变')
            feeds[feed] = read(file)
    result = join(hmi_summary, digest(hmi / 'summary.json'), feeds, xrs_summary)
    result['xrs_receipt_sha256'] = digest(xrs / 'summary.json')
    result['code_sha256'] = digest(__file__)
    atomic_json(output / 'summary.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dataset', 'hmi', 'xrs', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    print(json.dumps(run(**vars(parser.parse_args())), ensure_ascii=False))


if __name__ == '__main__':
    main()
