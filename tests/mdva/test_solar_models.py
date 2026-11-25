import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from mdva.web import solar_models as models
from mdva.web.services import Hub
from mdva.training.durable import atomic_json
from mdva.training.runtime import digest


@pytest.fixture
def setup(tmp_path,monkeypatch):
    data=tmp_path/'data';images=data/'images';images.mkdir(parents=True)
    Image.new('RGB',(31,23),(90,120,80)).save(images/'normal.png')
    Image.new('RGB',(31,23),(10,10,10)).save(images/'holdout.png')
    out=tmp_path/'out';out.mkdir();exp=out/'training-test';exp.mkdir()
    frozen=tmp_path/'frozen';frozen.mkdir()
    atomic_json(exp/'protocol.json',{'dataset':str(frozen)})
    rows=[{'uri':'images/normal.png','eligible':True,'category':'AR','split':'validation'},
          {'uri':'images/holdout.png','eligible':True,'category':'AR','split':'test'}]
    (frozen/'segmentation-ready.jsonl').write_text('\n'.join(json.dumps(r) for r in rows))
    (frozen/'source-files.jsonl').write_text('\n'.join(json.dumps({'uri':r['uri'],'sha256':digest(data/r['uri'])}) for r in rows))
    hub=Hub(data/'mdva.sqlite',out_dir=out,data_root=data)
    dataset=hub.store.create_dataset('images','segmentation',images)
    version=hub.store.put_version(dataset,None,'import')
    monkeypatch.setattr(models,'experiment',lambda _out:exp)
    monkeypatch.setattr(models,'inventory',lambda _out:{'models':[{'id':'AR-A0-s42','category':'AR',
        'condition':'magnetogram','available':True,'scientific_test_complete':False,'weights_sha256':'model-hash'}]})
    monkeypatch.setattr(models,'ensure_worker',lambda _out:None)
    params={'model_id':'AR-A0-s42','condition':'magnetogram','dataset':dataset,'version':version,
            'uris':['images/normal.png'],'request_id':'request_001','acknowledge_research':True}
    yield hub,out,exp,params,images
    hub.close()


def test_membership_condition_version_and_holdout_are_checked_before_job_creation(setup):
    hub,out,exp,params,images=setup
    for patch in ({'condition':'AIA193'},{'version':'missing'},{'uris':['../secret.png']},
                  {'uris':['images/holdout.png']},{'acknowledge_research':False},
                  {'uris':['images/normal.png']*201}):
        with pytest.raises(ValueError):models.create(hub,out,{**params,**patch},spawn=False)
    import shutil
    shutil.copy2(images/'holdout.png',images/'copy.png')
    with pytest.raises(ValueError,match='同源'):models.create(hub,out,{**params,'uris':['images/copy.png']},spawn=False)
    assert not list((out/'solar-inference').glob('*/job.json'))


def test_job_is_idempotent_preserves_context_and_does_not_write_annotations(setup):
    hub,out,exp,params,images=setup
    before=hub.store.list_annotations(params['version'])
    first=models.create(hub,out,params,spawn=False)
    second=models.create(hub,out,params,spawn=False)
    assert first['id']==second['id'] and first['state']=='queued'
    assert 'inputs' not in first and 'workspace_db' not in first and 'package' not in first
    assert hub.store.list_annotations(params['version'])==before
    assert hub.store.get_task(first['task_id'])['kind']=='solar_inference'
    Image.new('RGB',(31,23),(1,2,3)).save(images/'normal.png')
    with pytest.raises(ValueError,match='不同输入'):models.create(hub,out,params,spawn=False)


