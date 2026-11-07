"""Real-source contracts using tiny fixtures: labels, holes, references and write boundaries."""
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from mdva import io as MDIO
from mdva.annotate.store import OutsideWorkspace
from mdva.web.deps import Workspaces
from mdva.web.routes import call
from mdva.web.solar import Solar


@pytest.fixture
def solar(tmp_path):
    source = tmp_path / '原始 source'
    for category in ('AR', 'CH', 'FL'):
        images, masks = source/category/(category+'_image'), source/category/(category+'_npy')
        images.mkdir(parents=True); masks.mkdir()
        for i in range(2):
            Image.new('RGB', (32, 24), (i*40, 90, 120)).save(images/f'{category}{i}.jpg')
            arr = np.zeros((24, 32), np.int32)
            arr[3:20, 4:27] = 301 if category == 'AR' else 1
            arr[8:11, 10:13] = 0      # A hole must survive the COCO export.
            np.save(masks/f'{category}{i}.npy', arr)
    Image.new('RGB', (32,24)).save(source/'AR/AR_image/unlabeled.jpg')
    np.save(source/'CH/CH_npy/orphan.npy', np.zeros((24,32), np.uint8))
    data = tmp_path/'workspace'; data.mkdir()
    ws = Workspaces(data, out_dir=tmp_path/'out', sources={'ssafd':source})
    hub = ws.hub()
    service = Solar(hub)
    preview = service.preflight('ssafd')
    imported = service.execute(preview['scan_id'])
    yield ws, hub, service, imported, source, preview
    ws.close()


