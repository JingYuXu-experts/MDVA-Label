"""Collection is resumable source preparation, not hidden training or relabeling."""
from copy import deepcopy
import hashlib
import json

import pytest

from mdva.training.forecast_coverage_plan import window_days, plan_rows, collect, code_identity
from mdva.training.xrs_coverage import read_source, snapshot_year, product_time
from mdva.web.space_weather import download_xrs_coverage_year


def row(time='2020-11-29T23:59:23+00:00', end='2020-11-30T23:59:23+00:00'):
    return {'eligible': True, 'time_utc': time, 'target_start': time, 'target_end': end,
            'noaa': 12790, 'split': 'train'}


def test_days_respect_utc_midnight_and_partial_minutes():
    assert window_days('2020-11-29T00:00Z', '2020-11-30T00:00Z') == ['2020-11-29']
    assert window_days('2020-11-29T23:59:23Z', '2020-11-30T23:59:23Z') == ['2020-11-29', '2020-11-30']
    assert window_days('2020-11-29T08:00+08:00', '2020-11-30T08:00+08:00') == ['2020-11-29']
    with pytest.raises(ValueError):window_days('2020-11-29T00:00Z', '2020-11-29T01:00Z')
    assert product_time(b'2010-1-1 0:0:0 UTC').isoformat() == '2010-01-01T00:00:00+00:00'
    with pytest.raises(ValueError):product_time('2010-1-1 0:0:0')


def test_plan_deduplicates_windows_and_does_not_claim_old_stars_after_retirement():
    rows = [row(), row()]
    original = deepcopy(rows)
    days, jobs = plan_rows(rows)
    assert [d['unique_AR_windows'] for d in days] == [1, 1]
    assert {j['satellite'] for j in jobs} == {16, 17}
    assert rows == original
    _, older = plan_rows([row('2010-05-01T00:00Z', '2010-05-02T00:00Z')])
    assert {j['satellite'] for j in older} == {14, 15}
    _, recent = plan_rows([row('2024-05-01T00:00Z', '2024-05-02T00:00Z')])
    assert {j['satellite'] for j in recent} == {16, 18}


def setup_collection(tmp_path):
    data = tmp_path/'data';data.mkdir()
    (data/'sources.json').write_text('{}')
    (data/'manifest-hashes.json').write_text('{}')
    sources = tmp_path/'sources';sources.mkdir()
    plan = {'dataset': str(data), 'dataset_manifest_sha256': hashlib.sha256(b'{}').hexdigest(),
            'code': code_identity(), 'jobs': [
                {'id': f'g{sat}-y2020', 'satellite': sat, 'year': 2020} for sat in (16, 17)]}
    path = tmp_path/'plan.json';path.write_text(json.dumps(plan))
    return path, sources, tmp_path/'collection'


def test_resumes_only_unfinished_verifies_cache_and_rejects_plan_change(tmp_path, monkeypatch):
    plan, sources, output = setup_collection(tmp_path)
    called = []
    def acquire(job, cache):
        called.append(job['id'])
        name = job['id']+'.nc';(cache/name).write_bytes(b'pinned')
        (cache/name).with_suffix('.source.json').write_bytes(b'pinned-meta')
        return {'state': 'verified', 'filename': name, 'sha256': hashlib.sha256(b'pinned').hexdigest(),
                'source_metadata_sha256': hashlib.sha256(b'pinned-meta').hexdigest()}
    monkeypatch.setattr('mdva.training.forecast_coverage_plan.acquire_one', acquire)
    first = collect(plan, sources, output, workers=1, limit=1)
    assert first['state'] == 'partial' and len(called) == 1
    second = collect(plan, sources, output, workers=1)
    assert second['state'] == 'completed' and len(called) == 2
    collect(plan, sources, output)
    assert len(called) == 2
    (sources/'g16-y2020.nc').write_bytes(b'changed')
    with pytest.raises(ValueError, match='缓存'):collect(plan, sources, output)


def test_pause_keeps_pending_work_and_code_changes_cannot_silently_resume(tmp_path, monkeypatch):
    plan, sources, output = setup_collection(tmp_path)
    output.mkdir();(output/'PAUSE').write_text('pause')
    monkeypatch.setattr('mdva.training.forecast_coverage_plan.acquire_one', lambda *a: pytest.fail('unexpected download'))
    result = collect(plan, sources, output)
    assert result['state'] == 'paused' and not result['jobs']
    changed = json.loads(plan.read_text());changed['code']['xrs_coverage.py'] = 'different'
    plan.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match='身份'):collect(plan, sources, output)


@pytest.mark.parametrize('sat,year,name', [(True, 2020, ''), (16, True, ''), (16, 2025, ''),
    (16, 2020, '../data.nc'), (16, 2020, 'https://evil.org/file.nc')])
