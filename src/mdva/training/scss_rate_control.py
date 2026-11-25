"""Fixed common-start LR control; only train16, no test or validation pixels."""
from __future__ import annotations
import argparse,json,os,signal,time
from pathlib import Path
import numpy as np
from .durable import atomic_json,atomic_checkpoint
from .runtime import digest,now,output_path,exclusive,Resources
from .scss_study import emit,read_protocol
ROOT=Path(__file__).resolve().parents[3]


def prepare(dataset,study):
    study=output_path(study,dataset);base=read_protocol(study,dataset);root=study/'rate-control'
    if root.exists():raise ValueError('新的控制版本目录已存在')
    initial=study/'regularization-control/group_keep_dropout/last.pt'
    if not initial.is_file():raise ValueError('共同起点不存在')
    protocol={'created_utc':now(),'scope':'CH train16 learning-rate control only','initial':str(initial),
        'initial_sha256':digest(initial),'base_protocol_sha256':digest(study/'protocol.json'),
        'arms':{'GN-lr0.001':.001,'GN-lr0.0001':.0001},'steps':200,
        'gate':{'low_rate_last_train_dice':.85,'low_minus_control_dice':.05},
        'hypothesis':'Reduce optimization step size after initial learning; architecture, dropout0.5, Adam moments and batch order remain fixed.',
        'code':{n:digest(Path(__file__).with_name(n)) for n in ('scss_rate_control.py','scss_study.py','scss_models.py','fast_data.py','reviewed_train.py','metrics.py')},
        'validation_accessed':False,'test_accessed':False,'models_replaced':False}
    root.mkdir();atomic_json(root/'protocol.json',protocol);return protocol


def run(dataset,study,arm,resume=False):
    import torch
    from .scss_models import SCSSNet
    from .fast_data import CachedDataset
    from .reviewed_train import seed_all,load_checkpoint,restore_rng,rng_state,evaluate,loss_value
    study=output_path(study,dataset);base=read_protocol(study,dataset);root=study/'rate-control'
    protocol=json.loads((root/'protocol.json').read_text(encoding='utf-8'))
    for name,sha in protocol['code'].items():
        if digest(Path(__file__).with_name(name))!=sha:raise ValueError('控制代码改变')
    if digest(protocol['initial'])!=protocol['initial_sha256'] or digest(study/'protocol.json')!=protocol['base_protocol_sha256']:raise ValueError('共同起点改变')
    if arm not in protocol['arms']:raise ValueError('未登记学习率')
    out=root/arm;out.mkdir(exist_ok=True);identity={'protocol_sha256':digest(root/'protocol.json'),'arm':arm}
    if (out/'identity.json').exists() and (not resume or json.loads((out/'identity.json').read_text(encoding='utf-8'))!=identity):raise ValueError('使用核验恢复，禁止覆盖')
    atomic_json(out/'identity.json',identity)
    with exclusive(ROOT/'.mdva/training-gpu.lock'):
        seed_all(2026);torch.backends.cuda.matmul.allow_tf32=False;torch.backends.cudnn.allow_tf32=False
        torch.cuda.set_per_process_memory_fraction(.75);torch.cuda.reset_peak_memory_stats();resource=Resources()
        data=CachedDataset(dataset,'CH','train',512);known={r['uri']:r for r in data.rows};data.rows=[known[u] for u in base['probe']['uris']]
        model=SCSSNet('group').cuda();optimizer=torch.optim.Adam(model.parameters(),lr=protocol['arms'][arm])
        saved=load_checkpoint(out/'last.pt' if resume else protocol['initial'])
        if resume and saved['identity']!=identity:raise ValueError('恢复身份不符')
        model.load_state_dict(saved['model']);optimizer.load_state_dict(saved['optimizer']);restore_rng(saved['rng'])
        for state in optimizer.state.values():
            for key,value in state.items():
                if torch.is_tensor(value) and key!='step':state[key]=value.cuda()
        for group in optimizer.param_groups:group['lr']=protocol['arms'][arm]
        step=saved['step'] if resume else 0;history=saved['history'] if resume else []
        before,_=evaluate(model,data,{'loss':'bce_dice','category':'CH'},resource=resource)
        paused={'value':False};handlers={s:signal.signal(s,lambda *_:paused.update(value=True)) for s in (signal.SIGINT,signal.SIGTERM)}
        def save():atomic_checkpoint(out/'last.pt',{'identity':identity,'step':step,'history':history,'model':model.state_dict(),'optimizer':optimizer.state_dict(),'rng':rng_state()})
        if not resume:save()
        last_emit=0
        try:
            while step<protocol['steps']:
                if paused['value'] or (study/'PAUSE').exists() or (root/'PAUSE').exists():
                    save();atomic_json(out/'status.json',{'state':'paused','step':step});emit(study,arm,'16图学习率控制','paused',step,200);return {'state':'paused'}
                model.train();optimizer.zero_grad(set_to_none=True)
                indices=np.random.default_rng(2026+300+step).choice(16,8,replace=False).tolist()
                for start in range(0,8,2):
                    items=[data[i] for i in indices[start:start+2]]
                    x=torch.stack([r['image'] for r in items]).cuda();y=torch.stack([r['semantic_mask'] for r in items]).cuda()
                    loss=loss_value(model(x),y,'bce_dice')
                    if not torch.isfinite(loss):raise ValueError('非有限损失')
                    (loss/4).backward()
                if not all(p.grad is not None and torch.isfinite(p.grad).all() for p in model.parameters()):raise ValueError('非有限梯度')
                optimizer.step();step+=1
                if step%25==0:
                    score,_=evaluate(model,data,{'loss':'bce_dice','category':'CH'},resource=resource)
                    history.append({'step':step,'dice':score['dice'],'loss':score['loss']});save();atomic_json(out/'history.json',history)
                    emit(study,arm,'16图学习率控制','running',step,200,train_dice=score['dice'],resource=resource.sample())
                    print(json.dumps({'arm':arm,**history[-1]}),flush=True);last_emit=time.monotonic()
                elif time.monotonic()-last_emit>10:
                    emit(study,arm,'16图学习率控制','running',step,200,train_dice=history[-1]['dice'] if history else before['dice'],resource=resource.sample());last_emit=time.monotonic()
            result={'state':'completed','arm':arm,'identity':identity,'before':before,'last':score,'resource':resource.sample(),
                'validation_accessed':False,'test_accessed':False,'models_replaced':False}
            atomic_json(out/'summary.json',result);emit(study,arm,'16图学习率控制','completed',200,200,last_train_dice=score['dice'],resource=resource.sample());return result
        except Exception as exc:
            atomic_json(out/'status.json',{'state':'failed','step':step,'error':str(exc)});emit(study,arm,'16图学习率控制','failed',step,200,error=str(exc));raise
        finally:
            for sig,handler in handlers.items():signal.signal(sig,handler)


if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment();p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','run']);p.add_argument('--dataset',required=True);p.add_argument('--study',required=True);p.add_argument('--arm');p.add_argument('--resume',action='store_true')
    a=p.parse_args();r=prepare(a.dataset,a.study) if a.action=='prepare' else run(a.dataset,a.study,a.arm,a.resume)
    print(json.dumps({'state':r.get('state'),'dice':r.get('last',{}).get('dice')}),flush=True)
