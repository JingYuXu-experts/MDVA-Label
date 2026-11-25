"""One paired continuation to isolate strong-dropout effects on train-only fit."""
from __future__ import annotations
import argparse,json,time
from pathlib import Path
import numpy as np
from .durable import atomic_json,atomic_checkpoint
from .runtime import digest,now,output_path,exclusive,Resources
from .scss_study import emit,read_protocol

ROOT=Path(__file__).resolve().parents[3]


def prepare(dataset,study):
    study=output_path(study,dataset);base=read_protocol(study,dataset)
    folder=study/'regularization-control';folder.mkdir(exist_ok=True)
    protocol=folder/'protocol.json'
    if protocol.exists():raise ValueError('协议已存在，不覆盖')
    initial=study/'probe-group/last.pt';status=json.loads((study/'probe-group/status.json').read_text(encoding='utf-8'))
    if status['state']!='completed' or status['step']!=200:raise ValueError('共同起点不是完整200步GN检查点')
    p={'id':'scss-gn-dropout-control-20261116','created_utc':now(),'scope':'same16 training images only',
       'base_protocol_sha256':digest(study/'protocol.json'),'initial_checkpoint_sha256':digest(initial),
       'arms':{'group_keep_dropout':.5,'group_no_dropout':0.},'steps':100,'learning_rate':.001,
       'new_gate':{'train_dice':.85,'paired_improvement':.05},'code':{n:digest(Path(__file__).with_name(n)) for n in
          ('scss_regularization_control.py','scss_study.py','scss_models.py','reviewed_train.py','fast_data.py','metrics.py')},
       'reason':'Both original learning probes failed with dropout0.5. Isolate this regularizer before making architecture conclusions.',
       'test_accessed':False,'validation_accessed':False,'original_gate_changed':False}
    atomic_json(protocol,p);return p


def run(dataset,study,arm):
    import torch
    from .scss_models import SCSSNet
    from .fast_data import CachedDataset
    from .reviewed_train import load_checkpoint,restore_rng,rng_state,seed_all,loss_value,evaluate
    study=output_path(study,dataset);base=read_protocol(study,dataset);root=study/'regularization-control'
    protocol=json.loads((root/'protocol.json').read_text(encoding='utf-8'))
    for n,sha in protocol['code'].items():
        if digest(Path(__file__).with_name(n))!=sha:raise ValueError('对照代码身份改变')
    if protocol['base_protocol_sha256']!=digest(study/'protocol.json') or digest(study/'probe-group/last.pt')!=protocol['initial_checkpoint_sha256']:
        raise ValueError('共同起点身份改变')
    if arm not in protocol['arms']:raise ValueError('未知对照组')
    output=root/arm
    if output.exists():raise ValueError('对照结果目录已存在，不覆盖')
    output.mkdir();identity={'protocol_sha256':digest(root/'protocol.json'),'arm':arm};atomic_json(output/'identity.json',identity)
    with exclusive(ROOT/'.mdva/training-gpu.lock'):
        seed_all(2026);torch.backends.cuda.matmul.allow_tf32=False;torch.backends.cudnn.allow_tf32=False
        torch.cuda.set_per_process_memory_fraction(.75)
        torch.cuda.reset_peak_memory_stats();resource=Resources()
        data=CachedDataset(dataset,'CH','train',512);known={r['uri']:r for r in data.rows};data.rows=[known[u] for u in base['probe']['uris']]
        model=SCSSNet('group').cuda();optimizer=torch.optim.Adam(model.parameters(),lr=.001)
        saved=load_checkpoint(study/'probe-group/last.pt');model.load_state_dict(saved['model']);optimizer.load_state_dict(saved['optimizer'])
        for state in optimizer.state.values():
            for k,v in state.items():
                if torch.is_tensor(v) and k!='step':state[k]=v.cuda()
        restore_rng(saved['rng']);model.drop.p=protocol['arms'][arm]
        before,_=evaluate(model,data,{'loss':'bce_dice','category':'CH'},resource=resource)
        expected=json.loads((study/'probe-group/status.json').read_text(encoding='utf-8'))['last_train_dice']
        if abs(before['dice']-expected)>1e-8:raise ValueError('共同起点重放不一致')
        history=[];step=0;last_heartbeat=0
        def save():
            atomic_checkpoint(output/'last.pt',{'identity':identity,'step':step,'model':model.state_dict(),
                'optimizer':optimizer.state_dict(),'rng':rng_state(),'history':history})
        save()
        try:
            while step<protocol['steps']:
                if (study/'PAUSE').exists() or (root/'PAUSE').exists():
                    save();emit(study,arm,'16图正则化控制','paused',step,100);return {'state':'paused'}
                model.train();optimizer.zero_grad(set_to_none=True)
                selected=np.random.default_rng(2026+200+step).choice(16,8,replace=False).tolist()
                for start in range(0,8,2):
                    items=[data[i] for i in selected[start:start+2]]
                    x=torch.stack([i['image'] for i in items]).cuda();y=torch.stack([i['semantic_mask'] for i in items]).cuda()
                    loss=loss_value(model(x),y,'bce_dice')
                    if not torch.isfinite(loss):raise ValueError('损失无效')
                    (loss/4).backward()
                if not all(p.grad is not None and torch.isfinite(p.grad).all() for p in model.parameters()):raise ValueError('梯度无效')
                optimizer.step();step+=1
                if step%25==0:
                    score,_=evaluate(model,data,{'loss':'bce_dice','category':'CH'},resource=resource)
                    history.append({'step':step,'dice':score['dice'],'loss':score['loss']});save();atomic_json(output/'history.json',history)
                    print(json.dumps({'arm':arm,**history[-1]}),flush=True)
                    emit(study,arm,'16图正则化控制','running',step,100,train_dice=score['dice'],resource=resource.sample());last_heartbeat=time.monotonic()
                elif time.monotonic()-last_heartbeat>10:
                    emit(study,arm,'16图正则化控制','running',step,100,train_dice=history[-1]['dice'] if history else before['dice'],resource=resource.sample());last_heartbeat=time.monotonic()
            result={'state':'completed','arm':arm,'dropout':model.drop.p,'before':before,'last':score,'history':history,
                'identity':identity,'resource':resource.sample(),'scope':'train-only matched regularization intervention, not generalization',
                'test_accessed':False,'validation_accessed':False,'models_replaced':False}
            atomic_json(output/'summary.json',result);emit(study,arm,'16图正则化控制','completed',100,100,last_train_dice=score['dice'],resource=resource.sample())
            return result
        except Exception as exc:
            atomic_json(output/'status.json',{'state':'failed','error':str(exc),'last_saved_step':history[-1]['step'] if history else 0})
            emit(study,arm,'16图正则化控制','failed',step,100,error=str(exc));raise


if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment();p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','run']);p.add_argument('--dataset',required=True);p.add_argument('--study',required=True);p.add_argument('--arm')
    a=p.parse_args();r=prepare(a.dataset,a.study) if a.action=='prepare' else run(a.dataset,a.study,a.arm)
    print(json.dumps({'state':r.get('state'),'dice':r.get('last',{}).get('dice')}),flush=True)
