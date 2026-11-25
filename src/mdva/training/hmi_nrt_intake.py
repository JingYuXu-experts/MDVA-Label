"""Pin a JSOC near-real-time LOS snapshot and local receipt time for research."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

import numpy as np

from mdva.web.historical_hmi import HmiRedirect, source_url

from .ar_hmi_registration import fits_field, source_grid
from .durable import atomic_json, commit
from .hmi_source_audit import tai_utc
from .runtime import digest, output_path


BASE = 'https://jsoc1.stanford.edu'
POINTER = BASE + '/data/hmi/fits/latest_fits_time'
MAX_AGE_SECONDS = 2 * 3600


def data_url(raw):
    lines = [line.split() for line in raw.decode('ascii').splitlines() if line.strip()]
    if len(lines) != 1 or len(lines[0]) != 2 or lines[0][0] != 'M_1k_fits':
        raise ValueError('JSOC近实时FITS索引格式改变')
    target = urlsplit(lines[0][1])
    match = re.fullmatch(r'/data/hmi/fits/\d{4}/\d{2}/\d{2}/hmi\.M_720s_nrt\.(\d{8}_\d{6})_TAI\.fits', target.path)
    if (not match or target.scheme not in ('http', 'https') or not target.netloc in ('jsoc.stanford.edu', 'jsoc1.stanford.edu')
            or target.query or target.fragment):
        raise ValueError('JSOC索引只接受登记的近实时HMI太阳磁图')
    nominal = match[1]
    historic, filename = source_url(nominal)
    expected = urlsplit(historic).path.replace(filename, filename.replace('M_720s.', 'M_720s_nrt.'))
    if expected != target.path:
        raise ValueError('文件名时刻与服务器日期目录不一致')
    return BASE + expected, nominal


def fetch(url, maximum):
    request = Request(url, headers={'User-Agent': 'MDVA-local-research/1.0'})
    with build_opener(HmiRedirect()).open(request, timeout=20) as response:
        final = urlsplit(response.geturl())
        if (final.scheme, final.netloc, final.path) != ('https', 'jsoc1.stanford.edu', urlsplit(url).path):
            raise ValueError('请求发生未登记的跳转')
        content = response.read(maximum + 1)
        headers = {name.lower(): value for name, value in response.headers.items()
                   if name.lower() in ('date', 'last-modified', 'age', 'etag', 'content-length')}
    if len(content) > maximum or ('content-length' in headers and len(content) != int(headers['content-length'])):
        raise ValueError('官方响应长度异常')
    return content, headers, datetime.now(timezone.utc)


def run(dataset, output):
    output = output_path(output, Path(dataset).resolve())
    output.mkdir(parents=True, exist_ok=False)
    report = {'state': 'unavailable', 'pointer_url': POINTER, 'issued_forecast': False, 'model_executed': False}
    try:
        raw, headers, received = fetch(POINTER, 4096)
        pointer_path = output / 'latest_fits_time.txt'
        commit(pointer_path, lambda stream: stream.write(raw))
        url, nominal = data_url(raw)
        report.update(pointer_sha256=digest(pointer_path), pointer_received_utc=received.isoformat(),
                      pointer_headers=headers, data_url=url)
        raw, headers, received = fetch(url, 16*1024*1024)
        path = output / urlsplit(url).path.rsplit('/', 1)[-1]
        commit(path, lambda stream: stream.write(raw))
        report.update(fits_sha256=digest(path), fits_received_utc=received.isoformat(), fits_headers=headers)
        field, header = fits_field(path)
        source_grid(header)
        record = tai_utc(header['T_REC'])
        observed = tai_utc(header['T_OBS'])
        nominal_tai = datetime.strptime(nominal, '%Y%m%d_%H%M%S').strftime('%Y.%m.%d_%H:%M:%S_TAI')
        if record != tai_utc(nominal_tai) or abs((record-observed).total_seconds()) > 60:
            raise ValueError('文件索引、FITS记录与观测时间不一致')
        age = (received - observed).total_seconds()
        if age < 0:
            raise ValueError('观测时刻晚于本机收到文件的时间')
        if header['QUALITY'] != 0:
            state = 'quality_review_required'
        elif age > MAX_AGE_SECONDS:
            state = 'stale_at_receipt'
        else:
            state = 'usable_snapshot_for_research'
        report.update(state=state, record_utc=record.isoformat(), observation_utc=observed.isoformat(),
                      oldest_decision_utc=received.isoformat(), age_seconds=age, maximum_age_seconds=MAX_AGE_SECONDS,
                      quality=header['QUALITY'], units='Gauss', field='B_LOS',
                      archive_creation=header.get('DATE'), finite_fraction=float(np.isfinite(field).mean()),
                      shape=list(field.shape))
    except (OSError, ValueError, KeyError) as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    report['scope'] = ('Official processed near-real-time LOS snapshot. Local receipt establishes this retrieval only; '
                       'no inference on scientific archive parity, publication time or continuity.')
    atomic_json(output / 'summary.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('dataset', 'output'):
        parser.add_argument('--' + key, required=True)
    result = run(**vars(parser.parse_args()))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
