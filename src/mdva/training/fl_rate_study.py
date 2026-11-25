"""One preregistered source-recipe LR control after the completed2x2 probe."""
import argparse,gc,json,os
from pathlib import Path
import torch
from .fl_author_model import AuthorGraph,loss_value,SOURCE
from .fl_learning_study import evaluate,emit,select_training,EXPERIMENT,ROOT,code as parent_code
from .datasets import SegmentationDataset
from .runtime import digest,now,exclusive,Resources,output_path
from .durable import atomic_json,atomic_checkpoint
from .reviewed_train import seed_all,load_checkpoint,rng_state,restore_rng,data_identity

ARM='batch-w10-lr0.0001'

def identity():
    return {n:digest(Path(__file__).with_name(n)) for n in ('fl_rate_study.py','fl_author_model.py','fl_learning_study.py','reviewed_train.py','runtime.py','durable.py','datasets.py','metrics.py')}

def prepare(parent,study):
    parent=Path(parent).resolve();base=json.loads((parent/'protocol.json').read_text(encoding='utf-8'))
    result=json.loads((parent/'summary.json').read_text(encoding='utf-8'))
    study=output_path(study,base['dataset'])
    if study.exists() or result['state']!='completed':raise ValueError('先结束原对照，并使用新目录')
    if result['protocol_sha256']!=digest(parent/'protocol.json') or base['code']!=parent_code():raise ValueError('原对照协议或代码改变')
    plan={'created_utc':now(),'parent':str(parent),'parent_protocol_sha256':digest(parent/'protocol.json'),'parent_summary_sha256':digest(parent/'summary.json'),
        'code':identity(),'learning_rate':1e-4,'steps':300,'control':'batch-w10 at1e-3, identical seed/initialization/sample schedule/loss/precision/input',
        'reason':'Original author train.py specifies Adam1e-4. Local1e-3 BN-w10 curve fluctuates (200:32.11%,225:70.62%,250:63.20%,300:79.56%). Test one source-supported lower LR; do not assume optimizer noise is the proven cause.',
        'source_train_sha256':digest(ROOT/'out/solar-model-sources/deepfilament-20261118/author/dfs/model/train.py'),
        'gate':{'last_train_dice':.85,'minimum_station_dice':.70,'gain_over_control':.05},
        'scope':'same16 training-only learning diagnosis; no automatic full training/model replacement/test/calibration'}
    study.mkdir(parents=True);atomic_json(study/'protocol.json',plan)
    progress=json.loads((parent/'page-progress.json').read_text(encoding='utf-8'))
    progress['supplement']='补充对照：保持最佳组合不变，仅将学习率降为作者代码使用的0.0001；最多300次更新。'
    atomic_json(study/'page-progress.json',progress)
    return plan

