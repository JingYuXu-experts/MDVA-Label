import hashlib
import json

import numpy as np
import openpyxl
from PIL import Image
import pytest

from mdva.training.provenance import observation, resolve_noaa, isolate_groups, split_for, segmentation_split
from mdva.training.prepare import Preparation
from mdva.training.datasets import FEATURES, read_frozen, resolve_uri, freeze_feature_preprocessing


def test_observation_uses_complete_patterns_and_tai_conversion():
    assert observation('hmi.m_720s.20120101_000000_TAI.1.magnetogram.jpg')['time_utc'].startswith('2011-12-31T23:59:26')
    assert observation('sHrHa220802022042fullc.jpg')['time_utc']=='2022-08-02T02:20:42+00:00'
    assert observation('HrHa2311290523fullo.jpg')['instrument']=='HSOS_Halpha'
    assert observation('kanz_halph_fd_20121231_083109.jpg')['date']=='2012-12-31'
    assert observation('mdi.fd_m_96m_lev182.19960421_000000_TAI.data.jpg')['instrument']=='MDI'
    with pytest.raises(ValueError): observation('arbitrary_20922032_bad.jpg')
    with pytest.raises(ValueError): observation('AIA20230231_0000_0193.jpg')


def test_noaa_resolution_requires_unique_contemporary_evidence():
    assert resolve_noaa('1067', {11067,11064})==(11067,'short_id_matched_nearby_reference')
    assert resolve_noaa('1067', set())[0] is None
    assert resolve_noaa('1067', {11067,21067})[0] is None
    assert resolve_noaa('11067.0', set())[0]==11067
    assert resolve_noaa('0', {11067})[0] is None


def test_split_quarantines_groups_instead_of_moving_future_to_train():
    rows=[{'split':'train','noaa':11067},{'split':'test','noaa':11067},{'split':'test','noaa':13001}]
    assert isolate_groups(rows, lambda r: [r['noaa']])==1
    assert rows[0]['exclusions'] and rows[1]['exclusions']
    assert 'exclusions' not in rows[2]
    assert rows[1]['split']=='test'
    assert split_for('2021-01-01T00:00:00+00:00').startswith('excluded')
    assert segmentation_split('2020-07-01T00:00:00+00:00','CH')=='validation'
    assert segmentation_split('2020-07-01T00:00:00+00:00','AR')=='train'


def test_feature_statistics_never_fit_on_test_or_identity_fields(tmp_path):
    rows=[]
    for split,value in [('train',2.),('train',4.),('test',1000000.)]:
        rows.append({'split':split,'eligible':True,'target_m_plus':0,
                     'features':{**{k:value for k in FEATURES},'NOAA_AR':99999,'provided_FI':1000000}})
    raw='\n'.join(json.dumps(r) for r in rows).encode()
    (tmp_path/'forecast-ready.jsonl').write_bytes(raw)
    (tmp_path/'manifest-hashes.json').write_text(json.dumps({'forecast-ready.jsonl':hashlib.sha256(raw).hexdigest()}))
    config=freeze_feature_preprocessing(tmp_path)
    assert config['fit_rows']==2 and config['mean']==[3.]*len(FEATURES)
    assert 'NOAA_AR' not in config['features'] and not config['classifier_trained']


