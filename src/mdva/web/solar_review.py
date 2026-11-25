"""Serve completed solar evidence receipts from the local research output."""

from __future__ import annotations

import hashlib
import io
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import BadZipFile


STUDY = 'recognition-improvement-20261118'
REVIEW = 'ch-multisource-review-01'
TEMPORAL = 'ch-neighbor-evidence-02'
PANEL_ID = 'ch-observation-comparison'


def stored(output, folder, name):
    root = Path(output).resolve()
    base = root/STUDY/folder
    path = (base/name).resolve()
    if not path.is_relative_to(base):
        raise FileNotFoundError('复核资料在工作区输出目录之外')
    return path.read_bytes()


def decoded(data, expected=None):
    if expected is not None and hashlib.sha256(data).hexdigest()!=expected:
        raise ValueError('复核资料指纹不符，请重新检查本地报告')
    return json.loads(data)


def source_link(data):
    url = data.get('url','')
    dest = urlsplit(url)
    if (dest.scheme=='https' and dest.netloc=='jsoc1.stanford.edu'
            and dest.path.startswith('/data/aia/synoptic/') and not dest.query and not dest.fragment):
        return url
    return None


def case_evidence(frame, row, records):
    neighbors = []
    for offset, record in sorted(records.items(), key=lambda item:int(item[0])):
        channels = [{'channel':band,'observed_utc':meta.get('observed_utc'),
                     'url':source_link(meta)} for band,meta in record['files'].items()]
        neighbors.append({'offset_hours':int(offset),'state':record['state'],
                          'nominal_utc':record['nominal_utc'],'channels':channels})
    return {'id':f"{row['candidate']}:{frame['time_utc']}",
            'observed_utc':frame['time_utc'],'state':'high_suspicion',
            'ratios':{band:row['candidate_to_local_background'][band] for band in ('193','211')},
            'dominant_polarity':row['field_dominant_sign'],'dominant_share':row['field_dominant_share'],
            'significant_share':row['field_significant_share'],'neighbors':neighbors}


def review_records(output):
    main = decoded(stored(output,REVIEW,'summary.json'))
    temporal = decoded(stored(output,TEMPORAL,'summary.json'))
    if any(r.get('state')!='completed' or r.get('test_accessed') for r in (main,temporal)):
        raise ValueError('复核案例状态无效')
    reviewed = decoded(stored(output,REVIEW,'cases.json'),main['cases_sha256'])
    followup = decoded(stored(output,TEMPORAL,'cases.json'),temporal['cases_sha256'])
    return main, temporal, reviewed, {(r['uri'],r['candidate']):r['neighbors'] for r in followup}


def evidence(output):
    try:
        main, temporal, reviewed, followup = review_records(output)
    except FileNotFoundError:
        return {'available':False,'status':'暂无已完成的多图复核案例'}
    cases = [case_evidence(frame,row,followup.get((frame['uri'],row['candidate']),{}))
             for frame in reviewed for row in frame['candidates'] if row['state']=='high_suspicion']
    if len(cases)!=main['counts'].get('high_suspicion',0) or len(cases)!=temporal['candidates']:
        raise ValueError('前后两次复核的候选数量不一致')
    retention = temporal['retention_audit']
    return {'available':True,'scope':'historical_research','asset_id':PANEL_ID,
            'images':main['images'],'remaining_review':main['counts'].get('needs_review',0),
            'existing_match_count':retention['matched'],'existing_gate_pass_count':retention['would_retain'],
            'cases':cases}


def same_component(component, current):
    """Require the same pixels and no new connection to this eight-connected region."""
    import numpy as np

    if not component.any() or not current[component].all():
        return False
    h, w = component.shape
    padded = np.pad(component, 1)
    neighborhood = component.copy()
    for y in range(3):
        for x in range(3):
            neighborhood |= padded[y:y+h,x:x+w]
    return not (current & neighborhood & ~component).any()


