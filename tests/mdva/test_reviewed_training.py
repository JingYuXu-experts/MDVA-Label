import json
from pathlib import Path

import numpy as np
import pytest

from mdva.training import durable
from mdva.training.reviewed_analysis import learning_decision


def test_unique_temporary_file_and_bounded_transient_retry_preserve_previous(tmp_path,monkeypatch):
    path=tmp_path/'state.json';durable.atomic_json(path,{'epoch':25})
    real=durable.os.replace;seen=[]
    def replace(a,b):
        seen.append(Path(a))
        if len(seen)<3:raise PermissionError('isolated WinError5')
        real(a,b)
    monkeypatch.setattr(durable.os,'replace',replace)
    monkeypatch.setattr(durable.time,'sleep',lambda _:None)
    durable.atomic_json(path,{'epoch':26})
    assert json.loads(path.read_text())=={'epoch':26}
    assert len(seen)==3 and seen[0].name!='state.json.tmp'
    assert not seen[0].exists()


def test_exhausted_replace_preserves_old_and_retains_complete_new_file(tmp_path,monkeypatch):
    path=tmp_path/'status.json';durable.atomic_json(path,{'epoch':25})
    monkeypatch.setattr(durable.os,'replace',lambda *_:(_ for _ in ()).throw(PermissionError('busy')))
    monkeypatch.setattr(durable.time,'sleep',lambda _:None)
    with pytest.raises(PermissionError,match='旧文件保留'):durable.atomic_json(path,{'epoch':26})
    assert json.loads(path.read_text())=={'epoch':25}
    pending=list(tmp_path.glob('.status.json.*.tmp'))
    assert len(pending)==1 and json.loads(pending[0].read_text())=={'epoch':26}


def test_producer_failure_never_truncates_previous(tmp_path):
    path=tmp_path/'last.pt';path.write_bytes(b'last complete checkpoint')
    def failed(stream):stream.write(b'partial');raise OSError('disk failed')
    with pytest.raises(OSError):durable.commit(path,failed)
    assert path.read_bytes()==b'last complete checkpoint'
    assert list(tmp_path.glob('.*.tmp'))==[]


def test_review_detects_no_learning_and_refuses_metric_mismatch():
    flat=[{'train_loss':1,'validation':{'dice':.1}}]*20
    assert not learning_decision(flat,True)['continue']
    learned=flat+[{'train_loss':.5,'validation':{'dice':.3}}]
    assert learning_decision(learned,True)['continue']
    assert not learning_decision(learned,False)['continue']


def test_old_queue_cannot_run_deferred_jobs_after_approval(tmp_path):
    from mdva.training.__main__ import queue
    (tmp_path/'approved-revision.json').write_text('{}')
    with pytest.raises(ValueError,match='修订队列'):queue(tmp_path)


def test_web_recognizes_between_run_review_as_live_and_respects_pause(tmp_path):
    from mdva.web.training import summary
    from mdva.training.runtime import now
    output=tmp_path/'out';folder=output/'training-example';folder.mkdir(parents=True)
    durable.atomic_json(folder/'protocol.json',{'recognition':[{'id':'AR-A0-s17'}],'resources':{}})
    durable.atomic_json(folder/'approved-revision.json',{'order':['AR-A0-s17','CH-C1-s17']})
    durable.atomic_json(folder/'reviewed-state.json',{'state':'analyzing','updated_utc':now(),'active':'AR-A0-s17','reason':'验证效果分析'})
    value=summary(tmp_path,output=output)
    assert value['progress']['automatic_continuation'] is True
    assert value['progress']['phase']=='M2'
    (folder/'PAUSE').touch()
    assert summary(tmp_path,output=output)['progress']['automatic_continuation'] is False


def test_reviewed_registry_rejects_c0_arbitrary_entrypoint_and_traversal(tmp_path):
    from mdva.training.run_locations import location
    def register(identifier,relative='revisions/io-reviewed-v1',entry='mdva.training.reviewed_train'):
        durable.atomic_json(tmp_path/'run-revisions.json',{'runs':{identifier:{'relative':relative,'entrypoint':entry}}})
    register('AR-A0-s17')
    assert location(tmp_path,'AR-A0-s17')[1]=='mdva.training.reviewed_train'
    for identifier,relative,entry in [('CH-C0-s17','revisions/io-reviewed-v1','mdva.training.reviewed_train'),
            ('AR-A0-s17','../../outside','mdva.training.reviewed_train'),('AR-A0-s17','revisions/io-reviewed-v1','arbitrary')]:
        register(identifier,relative,entry)
        with pytest.raises(ValueError):location(tmp_path,identifier)