def test_yearly_download_identity_invalid_before_network(monkeypatch, sat, year, name):
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: pytest.fail('unexpected download'))
    with pytest.raises(ValueError):download_xrs_coverage_year(sat, year, name)


@pytest.mark.parametrize('first_daily_id', [False, True])
def test_annual_partial_header_does_not_imply_full_year_coverage(tmp_path, first_daily_id):
    h5py = pytest.importorskip('h5py')
    import numpy as np
    from datetime import datetime, timezone
    name = 'sci_xrsf-l2-avg1m_g16_y2020_v2-2-2.nc'
    path = tmp_path/name
    base = datetime(2000, 1, 1, 12, tzinfo=timezone.utc).timestamp()
    at = datetime(2020, 11, 29, tzinfo=timezone.utc).timestamp()-base
    with h5py.File(path, 'w') as f:
        f.attrs.update(platform='g16', id=name.replace('_y2020_', '_d20201129_') if first_daily_id else name, time_coverage_resolution='PT1M',
                       time_coverage_start='2020-11-29 0:0:0 UTC', time_coverage_end='2020-11-29 0:2:0 UTC')
        t = f.create_dataset('time', data=[at, at+60, at+120])
        t.attrs.update(units='seconds since 2000-01-01 12:00:00 UTC', long_name='Record start time.')
        flux = f.create_dataset('xrsb_flux', data=[1e-7]*3)
        flux.attrs.update(units='W/m2', _FillValue=-9999., valid_min=1e-9, valid_max=.2)
        flag = f.create_dataset('xrsb_flag', data=[0, 4, 0])
        flag.attrs.update(flag_values=np.array([0,1,2,4]), flag_meanings='good_data eclipse bad_data interpolated')
        counts = f.create_dataset('xrsb_num', data=[60, 60, 60]);counts.attrs['_FillValue'] = 255
    meta = {'filename': name, 'satellite': 16, 'year': 2020, 'url': 'https://data.ngdc.noaa.gov/year.nc',
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'bytes': path.stat().st_size}
    report = read_source(tmp_path, meta)
    assert report['valid_minutes'] == 2
    assert len(report['intervals']) == 2
    assert sum(b-a for a,b in report['intervals']) == 120
    assert report['missing_minute_records'] > 500000
    path.with_suffix('.source.json').write_text(json.dumps(meta))
    assert snapshot_year(tmp_path, 16, 2020) == meta


def test_annual_bad_minutes_cannot_be_filled_with_older_daily_snapshot(tmp_path, monkeypatch):
    from mdva.training.xrs_coverage import load
    annual = {'filename': 'sci_xrsf-l2-avg1m_g16_y2020_v2-2-2.nc', 'satellite': 16, 'year': 2020}
    daily = {'filename': 'sci_xrsf-l2-avg1m_g16_d20201129_v2-1-0.nc', 'satellite': 16, 'day': '20201129'}
    for i, meta in enumerate((annual, daily)):
        (tmp_path/f'{i}.source.json').write_text(json.dumps(meta))
    monkeypatch.setattr('mdva.training.xrs_coverage.verify_source', lambda *a: None)
    def reader(directory, meta):
        assert meta == annual
        return {'filename': meta['filename'], 'intervals': []}
    monkeypatch.setattr('mdva.training.xrs_coverage.read_source', reader)
    result = load(tmp_path)
    assert not result.window('2020-11-29T00:00Z', '2020-11-30T00:00Z')['complete']
    assert len(result.ignored_sources) == 1


def test_truncated_annual_container_never_becomes_pinned_cache(tmp_path, monkeypatch):
    pytest.importorskip('h5py')
    monkeypatch.setattr('mdva.web.space_weather.download_xrs_coverage_year', lambda *a: (
        b'truncated', {'filename': 'sci_xrsf-l2-avg1m_g16_y2020_v2-2-2.nc', 'satellite': 16, 'year': 2020}))
    with pytest.raises(OSError):snapshot_year(tmp_path, 16, 2020)
    assert not list(tmp_path.glob('*.nc')) and not list(tmp_path.glob('*.source.json'))


def test_short_HTTP_response_is_rejected_before_snapshot(monkeypatch):
    class Reply:
        headers = {'Content-Length': '100'}
        def __enter__(self):return self
        def __exit__(self, *args):pass
        def geturl(self):return 'https://data.ngdc.noaa.gov/registered.nc'
        def read(self, limit):return b'short'
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **kw: Reply())
    with pytest.raises(OSError, match='截断'):
        download_xrs_coverage_year(16, 2020, 'sci_xrsf-l2-avg1m_g16_y2020_v2-2-2.nc')
