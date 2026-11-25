"""Bounded official-input source audit for already-frozen training images only."""
from __future__ import annotations
import argparse,json
from datetime import datetime,timezone
from pathlib import Path
import io
import urllib.error
from .datasets import SegmentationDataset
from .diagnostic_support import sample_id
from .durable import atomic_json,commit
from .runtime import digest,now,output_path


def eligible_training(uris,rows):
    known={r['uri']:r for r in rows if r['category']=='CH' and r['split']=='train'}
    if len(uris)!=16 or len(set(uris))!=16 or any(u not in known for u in uris):raise ValueError('仅允许已登记的16张CH训练图，禁止其他划分')
    return [known[u] for u in uris]


def prepare(dataset,selection,output):
    dataset=Path(dataset);selection=Path(selection);output=output_path(output,dataset)
    if output.exists():raise ValueError('使用新的资料审计版本')
    data=SegmentationDataset(dataset,'CH','train');protocol=json.loads(selection.read_text(encoding='utf-8'))
    rows=eligible_training(protocol['probe']['uris'],data.rows)
    items=[{'uri':r['uri'],'sha256':digest(data.source(r['uri'])),'id':sample_id(r['uri'])} for r in rows]
    value={'created_utc':now(),'scope':'16 fixed CH train images only; no masks/test/calibration','rows':items,
        'selection_sha256':digest(selection),'manifest_sha256':digest(dataset/'manifest-hashes.json'),
        'channels':['193','211'],'max_bytes_per_download':8*1024*1024,
        'alignment_review_gate':{'core_correlation':.98,'core_mae_8bit':5,'max_channel_time_delta_seconds':60},
        'code':{n:digest(Path(__file__).with_name(n)) for n in ('aia_pair_audit.py','aia_source_probe.py','datasets.py')},
        'network_code_sha256':digest(Path(__file__).parents[1]/'web/space_weather.py'),
        'training_allowed':False,'gate_scope':'source viability for further alignment review; not permission for training or physical-ratio use'}
    output.mkdir(parents=True);atomic_json(output/'protocol.json',value);return value


def run(dataset,output):
    from astropy.io import fits
    from .aia_source_probe import run as inspect
    from ..web.space_weather import download_aia_synoptic
    dataset=Path(dataset);output=output_path(output,dataset);protocol=json.loads((output/'protocol.json').read_text(encoding='utf-8'))
    if protocol['manifest_sha256']!=digest(dataset/'manifest-hashes.json'):raise ValueError('冻结输入改变')
    for n,sha in protocol['code'].items():
        if digest(Path(__file__).with_name(n))!=sha:raise ValueError('资料审计代码改变')
    if protocol['network_code_sha256']!=digest(Path(__file__).parents[1]/'web/space_weather.py'):raise ValueError('下载适配器代码改变')
    data=SegmentationDataset(dataset,'CH','train');eligible_training([r['uri'] for r in protocol['rows']],data.rows)
    results=[]
    for index,row in enumerate(protocol['rows']):
        if (output/'PAUSE').exists():atomic_json(output/'status.json',{'state':'paused','completed':index,'total':16});return {'state':'paused'}
        source=data.source(row['uri'])
        if digest(source)!=row['sha256']:raise ValueError('源图身份改变')
        folder=output/'pairs'/row['id'];folder.mkdir(parents=True,exist_ok=True)
        try:
            nominal=source.stem[3:16];records=[]
            for channel in protocol['channels']:
                path=folder/f'AIA{nominal}_0{channel}.fits';meta_path=path.with_suffix('.source.json')
                if path.exists():
                    meta=json.loads(meta_path.read_text(encoding='utf-8'))
                    if digest(path)!=meta['sha256']:raise ValueError('已有FITS缓存改变')
                else:
                    raw,meta=download_aia_synoptic(nominal,channel)
                    with fits.open(io.BytesIO(raw),memmap=False) as hdus:
                        h=next(h for h in hdus if h.data is not None and h.data.ndim==2)
                        if h.data.shape!=(1024,1024) or str(h.header.get('WAVELNTH'))!=channel:raise ValueError('FITS网格/通道不符')
                        meta['header']={k:h.header.get(k) for k in ('T_OBS','QUALITY','LVL_NUM')}
                    commit(path,lambda stream:stream.write(raw));meta.update(sha256=digest(path),bytes=len(raw),fetched_utc=now());atomic_json(meta_path,meta)
                records.append({'state':'downloaded_verified',**meta})
            if not (folder/'source-audit.json').exists():atomic_json(folder/'source-audit.json',{'state':'source_probe','train_uri':row['uri'],'train_sha256':row['sha256'],'sources':records,'mask_read':False,'test_accessed':False})
            mapping_path=folder/'mapping-review.json'
            mapping=json.loads(mapping_path.read_text(encoding='utf-8')) if mapping_path.exists() else inspect(dataset,folder)
            if mapping['code_sha256']!=protocol['code']['aia_source_probe.py'] or mapping['train_uri']!=row['uri']:raise ValueError('映射缓存版本不同')
            grid=mapping['wcs_grid_metadata'];same_grid=all(v['193']==v['211'] for v in grid.values())
            target=datetime.strptime(nominal,'%Y%m%d_%H%M').replace(tzinfo=timezone.utc)
            offsets=[abs((datetime.fromisoformat(v['header']['T_OBS'].replace('Z','+00:00'))-target).total_seconds()) for v in records]
            limits=protocol['alignment_review_gate']
            passed=(mapping['core_correlation']>=limits['core_correlation'] and mapping['core_mae_8bit']<=limits['core_mae_8bit'] and
                abs(mapping['signed_211_minus193_seconds'])<=limits['max_channel_time_delta_seconds'] and max(offsets)<=60 and same_grid)
            result={'uri':row['uri'],'state':'source_reviewed','alignment_review_gate_passed':bool(passed),
                'core_correlation':mapping['core_correlation'],'core_mae_8bit':mapping['core_mae_8bit'],
                'channel_delta_seconds':mapping['signed_211_minus193_seconds'],'same_wcs_grid_fields':same_grid,
                'mapping_sha256':digest(mapping_path),'training_allowed':False}
        except Exception as exc:
            result={'uri':row['uri'],'state':'failed','error':str(exc),'training_allowed':False}
        results.append(result);atomic_json(folder/'batch-result.json',result)
        progress={'state':'auditing_sources','completed':index+1,'total':16,'updated_utc':now(),
            'passed':sum(r.get('alignment_review_gate_passed',False) for r in results),'failed':sum(r['state']=='failed' for r in results),'training':False}
        atomic_json(output/'status.json',progress);print(json.dumps(progress),flush=True)
    report={'state':'completed','updated_utc':now(),'protocol_sha256':digest(output/'protocol.json'),'results':results,
        'source_review_passed':sum(r.get('alignment_review_gate_passed',False) for r in results),'training_allowed':False,
        'reason':'Outside-disk processing and complete pixel registration remain unresolved; no new training or label edits.'}
    atomic_json(output/'summary.json',report);atomic_json(output/'status.json',{'state':'completed','completed':16,'total':16,'training':False});return report


if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment();p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','run']);p.add_argument('--dataset',required=True);p.add_argument('--selection');p.add_argument('--output',required=True);a=p.parse_args()
    r=prepare(a.dataset,a.selection,a.output) if a.action=='prepare' else run(a.dataset,a.output);print(json.dumps({'state':r.get('state'),'source_review_passed':r.get('source_review_passed')}))
