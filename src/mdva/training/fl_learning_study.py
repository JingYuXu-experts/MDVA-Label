"""Finite2x2 FL learning diagnostic: normalization and positive-class weight."""
from __future__ import annotations
import argparse,gc,json,os
from pathlib import Path
import numpy as np
import torch
from .fl_author_model import AuthorGraph,loss_value,SOURCE
from .datasets import SegmentationDataset
from .durable import atomic_json,atomic_checkpoint
from .runtime import digest,now,exclusive,Resources,output_path
from .reviewed_train import seed_all,rng_state,restore_rng,load_checkpoint,data_identity
from .metrics import binary_segmentation,segmentation_summary

ROOT=Path(__file__).resolve().parents[3]
EXPERIMENT=ROOT/'out/training-20261112-v2'
ARMS=(('none-w1','none',1),('none-w10','none',10),('batch-w1','batch',1),('batch-w10','batch',10))


def select_training(selection,rows):
    known={r['uri']:r for r in rows if r['category']=='FL' and r['split']=='train'}
    selected=selection['probe_images'];uris=[r['uri'] for r in selected]
    if len(uris)!=16 or len(set(uris))!=16 or any(r['split']!='train' or r['uri'] not in known for r in selected):
        raise ValueError('只允许原固定16张FL训练图，不能使用其他划分')
    return [known[u] for u in uris]


def code():
    names=('fl_learning_study.py','fl_author_model.py','datasets.py','metrics.py','durable.py','reviewed_train.py','runtime.py')
    return {n:digest(Path(__file__).with_name(n)) for n in names}


def prepare(dataset,study):
    dataset=Path(dataset);study=output_path(study,dataset)
    if study.exists():raise ValueError('暗条诊断使用新版本，不覆盖')
    selection=EXPERIMENT/'fl-review-samples.json'
    data=SegmentationDataset(dataset,'FL','train',512)
    chosen=select_training(json.loads(selection.read_text(encoding='utf-8')),data.rows)
    rng=np.random.default_rng(2026)
    schedule=[rng.choice(16,8,replace=False).tolist() for _ in range(300)]
    plan={'created_utc':now(),'id':'fl-learning-20261118','dataset':str(dataset.resolve()),'manifest_sha256':digest(dataset/'manifest-hashes.json'),
        'selection_sha256':digest(selection),'uris':[r['uri'] for r in chosen],'code':code(),'source_provenance_sha256':digest(SOURCE/'provenance.json'),'data_identity':data_identity(dataset),
        'arms':[{'id':name,'normalization':norm,'positive_weight':weight} for name,norm,weight in ARMS],
        'optimizer_steps':300,'effective_batch':8,'micro_batch':2,'schedule':schedule,'seed':2026,
        'optimization':'Adam1e-3, FP32, TF32off, no augmentation or scheduler; same conv initialization and sample schedule for all arms',
        'loss':'0.5*BCEWithLogits(pos_weight1or10)+0.5*existing smoothed softDice; only two factorial variables',
        'scope':'16 old train-only diagnostic images, no validation/calibration/test pixels or full-data experiment',
        'gate':{'last_train_dice':.85,'minimum_station_train_dice':.70,'gain_over_none_w1':.10},
        'gate_scope':'learning diagnosis only; passing permits proposal of full development evaluation, not automatic full training or release',
        'author_adaptation_limits':['Not original paper reproduction: source repository used1024,[-1,1],FITS background correction,weak YOLO labels,weighted BCE alone and LR1e-4.',
            'None-normalization arm keeps canonical bias-free graph, so it is not the historical MDVA F0 graph. Historical28.06% and63.04% are context, not matched controls.'],
        'training_input':'unchanged display-domain PillowL/255,512, native references nearest; signed physical data not used',
        'original_model_replacement':False,'test_accessed':False,'calibration_accessed':False}
    study.mkdir(parents=True);atomic_json(study/'protocol.json',plan)
    return plan


