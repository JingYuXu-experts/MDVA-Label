"""Published benchmark, input provenance and numerical tracing (not a visual test)."""
import json
import math
import socket
import time
from types import SimpleNamespace
from pathlib import Path

import numpy as np
import pytest

from mdva.physics.magnetosphere import parameters, shue98, trace
from mdva.web.space_weather import Monitor, normalize, stamp

VALUES=dict(time=stamp('2026-11-11T12:00Z'),pressure=2,dst=-20,by=2,bz=-4)


def test_t96_upstream_published_fortran_benchmark():
    # tsssss/geopack/test_geopack1.md: T96 standalone external component.
    from mdva.physics.geopack.t96 import t96
    actual=t96([2,-87,2,-5,0,0,0,0,0,0],-.533585131,-5.1,.3,2.8)
    np.testing.assert_allclose(actual,[61.1784935,-1.46160448,-40.4486618],atol=.002,rtol=0)


def test_shue_response_has_physical_direction():
    quiet=shue98(2,2);compressed=shue98(8,2);south=shue98(2,-10)
    assert 8<quiet[0]<13
    assert compressed[0]<quiet[0] and south[0]<quiet[0]
    assert south[1]>quiet[1]


@pytest.mark.parametrize('key,value',[('pressure',0),('bz',math.nan),('dst',-999),('by',float('inf')),('time',stamp('2035-01-01'))])
def test_refuse_unusable_or_extrapolated_coefficients(key,value):
    with pytest.raises(ValueError):parameters({**VALUES,key:value})


def test_trace_offline_basis_domain_and_step_convergence(monkeypatch):
    def blocked(*args,**kwargs):raise AssertionError('Model evaluation must be offline')
    monkeypatch.setattr(socket.socket,'connect',blocked)
    coarse=trace(VALUES,latitudes=(60,),azimuths=1)
    fine=trace(VALUES,latitudes=(60,),azimuths=1,step_scale=.5)
    basis=np.array(coarse['basis_geo']).T
    np.testing.assert_allclose(basis.T@basis,np.eye(3),atol=1e-10)
    assert np.linalg.det(basis)>0.999999
    p=np.array(coarse['lines'][0]['points']);q=np.array(fine['lines'][0]['points'])
    assert np.isfinite(p).all() and (p[:,3]>0).all()
    assert p[:,0].min()>=-50 and p[:,0].max()<=20
    # A closed line's apex must converge under halving the spatial step.
    assert coarse['lines'][0]['ends']==fine['lines'][0]['ends']==['earth','earth']
    assert abs(np.linalg.norm(p[:,:3],axis=1).max()-np.linalg.norm(q[:,:3],axis=1).max())<.05


def test_driver_requires_complete_time_matched_observations(tmp_path):
    m=Monitor(tmp_path/'db',tmp_path/'cache')
    try:
        with pytest.raises(ValueError):m.magnetic_inputs(VALUES['time'])
        with m.db:
            for metric,v,offset in [('speed',400,0),('density',5,0),('by',2,0),('bz',-4,0),('dst',-20,-3600)]:
                m.db.execute('INSERT INTO point VALUES(?,?,?,?,?,?,?)',(metric,VALUES['time']+offset,v,'nT','test','observed',VALUES['time']))
        p,records=m.magnetic_inputs(VALUES['time'])
        assert p['pressure']==pytest.approx(1.338097538952)
        assert records['dst']['time']!=records['by']['time']
        with m.db:m.db.execute("UPDATE point SET t=t-1000 WHERE metric='bz'")
        with pytest.raises(ValueError,match='过期'):m.magnetic_inputs(VALUES['time'])
    finally:m.db.close()


def test_background_model_failure_is_persisted_and_retryable(tmp_path,monkeypatch):
    from mdva.web import space_weather
    m=Monitor(tmp_path/'db',tmp_path/'cache')
    def wait(key):
        end=time.monotonic()+3
        while time.monotonic()<end:
            rows=m.rows('SELECT * FROM physical_model WHERE id=?',(key,))
            if rows and rows[0]['state']!='running':return rows[0]
            time.sleep(.01)
        raise AssertionError('background worker did not finish')
    p={**VALUES,'time':'2026-11-11T12:00Z','mode':'manual'}
    try:
        monkeypatch.setattr(space_weather.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=1,stdout='',stderr='model failed'))
        job=m.magnetic_start(p);assert wait(job['id'])['state']=='failed'
        assert m.rows('SELECT state FROM job WHERE id=?',(job['id'],))[0]['state']=='failed'
        monkeypatch.setattr(space_weather.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout=json.dumps({'lines':[]}),stderr=''))
        retry=m.magnetic_start(p);row=wait(retry['id'])
        assert retry['id']==job['id'] and row['state']=='done'
        assert json.loads(row['payload'])['driver_mode']=='manual'
        assert m.rows('SELECT state FROM job WHERE id=?',(retry['id'],))[0]['state']=='done'
        # Offline use can reopen the exact completed output without spawning another job.
        assert m.magnetic_start(p)['state']=='done'
    finally:m.db.close()


def test_negative_indices_and_imf_are_kept_but_fill_removed():
    assert normalize('dst',[dict(time_tag='2026-11-11T00:00Z',dst=-80)])[0][2]==-80
    assert normalize('dst',[dict(time_tag='2026-11-11T00:00Z',dst=-99999)])==[]
    rows=normalize('mag',[dict(time_tag='2026-11-11T00:00Z',active=True,overall_quality=0,by_gsm=-5,bz_gsm=-10)])
    assert {r[0]:r[2] for r in rows}=={'by':-5,'bz':-10}


def test_immutable_solar_image_and_cached_request_offset(tmp_path,monkeypatch):
    from mdva.web import space_weather
    m=Monitor(tmp_path/'db',tmp_path/'cache');m.enabled=True;requests=[]
    def fetch(url,headers=None):
        requests.append(url)
        if 'getClosestImage' in url:return json.dumps(dict(id='123',date='2026-11-11 12:00:12',width=4096,height=4096,rsun=1600)).encode(),{}
        assert 'downloadImage/?id=123&' in url
        return b'\x89PNG\r\n\x1a\n',{'Content-Type':'image/png'}
    monkeypatch.setattr(space_weather,'fetch',fetch)
    try:
        a=m.solar_image('304',VALUES['time']);b=m.solar_image('304',VALUES['time']+20)
        assert a['id']==b['id'] and b['offset_seconds']==-8
        assert len(requests)==2
        m.enabled=False
        assert m.asset_read(a['id'])[1]=='image/png'
        with pytest.raises(ValueError):m.solar_image('arbitrary',VALUES['time'])
        with pytest.raises(ValueError,match='未来'):m.solar_image('304',stamp('2099-01-01'))
    finally:m.db.close()