def fake_data(tmp_path,monkeypatch,modules):
    torch=pytest.importorskip('torch')
    raw=tmp_path/'raw';raw.mkdir();frozen=tmp_path/'frozen';frozen.mkdir()
    for name in ('manifest-hashes.json','segmentation-ready.jsonl'):(frozen/name).write_text('{}')
    (frozen/'sources.json').write_text(json.dumps({'test':str(raw)}))
    mask=np.zeros((16,16),int);mask[4:12,4:12]=1;np.save(raw/'mask.npy',mask)
    class FakeData:
        def __init__(self,*args):
            self.rows=[{'uri':str(i),'mask_uri':'mask','time_utc':'2020-01-01T00:00:00+00:00',
                        'instrument':'test','split':'train'} for i in range(3)]
        def __len__(self):return len(self.rows)
        def source(self,uri):return raw/'mask.npy'
        def __getitem__(self,i):
            y=torch.zeros(1,256,256);y[:,64:192,64:192]=1
            return {'image':y*.8+.1,'semantic_mask':y}
    for module in modules:
        monkeypatch.setattr(module,'SegmentationDataset',FakeData)
        monkeypatch.setattr(module,'architecture',lambda c:(torch.nn.Sequential(torch.nn.Conv2d(1,1,1),torch.nn.Dropout(.1)),{'test':True}))
    config={'id':'engineering-equivalence','model':'unet','width':16,'size':256,'category':'FL','loss':'bce_dice',
            'seed':17,'max_epochs':3,'min_epochs':3,'patience':12,'effective_batch':8}
    return torch,frozen,config


@pytest.mark.parametrize('device',['cpu','cuda'])
def test_engineering_revision_matches_numerical_training_and_resumes_exactly(tmp_path,monkeypatch,device):
    pytest.importorskip('torch')
    from mdva.training import train,reviewed_train as revised
    torch,frozen,config=fake_data(tmp_path,monkeypatch,[train,revised])
    if device=='cuda' and not torch.cuda.is_available():pytest.skip('CUDA unavailable')
    train.train(config,frozen,tmp_path/'original',device=device,diagnostic_limit=3)
    revised.train(config,frozen,tmp_path/'revised',device=device,diagnostic_limit=3,stop_after=1)
    revised.train(config,frozen,tmp_path/'revised',device=device,diagnostic_limit=3,resume=True)
    a,b=[train.load_checkpoint(tmp_path/p/'last.pt') for p in ('original','revised')]
    assert a['identity']['code']!=b['identity']['code']
    assert all(torch.equal(a['model'][k],b['model'][k]) for k in a['model'])
    assert torch.equal(a['rng']['torch'],b['rng']['torch'])
    for x,y in zip(a['history'],b['history']):
        assert x['train_loss']==y['train_loss'] and x['validation']==y['validation']
    for key in a['optimizer']['state']:
        for name,x in a['optimizer']['state'][key].items():
            y=b['optimizer']['state'][key][name]
            assert torch.equal(x,y) if torch.is_tensor(x) else x==y


def test_resume_already_at_patience_never_executes_another_epoch(tmp_path,monkeypatch):
    pytest.importorskip('torch')
    from mdva.training import reviewed_train as revised
    torch,frozen,config=fake_data(tmp_path,monkeypatch,[revised])
    config.update(min_epochs=1,patience=1)
    folder=tmp_path/'resume';revised.train(config,frozen,folder,device='cpu',diagnostic_limit=3,stop_after=1)
    saved=revised.load_checkpoint(folder/'last.pt');saved['bad_epochs']=1
    revised.atomic_checkpoint(folder/'last.pt',saved)
    monkeypatch.setattr(revised,'loader_for',lambda *_:(_ for _ in ()).throw(AssertionError('unwanted extra epoch')))
    status=revised.train(config,frozen,folder,device='cpu',diagnostic_limit=3,resume=True)
    assert status['state']=='completed' and status['completed_epoch']==1
    assert status['reason']=='validation_early_stop_on_audited_resume'
    current=revised.load_checkpoint(folder/'last.pt')
    assert all(torch.equal(saved['model'][k],current['model'][k]) for k in saved['model'])
