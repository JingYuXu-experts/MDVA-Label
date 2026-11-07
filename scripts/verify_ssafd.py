"""Offline acceptance on AR/CH/FL x 30 real pairs; all writes stay under --out.

python scripts/verify_ssafd.py --source "datas of importance/Situation Awareness Dataset"
"""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from mdva.web.services import Hub
from mdva.web.solar import Solar
from mdva.export.reference import read_mask
import numpy as np


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', default=str(ROOT/'datas of importance'/'Situation Awareness Dataset'))
    parser.add_argument('--out', default=str(ROOT/'out'/'acceptance'/'real'))
    args = parser.parse_args()
    out = Path(args.out).resolve()
    source = Path(args.source).resolve(strict=True)
    if out == source or source in out.parents or out in source.parents:
        raise ValueError('验收输出目录必须独立于原始数据源')
    out.mkdir(parents=True, exist_ok=True)
    hub = Hub(out/'workspace'/'mdva.sqlite', out_dir=out/'derived')
    hub.store.register_source('ssafd', source)
    service = Solar(hub)
    result = {'seed':20261107,'samples_per_category':30,'checks':{},'timings_ms':{}}
    def rec(name, value=True):
        result['checks'][name] = value
        print(name, json.dumps(value,ensure_ascii=False),flush=True)
    def timed(name, action):
        begin=time.perf_counter(); value=action()
        result['timings_ms'][name]=round((time.perf_counter()-begin)*1000,2)
        return value
    try:
        preview=timed('preflight',lambda:service.preflight('ssafd'))
        rec('full_index_counts',preview['totals'])
        imported=service.execute(preview['scan_id'],name='真实数据离线验收',sample_per_category=30)
        did, original = imported['dataset_id'], imported['version_id']
        records=hub.store.indexed_items(did)
        assert len(records)==90
        assert all(sum(r['category']==cat for r in records)==30 for cat in ('AR','CH','FL'))
        rec('sample_counts',{cat:sum(r['category']==cat for r in records) for cat in ('AR','CH','FL')})
        files={str(hub.store.require_within(r[key])) for r in records for key in ('uri','mask_uri')}
        before={p:sha(p) for p in files}
        page=timed('items_first_page',lambda:hub.items_page(did,original,page_size=30))
        assert page['total']==90 and len(page['items'])==30
        timed('frame_cold',lambda:hub.frame(records[0]['uri']))
        timed('frame_warm',lambda:hub.frame(records[0]['uri']))
        timed('frame_changed_stretch',lambda:hub.frame(records[0]['uri'],{'stretch':'sqrt'}))
        rec('browse_and_render')
        raw=timed('export_original',lambda:hub.export(original,'coco',{'annotation_source':'original','seed':20261107}))
        check=timed('validate_original',lambda:hub.export_validate(original,raw['out_dir']))
        assert check['ok'],check
        assert check['check']['counts']['pixel_verified_masks']==90
        rec('original_export',{'images':raw['n_images'],'instances':raw['n_annotations'],'pixel_verified_masks':90})
        work=hub.new_version(did,original)['version_id']
        n=0
        begin=time.perf_counter()
        for record in records:
            anns=service.polygons(record['uri'])['anns']
            n+=len(anns)
            hub.put_annotations(work,record['uri'],anns,role='annotator')
            hub.submit_item(work,record['uri'],role='annotator')
        result['timings_ms']['save_submit_90']=round((time.perf_counter()-begin)*1000,2)
        hub.review_bulk(work,role='reviewer',action='approve')
        assert len(hub.store.list_annotations(work))==n
        assert hub.annotations(original)==[]
        hub.close()
        hub=Hub(out/'workspace'/'mdva.sqlite',out_dir=out/'derived')
        service=Solar(hub)
        assert len(hub.store.list_annotations(work))==n
        rec('polygon_save_review_reopen',{'annotations':n})
        uris=[r['uri'] for r in records]
        specs=[{'name':'blur','params':{'threshold':0}}]
        plan=hub.clean_preview(uris,specs)
        cleaned=hub.clean_apply(work,specs,uris)
        assert cleaned['summary']['n_dropped']==plan['n_dropped']==0
        assert len(hub.store.version_records(cleaned['version_id']))==90
        undone=hub.clean_rollback(cleaned['version_id'])
        assert len(hub.store.version_records(undone['new_version']))==90
        # Force-drop one sample, prove it disappears only from that derived version.
        dropped=hub.clean_apply(work,[{'name':'blur','params':{'threshold':1e20}}],[uris[0]])
        assert len(hub.store.version_records(dropped['version_id']))==89
        assert len(hub.store.version_records(work))==90
        rec('clean_preview_execute_membership_rollback')
        recipe={'seed':20261107,'factor':1,'ops':[{'name':'flip','params':{'p_horizontal':1.0,'p_vertical':0.0}}]}
        augmented=timed('augment_90',lambda:hub.augment_run(work,recipe,uris))
        assert augmented['summary']['n_generated']==90 and augmented['summary']['n_masks']==90
        replay=hub.augment_replay(augmented['version_id'])
        assert replay['identical'],replay
        mask_verified=0
        for written in hub.store.get_task(augmented['task_id'])['report']['written']:
            src=hub.store.indexed_item(written['source'])
            expected=np.fliplr(read_mask(hub.store.to_abs(src['mask_uri'])))
            actual=read_mask(written['aux']['mask']['written'])
            assert np.array_equal(expected,actual)
            mask_verified+=1
        rec('augment_masks_and_replay',{'pixel_verified_masks':mask_verified,'replay_identical':True})
        exports={}
        for fmt in ('coco','yolo','voc','multimodal'):
            exported=hub.export(augmented['version_id'],fmt,{'seed':20261107, **({'task':'segment'} if fmt=='yolo' else {})})
            validation=hub.export_validate(augmented['version_id'],exported['out_dir'])
            assert validation['ok'],(fmt,validation)
            exports[fmt]={'n_annotations':exported.get('n_annotations'),'ok':True}
        rec('manual_four_formats',exports)
        # Every augmented child must remain in its original's train/val/test fold.
        from mdva.export.base import load_dataset
        from mdva.export.split import plan_split
        ds=load_dataset(hub.store,augmented['version_id'])
        assignment=plan_split(ds,seed=20261107)
        groups={}
        for sample in ds.samples:
            groups.setdefault(sample.meta.get('source_group',sample.uri),set()).add(assignment[sample.name])
        assert all(len(folds)==1 for folds in groups.values())
        rec('same_source_split_isolation')
        assert before=={p:sha(p) for p in files}
        rec('original_180_files_sha256_unchanged')
        result['ok']=True
    except Exception as exc:
        result.update(ok=False,error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        hub.close()
        (out/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0


if __name__=='__main__':
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8')
    raise SystemExit(main())