def preflight(study):
    study=Path(study).resolve();p=json.loads((study/'protocol.json').read_text(encoding='utf-8'))
    if p['code']!=code() or p['data_identity']!=data_identity(p['dataset']):raise ValueError('预检输入或代码身份改变')
    if (study/'gpu-smoke.json').exists():raise ValueError('预检证据不覆盖')
    data=SegmentationDataset(p['dataset'],'FL','train',512);known={r['uri']:i for i,r in enumerate(data.rows)}
    batch=[data[known[u]] for u in p['uris'][:2]];results=[]
    with exclusive(ROOT/'.mdva/training-gpu.lock'):
        for arm,norm,weight in ARMS:
            seed_all(2026);torch.backends.cuda.matmul.allow_tf32=False;torch.backends.cudnn.allow_tf32=False
            torch.cuda.set_per_process_memory_fraction(.75);torch.cuda.reset_peak_memory_stats();resource=Resources()
            model=AuthorGraph(norm).cuda();optimizer=torch.optim.Adam(model.parameters(),lr=1e-3)
            before=model.network.conv.weight.detach().clone()
            x=torch.stack([i['image'] for i in batch]).cuda();y=torch.stack([i['semantic_mask'] for i in batch]).cuda()
            value=loss_value(model(x),y,weight);value.backward()
            finite_gradients=all(v.grad is None or torch.isfinite(v.grad).all() for v in model.parameters())
            if not finite_gradients:raise ValueError('真实512预检梯度非有限')
            optimizer.step()
            finite_weights=all(torch.isfinite(v).all() for v in model.parameters())
            changed=not torch.equal(before,model.network.conv.weight.detach())
            if not finite_weights or not changed:raise ValueError('真实512预检参数无有效更新')
            results.append({'arm':arm,'finite_loss':bool(torch.isfinite(value)),'finite_gradients':bool(finite_gradients),
                'finite_parameters':bool(finite_weights),'optimizer_changed_parameters':changed,'resource':resource.sample()})
            model.cpu();del model,optimizer,x,y,value,before;gc.collect();torch.cuda.empty_cache()
    result={'created_utc':now(),'protocol_sha256':digest(study/'protocol.json'),'passed':True,
        'results':results,'scope':'One fresh real512 microbatch2 optimizer update per arm, discarded; main probes restart from registered seed, no warm-start'}
    atomic_json(study/'gpu-smoke.json',result);return result


def emit(study,arm,state,step,**extra):
    from .journal import append
    file=study/'page-progress.json'
    p=json.loads(file.read_text(encoding='utf-8')) if file.exists() else {'id':'fl-learning-20261118','arms':{}}
    p.update(updated_utc=now(),state=state,active_arm=arm,total_steps=300,scope='固定16张训练样本的学习诊断；不是验证或测试成绩')
    p['arms'][arm]={'state':state,'step':step,'updated_utc':now(),**extra}
    atomic_json(file,p)
    append(EXPERIMENT,'development','暗条可学习性对照：'+arm,f'{state} · {step}/300次更新；训练样本成绩，不是泛化能力。',details={'fl_learning_study':p})


def evaluate(model,items,rows,resource):
    model.eval();scores=[]
    with torch.inference_mode():
        for item,row in zip(items,rows):
            logits=model(item['image'][None].cuda())
            if not torch.isfinite(logits).all():raise ValueError('评价输出非有限')
            probability=logits.sigmoid()[0,0].cpu().numpy()
            score=binary_segmentation(probability>=.5,item['semantic_mask'][0].numpy()>0,boundary=True)
            scores.append({**score,'uri':row['uri'],'instrument':row['instrument']})
    model.train();resource.sample()
    return {'overall':segmentation_summary(scores),'stations':{s:segmentation_summary([v for v in scores if v['instrument']==s]) for s in sorted({r['instrument'] for r in rows})},'per_image':scores}