def fingerprint(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def test_import_pairs_sources_and_reopen(solar):
    ws, hub, service, imported, source, preview = solar
    assert preview['totals'] == {'images':7,'masks':7,'paired':6,'image_only':1,'mask_only':1}
    records = hub.store.indexed_items(imported['dataset_id'])
    assert len(records) == 7 and sum(bool(r['mask_uri']) for r in records) == 6
    assert all(r['uri'].startswith('@ssafd/') for r in records)
    for record in records:
        assert hub.store.to_rel(hub.store.to_abs(record['uri'])) == record['uri']
    response = call(ws, 'GET', '/api/items', {'dataset':imported['dataset_id'], 'page':2,'page_size':2})
    assert response.status == 200 and response.json_body['total'] == 7
    assert len(response.json_body['items']) == 2 and response.json_body['counts']['unlabeled'] == 7
    assert len(service.reference(records[0]['uri'])['instances']) == 1
    assert service.reference(records[0]['uri'])['instances'][0]['id'] == 301
    assert Image.open(__import__('io').BytesIO(service.overlay(records[0]['uri'], 100,100))).size == (100,100)
    ws.close()
    from mdva.web.services import Hub
    reopened = Hub(hub.path)
    try:
        assert len(reopened.store.indexed_items(imported['dataset_id'])) == 7
        assert Path(reopened.store.to_abs(records[0]['uri'])).is_file()
    finally:
        reopened.close()


def test_reference_export_exact_and_manual_separate(solar):
    ws, hub, service, imported, source, preview = solar
    before = fingerprint(source)
    vid = imported['version_id']
    report = hub.export(vid, 'coco', {'annotation_source':'original', 'seed':7})
    assert report['n_images'] == 6 and report['n_annotations'] == 6
    check = hub.export_validate(vid, report['out_dir'])
    assert check['ok'], check
    assert check['check']['counts']['pixel_verified_masks'] == 6
    data = json.loads((Path(hub.store.to_abs(report['out_dir']))/'instances.json').read_text(encoding='utf-8'))
    assert isinstance(data['annotations'][0]['segmentation'], dict)
    original_uri = hub.store.indexed_items(imported['dataset_id'])[0]['uri']
    work = hub.new_version(imported['dataset_id'], vid)['version_id']
    contours = service.polygons(original_uri)['anns']
    assert contours and all(a['geom']=='poly' for a in contours)
    hub.put_annotations(work, original_uri, contours)
    assert hub.annotations(vid) == []
    manual = hub.export(work, 'coco')
    assert manual['n_annotations'] == len(contours)
    assert hub.export_validate(work, manual['out_dir'])['ok']
    assert fingerprint(source) == before
    # Corrupt a mask while retaining a valid image format: validation must detect the pixels.
    dest = Path(hub.store.to_abs(report['out_dir']))
    first = next((dest/'masks').glob('*.png'))
    Image.new('I;16', (32,24)).save(first)
    assert not hub.export_validate(vid, str(dest))['ok']


@pytest.mark.parametrize('encoding', ['object','string'])
def test_serialized_nested_reference_rejected_before_read(solar, tmp_path, encoding):
    ws, hub, service, imported, source, preview = solar
    outside = tmp_path/'secret.png'; Image.new('RGB',(32,24)).save(outside)
    specs = [{'name':'content_filter','params':{'reference':str(outside),'prompts':[{'kind':'box','coords':[1,1,10,10]}]}}]
    payload = {'uris':[hub.store.indexed_items(imported['dataset_id'])[0]['uri']],
               'specs':json.dumps(specs) if encoding=='string' else specs}
    response = call(ws, 'POST', '/api/clean/preview', None, payload)
    assert response.status == 400 and '越界' in response.json_body['error']


def test_readonly_and_atomic_undo(solar, tmp_path):
    ws, hub, service, imported, source, preview = solar
    before = fingerprint(source)
    uri = hub.store.indexed_items(imported['dataset_id'])[0]['uri']
    for path in (uri, str(source), str(source.parent)):
        with pytest.raises(OutsideWorkspace): hub.store.require_writable(path)
    dest = hub.store.base/'collected'; dest.mkdir()
    kept = dest/'kept.txt'; kept.write_text('keep',encoding='utf-8')
    secret = source/'AR/AR_image/AR0.jpg'
    manifest = dest/'mdva_collection.json'
    from mdva.collect import MANIFEST_NAME
    manifest = dest/MANIFEST_NAME
    manifest.write_text(json.dumps({'target':str(dest),'mode':'copy','entries':[
        {'src':str(secret),'dst':str(kept),'action':'copy','is_dir':False,'size':4,'mtime':kept.stat().st_mtime},
        {'src':str(kept),'dst':str(secret),'action':'copy','is_dir':False,'size':secret.stat().st_size,'mtime':secret.stat().st_mtime}
    ]}),encoding='utf-8')
    response = call(ws,'POST','/api/collect/undo',None,{'manifest':str(manifest),'force':True})
    assert response.status == 400 and kept.exists()
    assert fingerprint(source) == before
    response = call(ws,'POST','/api/collect',None,{'uris':[uri],'target':str(dest),'mode':'move'})
    assert response.status == 400


def test_arrays_never_fabricate_time_or_video(tmp_path,capsys):
    np.save(tmp_path/'label.npy',np.zeros((32,32),np.int32))
    np.save(tmp_path/'series.npy',np.zeros((32,4),np.float32))
    np.savez(tmp_path/'mixed.npz',a=np.zeros((5,12,12)),b=np.zeros((3,6,9)))
    with pytest.raises(ValueError,match='2D'): MDIO.sniff(tmp_path/'label.npy')
    with pytest.raises(ValueError,match='NPZ'): MDIO.sniff(tmp_path/'mixed.npz')
    assert MDIO.sniff(tmp_path/'series.npy') == 'series'
    items = MDIO.scan(tmp_path)
    assert len(items)==1 and items[0].uri.endswith('series.npy')
    assert '[warn]' in capsys.readouterr().out


def test_replace_is_atomic_when_approved_annotation_present(solar):
    _,hub,_,imported,_,_=solar
    uri=hub.store.indexed_items(imported['dataset_id'])[0]['uri']
    vid=imported['version_id']
    hub.put_annotations(vid,uri,[{'geom':'box','coords':[1,1,8,8],'label':'AR'}])
    hub.put_annotations(vid,uri,[{'geom':'box','coords':[2,2,9,9],'label':'AR'}],state='approved',replace=False)
    before=hub.annotations(vid)
    with pytest.raises(PermissionError): hub.put_annotations(vid,uri,[])
    assert hub.annotations(vid)==before


def test_clean_membership_and_augmented_masks_follow_versions(solar):
    _,hub,_,imported,source,_=solar
    did,vid=imported['dataset_id'],imported['version_id']
    record=hub.store.indexed_items(did)[0]
    cleaned=hub.clean_apply(vid,[{'name':'blur','params':{'threshold':1e20}}],[record['uri']])
    assert len(hub.store.version_records(cleaned['version_id']))==6
    assert len(hub.store.version_records(vid))==7
    rolled=hub.clean_rollback(cleaned['version_id'])
    assert len(hub.store.version_records(rolled['new_version']))==7
    recipe={'seed':42,'factor':1,'ops':[{'name':'flip','params':{'p_horizontal':1,'p_vertical':0}}]}
    augmented=hub.augment_run(vid,recipe,[record['uri']])
    assert augmented['summary']['n_masks']==1
    written=hub.store.get_task(augmented['task_id'])['report']['written'][0]
    from mdva.export.reference import read_mask
    assert np.array_equal(read_mask(written['aux']['mask']['written']),
                          np.fliplr(np.load(hub.store.to_abs(record['mask_uri']))))
    assert len(hub.store.version_records(augmented['version_id']))==8
    assert hub.augment_replay(augmented['version_id'])['identical']


@pytest.mark.parametrize('operation,params', [
    ('flip',{'horizontal':True,'vertical':False}),
    ('crop',{'fraction':0.75,'ox':0,'oy':0}),
    ('rotate90',{'k':1}),
])
def test_rgb_geometry_and_masks_use_spatial_axes(operation,params):
    from mdva.augment.ops import Spec,apply_image
    from mdva.clean.ops import map_geometry
    from mdva.annotate.model import Ann
    image=np.zeros((24,32,3),np.uint8)
    image[9:14,11:17]=[255,60,20]
    mask=np.zeros((24,32),np.uint16);mask[9:14,11:17]=301
    transformed,labels,chain=apply_image(image,mask,[Spec(operation,params)])
    ys,xs=np.nonzero(labels)
    moved,shape=map_geometry(Ann('box',[11,9,17,14],'AR'),chain,image.shape)
    assert moved.coords==[float(xs.min()),float(ys.min()),float(xs.max()+1),float(ys.max()+1)]
    assert tuple(int(v) for v in shape)==labels.shape==transformed.shape[:2]
    assert np.array_equal(transformed[labels==301],np.tile([255,60,20],(len(xs),1)))


def test_shipped_manifest_resolves_on_this_machine():
    path=Path(__file__).resolve().parents[2]/'data'/'manifest.json'
    manifest=json.loads(path.read_text(encoding='utf-8'))
    for entry in manifest['categories'].values():
        assert not Path(entry['root']).is_absolute()
        assert (path.parent/entry['root']).is_dir()


@pytest.mark.parametrize('method,endpoint,container', [
    ('GET','/api/clean/preview','specs'),
    ('POST','/api/clean/preview','specs'),
    ('POST','/api/clean/apply','specs'),
    ('POST','/api/smart/same_image','params'),
])
def test_serialized_containers_rejected_on_every_consumer(solar,tmp_path,monkeypatch,method,endpoint,container):
    ws,hub,_,imported,_,_=solar
    secret=tmp_path/'secret.png'; Image.new('RGB',(32,24)).save(secret)
    touched=[]
    original=MDIO.load_any
    def load(uri,*args,**kwargs):
        if Path(uri).resolve()==secret.resolve(): touched.append(uri)
        return original(uri,*args,**kwargs)
    monkeypatch.setattr(MDIO,'load_any',load)
    record=hub.store.indexed_items(imported['dataset_id'])[0]
    params={'uri':record['uri'],'uris':[record['uri']],'version':imported['version_id'],
            'coords':'1,1,20,18','kind':'box'}
    if container=='specs':
        params[container]=json.dumps([{'name':'content_filter','reference':str(secret),
            'prompts':[{'kind':'box','coords':[1,1,20,18]}],'rule':'keep_if_matched'}])
    else:params[container]=json.dumps({'source_uri':str(secret),'coords':[1,1,20,18],'kind':'box'})
    response=call(ws,method,endpoint,params if method=='GET' else None,params if method=='POST' else None)
    assert response.status==400 and '越界' in response.json_body['error']
    assert not touched


@pytest.mark.parametrize('serialized',[False,True])
def test_inside_template_is_really_consumed_twice(solar,monkeypatch,serialized):
    ws,hub,_,imported,_,_=solar
    records=hub.store.indexed_items(imported['dataset_id'])
    target=hub.store.to_abs(records[1]['uri'])
    touched=[]; original=MDIO.load_any
    def load(uri,*args,**kwargs):
        if Path(uri).resolve()==Path(target).resolve():touched.append(uri)
        return original(uri,*args,**kwargs)
    monkeypatch.setattr(MDIO,'load_any',load)
    specs=[{'name':'content_filter','reference':records[1]['uri'],
            'prompts':[{'kind':'box','coords':[1,1,20,18]}],'rule':'keep_if_matched'}]
    for _ in range(2):
        touched.clear()
        response=call(ws,'POST','/api/clean/preview',None,
                      {'uris':[records[0]['uri']],'specs':json.dumps(specs) if serialized else specs})
        assert response.status==200,response.json_body
        assert touched,'200 alone is insufficient: the distinct reference image must reach the loader'


def test_yolo_explicitly_derives_detection_boxes_from_polygons(solar):
    _,hub,service,imported,_,_=solar
    uri=hub.store.indexed_items(imported['dataset_id'])[0]['uri']
    hub.put_annotations(imported['version_id'],uri,service.polygons(uri)['anns'])
    result=hub.export(imported['version_id'],'yolo',{'task':'detect','polygons_as_boxes':True})
    assert result['n_annotations']>0
    assert hub.export_validate(imported['version_id'],result['out_dir'])['ok']
