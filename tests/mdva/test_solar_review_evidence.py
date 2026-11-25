import hashlib
import io
import json

import numpy as np
import pytest
from PIL import Image

from mdva.web.solar_review import PANEL_ID, REVIEW, STUDY, TEMPORAL, asset, evidence, result_evidence


def record(folder, name, value):
    folder.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value).encode()
    (folder / name).write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def test_evidence_keeps_unknown_followup_separate_and_only_serves_registered_image(tmp_path):
    panel = tmp_path / STUDY / REVIEW
    temporal = tmp_path / STUDY / TEMPORAL
    reviewed_sha = record(panel, 'cases.json', [{'uri': 'case', 'time_utc': '2014-07-08T03:00:00Z',
        'candidates': [{'candidate': 1, 'state': 'high_suspicion',
            'candidate_to_local_background': {'193': .41, '211': .37},
            'field_dominant_sign': 'negative', 'field_dominant_share': .84, 'field_significant_share': .27}]}])
    followup_sha = record(temporal, 'cases.json', [{'uri': 'case', 'candidate': 1, 'neighbors': {
        '2': {'state': 'alignment_uncertain', 'nominal_utc': '2014-07-08T05:00:00Z', 'files': {
            '193': {'url': 'https://example.com/wrong', 'observed_utc': '2014-07-08T05:00:07Z'}
        }}}}])
    record(panel, 'summary.json', {'state': 'completed', 'test_accessed': False,
        'cases_sha256': reviewed_sha, 'images': 32, 'counts': {'high_suspicion': 1, 'needs_review': 91}})
    record(temporal, 'summary.json', {'state': 'completed', 'test_accessed': False,
        'candidates': 1, 'cases_sha256': followup_sha, 'retention_audit': {'matched': 56, 'would_retain': 4}})
    card = evidence(tmp_path)
    assert card['cases'][0]['neighbors'][0]['state'] == 'alignment_uncertain'
    assert card['cases'][0]['neighbors'][0]['channels'][0]['url'] is None
    raw = b'\x89PNG\r\n\x1a\nfixture'
    (panel / 'multi-observation-cases.png').write_bytes(raw)
    record(panel, 'visual-audit.json', {'case_sha256': reviewed_sha, 'figure_sha256': hashlib.sha256(raw).hexdigest()})
    assert asset(tmp_path, PANEL_ID) == raw
    with pytest.raises(FileNotFoundError):
        asset(tmp_path, '../cases.json')
    (panel / 'multi-observation-cases.png').write_bytes(b'changed')
    with pytest.raises(ValueError):
        asset(tmp_path, PANEL_ID)


def test_workspace_without_studies_returns_no_cases(tmp_path):
    assert evidence(tmp_path)['available'] is False


@pytest.mark.parametrize(('time','image_hash','change','expected'),[
    ('2014-07-08T03:00:00+00:00','input','elsewhere','matched'),
    ('2014-07-08T03:00:00+00:00','input','join','candidate_unverified'),
    ('2014-07-08T03:00:00+00:00','input','omit','candidate_unverified'),
    ('2014-07-08T03:00:00+00:00','input','corrupt','record_unavailable'),
    ('2014-07-08T03:00:00+00:00','another',None,'unregistered_image'),
    ('2014-07-09T03:00:00+00:00','input',None,'time_conflict'),
    (None,'input',None,'missing_time'),
])
def test_uploaded_image_only_inherits_evidence_for_exact_region(tmp_path,time,image_hash,change,expected):
    panel = tmp_path / STUDY / REVIEW
    temporal = tmp_path / STUDY / TEMPORAL
    model_sha = record(tmp_path,'model.json',{'weights_sha256':'weights'})
    protocol_sha = record(panel,'protocol.json',{'model_package_sha256':model_sha,'threshold':.5})
    mask = np.zeros((8,8),dtype=np.uint8)
    mask[2:4,2:4] = 1
    mask_path = panel/'candidate-masks'/('a'*24+'.npz')
    mask_path.parent.mkdir()
    np.savez_compressed(mask_path,components=mask)
    reviewed_sha = record(panel,'cases.json',[{'uri':'case','time_utc':'2014-07-08T03:00:00+00:00',
        'aia_display_sha256':'input','hmi_array_sha256':'hmi','channels':['193','211','hmi'],
        'mask_artifact':mask_path.relative_to(panel).as_posix(),
        'mask_sha256':hashlib.sha256(mask_path.read_bytes()).hexdigest(),
        'candidates':[{'candidate':1,'state':'high_suspicion','reference_overlap_pixels':0,
            'bbox_xyxy':[2,2,4,4],'pixels':4,'candidate_to_local_background':{'193':.41,'211':.37},
            'field_dominant_sign':'positive','field_dominant_share':.84,'field_significant_share':.27}]}])
    followup_sha = record(temporal,'cases.json',[{'uri':'case','candidate':1,'neighbors':{
        '-2':{'state':'darkness_persists','nominal_utc':'2014-07-08T01:00:00+00:00','files':{}}}}])
    record(panel,'summary.json',{'state':'completed','cases_sha256':reviewed_sha,'protocol_sha256':protocol_sha})
    record(temporal,'summary.json',{'state':'completed','cases_sha256':followup_sha})
    if change=='elsewhere':mask[7,7]=1
    if change=='join':mask[4,4]=1
    if change=='omit':mask[3,3]=0
    buf = io.BytesIO()
    Image.fromarray(mask*255).save(buf,format='PNG')
    raw = buf.getvalue()
    value = {'image_sha256':image_hash,'width':8,'height':8,
             'assets':{'candidate-mask.png':hashlib.sha256(raw).hexdigest()}}
    job = {'weights_sha256':'weights','input_metadata':{'observed_utc':time}}
    review = result_evidence(tmp_path,job,value,tmp_path/'model.json',lambda:raw if change!='corrupt' else b'changed')
    assert review['state']==expected
    if expected=='matched':
        assert review['cases'][0]['bbox_xyxy']==[2,2,4,4]
    else:
        assert review['cases']==[]