def run(study):
    study=Path(study).resolve();p=json.loads((study/'protocol.json').read_text(encoding='utf-8'))
    dataset=Path(p['dataset']);output_path(study,dataset)
    if p['code']!=code() or p['data_identity']!=data_identity(dataset) or p['manifest_sha256']!=digest(dataset/'manifest-hashes.json') or p['source_provenance_sha256']!=digest(SOURCE/'provenance.json'):
        raise ValueError('暗条数据、代码或作者来源身份改变')
    smoke=json.loads((study/'gpu-smoke.json').read_text(encoding='utf-8'))
    if not smoke['passed'] or smoke['protocol_sha256']!=digest(study/'protocol.json'):raise ValueError('真实GPU预检未通过')
    selection=EXPERIMENT/'fl-review-samples.json'
    if digest(selection)!=p['selection_sha256']:raise ValueError('固定样本选择改变')
    data=SegmentationDataset(dataset,'FL','train',512)
    rows=select_training(json.loads(selection.read_text(encoding='utf-8')),data.rows)
    if [r['uri'] for r in rows]!=p['uris']:raise ValueError('固定样本顺序改变')
    data.rows=rows;items=[data[i] for i in range(16)]
    if (study/'summary.json').exists():raise ValueError('完整诊断已结束，不重训')
    with exclusive(ROOT/'.mdva/training-gpu.lock'):
        for arm,norm,weight in ARMS:
            folder=study/arm;folder.mkdir(exist_ok=True)
            previous=json.loads((folder/'status.json').read_text(encoding='utf-8')) if (folder/'status.json').exists() else {}
            if previous.get('state')=='completed':
                if previous['protocol_sha256']!=digest(study/'protocol.json'):raise ValueError('已完成对照身份改变')
                continue
            if (study/'PAUSE').exists():return {'state':'paused'}
            seed_all(2026);torch.backends.cuda.matmul.allow_tf32=False;torch.backends.cudnn.allow_tf32=False
            torch.cuda.set_per_process_memory_fraction(.75);torch.cuda.reset_peak_memory_stats()
            model=AuthorGraph(norm).cuda();optimizer=torch.optim.Adam(model.parameters(),lr=1e-3)
            resource=Resources();history=[];step=0
            identity={'protocol_sha256':digest(study/'protocol.json'),'arm':arm}
            if (folder/'last.pt').exists():
                saved=load_checkpoint(folder/'last.pt')
                if saved['identity']!=identity:raise ValueError('暗条检查点身份改变')
                model.load_state_dict(saved['model']);optimizer.load_state_dict(saved['optimizer'])
                for value in optimizer.state.values():
                    for key,tensor in value.items():
                        if torch.is_tensor(tensor) and key!='step':value[key]=tensor.cuda()
                step=saved['step'];history=saved['history'];restore_rng(saved['rng']);del saved
            def save():
                atomic_checkpoint(folder/'last.pt',{'identity':identity,'step':step,'model':model.state_dict(),
                    'optimizer':optimizer.state_dict(),'rng':rng_state(),'history':history})
            try:
                emit(study,arm,'running',step,normalization=norm,positive_weight=weight)
                while step<300:
                    if (study/'PAUSE').exists():
                        save();atomic_json(folder/'status.json',{'state':'paused','step':step,**identity});emit(study,arm,'paused',step);return {'state':'paused'}
                    model.train();optimizer.zero_grad(set_to_none=True)
                    for offset in range(0,8,2):
                        batch=[items[i] for i in p['schedule'][step][offset:offset+2]]
                        x=torch.stack([i['image'] for i in batch]).cuda();y=torch.stack([i['semantic_mask'] for i in batch]).cuda()
                        loss=loss_value(model(x),y,weight)
                        if not torch.isfinite(loss):raise ValueError('暗条损失非有限')
                        (loss/4).backward()
                    if any(v.grad is not None and not torch.isfinite(v.grad).all() for v in model.parameters()):raise ValueError('暗条梯度非有限')
                    optimizer.step();step+=1
                    if step%25==0:
                        result=evaluate(model,items,rows,resource);history.append({'step':step,**result})
                        save();status={'state':'running','step':step,**identity,'metrics':result,'resource':resource.sample(),'pid':os.getpid()}
                        atomic_json(folder/'status.json',status);emit(study,arm,'running',step,training_dice=result['overall']['dice'],resource=status['resource'])
                        print(json.dumps({'arm':arm,'step':step,'train_dice':result['overall']['dice']}),flush=True)
                final={'state':'completed','step':step,**identity,'metrics':history[-1],
                    'parameters':sum(v.numel() for v in model.parameters()),'resource':resource.sample(),'pid':os.getpid()}
                atomic_json(folder/'status.json',final);emit(study,arm,'completed',step,training_dice=final['metrics']['overall']['dice'],resource=final['resource'])
            except Exception as exc:
                atomic_json(folder/'status.json',{'state':'failed','step':step,**identity,'error':str(exc),'checkpoint':'last complete25-step boundary, or completed update on requested pause'})
                emit(study,arm,'failed',step,error=str(exc));raise
            finally:
                model.cpu();del model,optimizer;gc.collect();torch.cuda.empty_cache()
    results={arm:json.loads((study/arm/'status.json').read_text(encoding='utf-8')) for arm,_,_ in ARMS}
    baseline=results['none-w1']['metrics']['overall']['dice'];gates=p['gate']
    for arm,value in results.items():
        m=value['metrics'];value['learning_gate_passed']=(m['overall']['dice']>=gates['last_train_dice']
            and min(s['dice'] for s in m['stations'].values())>=gates['minimum_station_train_dice']
            and m['overall']['dice']-baseline>=gates['gain_over_none_w1'])
    summary={'state':'completed','created_utc':now(),'protocol_sha256':digest(study/'protocol.json'),'results':results,
        'scope':p['scope'],'formal_training_started':False,'model_replacement':False,'test_accessed':False,'calibration_accessed':False}
    atomic_json(study/'summary.json',summary);return summary


if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment();parser=argparse.ArgumentParser();parser.add_argument('action',choices=('prepare','check','run'));parser.add_argument('--dataset');parser.add_argument('--study',required=True);args=parser.parse_args()
    result=prepare(args.dataset,args.study) if args.action=='prepare' else preflight(args.study) if args.action=='check' else run(args.study)
    print(json.dumps({'state':result.get('state'),'scope':result.get('scope')}),flush=True)