def test_result_assets_are_registered_hashed_and_reject_changed_inputs(setup,tmp_path):
    hub,out,exp,params,images=setup
    created=models.create(hub,out,params,spawn=False)
    folder=out/'solar-inference'/created['id'];target=folder/'image-000000';target.mkdir()
    job=models.read(folder/'job.json')
    value={'image_sha256':job['inputs'][0]['sha256'],'model':'AR-A0-s42','width':31,'height':23,
           'image':str(images/'normal.png'),'annotations':[],'skipped':[]}
    atomic_json(target/'candidates.json',value)
    Image.new('RGBA',(31,23),(255,0,0,80)).save(target/'candidate-overlay.png')
    job.update(state='completed',completed=1,results=[{'index':0,'uri':params['uris'][0],'state':'completed',
        'result_sha256':digest(target/'candidates.json'),
        'asset_hashes':{'candidate-overlay.png':digest(target/'candidate-overlay.png')}}])
    atomic_json(folder/'job.json',job)
    result=models.result(hub,out,created['id'],0)
    assert 'image' not in result and result['width']==31
    blob,mime=models.asset(hub,out,created['id'],'image-000000/candidate-overlay.png')
    assert blob.startswith(b'\x89PNG') and mime=='image/png'
    for asset in ('../job.json','image-000000/../../secret','https://example.com/image.png'):
        with pytest.raises(ValueError):models.asset(hub,out,created['id'],asset)
    other=Hub(tmp_path/'other/mdva.sqlite',out_dir=out)
    with pytest.raises(FileNotFoundError):models.jobs(other,out,created['id'])
    other.close()
    Image.new('RGB',(31,23),(3,2,1)).save(images/'normal.png')
    with pytest.raises(ValueError,match='原图已变化'):models.result(hub,out,created['id'],0)


def test_polygons_preserve_native_coordinates_and_report_holes_or_degenerate_regions():
    pytest.importorskip('torch')
    from mdva.training.inference import polygons
    probability=np.zeros((31,47),dtype='float32');probability[2:14,4:19]=.8
    probability[17:29,25:44]=.9;probability[20:23,30:33]=0
    probability[30,0]=.9
    anns,skipped=polygons(probability,'CH','CH-C2-s2026')
    assert len(anns)==1 and anns[0]['label']=='CH'
    assert min(anns[0]['coords'][::2])==4 and max(anns[0]['coords'][::2])==18
    assert len(skipped)==2 and any('孔洞' in s['reason'] for s in skipped)
    probability[0,0]=np.nan
    with pytest.raises(ValueError,match='非有限'):polygons(probability,'CH','CH-C2-s2026')


def test_worker_resumes_only_verified_items_and_updates_task_record(setup,monkeypatch):
    torch=pytest.importorskip('torch')
    from mdva.training import solar_worker,inference,runtime
    hub,out,exp,params,images=setup
    Image.new('RGB',(31,23),(80,70,60)).save(images/'second.png')
    created=models.create(hub,out,{**params,'uris':['images/normal.png','images/second.png']},spawn=False)
    folder=out/'solar-inference'/created['id'];job=models.read(folder/'job.json')
    package=exp/'package';package.mkdir();(package/'weights.safetensors').write_bytes(b'weights')
    job['package']=str(package);job['weights_sha256']=digest(package/'weights.safetensors')
    monkeypatch.setattr(inference,'load_model',lambda _p,device:(object(),{'id':'AR-A0-s42'}))
    class Resource:
        def __init__(self,device):pass
        def sample(self):return {}
    monkeypatch.setattr(runtime,'Resources',Resource)
    monkeypatch.setattr(torch.cuda,'empty_cache',lambda:None)
    called=[]
    def predict(_model,_card,image,condition,target,device):
        called.append(str(image));target.mkdir()
        for name in ('candidate-mask.png','candidate-overlay.png','candidate-probability.npy'):
            (target/name).write_bytes(b'registered artifact')
        value={'image_sha256':digest(image),'annotations':[],'skipped':[]}
        atomic_json(target/'candidates.json',value);return value
    monkeypatch.setattr(inference,'predict_loaded',predict)
    solar_worker.execute(folder,job)
    assert len(called)==2 and job['state']=='completed'
    task=hub.store.get_task(job['task_id'])
    assert task['state']=='done' and task['report']['completed']==2
    first_hash=digest(folder/'image-000000/candidates.json')
    # Simulate a process lost after committing item 1. Item 2's old output is
    # preserved as an interrupted directory; item 1 must not be recomputed.
    job['results']=job['results'][:1];job['completed']=1;job['state']='interrupted'
    solar_worker.execute(folder,job)
    assert len(called)==3 and called[-1].endswith('second.png')
    assert digest(folder/'image-000000/candidates.json')==first_hash
    assert len(list(folder.glob('image-000001.interrupted-*')))==1


