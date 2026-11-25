"""Scientific supervision gates: missing labels/coverage never become quiet Sun."""
from datetime import datetime, timezone
import hashlib
import json

import numpy as np
import pytest

from mdva.training.forecast_supervision_audit import Supervisor, resolve_regions, audit
from mdva.training.xrs_coverage import Coverage, read_source, snapshot
from mdva.web.space_weather import download_xrs_coverage


START = '2020-11-29T00:00:00+00:00'
END = '2020-11-30T00:00:00+00:00'
A = datetime.fromisoformat(START).timestamp()
B = datetime.fromisoformat(END).timestamp()


def row(noaa=12790):
    return {'time_utc': START, 'target_start': START, 'target_end': END, 'noaa': noaa,
            'noaa_basis': 'reported_full_id', 'split': 'train', 'target_m_plus': 0,
            'eligible': True, 'parameters_uri': '@source/one.txt', 'parameter_line': 2}


def event(noaa=None, start='2020-11-29T12:34:00+00:00'):
    return {'id': 'M4.4', 'start': start, 'resolved_noaa': noaa, 'active_region_raw': '2790'}


def coverage(intervals=None):
    return Coverage([{'filename': 'source', 'intervals': intervals if intervals is not None else [[A, B]]}])


def test_complete_quiet_catalogue_window_is_negative_but_not_operational():
    answer = Supervisor([], coverage()).sample(row())
    assert answer['official_target'] == 0
    assert answer['coverage']['complete']
    assert not answer['input_availability_verified']
    assert not answer['usable_for_operational_training']


def test_no_observations_or_even_one_second_gap_cannot_prove_negative():
    for intervals in ([], [[A, B-1]]):
        answer = Supervisor([], coverage(intervals)).sample(row())
        assert answer['official_target'] is None
        assert answer['state'] == 'incomplete_quality_minute_coverage'


def test_unassigned_M_event_not_silently_negative_or_assigned_to_every_AR():
    answer = Supervisor([event()], coverage()).sample(row())
    assert answer['official_target'] is None
    assert answer['uncertain_events'][0]['reason'] == 'AR归属未确认'


def test_reported_AR_positive_needs_no_complete_coverage_and_other_AR_stays_negative():
    positive = Supervisor([event(12790)], coverage([])).sample(row())
    assert positive['official_target'] == 1 and positive['target_changed']
    other = Supervisor([event(12791)], coverage()).sample(row())
    assert other['official_target'] == 0


@pytest.mark.parametrize('time,target', [(START, None), (END, None),
    ('2020-11-29T23:59:00+00:00', 1), ('2020-11-28T23:58:00+00:00', 0)])
def test_minute_boundary_uncertainty(time, target):
    assert Supervisor([event(12790, time)], coverage()).sample(row())['official_target'] == target


def test_utc_seconds_partial_minute_and_satellite_union_not_double_counted():
    sources = [{'filename': 'one', 'intervals': [[A, A+100], [A+101, B]]},
               {'filename': 'two', 'intervals': [[A+50, A+150]]}]
    answer = Coverage(sources).window('2020-11-29T08:00:23+08:00', END)
    assert answer['complete'] and answer['valid_seconds'] == 86400-23
    assert answer['source_files'] == ['one', 'two']


def test_region_modulo_requires_unique_nearby_full_ID_and_ignores_inferred_ID():
    original = [row()]
    result = resolve_regions([event()], original)
    assert result[0]['resolved_noaa'] == 12790
    far = row(22790);far['time_utc'] = '2010-01-01T00:00:00+00:00'
    assert resolve_regions([event()], original+[far])[0]['resolved_noaa'] == 12790
    assert resolve_regions([event()], original+[row(22790)])[0]['resolved_noaa'] is None
    inferred = row();inferred['noaa_basis'] = 'guessed'
    assert resolve_regions([event()], [inferred])[0]['resolved_noaa'] is None
    assert original == [row()]


