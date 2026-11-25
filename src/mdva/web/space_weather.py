"""Public-source monitoring. No dataset pixels or private paths leave this service."""
from __future__ import annotations

import concurrent.futures
import gzip
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

BASE = 'https://services.swpc.noaa.gov'
SOURCES = {
    'xray': ('GOES X射线', BASE+'/json/goes/primary/xrays-1-day.json', 60, 'observed'),
    'protons': ('GOES 质子通量', BASE+'/json/goes/primary/integral-protons-1-day.json', 60, 'observed'),
    'wind': ('L1 太阳风', BASE+'/json/rtsw/rtsw_wind_1m.json', 60, 'observed'),
    'mag': ('行星际磁场', BASE+'/json/rtsw/rtsw_mag_1m.json', 60, 'observed'),
    'kp': ('行星 Kp 观测', BASE+'/products/noaa-planetary-k-index.json', 60, 'observed'),
    'kp_forecast': ('Kp 机构预报', BASE+'/products/noaa-planetary-k-index-forecast.json', 300, 'forecast'),
    'alerts': ('NOAA 通报', BASE+'/products/alerts.json', 60, 'forecast'),
    'forecast': ('NOAA 三天预报', BASE+'/text/3-day-forecast.txt', 300, 'forecast'),
    'enlil': ('WSA–ENLIL', BASE+'/products/animations/enlil.json', 300, 'forecast'),
    'dst': ('Kyoto Dst（经 NOAA 分发）', BASE+'/products/kyoto-dst.json', 300, 'observed'),
    **{'geospace_'+key: ('Geospace '+label, BASE+'/products/animations/geospace/'+key+'.json', 300, 'model')
       for key,label in [('velocity','速度'),('density','密度'),('pressure','压力')]},
    'cme': ('DONKI CME 研究分析', 'https://ccmc.gsfc.nasa.gov/DONKI/WS/get/CME', 300, 'research'),
    'simulations': ('DONKI 传播研究模拟', 'https://ccmc.gsfc.nasa.gov/DONKI/WS/get/WSAEnlilSimulations', 300, 'research'),
    'halpha': ('KSO Hα 缩略观测', 'https://cesar.kso.ac.at/halpha3s/JPEG/recent/'+str(datetime.now(timezone.utc).year)+'/', 900, 'observed'),
    **{key: (label, 'https://sdo.gsfc.nasa.gov/assets/img/latest/latest_1024_'+band+'.jpg', 900, 'observed')
       for key, label, band in [('hmi','HMI 视向磁图','HMIB'),('aia193','AIA 193 Å','0193'),('aia304','AIA 304 Å','0304')]},
}
HOSTS = {'services.swpc.noaa.gov', 'sdo.gsfc.nasa.gov', 'ccmc.gsfc.nasa.gov','cesar.kso.ac.at','api.helioviewer.org'}
SOLAR_CHANNELS = {'171':10, '193':11, '304':13, 'hmi':19, 'c2':4, 'c3':5}

def utc(value=None):
    return datetime.fromtimestamp(value if value is not None else time.time(), timezone.utc).isoformat().replace('+00:00','Z')

def stamp(value):
    if not value: return None
    try:
        parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError: return None

