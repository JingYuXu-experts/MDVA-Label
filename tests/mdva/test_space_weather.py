"""Monitoring invariants independent of live upstream availability."""
import json
import time

import pytest

from mdva.web.space_weather import Monitor, check_url, normalize, stamp, parse_forecast, parse_alerts


@pytest.fixture
def monitor(tmp_path):
    service=Monitor(tmp_path/'state',tmp_path/'cache')
    yield service
    service.close()
    service.db.close()


def test_source_boundary_and_offset():
    for address in ['http://services.swpc.noaa.gov/test','https://127.0.0.1/a','https://services.swpc.noaa.gov.evil/a','https://user@services.swpc.noaa.gov/a']:
        with pytest.raises(ValueError):check_url(address)
    assert stamp('2026-11-11T08:00:00+08:00')==stamp('2026-11-11T00:00:00Z')


def test_active_quality_energy_and_no_fabrication():
    rows=[{'time_tag':'2026-11-11T00:00:00Z','active':True,'overall_quality':0,'proton_speed':500},
          {'time_tag':'2026-11-11T00:01:00Z','active':False,'overall_quality':0,'proton_speed':900},
          {'time_tag':'2026-11-11T00:02:00Z','active':True,'overall_quality':1,'proton_speed':800}]
    points=normalize('wind',rows)
    assert len(points)==1 and points[0][2:4]==(500,'km/s')
    assert normalize('xray',[{'time_tag':rows[0]['time_tag'],'energy':'0.05-0.4nm','flux':1e-5}])==[]
    assert normalize('protons',[{'time_tag':rows[0]['time_tag'],'energy':'>=10 MeV','flux':2}])[0][2]==2


def test_historical_revisions_and_future_not_observed(monitor):
    with monitor.db:
        monitor.db.execute('INSERT INTO point VALUES(?,?,?,?,?,?,?)',('speed',100,400,'km/s','A','observed',110))
        monitor.db.execute('INSERT INTO point VALUES(?,?,?,?,?,?,?)',('speed',100,500,'km/s','A','observed',120))
    assert monitor.series('speed',0,200,115)['points'][0]['v']==400
    assert monitor.series('speed',0,200,125)['points'][0]['v']==500
    assert monitor.series('speed',0,200,90)['points']==[]


def test_registered_assets_cannot_read_arbitrary_files(monitor):
    with pytest.raises(KeyError):monitor.asset_read('../../secret')
    aid=monitor.asset_register('https://sdo.gsfc.nasa.gov/assets/img/latest/latest_1024_HMIB.jpg')
    monitor.enabled=True
    with pytest.raises(FileNotFoundError):monitor.asset_read(aid)


def test_restart_interrupted_and_empty_states(tmp_path):
    service=Monitor(tmp_path/'state',tmp_path/'cache')
    service.db.execute('INSERT INTO job VALUES(?,?,?,?,?)',('one','running',1,1,'{}'));service.db.commit();service.db.close()
    reopened=Monitor(tmp_path/'state',tmp_path/'cache')
    assert reopened.rows('SELECT state FROM job')[0]['state']=='interrupted'
    assert reopened.summary()['model']['state']=='not_ready'
    assert reopened.summary()['frames']==[]
    reopened.db.close()


def test_cleanup_preserves_outside_and_recent(monitor,tmp_path):
    outside=tmp_path/'original.jpg';outside.write_bytes(b'original')
    cached=monitor.cache/'old.bin';cached.write_bytes(b'cached')
    monitor.db.execute('INSERT INTO asset VALUES(?,?,?,?,?,?,?)',('old','https://services.swpc.noaa.gov/a','old.bin','image/jpeg',time.time()-8*86400,'run',1));monitor.db.commit()
    monitor.cleanup()
    assert not cached.exists() and outside.read_bytes()==b'original'


def test_forecast_structured_without_inventing_values():
    text=':Issued: 2026 Nov 11 0030 UTC\n Nov 11 Nov 12 Nov 13\n00-03UT 3.00 2.67 1.33\nR1-R2 60% 50% 40%\nS1 or greater 15% 10% 5%'
    result=parse_forecast(text)
    assert result['issued_at']=='2026-11-11T00:30:00Z'
    assert result['days'][1]['kp_max']==2.67
    assert result['days'][0]['r1_r2']==60
    assert result['days'][0]['r3_plus'] is None
    assert parse_forecast('unrecognized changed format')['days']==[]


def test_return_to_previous_forecast_is_new_revision(monitor,monkeypatch):
    from mdva.web import space_weather
    payloads=iter([b'A',b'B',b'A'])
    monkeypatch.setattr(space_weather,'fetch',lambda *args:(next(payloads),{}))
    monkeypatch.setattr(space_weather.time,'time',lambda:1234.0)
    for _ in range(3):monitor.collect('forecast')
    assert len(monitor.rows("SELECT * FROM document WHERE source='forecast'"))==3
    assert monitor.latest('forecast')['text']=='A'


def test_kp_forecast_does_not_relabel_observed():
    data=[{'time_tag':'2026-11-11T00:00Z','kp':4,'observed':'observed'},
          {'time_tag':'2026-11-12T00:00Z','kp':5,'observed':'predicted'}]
    assert len(normalize('kp_forecast',data))==1


def test_alert_validity_never_assumes_active_without_expiry():
    records=[{'message':'Valid From: 2026 Nov 09 0556 UTC\nNow Valid Until: 2026 Nov 10 1200 UTC'},
             {'message':'ALERT: HF radio condition'}, {'message':'CANCEL WARNING'}]
    result=parse_alerts(records,stamp('2026-11-11T00:00Z'))
    assert [r['status'] for r in result]==['expired','unknown','cancelled']


def test_pinned_run_survives_cache_retention(monitor):
    target=monitor.cache/'pinned.bin';target.write_bytes(b'frame')
    monitor.db.execute('INSERT INTO asset VALUES(?,?,?,?,?,?,?)',('pinned','https://services.swpc.noaa.gov/frame','pinned.bin','image/jpeg',time.time()-8*86400,'run',1));monitor.db.commit()
    monitor.pin_run('run');monitor.cleanup()
    assert target.read_bytes()==b'frame'


def test_monitor_api_does_not_initialize_annotation_workspace(tmp_path,monkeypatch):
    from mdva.web.routes import call
    from mdva.web.deps import Workspaces
    ws=Workspaces(tmp_path/'data')
    monkeypatch.setattr(ws,'hub',lambda *args:(_ for _ in ()).throw(RuntimeError('annotation initialization failed')))
    response=call(ws,'GET','/api/sw/summary')
    assert response.status==200 and response.json_body['observations']==[]