def test_duplicate_event_records_stay_unresolved_not_two_confirmed_positives():
    first, second = event(), event()
    second['id'] = 'different-record'
    resolved = resolve_regions([first, second], [row()])
    answer = Supervisor(resolved, coverage()).sample(row())
    assert answer['official_target'] is None
    assert answer['uncertain_events'][0]['reason'] == '目录疑似重复事件待核验'


def nc_source(tmp_path, satellite=16, flags=None, counts=None, flux=None, times=None, units=None):
    h5py = pytest.importorskip('h5py', reason='NetCDF科学文件解析需mdva311中的h5py')
    name = f'sci_xrsf-l2-avg1m_g{satellite}_d20201129_v2-2-2.nc'
    path = tmp_path/name
    relative = A-datetime(2000, 1, 1, 12, tzinfo=timezone.utc).timestamp()
    with h5py.File(path, 'w') as f:
        f.attrs.update(platform=f'GOES-{satellite}' if satellite < 16 else f'g{satellite}', id=name, time_coverage_resolution='PT1M',
                       time_coverage_start=START, time_coverage_end=END)
        t = f.create_dataset('time', data=np.array([relative, relative+60] if times is None else times))
        t.attrs.update(units=units or 'seconds since 2000-01-01 12:00:00 UTC', long_name='Record start time.')
        x = f.create_dataset('xrsb_flux', data=flux or [1e-7, 1e-6])
        x.attrs.update(units='W/m2', _FillValue=-9999., valid_min=1e-9, valid_max=.2)
        flag = f.create_dataset('xrsb_flag', data=flags or [0, 0])
        flag.attrs.update(flag_values=[0, 1, 2, 4], flag_meanings=(
            'good_data eclipse bad_data interpolated' if satellite >= 16 else
            'good_data bad_data eclipsed_by_earth temperature_recovery'))
        num = f.create_dataset('xrsb_num', data=counts or [60, 60])
        num.attrs['_FillValue'] = 255
    meta = {'filename': name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'bytes': path.stat().st_size, 'satellite': satellite, 'day': '20201129',
            'url': 'https://data.ngdc.noaa.gov/pinned.nc'}
    return path, meta


@pytest.mark.parametrize('flags,counts,flux,expected', [([0, 4], [60, 60], [1e-7, 1e-6], 1),
    ([0, 0], [0, 255], [1e-7, 1e-6], 0), ([0, 0], [60, 60], [-9999., float('nan')], 0)])
def test_quality_parser_rejects_interpolation_bad_counts_and_bad_flux(tmp_path, flags, counts, flux, expected):
    _, meta = nc_source(tmp_path, flags=flags, counts=counts, flux=flux)
    assert read_source(tmp_path, meta)['valid_minutes'] == expected


def test_older_sensor_counts_are_averages_not_seconds(tmp_path):
    _, meta = nc_source(tmp_path, satellite=15, counts=[29, 30])
    report = read_source(tmp_path, meta)
    assert report['valid_minutes'] == 2
    assert report['sample_count_distribution'] == {'29': 1, '30': 1}


def test_tampered_file_unknown_time_and_duplicate_records_refused(tmp_path):
    path, meta = nc_source(tmp_path)
    path.write_bytes(path.read_bytes()+b'changed')
    with pytest.raises(ValueError, match='指纹'):read_source(tmp_path, meta)
    _, meta = nc_source(tmp_path, units='milliseconds since midnight')
    with pytest.raises(ValueError, match='时间编码'):read_source(tmp_path, meta)
    _, meta = nc_source(tmp_path, times=[0, 0])
    with pytest.raises(ValueError, match='乱序'):read_source(tmp_path, meta)


@pytest.mark.parametrize('sat,day,name', [(True, '20201129', ''), (16., '20201129', ''),
    (16, '20251301', ''), (16, '20200231', ''), (16, '20201129', '../file.nc'),
    (16, '20201129', 'https://evil.org/a.nc'), (16, 20201129, '')])
def test_collector_invalid_identity_never_uses_network(monkeypatch, sat, day, name):
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **k: pytest.fail('unexpected network'))
    with pytest.raises(ValueError):download_xrs_coverage(sat, day, name)