def test_prepare_joins_actual_instances_and_keeps_sources_immutable(tmp_path):
    root=tmp_path/'只读太阳资料'
    for cat in ('AR','CH','FL'):
        for leaf in (cat+'_image',cat+'_npy'): (root/'Situation Awareness Dataset'/cat/leaf).mkdir(parents=True)
    (root/'Standardized Solar Observation Image Dataset').mkdir()
    (root/'Code').mkdir()
    forecast=root/'Solar Flare Forecasting Dataset'
    (forecast/'AR_parameters').mkdir(parents=True)
    stem='hmi.m_720s.20230211_000000_TAI.3.magnetogram'
    image=root/'Situation Awareness Dataset/AR/AR_image'/(stem+'.jpg')
    Image.fromarray(np.arange(64,dtype=np.uint8).reshape(8,8)).save(image)
    mask=np.zeros((8,8),dtype=np.int32);mask[2:6,2:6]=2
    np.save(root/'Situation Awareness Dataset/AR/AR_npy'/(stem+'.npy'),mask)
    mapping=openpyxl.Workbook();sheet=mapping.active;sheet.append(['FILENAME','MASK_NUM','NOAA_AR'])
    sheet.append([stem+'.jpg',2,13208]);mapping.save(root/'Situation Awareness Dataset/AR/MASK-NOAA.xlsx');mapping.close()
    (forecast/'AR_parameters'/(stem+'.txt')).write_text('Num\tNOAA_AR\tMGB_PIL\tR\tMGB\tTotal_Unsigned_Flux\tArea\tHale\tMcIntosh\n1\t13207\t1\t2\t3\t100\t16\t\t\n2\t3208\t1\t2\t3\t100\t16\tBeta\tBxo\n',encoding='utf-8')
    for directory in ['mask_flare_index_1024','mask_flare_label_1024/C-class or above','mask_flare_label_1024/M-class or above','mask_flare_label_1024/X-class']:
        (forecast/directory).mkdir(parents=True)
        data=(mask==2).astype(np.uint8) if '/C-' in directory or '/M-' in directory else np.zeros_like(mask)
        np.save(forecast/directory/(stem+'.npy'),data)
    book=openpyxl.Workbook();sheet=book.active
    sheet.append(['STARTHOUR','STARTMIN','DAY','ENDHOUR',None,'ENDMIN',None,'PEAKHOUR','PEAKMIN','CLASS','NOAA_NUM'])
    sheet.append([1,0,20230210,1,None,30,None,1,15,'B1.0',3208])
    # An in-progress event at T must not contribute its future peak class to past features.
    sheet.append([23,50,20230210,0,None,30,None,0,15,'M2.0',3208])
    sheet.append([12,0,20230211,12,None,30,None,'//','//','M1.2',3208])
    book.save(forecast/'flare list.xlsx');book.close()
    before={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    output=tmp_path/'prepared';report=Preparation(root,output).run()
    assert {str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}==before
    rows=[json.loads(s) for s in (output/'forecast-all.jsonl').read_text(encoding='utf-8').splitlines()]
    missing=next(r for r in rows if r['instance_id']==1)
    assert 'parameter_instance_absent_in_mask' in missing['exclusions']
    good=next(r for r in rows if r['instance_id']==2)
    assert good['eligible'] and good['target_m_plus']==1
    assert good['noaa']==13208 and good['noaa_raw']=='3208'
    assert good['features']['past24_completed_M']==0
    assert not {'Num','NOAA_AR','Hale','McIntosh','provided_FI'} & good['features'].keys()
    assert report['source_stat_changes']==0 and not report['training_started']
    assert len(report['forecast']['ready_by_split'])==1
    from mdva.training.validate import validate
    with pytest.raises(ValueError,match='空划分'): validate(output)
    assert read_frozen(output,'forecast-ready.jsonl')[0]['instance_id']==2
    assert set(FEATURES)=={'MGB_PIL','R','MGB','Total_Unsigned_Flux','Area',
                          'MGB_PIL_missing','R_missing','MGB_missing','Total_Unsigned_Flux_missing','Area_missing'}
    sources=json.loads((output/'sources.json').read_text(encoding='utf-8'))
    with pytest.raises(ValueError): resolve_uri('@ssafd/../../escape.jpg',sources)
    with pytest.raises(ValueError): resolve_uri(str(image),sources)
    with (output/'forecast-ready.jsonl').open('a',encoding='utf-8') as stream:stream.write('{}\n')
    with pytest.raises(ValueError,match='冻结'):read_frozen(output,'forecast-ready.jsonl')
    with pytest.raises(ValueError,match='原始数据'): Preparation(root,root/'output')
    with pytest.raises(ValueError,match='冻结'): Preparation(root,output)
