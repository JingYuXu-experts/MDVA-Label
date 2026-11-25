"""Between-run validation gates. No test/calibration data or threshold selection."""
from pathlib import Path
import json

from .diagnostic_support import read, write
from .runtime import exclusive, now


def publish_running(output):
    from .diagnose import publish
    models=[read(p) for p in sorted((output/'models').glob('*.json'))]
    cases=[read(p) for p in sorted((output/'cases').glob('*/*.json'))]
    report=publish(output,models,cases,read(output/'audit.json'),finished=False)
    report['training_stopped']=False
    if not any(m['id']=='AR-A0-s17' and m.get('partial_training') for m in models):
        report['limitations']=[v for v in report['limitations'] if not v.startswith('AR-A0-s17')]
    write(output/'report.json',report)
    lines=['# 修订训练逐项验证快照','','队列可能继续训练；此处仅训练/验证结果，最终测试封存。','',
           '| 配置 | 完成种子 | 验证Dice |','|---|---:|---:|']
    for g in report['groups']:lines.append(f"| {g['group']} | {g['seeds']} | {g['mean_dice']:.4%} |")
    lines += ['','完整学习信号、基线对比与原因限制见训练页“修订训练效果分析”。',*report['limitations']]
    from .durable import commit
    commit(output/'report.md',lambda stream:stream.write(('\n'.join(lines)+'\n').encode('utf-8')))
    commit(output/'interpretation.md',lambda stream:stream.write(('修订训练逐项验证中。当前不作最终优势或产品可用结论。\n'+
        '查看训练页的修订训练效果分析、模型同图案例及原20261115-03诊断；不启动新FL/预测或开放测试。\n').encode('utf-8')))
    return report


def learning_decision(history, reproduction_passed):
    if not history or not reproduction_passed:
        return {'continue': False, 'finding': '缺少历史或指标复算不一致，停止排查流程'}
    first, best = history[0], max(history, key=lambda r: r['validation']['dice'])
    loss_reduction = 1-min(r['train_loss'] for r in history)/max(first['train_loss'], 1e-12)
    gain = best['validation']['dice']-first['validation']['dice']
    learned = loss_reduction >= .02 or gain >= .01
    return {'continue': learned, 'training_loss_reduction_fraction': loss_reduction,
            'best_minus_first_validation_dice': gain,
            'finding': '训练存在学习信号，仍需基线和重复种子判断收益' if learned else
            '训练损失与验证均无明显学习信号，停止该队列并检查输入、数值和优化过程',
            'product_accepted': False}


def analyze(experiment, identifier):
    from .diagnose import evaluate_run, publish
    from .run_locations import run_folder
    from .durable import atomic_json
    experiment = Path(experiment)
    execution = read(experiment/'approved-revision.json')
    output = Path(execution['diagnostic_output'])
    dataset = Path(read(experiment/'protocol.json')['dataset'])
    samples = read(output/'samples.json')
    with exclusive(Path(__file__).resolve().parents[3]/'.mdva/training-gpu.lock'):
        write(output/'status.json', {'state': 'running', 'phase': 'between_runs', 'updated_utc': now(),
              'task_type': 'validation_analysis', 'training_stopped': True,
              'message': identifier+'训练结束，正在复算验证与固定训练样本、生成错误对照'})
        model, cases = evaluate_run(experiment, output, dataset, identifier, samples,
            lambda n,total: write(output/'status.json', {'state': 'running', 'phase': 'between_runs',
                'updated_utc':now(), 'task_type':'validation_analysis', 'training_stopped':True,
                'message':identifier+'验证效果分析', 'processed':n, 'total':total}))
    folder = run_folder(experiment, identifier)
    history = read(folder/'metrics.json')
    decision = learning_decision(history, model['reproduction_passed'])
    prior = read(experiment/'revision-analysis.json', {'runs':{}})
    baseline_id = 'AR-A0-s17' if identifier.startswith('AR-A1') else 'CH-C1-s17' if identifier.startswith('CH-C2') else None
    baseline = read(output/'models'/(baseline_id+'.json')) if baseline_id else None
    comparison = {'baseline_id':baseline_id, 'available':baseline is not None,
                  'same_seed': baseline_id is not None and identifier.endswith('s17'),
                  'dice_difference_pp':100*(model['validation']['dice']-baseline['validation']['dice']) if baseline else None,
                  'limitation':'不同种子单次差值只供查看，最终比较三种子均值与配对区间；配方效应不等于某组件贡献'}
    fit = model['training_diagnostics']
    decision.update(id=identifier, updated_utc=now(), validation=model['validation'], training_diagnostics=fit,
        reproduction_delta=model['reproduction_delta'], comparison=comparison, checkpoint_sha256=model['checkpoint_sha256'],
        cause_limit='错误分层和训练样本仅提供线索；未证明漏标、台站或架构是单一原因')
    prior['runs'][identifier] = decision
    prior.update(updated_utc=now(), test_accessed=False, automatic_model_expansion=False)
    atomic_json(experiment/'revision-analysis.json',prior)
    report=publish_running(output)
    models=report['models']
    write(output/'status.json',{'state':'running','phase':'training_review','updated_utc':now(),
          'task_type':'reviewed_training_queue','training_stopped':False,
          'message':identifier+'分析完成；'+decision['finding'],
          'models_completed':len(models),'models_total':39})
    lines=['# 修订训练：每项效果分析','', '仅训练/验证；无最终测试，无产品达标结论。','']
    for id,row in prior['runs'].items():
        lines += [f'## {id}', '', f"验证Dice：{row['validation']['dice']:.4%}；复算差值：{row['reproduction_delta']:.8f}。",
                  row['finding'], '训练损失降低比例：'+str(round(row['training_loss_reduction_fraction'],4)),
                  '对照：'+json.dumps(row['comparison'],ensure_ascii=False),
                  '训练样本与验证分层：'+json.dumps(row['training_diagnostics'],ensure_ascii=False),
                  '原因限制：'+row['cause_limit'],'']
    from .durable import commit
    commit(experiment/'修订训练效果分析.md',lambda stream:stream.write(('\n'.join(lines)+'\n').encode('utf-8')))
    return decision


if __name__ == '__main__':
    import argparse
    from .runtime import configure_environment
    configure_environment()
    p=argparse.ArgumentParser();p.add_argument('--experiment',type=Path,required=True);p.add_argument('--id',required=True)
    args=p.parse_args()
    result=analyze(args.experiment,args.id)
    print(json.dumps(result,ensure_ascii=False),flush=True)
    if not result['continue']:
        raise SystemExit(2)