def test_registered_legacy_path_latest_version_and_redirect_guard(monkeypatch):
    urls = []
    class Reply:
        def __init__(self, url):self.url = url
        def __enter__(self):return self
        def __exit__(self, *args):pass
        def geturl(self):return self.url
        def read(self, limit):
            if self.url.endswith('.nc'):return b'netcdf'
            return b'<a href="sci_xrsf-l2-avg1m_g15_d20160101_v2-2-1.nc">a</a><a href="sci_xrsf-l2-avg1m_g15_d20160101_v2-10-0.nc">b</a>'
    def opener(request, **kw):
        urls.append(request.full_url)
        return Reply(request.full_url)
    monkeypatch.setattr('urllib.request.urlopen', opener)
    raw, meta = download_xrs_coverage(15, '20160101')
    assert raw == b'netcdf' and meta['filename'].endswith('v2-10-0.nc')
    assert urls[0].startswith('https://www.ncei.noaa.gov/data/goes-space-environment-monitor/')
    monkeypatch.setattr('urllib.request.urlopen', lambda *a, **kw: Reply('https://evil.org/'))
    with pytest.raises(ValueError, match='跳转'):download_xrs_coverage(16, '20201129')


def test_snapshot_reuses_pinned_file_and_rejects_changed_cache(tmp_path, monkeypatch):
    path, meta = nc_source(tmp_path)
    path.with_suffix('.source.json').write_text(json.dumps(meta), encoding='utf-8')
    monkeypatch.setattr('mdva.web.space_weather.download_xrs_coverage', lambda *a: pytest.fail('download'))
    assert snapshot(tmp_path, 16, '20201129') == meta
    path.write_bytes(b'changed')
    with pytest.raises(ValueError, match='指纹'):snapshot(tmp_path, 16, '20201129')


def test_full_audit_preserves_labels_hashes_and_unknown_negatives(tmp_path):
    dataset = tmp_path/'data';dataset.mkdir()
    catalogue = tmp_path/'catalogue';catalogue.mkdir()
    sources = tmp_path/'sources';sources.mkdir()
    hashes = {}
    for name in ('forecast-ready.jsonl', 'forecast-all.jsonl'):
        path = dataset/name
        path.write_text(json.dumps(row())+'\n', encoding='utf-8')
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (dataset/'manifest-hashes.json').write_text(json.dumps(hashes))
    (dataset/'sources.json').write_text('{}')
    name = 'sci_xrsf-l2-flrpt_geo_y2020_v1-0-1.csv'
    path = catalogue/name
    path.write_text('flare_class,start_time,time,end_time,flare_id,active_region,event_id_swpc,xrsb_irrad_source\n'
                    'M4.4,2020-11-29 12:34:00,2020-11-29 13:11:00,,one,,2050,GOES-16\n')
    meta = catalogue/'sci_xrsf-l2-flrpt_geo_metadata.json'
    meta.write_text(json.dumps({'variable_attributes': {'time': {'comments': 'Time [UTC]'}}}))
    (catalogue/'audit.json').write_text(json.dumps({'snapshots': [
        {'name': path.name, 'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()},
        {'name': meta.name, 'bytes': meta.stat().st_size, 'sha256': hashlib.sha256(meta.read_bytes()).hexdigest()}]}))
    report = audit(dataset, catalogue, sources, tmp_path/'report')
    assert report['counts'] == {'unresolved_event_or_boundary': 1}
    assert report['canonical_AR_days'] == 1
    assert report['M_plus_end_not_reported'] == 1
    assert not report['training_started'] and not report['test_scores_computed']
    for name, expected in hashes.items():
        assert hashlib.sha256((dataset/name).read_bytes()).hexdigest() == expected
    with pytest.raises(ValueError, match='已经存在'):audit(dataset, catalogue, sources, tmp_path/'report')
    with pytest.raises(ValueError, match='分开'):audit(dataset, catalogue, sources, sources/'bad')
