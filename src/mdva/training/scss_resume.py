"""Resume only remaining epochs of a recorded stage, never add its whole budget again."""
import argparse,json
from pathlib import Path
from .runtime import digest


def remaining(stage,completed):
    if completed<stage['starting_epoch'] or completed>stage['target_epoch']:raise ValueError('检查点不在批准阶段范围内')
    return stage['target_epoch']-completed


def resume(study,stage_name='second'):
    from .scss_main import train
    from .reviewed_train import load_checkpoint
    if stage_name not in ('second','refinement25'):raise ValueError('未登记的接续阶段')
    study=Path(study)
    stage_file='active-stage.json' if stage_name=='second' else 'refinement-25.json'
    stage=json.loads((study/stage_file).read_text(encoding='utf-8'))
    if stage['protocol_sha256']!=digest(study/'protocol.json'):raise ValueError('阶段协议身份改变')
    saved=load_checkpoint(study/'run/last.pt');budget=remaining(stage,saved['epoch'])
    if stage_name=='refinement25':
        validate_refinement(study,stage,saved)
    if budget<=0:return {'state':'stage_budget_complete','completed_epoch':saved['epoch']}
    if (study/'run/PAUSE').exists():raise ValueError('用户暂停标记仍在，不自动解除')
    return train(study,resume=True,stop_after=budget)


def validate_refinement(study,stage,saved):
    if (stage.get('starting_epoch'),stage.get('target_epoch'),stage.get('additional_budget'))!=(20,25,5):
        raise ValueError('低学习率收敛核验仅允许20至25轮')
    if stage.get('resume_code_sha256')!=digest(__file__):raise ValueError('接续代码身份改变')
    for relative,sha in stage['review_evidence'].items():
        path=(study.parent/relative).resolve()
        if not path.is_relative_to(study.parent.resolve()) or digest(path)!=sha:raise ValueError('复核证据身份改变')
    prefix=study/'stage20-snapshot/run/metrics.json'
    if digest(prefix)!=stage['history20_sha256'] or saved['history'][:20]!=json.loads(prefix.read_text(encoding='utf-8')):
        raise ValueError('前20轮轨迹改变')
    if saved['epoch']==20 and digest(study/'run/last.pt')!=stage['checkpoint_at_start_sha256']:
        raise ValueError('第20轮起点检查点改变')


if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment();p=argparse.ArgumentParser();p.add_argument('--study',required=True);p.add_argument('--stage',choices=('second','refinement25'),default='second');a=p.parse_args();print(json.dumps(resume(a.study,a.stage)),flush=True)
