"""Read-only experiment dashboard; the web Python never imports training libraries."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


def experiment_folder(root, output=None):
    candidates = list((Path(output) if output else Path(root)/'out').glob('training-*/protocol.json'))
    if not candidates:
        raise FileNotFoundError('尚未建立训练协议')
    return max(candidates, key=lambda p: p.stat().st_mtime_ns).parent


def files(root, *, output=None):
    folder = experiment_folder(root, output)
    return {'files': [{key: value for key, value in row.items() if key != 'path'}
                      for row in artifact_paths(folder).values()]}


def artifact_paths(folder):
    from ..training.run_locations import run_folder
    project = Path(__file__).resolve().parents[3]
    registered = {r['id'] for r in read(folder/'protocol.json')['recognition']}
    active = read(folder/'queue-state.json', {}).get('active', '')
    if active not in registered or not re.fullmatch(r'(AR-A[01]|CH-C[012]|FL-F[0-8])-s(17|42|2026)', active):
        active = None
    definitions = [
        ('forecast-supervision-guide','预测标签与观测覆盖核验',project/'docs/预测监督-权威目录与观测覆盖审计-20261116.md','正例、负例与未知分开；当前没有新增预测训练'),
        ('forecast-supervision-report','预测监督首轮审计数据',project/'out/forecast-supervision-20261116/audit-03/summary.json','真实数量、来源哈希、旧标签差异和剩余科学门槛'),
        ('direct-recognition-guide','图片直接识别与本轮实验结论',project/'docs/图片直接识别与区域损失对照-交付接续-20261116.md','无需建数据集的图片输入、真实验收、区域损失对照与接续'),
        ('regional-results','区域损失同预算对照报告',project/'out/recognition-improvement-20261116/regional-REPORT.md','固定末轮验证、漏检与不采用的理由'),
        ('release-guide','当前M7—M12交付与接续总账',project/'docs/模型交付-M7至M12实施与接续-20261116.md','当前状态、独立测试结果、使用方式和剩余人工验收'),
        ('human-trial-template','60图人工试用计时表',project/'out/acceptance/model-release-20261116/人工试用计时.csv','空白时间和质量字段，必须实际操作后填写；不包含虚构数字'),
        ('monitor-soak','连续监测观察快照',project/'out/acceptance/model-release-20261116/monitor-soak.json','逐信源成功检查比例、最大间断及记录跨度；不是在线率保证'),
        ('handoff', '实时进度与下一步', folder/'训练进度与接续.md', '按轮次和状态变化自动保存，重新打开即可接续'),
        ('plan', 'M0—M6 实施与接续总账', project/'docs/模型训练-M0至M6实施与接续总账-20261112.md', '阶段、工作量、交付门槛与完整接续说明'),
        ('findings', '首轮结果与缺陷台账', project/'docs/模型实验-首轮结果与缺陷台账-20261112.md', '研究结果、已修问题与尚未通过的门槛'),
        ('progress', '实时进度记录 JSON', folder/'checkpoint-progress.json', '可供下一次开发任务直接读取的结构化状态'),
        ('coordinator', '连续队列运行日志', folder/'continuation-console.log', '后续模型队列输出及异常堆栈'),
        ('watchdog', '夜间守护实时状态', folder/'watchdog.json', '进程监看、防自动睡眠、恢复次数与故障状态'),
        ('watchdog-log', '自动恢复控制台日志', folder/'watchdog-console.log', '故障后原配置恢复的运行记录；尚未恢复时可能没有此文件'),
        ('watchdog-guide', '夜间训练与故障恢复说明', project/'docs/夜间训练与故障守护-20261113.md', '恢复边界、日志位置、暂停与下次启动方式'),
        ('diagnostic-guide', '效果诊断与验收记录', project/'docs/模型效果诊断-执行与验收-20261115.md', '诊断结论、真实案例、取舍门槛与后续接续条件'),
        ('revised-plan', '诊断后修订训练计划', project/'docs/模型训练-诊断后修订计划与验收-20261115.md', '当前获准范围、阶段与停止条件'),
        ('revised-effects', '修订训练效果分析', folder/'修订训练效果分析.md', '每项完整验证复算、学习信号、对照与原因限制'),
        ('revised-state', '修订队列真实状态', folder/'reviewed-state.json', '受限队列、当前子进程和停止原因'),
        ('fl-probe', '暗条小样本学习诊断', folder/'revisions/io-reviewed-v1/FL-learning-probe/status.json', '16张训练图学习检查，不是验证集或正式结果'),
        ('fl-probe-reasons', '暗条学习检查：原因与取舍', folder/'暗条学习检查-原因与取舍.md', '未过参考线的事实、原因假设与暂缓决定'),
        ('reviewed-guide', '修订训练工程验收与运行记录', project/'docs/修订训练-工程验收与运行记录-20261115.md', '工程修复、恢复一致性与逐项分析规则'),
        ('ar-precision-guide', '11月16日训练异常：原因、修复与接续', project/'docs/训练异常-FP16溢出修复与接续-20261116.md', '第28轮FP16溢出的真实复现、精度修复和检查点保护'),
        ('ar-precision-state', 'AR精度修复登记', folder/'ar-precision-revision.json', '独立代码身份、验收证据、影响范围和跨种子限制'),
        ('recognition-closeout','39项成果与冻结清单',folder/'成果与测试冻结清单.md','训练/验证统计与原始待确认协议快照；实际测试状态见独立测试结果'),
        ('recognition-protocol','12项独立评价冻结协议',folder/'recognition-evaluation-protocol.json','模型、数据、指标与代码哈希；保留确认时的原始版本'),
        ('recognition-test-results','AR/CH首次独立测试报告',folder/'reports/recognition-20261116/REPORT.md','12项冻结评价、三种子、配对区间与限制'),
        ('recognition-test-metrics','逐模型独立测试数据',folder/'reports/recognition-20261116/model-metrics.csv','真实首次测试结果，可下载CSV'),
    ]
    if active:
        definitions.extend([
            ('current-console', f'{active} 训练日志', run_folder(folder, active)/'console.log', '当前或最近实验的原始控制台日志'),
            ('current-metrics', f'{active} 逐轮指标', run_folder(folder, active)/'metrics.json', '真实训练、开发指标和资源记录'),
            ('current-precision-events', f'{active} 精度重试记录', run_folder(folder, active)/'precision-events.jsonl', '有限AMP输出不变；每次FP32重算及时间留痕'),
        ])
    result = {}
    for identifier, title, path, description in definitions:
        available = path.is_file()
        result[identifier] = {'id': identifier, 'title': title, 'description': description,
            'filename': path.name, 'available': available, 'size': path.stat().st_size if available else 0,
            'updated_utc': datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat() if available else None,
            'path': path}
    return result


def artifact(root, identifier, *, output=None, download=False):
    folder = experiment_folder(root, output)
    catalog = artifact_paths(folder)
    if identifier not in catalog:
        raise ValueError('未知训练文件编号；不接受任意本地路径')
    item = catalog[identifier]
    if not item['available']:
        raise FileNotFoundError('文件尚未生成')
    path = item['path']
    if download:
        if path.stat().st_size > 8*1024*1024:
            raise ValueError('日志过大，请在本机实验目录查看完整文件')
        return path.read_bytes(), item['filename']
    limit = 120000
    with path.open('rb') as stream:
        if item['size'] > limit:
            stream.seek(-limit, 2)
        raw = stream.read(limit)
    return {key: value for key, value in item.items() if key != 'path'} | {
        'text': raw.decode('utf-8', errors='replace'), 'truncated': item['size'] > limit}


def journal(root, page=1, page_size=12, kind='all', *, output=None):
    from ..training.journal import entries
    return entries(experiment_folder(root, output), page, page_size, kind)


def run_status(folder, identifier):
    from ..training.run_locations import run_folder
    current = run_folder(folder, identifier)
    result = read(current/'status.json', {'id': identifier, 'state': 'not_started', 'completed_epoch': 0})
    if current != folder/'runs'/identifier:
        result['numerical_revision'] = current.parent.parent.name
    # A stale file is not evidence that a process is still running.
    if result['state'] == 'running' and result.get('updated_utc'):
        age = (datetime.now(timezone.utc)-datetime.fromisoformat(result['updated_utc'])).total_seconds()
        result['update_age_seconds'] = max(0, age)
        if age > 300:
            result['state'] = 'needs_verification'
            result['notice'] = '超过5分钟没有新进度；请核对训练进程，不能据旧状态认定仍在运行'
    return result


def retain_last_run(folder, runs, progress):
    """A stopped process must not erase its saved progress from the dashboard."""
    from ..training.run_locations import run_folder
    known={r['id']:r for r in runs if r.get('state') not in ('not_started','deferred')}
    preferred=read(folder/'reviewed-state.json',{}).get('active') or read(folder/'queue-state.json',{}).get('active')
    last=progress.get('active') or known.get(preferred)
    if not last and known:
        last=max(known.values(),key=lambda r:r.get('updated_utc') or '')
    progress['last_run']=last
    if last and not progress.get('active'):
        current=run_folder(folder,last['id']);checkpoint=current/'last.pt'
        history=read(current/'metrics.json',[])
        saved_epoch=history[-1]['epoch'] if history else last.get('completed_epoch',0)
        progress['checkpoint']={'exists':checkpoint.is_file(),'filename':checkpoint.relative_to(folder).as_posix(),
            'completed_epoch':saved_epoch,
            'saved_utc':datetime.fromtimestamp(checkpoint.stat().st_mtime,timezone.utc).isoformat() if checkpoint.is_file() else None}
    return progress


def localization_report(output):
    candidates=[]
    for path in (Path(output)/'model-localization').glob('*/summary.json'):
        record=read(path,{})
        if record.get('state')=='completed' and record.get('scope')=='complete_validation_only':
            candidates.append((path.stat().st_mtime_ns,record))
    return max(candidates,key=lambda v:v[0])[1] if candidates else None


def summary(root, *, output=None):
    from .model_study import summary as study_summary
    root = Path(root)
    try:
        folder = experiment_folder(root, output)
    except FileNotFoundError:
        return {'available': False, 'runs': [], 'models': [], 'notice': '尚未建立训练协议'}
    protocol = read(folder/'protocol.json')
    runs = [run_status(folder, c['id']) for c in protocol['recognition']]
    audit = read(folder/'label-audit.json', {})
    official = read(folder/'official-catalogue/audit.json', {})
    from ..training.context import active_forecast
    forecast_folder = active_forecast(folder)
    forecast = []
    for file in sorted((forecast_folder/'forecast').glob('**/*result.json')):
        record = read(file)
        if 'validation_calibrated' not in record:
            continue
        relative = file.relative_to(forecast_folder/'forecast').as_posix()
        test = read(forecast_folder/'forecast-test'/relative.replace('/', '--').replace('.json', '')/'summary.json')
        forecast.append({'id': file.parent.name+'/'+file.name, 'model': record.get('model', record.get('id')),
                         'mode': record.get('mode'), 'validation': record.get('validation_calibrated'),
                         'test_evaluated': test is not None, 'test': test.get('calibrated') if test else None,
                         'state': record.get('state', 'completed')})
    complete = sum(r['state'] == 'completed' for r in runs)
    from ..training.journal import progress
    live_progress = progress(folder, protocol, runs)
    diagnostic_hold = read(folder/'diagnostic-hold.json')
    if diagnostic_hold and diagnostic_hold.get('requires_user_confirmation', True) and live_progress.get('paused_requested'):
        live_progress.update(headline='训练保持停止，效果诊断待确认',
            next_step='先查看上方效果诊断；用户确认取舍并解决WinError 5后，才恢复训练。',
            next_commands=[{'label': '查看原实验状态（不恢复训练）', 'command': 'python -m mdva.training status'}])
    approval = read(folder/'approved-revision.json')
    from ..training.recognition_release import scope_counts
    counts = scope_counts(protocol,runs,approval)
    for row in runs:
        if row['id'] in counts['deferred_ids'] and row['state']=='not_started':
            row.update(state='deferred',reason='修订计划暂缓，不属于本轮待训练项目')
    if approval:
        execution = read(folder/'reviewed-state.json', {})
        execution_age = ((datetime.now(timezone.utc)-datetime.fromisoformat(execution['updated_utc'])).total_seconds()
                         if execution.get('updated_utc') else float('inf'))
        executing = execution.get('state') in ('executing','analyzing') and execution_age < 30
        pending = [identifier for identifier in approval['order']
                   if next((r for r in runs if r['id'] == identifier), {}).get('state') != 'completed']
        live_progress.update(training_plan_revision='20261115-R1',
            automatic_continuation=executing and not live_progress.get('paused_requested'),
            phase='M2' if execution.get('active') in (None,'AR-A0-s17','CH-C1-s17') else
                  'M3' if execution.get('active') in approval['order'][2:6] else 'M4',
            headline=execution.get('reason', '修订训练已获准，按工程门槛与效果分析逐项执行'),
            next_step='每项结束复算验证与训练样本，失败停止留证；C0、新FL和最终测试不自动启动。',
            next_run_id=next((identifier for identifier in pending if identifier != (live_progress.get('active') or {}).get('id')), None),
            next_commands=[{'label':'查看真实状态','command':'python -m mdva.training status'}])
        if counts['approved_scope_complete'] and not executing:
            live_progress.update(headline=f"本轮{counts['approved_completed']}/{counts['approved_total']}完成；原登记{len(runs)}，暂缓{counts['deferred_total']}",
                state='completed',completed_percent=100.,
                phase='M8' if (folder/'closeout.json').exists() else 'M7',
                next_step='验收成果及测试冻结清单；确认前识别测试封存，训练不自动重启。',
                automatic_continuation=False,paused_requested=False,next_run_id=None,
                completed_runs=counts['approved_completed'],total_runs=counts['approved_total'])
    live_progress.update(**counts)
    test_state=read(folder/'recognition-test-state.json',{})
    if test_state.get('state')=='running':
        live_progress.update(state='evaluating',phase='M9',
            headline=f"独立测试推理：{test_state.get('active')}，已完成{test_state.get('completed',0)}/12（不训练）",
            next_step='固定模型和阈值评价，不根据测试改参；训练保持停止。')
    elif test_state.get('state')=='completed':
        live_progress.update(phase='M11',
            next_step='AR/CH独立测试已完成；查看研究候选并进行人工质量与耗时验收，训练保持停止。')
    elif test_state.get('state')=='completed':
        live_progress.update(phase='M11',headline='本轮训练39/39完成；AR/CH独立测试12/12完成；3项C0暂缓',
            next_step='进入标注页试用研究候选；人工质量/计时和长期监测仍待验收，不自动训练或扩展FL。')
    retain_last_run(folder,runs,live_progress)
    phases = [
        {'id': 'M0', 'name': '数据与实验协议', 'state': 'partial', 'gate': '识别数据及42项配置已冻结；权威预测监督仍有覆盖/等级门槛'},
        {'id': 'M1', 'name': '环境与资源基线', 'state': 'completed', 'gate': '独立Conda环境与GPU运算通过'},
        {'id': 'M2', 'name': '训练器与首个FL模型', 'state': next((r['state'] for r in runs if r['id']=='FL-F2-s17'), 'pending'),
         'gate': '可恢复训练、资源合格并完成首个FL正式运行'},
        {'id': 'M3', 'name': '识别主实验（获准范围）', 'state': 'completed' if counts['approved_scope_complete'] else 'running' if any(r['state']=='running' and r['id']!='FL-F2-s17' for r in runs) else 'pending', 'gate': '获准39项完成，3项C0暂缓；训练完成不代表产品验收'},
        {'id': 'M4', 'name': '预测主实验', 'state': 'partial' if forecast else 'pending', 'gate': '参考目录研究与权威监督分别验收，结果不能冒充实时业务预报'},
        {'id': 'M5', 'name': '消融与最终测试', 'state': 'partial' if test_state.get('state')=='completed' else 'running' if (folder/'selection-lock.json').exists() else 'partial' if (forecast_folder/'forecast-selection-lock.json').exists() else 'pending', 'gate': 'AR/CH测试见M9；FL测试继续封存，原全部任务未完成'},
        {'id': 'M6', 'name': '平台接入与论文材料', 'state': 'partial', 'gate': 'AR/CH候选已接入；实际人工试用与最终交付见M10—M12'},
    ]
    closeout=read(folder/'closeout.json')
    release=read(folder/'release-stages.json',{})
    if approval:
        phases += [
            {'id':'M7','name':'成果归档与状态纠正','state':'completed' if closeout else 'pending','gate':'39项检查点、诊断和状态一致'},
            {'id':'M8','name':'最终统计与选型冻结','state':'completed' if closeout else 'pending','gate':'12项AR/CH冻结清单已确认，候选与阈值保持固定' if (folder/'recognition-test-authorization.json').exists() else '冻结12项AR/CH；测试等待用户单独确认'},
            {'id':'M9','name':'AR/CH独立测试','state':'completed' if release.get('M9')=='completed' else test_state.get('state','blocked'),'gate':'按已单独确认的冻结清单评价；不依据测试改参'},
            *[{'id':i,'name':name,'state':release.get(i,'pending'),'gate':gate} for i,name,gate in [
                ('M10','模型候选与网页接入','无掩膜输入、独立推理任务、人工接纳'),
                ('M11','全流程与人工试用','工程回归与实际人工计时分别验收'),
                ('M12','交付与论文材料','只报告已有证据；未完成内容保留')]]]
    return {'available': True, 'experiment': folder.name, 'phases': phases, 'runs': runs,
            'completed': complete, 'total': len(runs), 'queue': read(folder/'queue-state.json', {}),
            **counts,'closeout':closeout,'evaluation_protocol':read(folder/'recognition-evaluation-protocol.json'),'test_evaluation':test_state,
            'test_results':read(folder/'test-results.json'),'localization_audit':localization_report(folder.parent),
            'improvement_study':study_summary(folder.parent),
            'progress': live_progress,
            'resources': protocol['resources'], 'forecast': forecast, 'gates': audit.get('gates', {}),
            'forecast_revision': forecast_folder.name if forecast_folder != folder else 'v1',
            'official_audit': {k: official.get(k) for k in ('unique_time_matches', 'M_threshold_disagreements', 'remaining_gates')},
            'models': [read(p) for p in sorted((folder/'packages').glob('*/model.json')) if not p.parent.name.startswith('.')],
            'test_unlocked': (folder/'selection-lock.json').exists()}


def detail(root, identifier, *, output=None):
    if not re.fullmatch(r'(AR-A[01]|CH-C[012]|FL-F[0-8])-s(17|42|2026)', identifier):
        raise ValueError('实验编号不合法')
    folder = experiment_folder(root, output)
    from ..training.run_locations import location
    scope, _ = location(folder, identifier)
    config = read(scope/'configs'/f'{identifier}.json')
    if config is None:
        raise FileNotFoundError('没有这个实验')
    run = scope/'runs'/identifier
    return {'config': config, 'status': run_status(folder, identifier), 'metrics': read(run/'metrics.json', []),
            'evaluation': {s: read(run/'evaluation'/s/'summary.json') for s in ('validation', 'calibration', 'test')}}