def run(study):
    study=Path(study).resolve();p=json.loads((study/'protocol.json').read_text(encoding='utf-8'));parent=Path(p['parent'])
    if p['code']!=identity() or digest(parent/'protocol.json')!=p['parent_protocol_sha256'] or digest(parent/'summary.json')!=p['parent_summary_sha256']:raise ValueError('对照身份改变')
    if (study/'summary.json').exists():raise ValueError('已完成对照不重训')
    base=json.loads((parent/'protocol.json').read_text(encoding='utf-8'));output_path(study,base['dataset'])
    if base['data_identity']!=data_identity(base['dataset']) or digest(SOURCE/'provenance.json')!=base['source_provenance_sha256']:raise ValueError('数据或作者来源改变')
    if p['learning_rate']!=1e-4 or p['steps']!=300 or digest(SOURCE/'author/dfs/model/train.py')!=p['source_train_sha256']:raise ValueError('学习率对照范围或作者证据改变')
    data=SegmentationDataset(base['dataset'],'FL','train',512)
    rows=select_training(json.loads((EXPERIMENT/'fl-review-samples.json').read_text(encoding='utf-8')),data.rows)
    if [r['uri'] for r in rows]!=base['uris']:raise ValueError('原固定样本改变')
    data.rows=rows;items=[data[i] for i in range(16)]
    with exclusive(ROOT/'.mdva/training-gpu.lock'):
        if (study/'PAUSE').exists():return {'state':'paused'}
        seed_all(2026);torch.backends.cuda.matmul.allow_tf32=False;torch.backends.cudnn.allow_tf32=False
        torch.cuda.set_per_process_memory_fraction(.75);torch.cuda.reset_peak_memory_stats()
        model=AuthorGraph('batch').cuda();optimizer=torch.optim.Adam(model.parameters(),lr=1e-4)
        resource=Resources();step=0;history=[]
        if (study/'last.pt').exists():
            s=load_checkpoint(study/'last.pt')
            if s['protocol_sha256']!=digest(study/'protocol.json'):raise ValueError('恢复身份改变')
            model.load_state_dict(s['model']);optimizer.load_state_dict(s['optimizer'])
            for state in optimizer.state.values():
                for key,value in state.items():
                    if torch.is_tensor(value) and key!='step':state[key]=value.cuda()
            step=s['step'];history=s['history'];restore_rng(s['rng']);del s
        def save():atomic_checkpoint(study/'last.pt',{'protocol_sha256':digest(study/'protocol.json'),'step':step,'model':model.state_dict(),'optimizer':optimizer.state_dict(),'history':history,'rng':rng_state()})
        try:
            emit(study,ARM,'running',step)
            while step<300:
                if (study/'PAUSE').exists():save();emit(study,ARM,'paused',step);return {'state':'paused'}
                model.train();optimizer.zero_grad(set_to_none=True)
                for offset in range(0,8,2):
                    batch=[items[i] for i in base['schedule'][step][offset:offset+2]]
                    x=torch.stack([i['image'] for i in batch]).cuda();y=torch.stack([i['semantic_mask'] for i in batch]).cuda()
                    loss=loss_value(model(x),y,10)
                    if not torch.isfinite(loss):raise ValueError('损失非有限')
                    (loss/4).backward()
                if any(v.grad is not None and not torch.isfinite(v.grad).all() for v in model.parameters()):raise ValueError('梯度非有限')
                optimizer.step();step+=1
                if step%25==0:
                    metrics=evaluate(model,items,rows,resource);history.append({'step':step,**metrics});save()
                    emit(study,ARM,'running',step,training_dice=metrics['overall']['dice'],resource=resource.sample())
                    print(json.dumps({'step':step,'train_dice':metrics['overall']['dice']}),flush=True)
            control=json.loads((parent/'summary.json').read_text(encoding='utf-8'))['results']['batch-w10']['metrics']['overall']['dice']
            final=history[-1];g=p['gate']
            checks={'dice':final['overall']['dice']>=g['last_train_dice'],'stations':min(s['dice'] for s in final['stations'].values())>=g['minimum_station_dice'],'gain':final['overall']['dice']-control>=g['gain_over_control']}
            report={'state':'completed','created_utc':now(),'protocol_sha256':digest(study/'protocol.json'),'metrics':final,'history':history,'control_dice':control,'checks':checks,'learning_gate_passed':all(checks.values()),'scope':p['scope'],'test_accessed':False,'models_replaced':False}
            atomic_json(study/'summary.json',report);emit(study,ARM,'completed',step,training_dice=final['overall']['dice'],resource=resource.sample())
            return report
        except Exception as exc:emit(study,ARM,'failed',step,error=str(exc));raise
        finally:model.cpu();del model,optimizer;gc.collect();torch.cuda.empty_cache()

if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment();p=argparse.ArgumentParser();p.add_argument('action',choices=('prepare','run'));p.add_argument('--parent');p.add_argument('--study',required=True);a=p.parse_args()
    r=prepare(a.parent,a.study) if a.action=='prepare' else run(a.study);print(json.dumps({'state':r.get('state')}))
