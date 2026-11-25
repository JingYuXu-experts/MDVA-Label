import base64
import hashlib
import io
import json

import pytest
from PIL import Image

from mdva.web import solar_models as models
from mdva.web.solar_input import analyze,original
from mdva.web.services import Hub
from mdva.training.durable import atomic_json


def encoded(color=(90,80,70),size=(512,512),format='PNG'):
    stream=io.BytesIO();Image.new('RGB',size,color).save(stream,format=format)
    return 'data:image/'+('png' if format=='PNG' else 'jpeg')+';base64,'+base64.b64encode(stream.getvalue()).decode()


@pytest.fixture
def context(tmp_path,monkeypatch):
    out=tmp_path/'out';frozen=tmp_path/'frozen';frozen.mkdir();exp=tmp_path/'exp'
    atomic_json(exp/'protocol.json',{'dataset':str(frozen)})
    (frozen/'segmentation-ready.jsonl').write_text('')
    (frozen/'source-files.jsonl').write_text('')
    monkeypatch.setattr(models,'inventory',lambda _:{'models':[{'id':'AR-A0-s42','category':'AR','condition':'magnetogram','available':True,'weights_sha256':'weights'}]})
    monkeypatch.setattr(models,'experiment',lambda _:exp)
    monkeypatch.setattr(models,'ensure_worker',lambda _:None)
    hub=Hub(tmp_path/'data/mdva.sqlite',out_dir=out)
    params={'model_id':'AR-A0-s42','condition':'magnetogram','confirm_input':True,
            'image_data':encoded(),'filename':'sun.png','request_id':'request_001','acknowledge_research':True}
    yield hub,out,frozen,params
    hub.close()


def test_image_only_job_does_not_require_or_create_datasets_and_is_durable(context):
    hub,out,_,params=context
    a=analyze(hub,out,params,spawn=False);b=analyze(hub,out,params,spawn=False)
    assert a['id']==b['id'] and a['dataset']==a['version']==''
    assert a['input_kind']=='uploaded_image' and not a['input_metadata']['reference_mask_input']
    assert hub.store.list_datasets()==[]
    assert hub.store.get_task(a['task_id'])['kind']=='solar_inference'
    blob,mime=original(hub,out,a['id']);assert mime=='image/png'
    assert hashlib.sha256(blob).hexdigest()==a['input_metadata']['sha256']
    before=list((out/'solar-inputs').rglob('*.png'))
    with pytest.raises(ValueError,match='请求ID'):analyze(hub,out,{**params,'image_data':encoded((1,2,3))},spawn=False)
    assert list((out/'solar-inputs').rglob('*.png'))==before


@pytest.mark.parametrize('change',[{'confirm_input':False},{'condition':'AIA193'},{'acknowledge_research':False},
    {'filename':'../sun.png'},{'image_data':'https://example.com/a.jpg'},
    {'image_data':'data:image/png;base64,AAAA'},{'image_data':encoded(size=(32,32))}])
def test_bad_inputs_are_rejected_before_any_persistent_write(context,change):
    hub,out,_,params=context
    with pytest.raises(ValueError):analyze(hub,out,{**params,**change},spawn=False)
    assert not list(out.glob('solar-inputs/**/*'))
    assert hub.store.list_tasks()==[]


def test_reencoded_sealed_pixels_and_cross_workspace_access_are_rejected(context,tmp_path):
    hub,out,frozen,params=context
    image=Image.new('RGB',(512,512),(90,80,70))
    pixel_hash=hashlib.sha256(f'{image.mode}:{image.size}:'.encode()+image.tobytes()).hexdigest()
    row={'uri':'@source/calibration.jpg','split':'calibration','category':'AR','pixel_sha256':pixel_hash}
    (frozen/'segmentation-ready.jsonl').write_text(json.dumps(row)+'\n')
    (frozen/'source-files.jsonl').write_text(json.dumps({'uri':row['uri'],'sha256':'different-encoding-sha'})+'\n')
    with pytest.raises(ValueError,match='重编码'):analyze(hub,out,params,spawn=False)
    assert not list(out.glob('solar-inputs/**/*'))
    (frozen/'segmentation-ready.jsonl').write_text('')
    job=analyze(hub,out,params,spawn=False)
    other=Hub(tmp_path/'other/mdva.sqlite',out_dir=out)
    try:
        with pytest.raises(FileNotFoundError):original(other,out,job['id'])
    finally:other.close()


def test_jpeg_data_url_survives_path_guard(context,tmp_path):
    from mdva.web.deps import Workspaces
    from mdva.web.routes import call
    hub,out,_,params=context
    workspaces=Workspaces(hub.path.parent,out_dir=out)
    try:
        response=call(workspaces,'POST','/api/solar/analyze',body={**params,'image_data':encoded(format='JPEG'),'filename':'sun.jpg'})
        assert response.status==200,response.json_body
    finally:workspaces.close()
