"""Check CH review candidates against new AIA observations and reference retention."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
import warnings

from astropy.io import fits
import cv2
import numpy as np

from .aia_source_probe import ones_sum
from .ch_dense_multichannel import cards
from .ch_matched_tiles import CORE, RADIUS
from .ch_multisource_review import CONTRAST, audited, fetch_arrays
from .durable import atomic_json, commit
from .hmi_angular_projection import local_geometry
from .journal import read
from .runtime import configure_environment, digest, now
from .xrs_coverage import utc
from ..web.space_weather import download_aia_synoptic


OFFSETS = (-2,2)
CHANNELS = ('193','211')
MAX_SHIFT = 10


def recount(cases):
    totals = Counter()
    for case in cases:
        objects = {r['candidate']:r for r in case['candidates']}
        for match in case['known_reference_metrics']['matching_regions']:
            row = objects[match['candidate']]
            totals['matched'] += 1
            totals['with_magnetogram'] += int('hmi' in case['channels'])
            totals['verified_spatial_support'] += int(row['spectral_state']!='outside_verified_support')
            totals['would_retain'] += int(
                row['spectral_state']=='coincident_darkness' and row['field_state']=='dominant_polarity')
    return dict(totals)


def highpass(x):
    z = np.log1p(np.maximum(np.where(np.isfinite(x),x,0.),0.)).astype(np.float32)
    return z-cv2.GaussianBlur(z,(0,0),4.)


def local_shift(first, second, component):
    """Fit the offset on surrounding texture, excluding the candidate itself."""
    img,other = highpass(first),highpass(second)
    y,x = np.where(component)
    y0,y1 = max(MAX_SHIFT,y.min()-22),min(512-MAX_SHIFT,y.max()+23)
    x0,x1 = max(MAX_SHIFT,x.min()-22),min(512-MAX_SHIFT,x.max()+23)
    bounds = np.s_[y0:y1,x0:x1]
    yy,xx = np.nonzero((~component & (RADIUS<=CORE) & np.isfinite(first))[bounds])
    yy,xx = yy+y0,xx+x0
    if len(xx)<128:
        return {'state':'insufficient_texture'}
    matches = []
    for dy in range(-MAX_SHIFT,MAX_SHIFT+1):
        for dx in range(-MAX_SHIFT,MAX_SHIFT+1):
            chosen = np.isfinite(second[yy+dy,xx+dx])
            if chosen.mean()<.94:
                continue
            a,b = img[yy[chosen],xx[chosen]],other[yy[chosen]+dy,xx[chosen]+dx]
            a,b = a-a.mean(),b-b.mean()
            denom = np.sqrt(np.dot(a,a)*np.dot(b,b))
            if denom>1e-6:
                matches.append((float(np.dot(a,b)/denom),dy,dx))
    if not matches:
        return {'state':'insufficient_texture'}
    corr,dy,dx = max(matches)
    rival = max((v for v,oy,ox in matches if max(abs(oy-dy),abs(ox-dx))>2),default=-1.)
    success = corr>=.60 and corr-rival>=.02 and max(abs(dx),abs(dy))<MAX_SHIFT
    return {'state':'registered' if success else 'alignment_uncertain',
            'offset_yx':[dy,dx],'correlation':corr,'margin':corr-rival,'pixels':len(xx)}


def retrieve(folder, stamp, channel):
    filename = f"AIA{stamp:%Y%m%d_%H%M}_0{channel}.fits"
    target = folder/filename
    path = target.with_suffix('.source.json')
    try:
        if target.is_file():
            info = read(path)
            if not info or digest(target)!=info['sha256']:
                raise ValueError('已有AIA归档指纹不可核验')
            original = target.read_bytes()
        else:
            original,info = download_aia_synoptic(stamp.strftime('%Y%m%d_%H%M'),channel)
        from io import BytesIO
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            with fits.open(BytesIO(original),disable_image_compression=True,memmap=False) as hdus:
                checks = []
                for h in hdus:
                    if 'CHECKSUM' in h.header:
                        offsets = h.fileinfo()
                        data = original[offsets['hdrLoc']:offsets['datLoc']+offsets['datSpan']]
                        checks.append(ones_sum(data)==0xffffffff and h.verify_datasum()==1)
                if not checks or not all(checks):
                    raise ValueError('AIA完整性校验失败')
            with fits.open(BytesIO(original),memmap=False) as hdus:
                image = next(h for h in hdus if h.data is not None and h.data.ndim==2)
                header = image.header
                observed = utc(header['T_OBS'].replace('Z','+00:00'))
                if (str(header['WAVELNTH'])!=channel or header['QUALITY']!=0 or header['LVL_NUM']!=1.5
                        or abs((observed-stamp).total_seconds())>60):
                    raise ValueError('不符合通道、质量或时刻门槛')
                grid = [header[k] for k in ('CRPIX1','CRPIX2','CDELT1','CDELT2','CRVAL1','CRVAL2','CROTA2','CTYPE1','CTYPE2','RSUN_OBS')]
                arrays = local_geometry(image.data.astype(np.float64),header)
        if not target.is_file():
            commit(target,lambda fd:fd.write(original))
            info['sha256'] = digest(target)
            atomic_json(path,info)
        return {'state':'ready','observed_utc':observed.isoformat(),'url':info['url'],
                'sha256':info['sha256'],'grid':grid},arrays
    except (ValueError,OSError,KeyError) as exc:
        return {'state':'unavailable','error_type':type(exc).__name__,'reason':str(exc)},None


def persistence(mask, affected, frames, registration):
    dy,dx = registration['offset_yx']
    y,x = np.where(mask)
    coords = (y+dy,x+dx)
    if not np.all((coords[0]>=0)&(coords[0]<512)&(coords[1]>=0)&(coords[1]<512)):
        return {'state':'off_grid'}
    y,x = np.where(affected)
    context = (y+dy,x+dx)
    ratios = {}
    for channel in CHANNELS:
        values,background = frames[channel][coords],frames[channel][context]
        if (np.isfinite(values).mean()<.95 or np.isfinite(background).mean()<.95
                or np.nanmedian(background)<=0):
            return {'state':'insufficient_common_support'}
        ratios[channel] = float(np.nanmedian(values)/np.nanmedian(background))
    return {'state':'darkness_persists' if all(ratios[k]<=v for k,v in CONTRAST.items())
                else 'darkness_does_not_persist_at_cutoffs','ratios':ratios}


def coincident(first, second):
    a,b = first['grid'],second['grid']
    # The observed apparent radius can change slightly between channels a few seconds apart.
    # Keep the discrete display crop and all actual WCS placement cards identical.
    return (a[:-1]==b[:-1] and abs(a[-1]-b[-1])<=.05
            and int(a[-1]/a[2]*1.1)==int(b[-1]/b[2]*1.1)
            and abs((utc(first['observed_utc'])-utc(second['observed_utc'])).total_seconds())<=60)


def run(input_report, source, output, archive=None):
    if output.exists():
        raise ValueError('相邻实测使用新目录')
    summary = read(input_report/'summary.json')
    if summary['test_accessed'] or digest(input_report/'cases.json')!=summary['cases_sha256']:
        raise ValueError('仅接受已核验的未标候选名单')
    cases = read(input_report/'cases.json')
    previous = list(cards(source,source/'selection.json'))
    history = {r['uri']:(r,a,b) for r,a,b in previous}
    magnet,_ = audited(source,'angular')
    headers,_ = audited(source,'hmi')
    selected = [(r,v) for r in cases for v in r['candidates'] if v['state']=='high_suspicion']
    schedule = [(utc(r['time_utc'])+timedelta(hours=delta),wave)
                for r,_ in selected for delta in OFFSETS for wave in CHANNELS]
    atomic_json(output/'protocol.json',{'code_sha256':digest(__file__),'prior_sha256':digest(input_report/'summary.json'),
        'targets':[{'uri':r['uri'],'candidate':v['candidate']} for r,v in selected],'offset_hours':OFFSETS,
        'channels':CHANNELS,'registration':'local AIA193 context excluding the candidate; same shift applied to 211',
        'same_epoch_grid':'exact WCS placement and display crop, apparent solar radius tolerance 0.05 arcsec',
        'test_accessed':False})

    def frame(request):
        stamp, channel = request
        name = f"AIA{stamp:%Y%m%d_%H%M}_0{channel}.fits"
        folder = archive if archive and (archive/name).is_file() else output/'fits'
        return retrieve(folder,stamp,channel)

    with ThreadPoolExecutor(max_workers=6) as pool:
        found = dict(zip(schedule,pool.map(frame,schedule),strict=True))
    results = []
    for row,candidate in selected:
        source_row,original,additional = history[row['uri']]
        current = fetch_arrays(source,source_row,original,additional,magnet,headers)
        artifact = input_report/row['mask_artifact']
        if digest(artifact)!=row['mask_sha256']:
            raise ValueError('先前区域位置已改变')
        with np.load(artifact,allow_pickle=False) as masks:
            objects = masks['components']
            part = objects==candidate['candidate']
        ring = cv2.distanceTransform((~part).astype('uint8'),cv2.DIST_L2,cv2.DIST_MASK_PRECISE)
        local = (ring>=3)&(ring<=16)&(objects==0)&(RADIUS<=CORE)&np.isfinite(current['193'])
        entry = {'uri':row['uri'],'candidate':candidate['candidate'],'neighbors':{}}
        for delta in OFFSETS:
            time = utc(row['time_utc'])+timedelta(hours=delta)
            coverage = {k:found[(time,k)][0] for k in CHANNELS}
            result = {'nominal_utc':time.isoformat(),'files':coverage,'state':'source_missing'}
            if all(x['state']=='ready' for x in coverage.values()):
                a,b = (coverage[k] for k in CHANNELS)
                if not coincident(a,b):
                    result['state']='channels_not_coincident'
                else:
                    arrays = {k:found[(time,k)][1] for k in CHANNELS}
                    registration = local_shift(current['193'],arrays['193'],part)
                    result.update(registration=registration,state=registration['state'])
                    if registration['state']=='registered':
                        result.update(persistence(part,local,arrays,registration))
            entry['neighbors'][str(delta)] = result
        results.append(entry)
    atomic_json(output/'cases.json',results)
    atomic_json(output/'summary.json',{'state':'completed','recorded_utc':now(),'candidates':len(results),
        'retention_audit':recount(cases),'neighbor_states':dict(Counter(v['state'] for r in results for v in r['neighbors'].values())),
        'protocol_sha256':digest(output/'protocol.json'),'cases_sha256':digest(output/'cases.json'),
        'test_accessed':False,'deployed':False})


if __name__=='__main__':
    configure_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('input-report','source','output'):
        parser.add_argument('--'+name,required=True,type=Path)
    parser.add_argument('--archive',type=Path)
    run(**vars(parser.parse_args()))