class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def check_url(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme != 'https' or p.hostname not in HOSTS or p.username or p.port not in (None,443):
        raise ValueError('信源地址不在登记范围内')

def fetch(url, headers=None):
    check_url(url)
    req=urllib.request.Request(url, headers={'User-Agent':'MDVA-SpaceWeather/0.1', 'Accept-Encoding':'gzip', **(headers or {})})
    with urllib.request.build_opener(SafeRedirect()).open(req, timeout=20) as response:
        raw=response.read(16*1024*1024+1)
        if len(raw)>16*1024*1024: raise ValueError('信源响应超过16MB')
        if response.headers.get('Content-Encoding')=='gzip': raw=gzip.decompress(raw)
        if len(raw)>32*1024*1024: raise ValueError('解压后的响应过大')
        return raw, dict(response.headers)

def normalize(source, payload):
    """Keep source/units and actual cadence; no interpolation of missing observations."""
    mapping={'wind': [('speed','proton_speed','km/s'),('density','proton_density','cm⁻³')],
             'mag':[('bt','bt','nT'),('by','by_gsm','nT'),('bz','bz_gsm','nT')], 'kp':[('kp','Kp','')],
             'dst':[('dst','dst','nT')],
             'kp_forecast':[('kp_forecast','kp','')], 'xray':[('xray','flux','W/m²')],
             'protons':[('protons','flux','pfu')]}
    points=[]
    for row in payload if isinstance(payload,list) else []:
        if not isinstance(row,dict): continue
        if source in ('wind','mag') and (row.get('active') is not True or row.get('overall_quality') not in (0,'0',None)): continue
        if source=='xray' and row.get('energy')!='0.1-0.8nm': continue
        if source=='protons' and row.get('energy')!='>=10 MeV': continue
        if source=='kp_forecast' and str(row.get('observed','')).lower() not in ('predicted','forecast'):continue
        t=stamp(row.get('time_tag'))
        if t is None: continue
        for metric,key,unit in mapping.get(source,[]):
            try: v=float(row[key])
            except (KeyError,ValueError,TypeError): continue
            if not math.isfinite(v) or (metric not in ('bz','by','dst') and v<0) or v <= -999: continue
            points.append((metric,t,v,unit,str(row.get('source') or row.get('satellite') or 'NOAA'),row.get('observed','observed' if source!='kp_forecast' else 'predicted')))
    return points

def parse_forecast(text):
    result={'text':text,'issued_at':None,'days':[],'valid_until':None}
    issued=re.search(r':Issued:\s*(\d{4} \w{3} \d{1,2} \d{4}) UTC',text)
    if issued:
        result['issued_at']=utc(datetime.strptime(issued[1],'%Y %b %d %H%M').replace(tzinfo=timezone.utc).timestamp())
    header=next((line for line in text.splitlines() if len(re.findall(r'\b[A-Z][a-z]{2}\s+\d{1,2}',line))==3),'')
    dates=re.findall(r'\b[A-Z][a-z]{2}\s+\d{1,2}',header)
    year=int(issued[1][:4]) if issued else None
    if len(dates)!=3 or year is None:return result
    for date in dates:
        dt=datetime.strptime(str(year)+' '+date,'%Y %b %d').replace(tzinfo=timezone.utc)
        if result['issued_at'] and dt.timestamp()<(stamp(result['issued_at']) or 0)-86400:dt=dt.replace(year=year+1)
        result['days'].append({'date':dt.date().isoformat(),'kp_max':None,'r1_r2':None,'r3_plus':None,'s1_plus':None})
    result['valid_until']=utc((stamp(result['days'][-1]['date']) or 0)+86400)
    kp=[]
    for line in text.splitlines():
        if re.match(r'\d{2}-\d{2}UT',line):
            values=re.findall(r'\d+\.\d+',line)
            if len(values)==3:kp.append([float(x) for x in values])
        for label,key in [('R1-R2','r1_r2'),('R3 or greater','r3_plus'),('S1 or greater','s1_plus')]:
            if line.strip().startswith(label):
                values=re.findall(r'(\d+)%',line)
                if len(values)==3:
                    for day,value in zip(result['days'],values):day[key]=int(value)
    if kp:
        for i,day in enumerate(result['days']):day['kp_max']=max(row[i] for row in kp)
    return result

def parse_alerts(records,now):
    result=[]
    for original in records:
        row=dict(original);message=row.get('message','')
        def field(pattern):
            match=re.search(pattern+r'\s*(\d{4} \w{3} \d{1,2} \d{4}) UTC',message)
            if not match:return None
            try:return utc(datetime.strptime(match[1],'%Y %b %d %H%M').replace(tzinfo=timezone.utc).timestamp())
            except ValueError:return None
        row['valid_from']=field('Valid From:');row['valid_until']=field('(?:Now )?Valid Until:')
        row['status']='cancelled' if 'CANCEL' in message.upper() else 'unknown'
        if row['status']!='cancelled' and row['valid_until']:
            row['status']='expired' if (stamp(row['valid_until']) or 0)<now else 'upcoming' if (stamp(row['valid_from']) or 0)>now else 'valid'
        row['communication_related']=bool(re.search(r'\bHF\b|Radio Blackout|X-ray',message,re.I))
        row['title']='空间天气通报'
        for pattern,title in [('Electron','高能电子通量通报'),('Geomagnetic','地磁活动通报'),('Proton','高能质子通报'),('X-ray|Radio Blackout','太阳X射线 / 无线电通报')]:
            if re.search(pattern,message,re.I):row['title']=title;break
        result.append(row)
    return result

class Monitor:
    def __init__(self, root, cache, enabled=False):
        self.root=Path(root); self.root.mkdir(parents=True,exist_ok=True)
        self.cache=Path(cache); self.cache.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock(); self.sync_lock=threading.Lock(); self.stop=threading.Event()
        self.enabled=enabled
        self.last_cleanup=0
        self.db=sqlite3.connect(self.root/'space_weather.sqlite',check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;
        CREATE TABLE IF NOT EXISTS document(source TEXT, digest TEXT, seen REAL, payload TEXT, PRIMARY KEY(source,digest));
        CREATE TABLE IF NOT EXISTS point(metric TEXT,t REAL,v REAL,unit TEXT,provider TEXT,kind TEXT,seen REAL,PRIMARY KEY(metric,t,provider,seen));
        CREATE INDEX IF NOT EXISTS point_range ON point(metric,t);
        CREATE TABLE IF NOT EXISTS source(id TEXT PRIMARY KEY, checked REAL, success REAL, error TEXT, etag TEXT, modified TEXT);
        CREATE TABLE IF NOT EXISTS asset(id TEXT PRIMARY KEY,url TEXT, file TEXT,mime TEXT,seen REAL,run TEXT,valid REAL);
        CREATE TABLE IF NOT EXISTS job(id TEXT PRIMARY KEY,state TEXT,created REAL,updated REAL,payload TEXT);
        CREATE TABLE IF NOT EXISTS artifact(id TEXT PRIMARY KEY,source TEXT,file TEXT,seen REAL);
        CREATE TABLE IF NOT EXISTS health_sample(source TEXT,t REAL,error TEXT);
        CREATE TABLE IF NOT EXISTS pinned_asset(id TEXT PRIMARY KEY,created REAL);
        CREATE TABLE IF NOT EXISTS solar_image(channel TEXT,t REAL,id TEXT,meta TEXT,PRIMARY KEY(channel,t));
        CREATE TABLE IF NOT EXISTS physical_model(id TEXT PRIMARY KEY,state TEXT,created REAL,payload TEXT,error TEXT);
        PRAGMA user_version=1;''')
        self.db.execute("UPDATE job SET state='interrupted' WHERE state='running'");self.db.commit()
        with self.db:self.db.execute("UPDATE physical_model SET state='interrupted',error='服务已重启，请重新计算' WHERE state='running'")
        with self.db:
            # New metric parser must not be bypassed by an old HTTP 304 validator.
            if not self.db.execute("SELECT 1 FROM point WHERE metric='by' LIMIT 1").fetchone():
                self.db.execute("UPDATE source SET etag='',modified='',checked=0 WHERE id='mag'")
        if enabled:
            threading.Thread(target=self.loop,daemon=True,name='space-weather-scheduler').start()

    def rows(self,sql,args=()):
        with self.lock: return [dict(r) for r in self.db.execute(sql,args).fetchall()]

    def latest(self,source,as_of=None):
        rows=self.rows('SELECT payload,seen FROM document WHERE source=? AND seen<=? ORDER BY seen DESC,rowid DESC LIMIT 1',(source,as_of or time.time()))
        return json.loads(rows[0]['payload']) if rows else None

    def asset_register(self,url,run='',valid=None):
        check_url(url); aid=hashlib.sha256(url.encode()).hexdigest()[:32]
        with self.lock,self.db:
            self.db.execute('INSERT OR IGNORE INTO asset VALUES(?,?,?,?,?,?,?)',(aid,url,'','',time.time(),run,valid))
        return aid

    def asset_read(self,aid):
        rows=self.rows('SELECT * FROM asset WHERE id=?',(aid,))
        if not rows: raise KeyError('未知资源ID')
        row=rows[0]
        if row['file'] and (self.cache/row['file']).is_file():
            return (self.cache/row['file']).read_bytes(),row['mime']
        if not self.enabled or not row['run']: raise FileNotFoundError('资源尚未缓存或已经过期；不会用最新图片替换历史影像')
        raw,heads=fetch(row['url']);mime=heads.get('Content-Type','').split(';')[0]
        if mime not in ('image/jpeg','image/png','image/gif'): raise ValueError('信源没有返回支持的图片')
        filename=aid+'.bin';tmp=self.cache/(aid+'.'+str(threading.get_ident())+'.part')
        tmp.write_bytes(raw);os.replace(tmp,self.cache/filename)
        with self.lock,self.db: self.db.execute('UPDATE asset SET file=?,mime=? WHERE id=?',(filename,mime,aid))
        return raw,mime

    def collect(self,source):
        label,url,interval,kind=SOURCES[source];now=time.time()
        previous=self.rows('SELECT * FROM source WHERE id=?',(source,))
        headers={}
        if previous and previous[0]['etag']:headers['If-None-Match']=previous[0]['etag']
        if previous and previous[0]['modified']:headers['If-Modified-Since']=previous[0]['modified']
        try:
            if source in ('cme','simulations'):
                url+='?'+urllib.parse.urlencode({'startDate':utc(now-7*86400)[:10],'endDate':utc(now)[:10]})
            raw,heads=fetch(url,headers)
            observed=None
            if source=='halpha':
                candidates=re.findall(r'href="(kanz_halph_fd_(\d{8}_\d{6})\.icon\.jpg)"',raw.decode('utf-8'))
                if not candidates:raise ValueError('KSO 未提供可识别观测缩略图')
                filename,obs=max(candidates,key=lambda row:row[1])
                observed=utc(datetime.strptime(obs,'%Y%m%d_%H%M%S').replace(tzinfo=timezone.utc).timestamp())
                url=urllib.parse.urljoin(url,filename);raw,heads=fetch(url)
            if source in ('hmi','aia193','aia304','halpha'):
                digest=hashlib.sha256(raw).hexdigest(); filename=digest+'.jpg'
                if not raw.startswith(b'\xff\xd8'):raise ValueError('无效的太阳JPEG影像')
                (self.cache/filename).write_bytes(raw)
                aid=digest[:32]
                with self.lock,self.db:
                    self.db.execute('INSERT OR IGNORE INTO asset VALUES(?,?,?,?,?,?,?)',(aid,url,filename,'image/jpeg',now,'',None))
                payload={'id':aid,'instrument':label,'observed_at':observed,'fetched_at':utc(now),'time_note':'KSO 缩略图，非推理输入' if source=='halpha' else '观测时间见原图图注；获取时间不代表观测时间','source_url':url}
            elif source=='forecast':payload={**parse_forecast(raw.decode('utf-8')),'source_url':url}
            else:payload=json.loads(raw)
            if source=='enlil':
                frames=[]
                for entry in payload:
                    relative=entry.get('url','');m=re.search(r'(enlil[^/]+)_(\d{8}T\d{6})\.jpg$',relative)
                    if not m:continue
                    valid=datetime.strptime(m[2],'%Y%m%dT%H%M%S').replace(tzinfo=timezone.utc).timestamp()
                    aid=self.asset_register(urllib.parse.urljoin(BASE,relative),m[1],valid)
                    frames.append({'id':aid,'run':m[1],'valid_at':utc(valid)})
                if not frames:raise ValueError('传播清单没有可识别的帧')
                payload=frames
            if source.startswith('geospace_'):
                frames=[]
                for entry in payload:
                    relative=entry.get('url','')
                    match=re.fullmatch(r'/images/animations/geospace/(velocity|density|pressure)/magnetosphere_cut_planes_\1_(\d{8}T\d{4})_(\d{8}T\d{4})\.png',relative)
                    if not match or 'geospace_'+match[1]!=source:continue
                    valid=datetime.strptime(match[3],'%Y%m%dT%H%M').replace(tzinfo=timezone.utc).timestamp()
                    run='geospace-'+match[1]+'-'+match[2]
                    aid=self.asset_register(BASE+relative,run,valid)
                    frames.append({'id':aid,'run':run,'valid_at':utc(valid),'kind':'MHD_model','parameter':match[1]})
                if not frames:raise ValueError('Geospace 清单没有合法的模型帧')
                payload=frames
            points=normalize(source,payload)
            raw_digest=hashlib.sha256(raw).hexdigest()
            raw_file='raw-'+raw_digest+'.gz'
            if not (self.cache/raw_file).exists():(self.cache/raw_file).write_bytes(gzip.compress(raw))
            if source in ('wind','mag','kp','kp_forecast','xray','protons','dst'):
                payload={'source_url':url,'raw_digest':raw_digest,'latest_observation':utc(max(p[1] for p in points)) if points else None}
            packed=json.dumps(payload,ensure_ascii=False);digest=hashlib.sha256(packed.encode()).hexdigest()
            with self.lock,self.db:
                self.db.execute('INSERT OR REPLACE INTO artifact VALUES(?,?,?,?)',(raw_digest,source,raw_file,now))
                prior=self.db.execute('SELECT payload FROM document WHERE source=? ORDER BY seen DESC,rowid DESC LIMIT 1',(source,)).fetchone()
                if prior is None or prior[0]!=packed:
                    self.db.execute('INSERT INTO document VALUES(?,?,?,?)',(source,digest+'-'+uuid.uuid4().hex,now,packed))
                for metric,t,v,unit,provider,k in points:
                    old=self.db.execute('SELECT v,kind FROM point WHERE metric=? AND t=? AND provider=? ORDER BY seen DESC LIMIT 1',(metric,t,provider)).fetchone()
                    if old is None or old['v']!=v or old['kind']!=str(k):
                        self.db.execute('INSERT INTO point VALUES(?,?,?,?,?,?,?)',(metric,t,v,unit,provider,str(k),now))
                self.db.execute('INSERT OR REPLACE INTO source VALUES(?,?,?,?,?,?)',(source,now,now,'',heads.get('ETag',''),heads.get('Last-Modified','')))
                self.db.execute('INSERT INTO health_sample VALUES(?,?,?)',(source,now,''))
            if source=='enlil':
                # First frame ready quickly; rest of a selected run are fetched on demand.
                self.asset_read(min(payload,key=lambda f:abs((stamp(f['valid_at']) or 0)-now))['id'])
                # Immutable official frames are cached for offline replay, never synthesized.
                def cache_frame(frame):
                    try:self.asset_read(frame['id'])
                    except Exception:return frame['id']
                    return None
                with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                    missing=[aid for aid in pool.map(cache_frame,payload) if aid]
                if missing:raise ValueError(f'传播清单可用，{len(missing)}帧缓存失败；可重试更新')
            elif source.startswith('geospace_'):
                # Cache a bounded set for immediate/offline viewing, lazy-load other immutable frames.
                self.asset_read(payload[-1]['id'])
                for frame in payload[::max(1,len(payload)//12)]:
                    try:self.asset_read(frame['id'])
                    except Exception:pass
        except urllib.error.HTTPError as exc:
            if exc.code==304:
                with self.lock,self.db:
                    self.db.execute('UPDATE source SET checked=?,success=?,error=? WHERE id=?',(now,now,'',source))
                    self.db.execute('INSERT INTO health_sample VALUES(?,?,?)',(source,now,''))
            else:self.failure(source,now,str(exc))
        except Exception as exc:self.failure(source,now,str(exc))

    def failure(self,source,now,error):
        with self.lock,self.db:
            self.db.execute('INSERT INTO source VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET checked=excluded.checked,error=excluded.error',(source,now,None,error[:500],'',''))
            self.db.execute('INSERT INTO health_sample VALUES(?,?,?)',(source,now,error[:500]))

    def synchronize(self,only=None):
        if only and only not in SOURCES:raise ValueError('未知信源')
        if not self.enabled:raise ValueError('离线模式已关闭网络采集')
        if not self.sync_lock.acquire(False):return {'state':'running'}
        jid=str(time.time_ns()); now=time.time()
        with self.lock,self.db:self.db.execute('INSERT INTO job VALUES(?,?,?,?,?)',(jid,'running',now,now,'{}'))
        def run():
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(self.collect,[only] if only else list(SOURCES)))
                errors=[r for r in self.rows('SELECT id,error FROM source') if r['error'] and (not only or r['id']==only)]
                with self.lock,self.db:self.db.execute('UPDATE job SET state=?,updated=?,payload=? WHERE id=?',('partial' if errors else 'done',time.time(),json.dumps({'errors':errors}),jid))
            except Exception as exc:
                with self.lock,self.db:self.db.execute('UPDATE job SET state=?,updated=?,payload=? WHERE id=?',('failed',time.time(),json.dumps({'error':str(exc)}),jid))
            finally:self.sync_lock.release()
        threading.Thread(target=run,daemon=True).start()
        return {'id':jid,'state':'running'}

    def loop(self):
        while not self.stop.is_set():
            states={r['id']:r for r in self.rows('SELECT * FROM source')}
            due=[s for s,(_,_,interval,_) in SOURCES.items() if time.time()-states.get(s,{}).get('checked',0)> (max(interval,300) if states.get(s,{}).get('error') else interval)]
            if due and self.sync_lock.acquire(False):
                try:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(self.collect,due))
                finally:self.sync_lock.release()
            if time.time()-self.last_cleanup>3600:
                self.cleanup();self.last_cleanup=time.time()
            self.stop.wait(15)

    def close(self):
        self.stop.set()

    def cleanup(self):
        """Only this module's registered cache files; original datasets never participate."""
        cutoff=time.time()-7*86400
        targets=self.rows('SELECT id,file FROM asset WHERE seen<? AND file<>? AND id NOT IN (SELECT id FROM pinned_asset)',(cutoff,''))
        for row in targets:
            path=(self.cache/row['file']).resolve()
            if path.parent!=self.cache.resolve():raise ValueError('缓存清理路径越界')
        for row in targets:
            (self.cache/row['file']).unlink(missing_ok=True)
        artifacts=self.rows('SELECT * FROM artifact WHERE seen<?',(cutoff,))
        for row in artifacts:
            path=(self.cache/row['file']).resolve()
            if path.parent!=self.cache.resolve():raise ValueError('原始缓存清理路径越界')
        for row in artifacts:(self.cache/row['file']).unlink(missing_ok=True)
        with self.lock,self.db:
            self.db.execute('DELETE FROM artifact WHERE seen<?',(cutoff,))
            self.db.execute("UPDATE asset SET file='' WHERE seen<? AND id NOT IN (SELECT id FROM pinned_asset)",(cutoff,))
            self.db.execute('DELETE FROM document WHERE seen<?',(time.time()-365*86400,))
            self.db.execute('DELETE FROM point WHERE t<?',(time.time()-365*86400,))
            self.db.execute('DELETE FROM health_sample WHERE t<?',(time.time()-365*86400,))

    def series(self,metric,start,end,as_of=None):
        if end<start or end-start>366*86400:raise ValueError('查询范围无效或超过一年')
        rows=self.rows('SELECT * FROM point WHERE metric=? AND t>=? AND t<=? AND seen<=? ORDER BY t,seen',(metric,start,end,as_of or time.time()))
        latest={}
        for row in rows:latest[(row['t'],row['provider'])]=row
        values=sorted(latest.values(),key=lambda p:p['t'])
        if metric=='kp_forecast':values=[p for p in values if p['kind'].lower() in ('predicted','forecast')]
        # Bucket extrema retain short flare peaks instead of averaging them away.
        if len(values)>1800:
            width=max(1,len(values)//600)
            values=[p for i in range(0,len(values),width) for p in sorted({r['t']:r for r in (min(values[i:i+width],key=lambda p:p['v']),max(values[i:i+width],key=lambda p:p['v']))}.values(),key=lambda p:p['t'])]
        return {'metric':metric,'points':values,'unit':values[0]['unit'] if values else ''}

    def summary(self,as_of=None):
        states={r['id']:r for r in self.rows('SELECT * FROM source')}; sources=[]
        for sid,(name,url,interval,kind) in SOURCES.items():
            state=states.get(sid,{})
            sources.append({'id':sid,'name':name,'url':url,'kind':kind,'interval':interval,**state,
                            'status':'error' if state.get('error') else 'stale' if time.time()-(state.get('success') or 0)>interval*3 else 'ok'})
        forecast=self.latest('forecast',as_of)
        if forecast and 'days' not in forecast:forecast={**forecast,**parse_forecast(forecast.get('text',''))}
        return {'now':utc(),'enabled':self.enabled,'sources':sources,'observations':[x for s in ('hmi','aia193','aia304','halpha') if (x:=self.latest(s,as_of))],
                'forecast':forecast,'alerts':parse_alerts(self.latest('alerts',as_of) or [],as_of or time.time()),
                'frames':self.latest('enlil',as_of) or [],'events':self.latest('cme',as_of) or [],
                'simulations':self.latest('simulations',as_of) or [],'model':{'state':'not_ready','reason':'太阳识别与24小时预测尚未训练验收'}}

    def solar_image(self, channel, requested=None, *, latest=False):
        if channel not in SOLAR_CHANNELS:raise ValueError('未知太阳观测通道')
        if latest:requested=time.time()
        if requested is None or not math.isfinite(requested):raise ValueError('需要有效的UTC观测时间')
        if requested<stamp('1996-01-01') or requested>time.time()+300:raise ValueError('观测时间超出可查询范围，不能获取未来影像')
        # Cache request-to-observation mapping, including nearest-image offsets.
        key=int(requested//60)*60
        cached=self.rows('SELECT meta FROM solar_image WHERE channel=? AND t=?',(channel,key))
        if latest and not self.enabled:
            cached=self.rows('SELECT meta FROM solar_image WHERE channel=? AND t<=? ORDER BY t DESC LIMIT 100',(channel,key))
        for row in cached:
            record=json.loads(row['meta'])
            offset=stamp(record['observed_at'])-requested
            if not latest and abs(offset)>1800:raise ValueError('距请求时间最近的影像相差超过30分钟；该时段缺测')
            # A metadata row can outlive the ordinary image cache. Verify/retrieve
            # that exact immutable asset rather than returning a broken image.
            try:self.asset_read(record['id'])
            except FileNotFoundError:
                if latest and not self.enabled:continue
                raise
            return {**record,'requested_at':utc(requested),'offset_seconds':offset,
                    'selection':'latest' if latest else 'at_time','cache_hit':True}
        if not self.enabled:raise ValueError('此时刻影像尚未缓存，离线模式无法获取')
        query=urllib.parse.urlencode({'date':utc(key),'sourceId':SOLAR_CHANNELS[channel]})
        url='https://api.helioviewer.org/v2/getClosestImage/?'+query
        raw,_=fetch(url);meta=json.loads(raw)
        observed=stamp(meta.get('date'));image_id=str(meta.get('id',''))
        if not image_id.isdigit() or observed is None:raise ValueError('Helioviewer 返回的影像元数据无效')
        if observed>time.time()+300:raise ValueError('信源返回未来观测时间，已拒绝作为实测影像')
        if not latest and abs(observed-requested)>1800:raise ValueError('距请求时间最近的影像相差超过30分钟；该时段缺测')
        image_url='https://api.helioviewer.org/v2/downloadImage/?'+urllib.parse.urlencode({'id':image_id,'width':1536,'type':'png'})
        aid=self.asset_register(image_url,'solar-'+image_id,observed)
        # No remote image addresses are accepted from the browser; immutable image ID only.
        self.asset_read(aid)
        record={'id':aid,'image_id':image_id,'channel':channel,'observed_at':utc(observed),
                'requested_at':utc(requested),'offset_seconds':observed-requested,'kind':'observed',
                'source_url':url,'image_url':image_url,'metadata':meta,
                'note':'原始观测视野；颜色为仪器通道伪彩。单视角不能重建日珥深度或太阳背面。'}
        with self.lock,self.db:self.db.execute('INSERT OR REPLACE INTO solar_image VALUES(?,?,?,?)',(channel,key,aid,json.dumps(record)))
        return {**record,'selection':'latest' if latest else 'at_time','cache_hit':False}

    def magnetic_inputs(self, when):
        values={};records={}
        for metric in ('speed','density','by','bz','dst'):
            rows=self.rows('SELECT * FROM point WHERE metric=? AND t<=? ORDER BY t DESC,seen DESC LIMIT 1',(metric,when))
            if not rows:raise ValueError('缺少'+metric+'，请更新信源或明确进入手动实验')
            record=rows[0];age=when-record['t']
            if age>(6*3600 if metric=='dst' else 900):raise ValueError(metric+' 已过期，不能构建该时刻的观测驱动场')
            source='wind' if metric in ('speed','density') else 'dst' if metric=='dst' else 'mag'
            values[metric]=record['v'];records[metric]={'time':utc(record['t']),'provider':record['provider'],'value':record['v'],'unit':record['unit'],'source_url':SOURCES[source][1]}
        times=[stamp(records[m]['time']) for m in ('speed','density','by','bz')]
        if max(times)-min(times)>300:raise ValueError('太阳风和磁场观测时间不匹配，超过5分钟')
        return {'pressure':1.67262192369e-6*values['density']*values['speed']**2,
                'dst':values['dst'],'by':values['by'],'bz':values['bz'],'time':when}, records

    def magnetic_start(self,p):
        from ..physics.magnetosphere import parameters,REVISION
        mode=p.get('mode','observed')
        if mode not in ('observed','manual'):raise ValueError('未知模型参数模式')
        when=stamp(p.get('time'))
        if when is None:raise ValueError('需要模型UTC时间')
        if mode=='observed':values,records=self.magnetic_inputs(when)
        else:values={**p,'time':when};records={}
        values=parameters(values)
        key=hashlib.sha256(json.dumps([REVISION,values,mode,records],sort_keys=True).encode()).hexdigest()[:32]
        with self.lock,self.db:
            existing=self.db.execute('SELECT id,state,error FROM physical_model WHERE id=?',(key,)).fetchone()
            if existing and existing['state'] in ('running','done'):return dict(existing)
            if self.db.execute("SELECT COUNT(*) FROM physical_model WHERE state='running'").fetchone()[0]>=2:
                raise ValueError('磁场计算任务已满，请等待已有任务完成')
            self.db.execute('INSERT OR REPLACE INTO physical_model VALUES(?,?,?,?,?)',(key,'running',time.time(),'{}',''))
            self.db.execute('INSERT OR REPLACE INTO job VALUES(?,?,?,?,?)',(key,'running',time.time(),time.time(),json.dumps({'kind':'magnetosphere','mode':mode,'parameters':values})))
        def run():
            try:
                # CPU-heavy Fortran-style routines run in an isolated process:
                # no shared globals, and no Python GIL starvation of HTTP requests.
                process=subprocess.run([sys.executable,'-m','mdva.physics.magnetosphere'],
                                       input=json.dumps(values),text=True,encoding='utf-8',capture_output=True,
                                       timeout=180,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                if process.returncode:raise RuntimeError(process.stderr.strip()[-500:] or '磁场计算子进程失败')
                result=json.loads(process.stdout)
                result.update({'driver_mode':mode,'driver_observations':records,
                               'driver_note':'动压仅计质子，未含α粒子；L1驱动未作传播时移；Dst为小时指数。结果为参数条件下的瞬时经验估计。' if mode=='observed' else '用户手动实验，非当前观测、非业务预报。'})
                with self.lock,self.db:
                    self.db.execute('UPDATE physical_model SET state=?,payload=? WHERE id=?',('done',json.dumps(result),key))
                    self.db.execute("UPDATE job SET state='done',updated=? WHERE id=?",(time.time(),key))
            except Exception as exc:
                with self.lock,self.db:
                    self.db.execute('UPDATE physical_model SET state=?,error=? WHERE id=?',('failed',str(exc)[:500],key))
                    self.db.execute("UPDATE job SET state='failed',updated=?,payload=? WHERE id=?",(time.time(),json.dumps({'kind':'magnetosphere','error':str(exc)[:500]}),key))
        threading.Thread(target=run,daemon=True,name='magnetic-trace').start()
        return {'id':key,'state':'running'}

    def pin_run(self,run):
        assets=self.rows('SELECT id FROM asset WHERE run=?',(run,))
        if not assets:raise ValueError('未知传播批次')
        with self.lock,self.db:
            self.db.executemany('INSERT OR IGNORE INTO pinned_asset VALUES(?,?)',[(r['id'],time.time()) for r in assets])
        return {'run':run,'pinned_frames':len(assets)}

def download_flare_catalogue(name):
    """Explicit M0 audit download; no user URL, dataset upload or implicit training fetch."""
    import re
    from urllib.request import Request, urlopen
    if not re.fullmatch(r'sci_xrsf-l2-flrpt_geo_(?:y20(?:1[0-9]|2[0-4])_v1-0-1\.csv|metadata\.json)', name):
        raise ValueError('只接受已登记的2010–2024官方耀斑目录文件')
    url = ('https://data.ngdc.noaa.gov/platforms/solar-space-observing-satellites/goes/'
           'multi/l2/data/xrsf-l2-flrpt_science/csv/' + name)
    with urlopen(Request(url, headers={'User-Agent': 'MDVA-local-research/1.0'}), timeout=25) as response:
        result = response.read(20*1024*1024+1)
    if len(result) > 20*1024*1024:
        raise ValueError('官方目录超过预期大小')
    return result


def download_xrs_coverage(satellite,day,filename='',*,compare_recent=False):
    """Registered historical XRS science products for supervision coverage audits.

    Only generated NOAA paths are accepted. This is explicit preparation, not a
    background training download and never transmits user images or parameters.
    """
    import re
    from datetime import datetime, timedelta, timezone
    from urllib.parse import urlparse
    from urllib.request import Request,urlopen
    if type(satellite) is not int or satellite not in range(8,20):raise ValueError('未登记的GOES卫星')
    if not isinstance(day,str) or not re.fullmatch(r'20\d{6}',day):raise ValueError('GOES观测日期无效')
    if not isinstance(filename,str):raise ValueError('数据文件名无效')
    date=datetime.strptime(day,'%Y%m%d')
    if compare_recent:
        today = datetime.now(timezone.utc).date()
        if satellite < 16 or not today - timedelta(days=14) <= date.date() < today:
            raise ValueError('实时口径对照仅接受最近14天内已结束的GOES-R日期')
    elif not 2010 <= date.year <= 2024:
        raise ValueError('仅接受2010—2024的历史覆盖核验')
    if satellite < 16:
        # The NCEI instrument page registers a different archive for GOES 8–15.
        base=('https://www.ncei.noaa.gov/data/goes-space-environment-monitor/access/'
              f'science/xrs/goes{satellite}/xrsf-l2-avg1m_science/{date:%Y/%m}/')
    else:
        base=('https://data.ngdc.noaa.gov/platforms/solar-space-observing-satellites/goes/'
              f'goes{satellite}/l2/data/xrsf-l2-avg1m_science/{date:%Y/%m}/')
    pattern=rf'sci_xrsf-l2-avg1m_g{satellite:02d}_d{day}_v(\d+)-(\d+)-(\d+)\.nc'
    def fetch(url,limit):
        with urlopen(Request(url,headers={'User-Agent':'MDVA-local-research/1.0'}),timeout=25) as response:
            target=urlparse(response.geturl())
            if (target.scheme!='https' or target.hostname not in ('data.ngdc.noaa.gov','www.ncei.noaa.gov')
                or target.port not in (None,443) or target.username or target.password):raise ValueError('历史数据发生未登记的跨站跳转')
            raw=response.read(limit+1)
        if len(raw)>limit:raise ValueError('历史覆盖资料超过预期大小')
        return raw
    if not filename:
        listing=fetch(base,2*1024*1024).decode('utf-8')
        names=re.findall(r'href="([^"/]+\.nc)"',listing)
        options=[(tuple(map(int,m.groups())),n) for n in names if (m:=re.fullmatch(pattern,n))]
        if not options:raise FileNotFoundError('官方目录未列出所选日期的一分钟科学产品')
        filename=max(options)[1]
    if not re.fullmatch(pattern,filename):raise ValueError('数据文件不属于所选卫星和日期')
    return fetch(base+filename,64*1024*1024),{'url':base+filename,'filename':filename,
        'satellite':satellite,'day':day,'scope':'historical science supervision, not operational availability'}


def download_cnn193_browser_asset(filename):
    """Pinned author-linked public model data, not executable checkpoints."""
    import re
    from urllib.parse import urlparse
    from urllib.request import Request,urlopen
    if filename!='model.json' and not re.fullmatch(r'group(?:[1-9]|1[0-9]|2[0-3])-shard[12]of[12]',str(filename)):
        raise ValueError('不属于登记的作者模型文件')
    commit='a73a848bbc7eb5ef012daa9373a83fd7ee8cfd9a'
    url=f'https://raw.githubusercontent.com/illarionovEA/tfjs/{commit}/keras/{filename}'
    limit=8*1024*1024
    with urlopen(Request(url,headers={'User-Agent':'MDVA-local-research/1.0'}),timeout=25) as response:
        target=urlparse(response.geturl())
        if target.scheme!='https' or target.hostname!='raw.githubusercontent.com' or target.port not in (None,443):
            raise ValueError('模型资料发生未登记的跳转')
        expected=response.headers.get('Content-Length')
        raw=response.read(limit+1)
    if len(raw)>limit:raise ValueError('模型分片超出8MiB上限')
    if expected is not None and len(raw)!=int(expected):raise OSError('模型资料下载被截断，未缓存')
    return raw,{'url':url,'commit':commit,'filename':filename}


def download_cnn193_demo_image(filename):
    """Pinned public author demo examples, not a general URL fetch endpoint."""
    from urllib.parse import urlparse
    from urllib.request import Request,urlopen
    allowed={'30c4bdc6b5b012ac7523fb3216664adc.jpg','c10a7b74eca872a93aa6cd599ce6353f.jpg',
             'b5bd17089bc076bd7bd4a8f6e7a896d9.jpg','9c86ebc474a109f72fae7876e28b746c.jpg'}
    if filename not in allowed:raise ValueError('未登记的作者示例')
    commit='6932a61dd3d5d83530aa2c21cf2c482f5b39e38b'
    url=f'https://raw.githubusercontent.com/illarionovEA/illarionovEA.github.io/{commit}/{filename}'
    with urlopen(Request(url,headers={'User-Agent':'MDVA-local-research/1.0'}),timeout=25) as response:
        target=urlparse(response.geturl())
        if target.scheme!='https' or target.hostname!='raw.githubusercontent.com' or target.port not in (None,443):
            raise ValueError('作者示例发生未登记跳转')
        length=response.headers.get('Content-Length');raw=response.read(2*1024*1024+1)
    if len(raw)>2*1024*1024 or length is not None and len(raw)!=int(length):raise ValueError('作者示例长度异常')
    return raw,{'url':url,'commit':commit,'filename':filename}


def download_scss_notebook():
    """Read-only author training recipe snapshot; never execute notebook cells."""
    from urllib.parse import urlparse
    from urllib.request import Request,urlopen
    url=('https://raw.githubusercontent.com/harmi97/sun-segmentation/'
         'd70e09c0187376ab1ff983a6ab864e7d089635f5/notebooks/ch_segmentation.ipynb')
    with urlopen(Request(url,headers={'User-Agent':'MDVA-local-research/1.0'}),timeout=25) as response:
        target=urlparse(response.geturl())
        if target.scheme!='https' or target.hostname!='raw.githubusercontent.com' or target.port not in (None,443):
            raise ValueError('SCSS作者资料发生未登记跳转')
        raw=response.read(4*1024*1024+1)
    if len(raw)!=1907858:raise ValueError('SCSS原始笔记本长度不符')
    import hashlib
    if hashlib.sha1(f'blob {len(raw)}\0'.encode()+raw).hexdigest()!='a88f6fd8a59efed57ffcd5a79a1f66496df187f6':
        raise ValueError('SCSS原始笔记本身份不符')
    return raw,{'url':url,'commit':'d70e09c0187376ab1ff983a6ab864e7d089635f5','git_blob':'a88f6fd8a59efed57ffcd5a79a1f66496df187f6'}


def download_aia_synoptic(nominal,channel):
    """One registered historical training-input audit, not arbitrary remote assets."""
    from datetime import datetime,timezone
    from urllib.parse import urlparse
    from urllib.request import Request,urlopen
    if channel not in ('171','193','211'):raise ValueError('未登记的AIA通道')
    try:stamp=datetime.strptime(nominal,'%Y%m%d_%H%M').replace(tzinfo=timezone.utc)
    except (ValueError,TypeError):raise ValueError('名义时刻格式无效') from None
    if not 2010<=stamp.year<=2026 or stamp.minute%2:raise ValueError('非登记历史时刻或两分钟网格')
    filename=f'AIA{nominal}_0{channel}.fits'
    url=f'https://jsoc1.stanford.edu/data/aia/synoptic/{stamp:%Y/%m/%d}/H{stamp:%H}00/{filename}'
    with urlopen(Request(url,headers={'User-Agent':'MDVA-local-research/1.0'}),timeout=25) as response:
        target=urlparse(response.geturl())
        if target.scheme!='https' or target.hostname!='jsoc1.stanford.edu' or target.port not in (None,443):raise ValueError('AIA资料发生未登记跳转')
        length=response.headers.get('Content-Length');raw=response.read(8*1024*1024+1)
    if len(raw)>8*1024*1024 or length is not None and len(raw)!=int(length):raise ValueError('AIA下载长度异常')
    if not raw.startswith(b'SIMPLE  '):raise ValueError('响应不是登记的FITS文件')
    return raw,{'url':url,'filename':filename,'nominal_utc':stamp.isoformat(),'channel':channel,
        'kind':'historical definitive synoptic, not an operational NRT availability claim'}


def download_xrs_coverage_year(satellite,year,filename=''):
    """Registered annual equivalents; avoid thousands of repeated daily requests."""
    import re
    from urllib.parse import urlparse
    from urllib.request import Request,urlopen
    if type(satellite) is not int or satellite not in range(13,20):raise ValueError('未核验的GOES卫星')
    if type(year) is not int or year not in range(2010,2025):raise ValueError('历史年份无效')
    if not isinstance(filename,str):raise ValueError('数据文件名无效')
    if satellite<16:
        base=('https://www.ncei.noaa.gov/data/goes-space-environment-monitor/access/'
              f'science/xrs/goes{satellite}/xrsf-l2-avg1m_science/')
    else:
        base=('https://data.ngdc.noaa.gov/platforms/solar-space-observing-satellites/goes/'
              f'goes{satellite}/l2/data/xrsf-l2-avg1m_science/')
    pattern=rf'sci_xrsf-l2-avg1m_g{satellite:02d}_y{year}_v(\d+)-(\d+)-(\d+)\.nc'
    def fetch_year(url,limit):
        with urlopen(Request(url,headers={'User-Agent':'MDVA-local-research/1.0'}),timeout=25) as response:
            target=urlparse(response.geturl())
            if (target.scheme!='https' or target.hostname not in ('data.ngdc.noaa.gov','www.ncei.noaa.gov')
                or target.port not in (None,443) or target.username or target.password):raise ValueError('历史数据发生未登记的跨站跳转')
            length=response.headers.get('Content-Length') if hasattr(response,'headers') else None
            raw=response.read(limit+1)
        if len(raw)>limit:raise ValueError('年度观测资料超过64MiB限制')
        if length is not None and len(raw)!=int(length):raise OSError('年度资料下载被截断，未缓存，请重试')
        return raw
    if not filename:
        listing=fetch_year(base,2*1024*1024).decode('utf-8')
        options=[(tuple(map(int,m.groups())),name)
                 for name in re.findall(r'href="([^"/]+\.nc)"',listing)
                 if (m:=re.fullmatch(pattern,name))]
        if not options:raise FileNotFoundError('官方目录未列出所选年度科学产品')
        filename=max(options)[1]
    if not re.fullmatch(pattern,filename):raise ValueError('年度文件不属于所选卫星和年份')
    return fetch_year(base+filename,64*1024*1024),{'url':base+filename,'filename':filename,
        'satellite':satellite,'year':year,'scope':'historical science supervision, not operational availability'}


_instances={};_instances_lock=threading.Lock()
def instance(workspaces):
    key=str(workspaces.root.resolve())
    with _instances_lock:
        if key not in _instances:
            out=workspaces.out_dir or workspaces.root.parent/'out'
            _instances[key]=Monitor(workspaces.root.parent/'.mdva',Path(out)/'space_weather',os.environ.get('MDVA_MONITOR_ONLINE')=='1')
        return _instances[key]

def dispatch(workspaces,method,path,p):
    from .routes import Response,Image
    service=instance(workspaces)
    try:
        if method=='POST' and path=='sync':return Response.of(service.synchronize(p.get('source')))
        if method=='POST' and path=='pin':return Response.of(service.pin_run(p.get('run','')))
        if method=='POST' and path=='magnetosphere/model':return Response.of(service.magnetic_start(p))
        if method!='GET':return Response.of({'error':'仅支持GET或sync POST'},405)
        as_of=stamp(p.get('as_of'))
        if p.get('as_of') and as_of is None:raise ValueError('无效的回放时间')
        if path=='series':return Response.of(service.series(p.get('metric','speed'),stamp(p.get('start')) or time.time()-86400,stamp(p.get('end')) or time.time(),as_of))
        if path=='assets':
            raw,mime=service.asset_read(p.get('id',''));return Response.of(Image(raw,mime))
        if path=='solar/image':return Response.of(service.solar_image(p.get('channel','304'),stamp(p.get('time')),latest=p.get('latest') in ('true','1')))
        if path=='magnetosphere/frames':return Response.of({'frames':service.latest('geospace_'+str(p.get('parameter','velocity')),as_of) or []})
        if path=='magnetosphere/model':
            rows=service.rows('SELECT * FROM physical_model WHERE id=?',(p.get('id',''),))
            if not rows:raise KeyError('未知磁场任务')
            row=rows[0];row['result']=json.loads(row.pop('payload'));return Response.of(row)
        if path=='jobs':return Response.of({'jobs':service.rows('SELECT * FROM job ORDER BY created DESC LIMIT 100')})
        data=service.summary(as_of)
        if path=='summary':return Response.of(data)
        if path=='events':
            search=str(p.get('search','')).lower();page=max(1,int(p.get('page',1)));size=max(1,min(100,int(p.get('page_size',30))))
            events=[e for e in data['events'] if search in json.dumps(e,ensure_ascii=False).lower()]
            events.sort(key=lambda e:e.get('startTime',''),reverse=True)
            return Response.of({'events':events[(page-1)*size:page*size],'total':len(events),'page':page})
        if path in ('sources','observations','alerts'):return Response.of({path:data[path]})
        if path=='forecasts':return Response.of({'forecast':data['forecast']})
        if path=='propagation/runs':return Response.of({'runs':service.rows("SELECT run,MIN(valid) first,MAX(valid) last,COUNT(*) frames FROM asset WHERE run LIKE 'enlil%' AND seen<=? GROUP BY run ORDER BY last DESC",(as_of or time.time(),))})
        if path=='propagation/frames':return Response.of({'frames':service.rows('SELECT id,run,valid FROM asset WHERE run=? AND seen<=? ORDER BY valid',(p.get('run',''),as_of or time.time()))})
        return Response.of({'error':'未知监测接口'},404)
    except Exception as exc:
        return Response.of({'error':str(exc)},404 if isinstance(exc,(KeyError,FileNotFoundError)) else 400 if isinstance(exc,ValueError) else 502)
