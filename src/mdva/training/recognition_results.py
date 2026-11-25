"""Immutable tables and figures from the authorized first test results."""
from pathlib import Path
import argparse
import csv
import json
import statistics

from .diagnostic_support import read,sample_id
from .durable import atomic_json,commit
from .runtime import configure_environment,digest,now
from .recognition_release import FAMILIES,CANDIDATES,validate_authorized_scope


def generate(experiment):
    from .run_locations import run_folder
    from .recognition_closeout import primary_statistics
    experiment=Path(experiment).resolve()
    frozen=validate_authorized_scope(experiment)
    if read(experiment/'recognition-test-state.json',{}).get('state')!='completed':
        raise ValueError('12项首次测试尚未完整结束')
    destination=experiment/'reports/recognition-20261116'
    if destination.exists():raise ValueError('测试成果已存在，不覆盖首次材料')
    rows=[];models=[];evidence={}
    for identifier,identity in frozen['recognition'].items():
        base=run_folder(experiment,identifier)/'evaluation/test'
        summary=read(base/'summary.json');images=read(base/'images.json')
        if summary['checkpoint_sha256']!=identity['checkpoint_sha256'] or len(images)!=frozen['test_images_by_task'][identifier[:2]]:
            raise ValueError('测试结果身份或数量不符')
        models.append({'id':identifier,**summary})
        for row in images:
            # Shared numeric routine uses a validation-row selector; the returned
            # statistics below are explicitly tagged as the frozen final test.
            rows.append({**row,'run_id':identifier,'sample_id':sample_id(row['uri']),'split':'validation'})
        evidence[identifier]={'summary_sha256':digest(base/'summary.json'),'images_sha256':digest(base/'images.json')}
    pairs=primary_statistics(rows)
    for p in pairs:p['scope']='frozen_final_test'
    groups=[]
    for family in FAMILIES:
        members=[r for r in models if r['id'].rsplit('-s',1)[0]==family]
        group={'group':family,'seeds':3,'images':members[0]['images']}
        for metric in ('dice','iou','precision','recall','boundary_f1'):
            values=[m[metric] for m in members]
            group[metric+'_mean']=statistics.mean(values);group[metric+'_sd']=statistics.stdev(values)
        group['empty_images']=members[0]['empty_images'];groups.append(group)
    value={'created_utc':now(),'state':'completed','groups':groups,'models':models,
        'paired_comparisons':pairs,'evidence':evidence,'candidates':CANDIDATES,
        'evaluation_protocol_sha256':digest(experiment/'recognition-evaluation-protocol.json'),
        'test_model_image_evaluations':len(rows),'selection_changed_after_test':False,
        'limitations':['测试AR/CH均无空参考图，不能评价独立无目标误报率',
                      '无人工计时结果，不宣称提效；模型仍为研究候选',
                      'AR与CH测试表现低于验证，不能用验证分数代替泛化成绩',
                      'FL测试与全部识别校准图像继续封存']}
    destination.mkdir(parents=True)
    atomic_json(destination/'results.json',value)
    with (destination/'model-metrics.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        fields=['id','dice','iou','precision','recall','boundary_f1','images','epoch']
        writer=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore');writer.writeheader();writer.writerows(models)
    with (destination/'error-cases.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        fields=['run_id','uri','dice','iou','precision','recall','boundary_f1']
        writer=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore');writer.writeheader()
        for identifier in frozen['recognition']:
            writer.writerows(sorted((r for r in rows if r['run_id']==identifier),key=lambda r:r['dice'])[:10])
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    diagnostic=read(experiment/'closeout.json')
    dev=read(experiment.parent/'model-diagnostics'/diagnostic['report_id']/'report.json')
    dev_by={g['group']:g for g in dev['groups']}
    figure,axes=plt.subplots(1,2,figsize=(10,4),layout='constrained')
    import numpy as np
    x=np.arange(4)
    axes[0].bar(x-.18,[100*dev_by[g['group']]['mean_dice'] for g in groups],width=.36,label='Validation')
    axes[0].bar(x+.18,[100*g['dice_mean'] for g in groups],width=.36,
                yerr=[100*g['dice_sd'] for g in groups],capsize=3,label='Final test (mean +/- SD)')
    axes[0].set(xticks=x,xticklabels=FAMILIES,ylabel='Native-grid Dice (%)',ylim=(0,100));axes[0].legend(fontsize=8)
    for index,p in enumerate(pairs):
        center=100*p['difference_b_minus_a'];low,high=[100*v for v in p['ci95']]
        axes[1].errorbar(center,index,xerr=[[center-low],[high-center]],fmt='o',capsize=5)
    axes[1].axvline(0,color='grey',linestyle='--');axes[1].set(yticks=[0,1],yticklabels=['AR A1 - A0','CH C2 - C1'],
        xlabel='Final test difference (percentage points)',title='95% paired time-block bootstrap CI')
    figure.savefig(destination/'validation-vs-test.png',dpi=200);plt.close(figure)
    lines=['# AR/CH首次独立测试结果','', '12项固定模型已完成首次评价；以下为三种子均值，±为样本标准差（百分点）。','',
        '| 配置 | 验证Dice | 测试Dice | 测试Precision | 测试Recall |','|---|---:|---:|---:|---:|']
    for g in groups:
        lines.append(f"| {g['group']} | {dev_by[g['group']]['mean_dice']:.2%} | {g['dice_mean']:.2%} ± {100*g['dice_sd']:.2f} | {g['precision_mean']:.2%} | {g['recall_mean']:.2%} |")
    lines += ['', '## 预注册的两个主比较']
    for p in pairs:lines.append(f"- {p['candidate']} − {p['baseline']}：{100*p['difference_b_minus_a']:+.2f}个百分点；95%区间[{100*p['ci95'][0]:.2f}, {100*p['ci95'][1]:.2f}]；Holm p={p['p_holm']:.4f}。")
    lines += ['', '候选仍固定为AR-A0-s42、CH-C2-s2026，没有根据测试更换。',
        '测试分数低于验证，不批准无人审核的自动标注；优先验收人工辅助是否有价值。',
        '具体失败图像与逐模型分数见error-cases.csv和model-metrics.csv。',*value['limitations']]
    commit(destination/'REPORT.md',lambda stream:stream.write(('\n'.join(lines)+'\n').encode('utf-8')))
    for category,identifier in CANDIDATES.items():
        package=experiment/'packages'/identifier;card=read(package/'model.json')
        test=next(m for m in models if m['id']==identifier)
        card.update(scientific_test_complete=True,test_state='completed',test=test,
                    test_summary_sha256=evidence[identifier]['summary_sha256'],
                    release_state='research_candidate',product_efficiency_verified=False)
        atomic_json(package/'model.json',card)
        note=f"# {identifier}\n\n研究候选，仅供人工辅助。\n\n输入：{card['input']}。\n\n单模型原生网格验证Dice {card['validation']['dice']:.2%}；独立测试Dice {test['dice']:.2%}、Precision {test['precision']:.2%}、Recall {test['recall']:.2%}。Dice是区域重合指标，不是识别正确概率。\n\n原始数据和掩膜不随包分发。保留栅格候选和多边形转换跳过原因，模型不生成NOAA身份。独立无目标误报率、人工质量和提效仍待验收。\n\n许可与实现出处见model.json。\n"
        commit(package/'MODEL_CARD.md',lambda stream,note=note:stream.write(note.encode('utf-8')))
    atomic_json(experiment/'test-results.json',{'report':str(destination.relative_to(experiment)),
        'created_utc':now(),'groups':groups,'paired_comparisons':pairs,'candidates':CANDIDATES,
        'selection_changed_after_test':False,'product_release':'research_candidate'})
    return value


if __name__=='__main__':
    configure_environment();p=argparse.ArgumentParser();p.add_argument('--experiment',type=Path,required=True)
    result=generate(p.parse_args().experiment)
    print(json.dumps({'groups':result['groups'],'paired_comparisons':result['paired_comparisons']},ensure_ascii=False))
