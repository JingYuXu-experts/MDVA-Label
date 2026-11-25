"""Latest observation semantics, per-channel identity and immutable offline assets."""
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from mdva.web import space_weather
from mdva.web.space_weather import Monitor, SOLAR_CHANNELS, stamp, utc


@pytest.fixture
def solar(tmp_path,monkeypatch):
    when=stamp('2026-11-12T10:00:00Z')
    monkeypatch.setattr(space_weather.time,'time',lambda:when)
    service=Monitor(tmp_path/'state',tmp_path/'cache')
    service.enabled=True
    requests=[]
    def fetch(url,headers=None):
        requests.append(url)
        params=parse_qs(urlsplit(url).query)
        if 'getClosestImage' in url:
            return json.dumps(dict(id=params['sourceId'][0],date=utc(when-3600),width=4096,height=4096,rsun=1600)).encode(),{}
        return b'\x89PNG\r\n\x1a\n'+params['id'][0].encode(),{'Content-Type':'image/png'}
    monkeypatch.setattr(space_weather,'fetch',fetch)
    yield service,when,requests
    service.close();service.db.close()


def test_latest_can_be_delayed_but_same_timestamp_is_not_valid_historical_match(solar):
    m,when,requests=solar
    latest=m.solar_image('304',latest=True)
    assert latest['observed_at']==utc(when-3600)
    assert latest['offset_seconds']==-3600 and latest['selection']=='latest'
    assert len(requests)==2
    assert m.solar_image('304',latest=True)['cache_hit'] is True
    with pytest.raises(ValueError,match='30分钟'):m.solar_image('304',when)


@pytest.mark.parametrize('channel',list(SOLAR_CHANNELS))
def test_each_channel_uses_its_own_registered_source_and_asset(solar,channel):
    m,_,requests=solar
    frame=m.solar_image(channel,latest=True)
    assert frame['channel']==channel and frame['image_id']==str(SOLAR_CHANNELS[channel])
    assert parse_qs(urlsplit(requests[0]).query)['sourceId']==[str(SOLAR_CHANNELS[channel])]
    assert m.asset_read(frame['id'])[0].endswith(str(SOLAR_CHANNELS[channel]).encode())


def test_cached_metadata_restores_same_evicted_asset_without_replacing_by_latest(solar):
    m,_,requests=solar
    frame=m.solar_image('171',latest=True)
    asset=m.rows('SELECT file FROM asset WHERE id=?',(frame['id'],))[0]
    (m.cache/asset['file']).unlink()
    restored=m.solar_image('171',latest=True)
    assert restored['image_id']==frame['image_id']
    assert len(requests)==3 and 'downloadImage' in requests[-1]


def test_offline_latest_reuses_available_cache_and_preserves_age_and_channel(solar,monkeypatch):
    m,when,_=solar
    frame=m.solar_image('304',latest=True)
    m.enabled=False
    monkeypatch.setattr(space_weather.time,'time',lambda:when+7200)
    monkeypatch.setattr(space_weather,'fetch',lambda *args:pytest.fail('offline access must not use network'))
    cached=m.solar_image('304',latest=True)
    assert cached['image_id']==frame['image_id'] and cached['observed_at']==frame['observed_at']
    assert cached['offset_seconds']==-10800
    with pytest.raises(ValueError,match='离线'):m.solar_image('193',latest=True)


def test_future_source_observation_is_rejected(solar,monkeypatch):
    m,when,_=solar
    monkeypatch.setattr(space_weather,'fetch',lambda *args:(json.dumps({'id':'123','date':utc(when+900)}).encode(),{}))
    with pytest.raises(ValueError,match='未来观测'):m.solar_image('304',latest=True)