def decode_rle(encoded):
    flat=np.concatenate([np.full(n,index%2,dtype='uint8') for index,n in enumerate(encoded['counts'])])
    return flat.reshape(encoded['size'],order='F')


def test_machine_regions_keep_holes_single_pixels_diagonal_connectivity_and_native_geometry():
    from mdva.web.solar_products import regions
    mask=np.zeros((23,31),dtype='uint8')
    mask[2:12,3:15]=255;mask[5:8,6:9]=0
    mask[20,29]=255;mask[21,30]=255  # One eight-connected component.
    parts=regions(mask)
    assert len(parts)==2 and parts[0]['has_holes']
    assert parts[0]['bbox']==[3,2,12,10] and parts[0]['area_pixels']==111
    assert parts[1]['area_pixels']==2
    rebuilt=sum(decode_rle(p['segmentation']) for p in parts)
    np.testing.assert_array_equal(rebuilt,mask>0)
    for value in (np.zeros((1,1),dtype='uint8'),np.full((1,1),255,dtype='uint8')):
        parts=regions(value)
        if parts:np.testing.assert_array_equal(decode_rle(parts[0]['segmentation']),value>0)


@pytest.mark.parametrize('self_check', [None, {'state':'needs_review', 'reason_codes':['regions_sensitive_to_exposure']}])
def test_machine_archive_is_direct_lossless_scoped_and_does_not_require_acceptance(setup,tmp_path,self_check):
    import io,zipfile
    hub,out,exp,params,images=setup
    before=hub.store.list_annotations(params['version'])
    created=models.create(hub,out,params,spawn=False)
    with pytest.raises(ValueError,match='等待'):models.annotations_archive(hub,out,created['id'])
    folder=out/'solar-inference'/created['id'];target=folder/'image-000000';target.mkdir()
    job=models.read(folder/'job.json')
    mask=np.zeros((23,31),dtype='uint8');mask[2:20,2:29]=255;mask[5:8,5:8]=0
    Image.fromarray(mask).save(target/'candidate-mask.png')
    candidate={'image_sha256':job['inputs'][0]['sha256'],
        'width':31,'height':23,'annotations':[],'skipped':[{'reason':'has holes'}]}
    if self_check is not None:
        candidate['self_check']=self_check
    atomic_json(target/'candidates.json',candidate)
    job.update(state='completed',completed=1,results=[{'index':0,'state':'completed',
        'result_sha256':digest(target/'candidates.json'),
        'asset_hashes':{'candidate-mask.png':digest(target/'candidate-mask.png')}}])
    atomic_json(folder/'job.json',job)
    raw=models.annotations_archive(hub,out,created['id'])
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        manifest=json.loads(archive.read('manifest.json'))
        coco=json.loads(archive.read('coco.json'))
        assert manifest['status']=='machine_unreviewed' and manifest['forecast']['state']=='not_computed'
        assert manifest['images'][0]['regions'][0]['has_holes']
        if self_check is None:
            assert manifest['images'][0]['self_check']['state']=='unavailable'
        else:
            assert manifest['images'][0]['self_check']==self_check
        np.testing.assert_array_equal(decode_rle(coco['annotations'][0]['segmentation']),mask>0)
        assert archive.read('masks/000001.png')==(target/'candidate-mask.png').read_bytes()
        assert coco['images'][0]['file_name']=='images/000001.png'
        assert archive.read(coco['images'][0]['file_name'])==(images/'normal.png').read_bytes()
        assert str(images) not in archive.read('manifest.json').decode()
    assert hub.store.list_annotations(params['version'])==before
    other=Hub(tmp_path/'other/mdva.sqlite',out_dir=out)
    try:
        with pytest.raises(FileNotFoundError):models.annotations_archive(other,out,created['id'])
    finally:other.close()
    Image.new('L',(31,23),255).save(target/'candidate-mask.png')
    with pytest.raises(ValueError,match='校验'):models.annotations_archive(hub,out,created['id'])
