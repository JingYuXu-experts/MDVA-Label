import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pytest
from PIL import Image


@pytest.fixture
def frozen(tmp_path):
    raw=tmp_path/'raw';raw.mkdir();data=tmp_path/'frozen';data.mkdir();rows=[];files=[]
    sources={'test':str(raw)}
    (data/'sources.json').write_text(json.dumps(sources),encoding='utf-8')
    for i in range(22):
        y,x=np.mgrid[:513,:513]
        image=np.stack([(x*3+i)%256,(y*5+i)%256,(x+y+i)%256],axis=-1).astype('uint8')
        mask=np.zeros((513,513),dtype='int64');mask[90:190,70+i:160+i]=2;mask[290:370,300:420]=5
        Image.fromarray(image).save(raw/f'{i}.png');np.save(raw/f'{i}.npy',mask)
        for name in (f'{i}.png',f'{i}.npy'):
            p=raw/name;st=p.stat();files.append({'uri':'@test/'+name,'bytes':st.st_size,'mtime_ns':st.st_mtime_ns,
                'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
        rows.append({'uri':f'@test/{i}.png','mask_uri':f'@test/{i}.npy','eligible':True,'category':'AR',
            'split':'train' if i<17 else 'validation','instrument':'HMI','time_utc':'2020-01-01T00:00:00+00:00'})
    for name,records in [('segmentation-ready.jsonl',rows),('source-files.jsonl',files)]:
        (data/name).write_text('\n'.join(json.dumps(r) for r in records),encoding='utf-8')
    (data/'manifest-hashes.json').write_text(json.dumps({n:hashlib.sha256((data/n).read_bytes()).hexdigest()
        for n in ['segmentation-ready.jsonl','source-files.jsonl']}),encoding='utf-8')
    return data,raw,tmp_path/'cache'


def make_cache(frozen):
    pytest.importorskip('torch')
    from mdva.training.fast_data import build
    data,_,cache=frozen
    for split in ('train','validation'):build(data,cache,'AR',split)


def test_cache_roundtrip_preserves_every_pixel_and_native_instances(frozen):
    torch=pytest.importorskip('torch')
    from mdva.training.datasets import SegmentationDataset
    from mdva.training.fast_data import CachedDataset
    make_cache(frozen);data,raw,cache=frozen
    for split in ('train','validation'):
        a=SegmentationDataset(data,'AR',split,512);b=CachedDataset(data,'AR',split,512,cache_root=cache)
        for i in range(len(a)):
            x,y=a[i],b[i]
            assert torch.equal(x['image'],y['image']) and torch.equal(x['semantic_mask'],y['semantic_mask'])
            assert x['uri']==y['uri']
        assert set(np.unique(np.load(b.source(b.rows[0]['mask_uri']))))=={0,2,5}


def test_cache_rejects_test_and_raw_output(tmp_path,frozen):
    pytest.importorskip('torch')
    from mdva.training.fast_data import build,CachedDataset
    data,raw,_=frozen
    with pytest.raises(ValueError):build(data,raw/'unsafe','AR','train')
    for split in ('test','calibration'):
        with pytest.raises(ValueError):CachedDataset(data,'AR',split,512)
        with pytest.raises(ValueError):build(data,tmp_path/'cache','AR',split)


@pytest.mark.parametrize('which',['cache','raw'])
def test_cache_or_same_size_same_mtime_raw_corruption_is_rejected(frozen,which):
    pytest.importorskip('torch')
    from mdva.training.fast_data import CachedDataset
    make_cache(frozen);data,raw,cache=frozen
    path=cache/'AR-train-512/images.npy' if which=='cache' else raw/'0.npy'
    st=path.stat()
    with path.open('r+b') as stream:
        stream.seek(-1,2);v=stream.read(1);stream.seek(-1,2);stream.write(bytes([v[0]^1]))
    os.utime(path,ns=(st.st_atime_ns,st.st_mtime_ns))
    with pytest.raises(ValueError):CachedDataset(data,'AR','train',512,cache_root=cache)


def test_prefetch_preserves_shuffled_order_and_partial_last_batch(frozen):
    pytest.importorskip('torch')
    from mdva.training.fast_data import CachedDataset,loader_for
    from mdva.training.reviewed_train import loader_for as original
    from mdva.training.datasets import SegmentationDataset
    make_cache(frozen);data,_,cache=frozen
    baseline=SegmentationDataset(data,'AR','train',512);cached=CachedDataset(data,'AR','train',512,cache_root=cache)
    for epoch in (0,3):
        expected=[uri for batch in original(baseline,{'seed':2026},epoch,2) for uri in batch['uri']]
        for workers in (0,2):
            actual=[uri for batch in loader_for(cached,{'seed':2026,'data_workers':workers},epoch,2) for uri in batch['uri']]
            assert actual==expected and len(actual)==17


def test_fast_route_requires_gate_and_current_source_hashes(tmp_path):
    from mdva.training.run_locations import location
    from mdva.training.runtime import digest
    from mdva.training import run_locations
    registry={'runs':{'AR-A1-s2026':{'relative':'revisions/io-fast-v1','entrypoint':'mdva.training.fast_train'}}}
    (tmp_path/'run-revisions.json').write_text(json.dumps(registry),encoding='utf-8')
    gate={'enabled':True,'runs':{'AR-A1-s2026':{}},'code':{n:digest(Path(run_locations.__file__).parent/n) for n in ('fast_data.py','fast_train.py')}}
    (tmp_path/'throughput-revision.json').write_text(json.dumps(gate),encoding='utf-8')
    assert location(tmp_path,'AR-A1-s2026')[1]=='mdva.training.fast_train'
    gate['code']['fast_data.py']='bad'
    (tmp_path/'throughput-revision.json').write_text(json.dumps(gate),encoding='utf-8')
    with pytest.raises(ValueError):location(tmp_path,'AR-A1-s2026')


def test_initialization_failure_keeps_a_visible_failed_state(frozen,tmp_path,monkeypatch):
    pytest.importorskip('torch')
    from mdva.training import fast_train,reviewed_train
    data,_,cache=frozen
    config={'id':'cache-failure','category':'AR','model':'unet','width':16,'size':512,'loss':'bce',
            'seed':17,'execution_revision':'io-fast-v1','cache_directory':str(cache)}
    monkeypatch.setattr(reviewed_train,'train',lambda *_a,**_k:(_ for _ in ()).throw(ValueError('cache damaged')))
    folder=tmp_path/'failed'
    with pytest.raises(ValueError,match='cache damaged'):fast_train.train(config,data,folder)
    state=json.loads((folder/'status.json').read_text(encoding='utf-8'))
    assert state['state']=='failed' and state['error']=='cache damaged'


@pytest.mark.parametrize('device',['cpu','cuda'])
def test_cached_engine_and_resume_preserve_weights_optimizer_and_rng(frozen,tmp_path,monkeypatch,device):
    torch=pytest.importorskip('torch')
    if device=='cuda' and not torch.cuda.is_available():pytest.skip('CUDA unavailable')
    from mdva.training import reviewed_train as engine,fast_train
    make_cache(frozen);data,_,cache=frozen
    monkeypatch.setattr(engine,'architecture',lambda config:(torch.nn.Sequential(torch.nn.Conv2d(1,1,1),torch.nn.Dropout(.1)),{'test':True}))
    config={'id':'pipeline-equivalence','category':'AR','model':'unet','width':16,'size':512,'loss':'bce_dice',
        'seed':17,'micro_batch':2,'effective_batch':8,'max_epochs':3,'min_epochs':3,'patience':12,
        'execution_revision':'io-fast-v1','cache_directory':str(cache),'data_workers':0}
    engine.train(config,data,tmp_path/'original',device=device,diagnostic_limit=17)
    fast_train.train(config,data,tmp_path/'cached',device=device,diagnostic_limit=17,stop_after=1)
    fast_train.train(config,data,tmp_path/'cached',device=device,diagnostic_limit=17,resume=True)
    a,b=[engine.load_checkpoint(tmp_path/p/'last.pt') for p in ('original','cached')]
    assert a['identity']['code']!=b['identity']['code']
    assert all(torch.equal(a['model'][k],b['model'][k]) for k in a['model'])
    assert torch.equal(a['rng']['torch'],b['rng']['torch'])
    assert all(torch.equal(x,y) for x,y in zip(a['rng']['cuda'],b['rng']['cuda']))
    for x,y in zip(a['history'],b['history']):
        assert x['train_loss']==y['train_loss'] and x['validation']==y['validation']
    for key in a['optimizer']['state']:
        for name,x in a['optimizer']['state'][key].items():
            y=b['optimizer']['state'][key][name]
            assert torch.equal(x,y) if torch.is_tensor(x) else x==y