def result_evidence(output, job, value, model_card, output_mask):
    """Carry archived physical evidence over only to an identical current region."""
    import numpy as np
    from PIL import Image

    def status(state, message, cases=()):
        return {'state':state,'message':message,'scope':'registered_historical_observations','cases':list(cases)}

    try:
        main, _, reviewed, followup = review_records(output)
        frame = next((r for r in reviewed if r['aia_display_sha256']==value['image_sha256']),None)
        if frame is None:
            return status('unregistered_image','本地图像档案中没有与这张原图完全相同的多通道观测记录。')
        supplied = job['input_metadata'].get('observed_utc')
        if not supplied:
            return status('missing_time','找到相同原图；填写并核对观测时间后才能关联多图证据。')
        delta = datetime.fromisoformat(supplied) - datetime.fromisoformat(frame['time_utc'])
        if abs(delta.total_seconds()) > 60:
            return status('time_conflict','填写的观测时间与相同原图的档案时间不符，请核对时间与时区。')

        policy = decoded(stored(output,REVIEW,'protocol.json'),main['protocol_sha256'])
        card = decoded(model_card.read_bytes(),policy['model_package_sha256'])
        if policy['threshold']!=.5 or card['weights_sha256']!=job['weights_sha256']:
            raise ValueError('模型身份与复核记录不符')
        if not frame['hmi_array_sha256'] or not {'193','211','hmi'}.issubset(frame['channels']):
            return status('insufficient_channels','找到相同原图，但缺少已核验的同期 AIA193、AIA211 或 HMI 磁图。')
        high = [r for r in frame['candidates'] if r['state']=='high_suspicion' and r['reference_overlap_pixels']==0]
        if not high:
            return status('no_supported_candidate','这张观测目前没有满足条件的额外高度疑似范围。')

        name = frame['mask_artifact']
        if not re.fullmatch(r'candidate-masks/[a-f0-9]{24}\.npz',name):
            raise ValueError('复核掩膜资源无效')
        raw = stored(output,REVIEW,name)
        if hashlib.sha256(raw).hexdigest()!=frame['mask_sha256']:
            raise ValueError('复核掩膜内容已变更')
        mask = output_mask()
        if hashlib.sha256(mask).hexdigest()!=value['assets']['candidate-mask.png']:
            raise ValueError('当前候选掩膜校验失败')
        with np.load(io.BytesIO(raw),allow_pickle=False) as group, Image.open(io.BytesIO(mask)) as image:
            components = group['components']
            current = np.asarray(image)>0
        if current.shape != (value['height'],value['width']) or components.shape != current.shape:
            raise ValueError('当前候选与复核记录尺寸不同')

        cases = []
        for row in high:
            neighbors = followup.get((frame['uri'],row['candidate']),{})
            if (not any(item['state']=='darkness_persists' for item in neighbors.values())
                    or not same_component(components==row['candidate'],current)):
                continue
            cases.append(case_evidence(frame,row,neighbors) | {'bbox_xyxy':row['bbox_xyxy'],'pixels':row['pixels']})
        if cases:
            return status('matched','已核对原图、观测时间和候选位置。下列范围有多通道和前后观测支持，需人工确认。',cases)
        return status('candidate_unverified','当前候选与档案证据未通过核对，保留模型输出供复核。')
    except (OSError, ValueError, KeyError, TypeError, BadZipFile):
        return status('record_unavailable','本地复核资料或模型身份未通过校验。')


def asset(output, identity):
    if identity!=PANEL_ID:
        raise FileNotFoundError('图片证据不存在')
    audit = decoded(stored(output,REVIEW,'visual-audit.json'))
    study = decoded(stored(output,REVIEW,'summary.json'))
    raw = stored(output,REVIEW,'multi-observation-cases.png')
    if (audit['case_sha256']!=study['cases_sha256'] or hashlib.sha256(raw).hexdigest()!=audit['figure_sha256']
            or not raw.startswith(b'\x89PNG\r\n\x1a\n')):
        raise ValueError('对照图指纹不符，请重新检查本地报告')
    return raw
