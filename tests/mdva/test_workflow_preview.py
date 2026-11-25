"""Actual augmentation preview uses the run plan and never persists sample output."""
import base64
import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from mdva.web.services import Hub
from mdva.augment.policy import get
from mdva.annotate.model import Ann


def test_solar_preview_is_non_destructive_and_matches_written_output(tmp_path):
    images=tmp_path/'images';images.mkdir()
    rng=np.random.default_rng(82)
    for i in range(3):Image.fromarray(rng.integers(0,256,(48,80,3),dtype=np.uint8)).save(images/f'{i}.png')
    hub=Hub(tmp_path/'mdva.sqlite',out_dir=tmp_path/'out')
    try:
        ds=hub.create_dataset('太阳预览验证','segmentation',images,category='aerospace',labels=['AR','CH'])
        uris=[str(p) for p in sorted(images.glob('*.png'))]
        before={p.name:p.read_bytes() for p in images.glob('*.png')}
        recipe=get('solar_photometric')['recipe'];recipe['seed']=37
        report=hub.augment_preview(uris[::-1],recipe,uris[1])
        assert report['rep']==0 and report['seed']==37
        assert not (tmp_path/'out').exists()
        assert {p.name:p.read_bytes() for p in images.glob('*.png')}==before
        assert report['before']!=report['after']
        generated=hub.augment_run(ds['version_id'],recipe,uris)
        lineage=hub.augment_lineage(generated['version_id'])
        match=next(row for row in lineage if Path(row['source']).name=='1.png')
        assert report['specs']==match['specs'] and report['chain']==match['chain']
        written=hub.frame(match['written'],{'width':640,'height':480,'vmin':0,'vmax':255})
        np.testing.assert_array_equal(np.asarray(Image.open(io.BytesIO(base64.b64decode(report['after'])))),
                                      np.asarray(Image.open(io.BytesIO(base64.b64decode(written['png'])))))
        assert {p.name:p.read_bytes() for p in images.glob('*.png')}==before
        assert len(hub.items(ds['dataset_id'],generated['version_id']))==3
    finally:hub.close()


def test_solar_presets_preserve_direction_and_refuse_empty_scope(tmp_path):
    for name in ('solar_photometric','solar_training'):
        assert all(op['name']=='photometric' for op in get(name)['recipe']['ops'])
    hub=Hub(tmp_path/'mdva.sqlite')
    try:
        with pytest.raises(ValueError,match='选择'):hub.augment_preview([],get('solar_photometric')['recipe'])
    finally:hub.close()


def test_default_admin_can_complete_submission_and_review(tmp_path):
    image=tmp_path/'image.png';Image.fromarray(np.zeros((20,30,3),dtype=np.uint8)).save(image)
    hub=Hub(tmp_path/'mdva.sqlite')
    try:
        ds=hub.create_dataset('审核闭环','segmentation',tmp_path,labels=['AR'])
        aid=hub.store.put_annotation(ds['version_id'],str(image),Ann('box',[1,2,8,10],label='AR'),role='admin')
        assert hub.submit_item(ds['version_id'],str(image),role='admin')['n_submitted']==1
        assert hub.store.get_annotation(aid)['state']=='submitted'
        hub.store.set_state(aid,'admin','approve')
        assert hub.store.get_annotation(aid)['state']=='approved'
        assert hub.submit_item(ds['version_id'],str(image),role='admin')['n_submitted']==0
    finally:hub.close()


def test_add_draft_preserves_approved_ids_and_refuses_hidden_edits(tmp_path):
    image=tmp_path/'image.png';Image.fromarray(np.zeros((20,30,3),dtype=np.uint8)).save(image)
    hub=Hub(tmp_path/'mdva.sqlite')
    try:
        ds=hub.create_dataset('混合审核状态','segmentation',tmp_path,labels=['AR'])
        approved=Ann('box',[1,2,8,10],label='AR')
        aid=hub.store.put_annotation(ds['version_id'],str(image),approved,role='admin',state='approved')
        draft=Ann('poly',[10,2,18,2,18,9],label='AR')
        result=hub.put_annotations(ds['version_id'],str(image),[approved,draft],role='admin',preserve_approved=True)
        assert result['n']==2 and aid in result['ids']
        assert hub.store.get_annotation(aid)['state']=='approved'
        with pytest.raises(PermissionError):hub.put_annotations(ds['version_id'],str(image),[draft],role='admin',preserve_approved=True)
        assert len(hub.annotations(ds['version_id'],str(image)))==2
    finally:hub.close()
