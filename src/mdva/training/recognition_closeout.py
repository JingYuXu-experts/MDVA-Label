"""Close completed validation experiments without training or test pixel access."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import gc
import json
import os
from pathlib import Path
import shutil

import numpy as np

from .diagnostic_support import read
from .durable import atomic_json, commit
from .runtime import digest, now, exclusive
from .recognition_release import FAMILIES, CANDIDATES, prepare_protocol, checkpoint_evidence


def primary_statistics(cases):
    from .metrics import paired_cluster_interval, holm
    by_run = defaultdict(dict)
    for row in cases:
        if row['split'] == 'validation':
            if row['sample_id'] in by_run[row['run_id']]:
                raise ValueError('逐图结果重复')
            by_run[row['run_id']][row['sample_id']] = row
    results=[]
    for left,right in [('AR-A0','AR-A1'),('CH-C1','CH-C2')]:
        identifiers=[f'{family}-s{seed}' for family in (left,right) for seed in (17,42,2026)]
        expected=set(by_run[identifiers[0]])
        if not expected or any(set(by_run[i])!=expected for i in identifiers):
            raise ValueError('三种子或方法间样本集合不同，拒绝静默取交集')
        keys=sorted(expected)
        for sid in keys:
            times={(by_run[i][sid]['uri'],by_run[i][sid]['time_utc']) for i in identifiers}
            if len(times)!=1:raise ValueError('同一图像的身份或时刻不同')
        dates=[datetime.fromisoformat(by_run[identifiers[0]][sid]['time_utc']) for sid in keys]
        if any(d.tzinfo is None for d in dates):raise ValueError('聚类时间必须明确时区')
        groups=np.asarray([int(d.timestamp()//(27*86400)) for d in dates])
        a=np.asarray([np.mean([by_run[f'{left}-s{s}'][sid]['dice'] for s in (17,42,2026)]) for sid in keys])
        b=np.asarray([np.mean([by_run[f'{right}-s{s}'][sid]['dice'] for s in (17,42,2026)]) for sid in keys])
        interval=paired_cluster_interval(a,b,groups,repeats=2000,seed=2026)
        unique,inverse=np.unique(groups,return_inverse=True)
        rng=np.random.default_rng(2026); observed=abs(float(np.mean(b-a)))
        null=[abs(float(np.mean((b-a)*rng.choice([-1,1],len(unique))[inverse]))) for _ in range(2000)]
        results.append({'baseline':left,'candidate':right,'images':len(keys),**interval,
            'p_randomization':(1+sum(x>=observed for x in null))/2001,
            'scope':'reused_validation_not_final_test','seed_handling':'per-image mean over three seeds',
            'sample_ids_sha256':__import__('hashlib').sha256('\n'.join(keys).encode()).hexdigest()})
    for row,p in zip(results,holm([r['p_randomization'] for r in results])):row['p_holm']=p
    return results


def finalize(experiment, source, output):
    from .diagnose import ensure_stopped, publish
    from .diagnostic_review import review
    from .diagnostic_statistics import station_intervals
    from .run_locations import run_folder
    from .recognition_release import scope_counts
    import torch
    torch.set_num_threads(4)
    experiment,source,output=map(lambda p:Path(p).resolve(),(experiment,source,output))
    ensure_stopped(experiment)
    if output.exists():raise ValueError('最终报告目录已存在，禁止覆盖旧证据')
    report=read(source/'report.json');cases=read(source/'case-index.json')
    models=report['models']; records=[]
    protocol=read(experiment/'protocol.json')
    from .runtime import output_path
    output_path(output,Path(protocol['dataset']))
    if output==source or output.is_relative_to(experiment) or source.is_relative_to(output):
        raise ValueError('最终报告必须使用独立诊断目录')
    expected={c['id'] for c in protocol['recognition'] if not c['id'].startswith('CH-C0-')}
    if {m['id'] for m in models}!=expected or len(models)!=39:
        raise ValueError('要求39个完整诊断模型')
    with exclusive(experiment/'closeout.lock'):
        for index,m in enumerate(models):
            folder=run_folder(experiment,m['id']); evidence=checkpoint_evidence(experiment,m['id'])
            identity=read(folder/'identity.json')
            saved=torch.load(folder/'last.pt',map_location='cpu',weights_only=False)
            best=torch.load(folder/'best.pt',map_location='cpu',weights_only=False)
            def finite(value):
                if torch.is_tensor(value):return bool(torch.isfinite(value).all())
                if isinstance(value,dict):return all(finite(v) for v in value.values())
                if isinstance(value,(list,tuple)):return all(finite(v) for v in value)
                return True
            if (saved['identity']!=identity or best['identity']!=identity or saved['epoch']!=len(saved['history'])
                or best['epoch']!=saved['best_epoch'] or not finite(saved['model'])
                or not finite(saved['optimizer']) or not finite(best['model'])):
                raise ValueError('检查点身份、历史或张量异常：'+m['id'])
            if (m['checkpoint_sha256']!=evidence['checkpoint_sha256'] or not m['reproduction_passed']
                or abs(m['validation']['dice']-best['validation']['dice'])>1e-5):
                raise ValueError('诊断与最佳检查点不一致：'+m['id'])
            retry=folder/'precision-events.jsonl'
            events=[json.loads(line) for line in retry.read_text(encoding='utf-8').splitlines()] if retry.exists() else []
            records.append({'id':m['id'],**evidence,'last_sha256':digest(folder/'last.pt'),
                'completed_epoch':saved['epoch'],'best_epoch':best['epoch'],
                'precision_retries':len(events),'finite_model_optimizer':True,
                'reproduction_delta':m['reproduction_delta']})
            del saved,best;gc.collect()
            print(json.dumps({'verified':index+1,'total':39,'id':m['id']}),flush=True)
        output.mkdir(parents=True)
        def link(a,b):
            os.link(a,b)
            return b
        for name in ('assets','probabilities','cases','models'):
            shutil.copytree(source/name,output/name,copy_function=link)
        for name in ('samples.json','audit.json'):shutil.copy2(source/name,output/name)
        atomic_json(output/'identity.json',{'created_utc':now(),'parent_report':source.name,
            'parent_report_sha256':digest(source/'report.json'),'experiment':str(experiment),
            'checkpoints':records,'code_sha256':digest(__file__),'test_accessed':False})
        final=publish(output,models,cases,read(output/'audit.json'),finished=True)
        final['paired_comparisons']=primary_statistics(cases)
        final['training_stopped']=True
        if all(not m.get('partial_training') for m in models):
            final['limitations']=[v for v in final['limitations'] if not v.startswith('AR-A0-s17为失败运行')]
        final['recommendations']=[r for r in final['recommendations'] if r['group'].startswith('FL-')]
        final['recommendations'] += [
            {'group':'AR-A0','decision':'应用候选','reason':'与A1均值差0.5个百分点内，保留简单基线；三种子完成，待独立测试'},
            {'group':'AR-A1','decision':'保留对照','reason':'验证均值收益小且种子波动较大，尚无充分改善证据'},
            {'group':'CH-C2','decision':'应用候选','reason':'验证均值与种子稳定性较C1改善；待独立测试'},
            {'group':'CH-C1','decision':'保留对照','reason':'三个种子已完成，不再提示补齐种子'}]
        atomic_json(output/'report.json',final)
        review(output);station_intervals(output)
        interpretation=read(output/'interpretation.json')
        interpretation['next_experiments']=[
            {'decision':'已完成','target':'AR/CH三种子主实验与基线','reason':'结果已齐，下一步独立评价与候选流程'},
            {'decision':'暂缓','target':'新增FL实验与业务预测','reason':'仍需具体错误原因及科学监督门槛'},
            {'decision':'待单独确认','target':'12项AR/CH最终测试','reason':'先验收固定模型与测试协议'}]
        atomic_json(output/'interpretation.json',interpretation)
        atomic_json(output/'next-experiment-proposal.json',{'state':'approved_training_completed',
            'training_restart':False,'test_unlock':False,'next':'验收冻结清单后单独确认12项AR/CH测试'})
        atomic_json(output/'status.json',{'state':'completed','phase':'M8','training_stopped':True,
            'task_type':'validation_closeout','models_completed':39,'models_total':39,'updated_utc':now(),
            'message':'39项验证成果收尾完成，识别最终测试等待冻结清单确认'})
        draft=prepare_protocol(experiment)
        atomic_json(experiment/'closeout.json',{'state':'completed','updated_utc':now(),
            'report_id':output.name,'checkpoints_verified':39,'test_authorized':False,
            'validation_primary_comparisons':final['paired_comparisons'],'candidates':CANDIDATES,
            'protocol_sha256':digest(experiment/'recognition-evaluation-protocol.json'),
            'test_model_image_evaluations':draft['model_image_evaluations']})
        lines=['# AR/CH识别成果与测试冻结清单','', '状态：待用户单独确认，尚未打开识别测试图像。','',
               '原协议39/42完成；本轮39/39完成，3项CH-C0暂缓。','',
               '| 配置 | 三种子验证Dice均值 | 样本标准差（百分点） |','|---|---:|---:|']
        for g in final['groups']:lines.append(f"| {g['group']} | {g['mean_dice']:.2%} | {100*g['sd_dice']:.2f} |")
        lines += ['', '## 两个主比较（重复开发的验证集，非最终测试）']
        for row in final['paired_comparisons']:
            lines += [f"- {row['candidate']} − {row['baseline']}：{100*row['difference_b_minus_a']:+.2f}个百分点；"
                f"95%块bootstrap区间[{100*row['ci95'][0]:.2f}, {100*row['ci95'][1]:.2f}]；Holm p={row['p_holm']:.4f}。"]
        lines += ['', '## 待确认的正式测试',
            '仅AR-A0/A1、CH-C1/C2各三个种子，共12项；不评价FL及CH-C0。',
            '候选固定AR-A0-s42、CH-C2-s2026；阈值0.5；不能根据测试更换。',
            '测试图像数：'+json.dumps(draft['test_images_by_task'],ensure_ascii=False)+'；模型—图像评价总数：'+str(draft['model_image_evaluations']),
            '协议SHA256：'+digest(experiment/'recognition-evaluation-protocol.json'),
            '', '数值精度适配、逐图来源与每项检查点哈希见report.json和identity.json。',
            '未开展独立无目标误报率和人工提效验收，不宣传相应收益。']
        commit(output/'report.md',lambda stream:stream.write(('\n'.join(lines)+'\n').encode('utf-8')))
        shutil.copy2(output/'report.md',experiment/'成果与测试冻结清单.md')
        return read(experiment/'closeout.json')


if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment()
    p=argparse.ArgumentParser();p.add_argument('--experiment',type=Path,required=True)
    p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();print(json.dumps(finalize(a.experiment,a.source,a.output),ensure_ascii=False),flush=True)
