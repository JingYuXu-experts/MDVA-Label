"""Expose the independent improvement study without changing frozen experiment counts."""
from pathlib import Path
import json


def read(path,default=None):
    try:return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:return default


def contrast_summary(output):
    folder = output / 'recognition-improvement-20261118/fl-local-contrast-01'
    plan = read(folder / 'protocol.json', {})
    arms = ('control', 'contrast')
    if plan.get('arms') != list(arms) or plan.get('budget_epochs_per_arm') != 3:
        return None
    statuses = {arm: read(folder / arm / 'run/status.json') for arm in arms}
    if not any(statuses.values()):
        return None

    arm = 'contrast' if statuses['contrast'] else 'control'
    status = statuses[arm]
    if status['state'] == 'running':
        import psutil

        try:
            owner = read(output.parent / '.mdva/training-gpu.lock', {})
            process = psutil.Process(status['pid'])
            args = process.cmdline()
            actual = (Path(process.cwd()) / args[args.index('--study') + 1]).resolve()
            verified = (
                owner['pid'] == status['pid']
                and abs(process.create_time() - owner['birth']) < .01
                and 'mdva.training.fl_contrast_study' in args
                and args[args.index('--arm') + 1] == arm and actual == folder.resolve()
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError, KeyError, ValueError, IndexError):
            verified = False
        if not verified:
            status = {**status, 'state': 'needs_verification'}

    label = '原灰度控制组' if arm == 'control' else '局部对比组'
    if status['state'] == 'completed':
        status = {**status, 'state': 'completed_budget', 'reason': None}
    fraction = (
        f'正在处理第 {status["active_epoch"]} 轮，已读 {status["images_processed"]}/{status["train_images"]} 张训练图。'
        if status['state'] == 'running' and 'images_processed' in status else ''
    )
    next_gate = (
        '控制组完成后运行局部对比组，同样训练三轮。'
        if arm == 'control' else '两组都完成后复算第 3 轮，比较区域查准率、检出率及各台站表现。'
    )
    review = read(folder.parent / 'fl-local-contrast-review-01/summary.json', {})
    if review.get('state') == 'completed':
        next_gate = (
            f'第 3 轮开发验证复算完成，区域检出率变动 {review["recall_gain"] * 100:+.2f} 个百分点，'
            f'查准率变动 {review["precision_gain"] * 100:+.2f} 个百分点。'
        )
    return {
        'title': f'暗条局部对比模型对照 · {label}',
        'scope': '3033 张训练图、182 张开发验证图；两组从同一检查点开始。',
        'evaluation_label': '开发验证 Dice', 'active_run': arm,
        'budget': f'每组固定 3 轮。{fraction}', 'next_gate': next_gate,
        'status': status, 'history': [
            {'epoch': row['epoch'], 'train_loss': row['train_loss'],
             'dice': row['validation']['dice'], 'seconds': row['seconds']}
            for row in read(folder / arm / 'run/metrics.json', [])
        ],
    }


def summary(output):
    output = Path(output)
    contrast = contrast_summary(output)
    if contrast:
        return contrast
    folder=output/'recognition-improvement-20261116'
    study=read(folder/'study.json')
    if not study:return None
    run=study.get('active_run','')
    if run not in ('probe','probe-long','development','regional-control','regional-weighted'):return None
    status=read(folder/run/'status.json',{})
    history=read(folder/run/'metrics.json',[])
    decision=read(folder/'regional-decision.json') if run.startswith('regional-') else None
    if decision and status.get('state')=='paused' and status.get('completed_epoch')==3 and status.get('reason')=='本次运行epoch预算用尽':
        status={**status,'raw_state':'paused','state':'completed_budget'}
    if status.get('state')=='running':
        import psutil
        try:
            process=psutil.Process(status['pid'])
            module='mdva.training.regional_train' if run.startswith('regional-') else 'mdva.training.author_train'
            if module not in ' '.join(process.cmdline()):status={**status,'state':'needs_verification'}
        except (psutil.NoSuchProcess,psutil.AccessDenied,KeyError):status={**status,'state':'needs_verification'}
    return {**study,'status':status,'decision':decision,'history':[{'epoch':r['epoch'],'train_loss':r['train_loss'],
        'dice':r['validation']['dice'],'seconds':r['seconds']} for r in history]}
