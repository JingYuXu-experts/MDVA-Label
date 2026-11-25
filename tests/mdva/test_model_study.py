from mdva.web.model_study import summary
from mdva.training.durable import atomic_json


def test_study_separates_training_probe_from_validation_and_old_counts(tmp_path):
    root=tmp_path/'recognition-improvement-20261116'
    assert summary(tmp_path) is None
    atomic_json(root/'study.json',{'active_run':'probe-long','title':'16 training images'})
    atomic_json(root/'probe-long/status.json',{'state':'paused','completed_epoch':5,'formal':False})
    atomic_json(root/'probe-long/metrics.json',[{'epoch':5,'train_loss':.5,'validation':{'dice':.4},'seconds':2}])
    value=summary(tmp_path)
    assert value['status']['formal'] is False and value['history'][0]['dice']==.4
    atomic_json(root/'study.json',{'active_run':'../other'})
    assert summary(tmp_path) is None


def test_missing_study_process_is_not_reported_running(tmp_path):
    root=tmp_path/'recognition-improvement-20261116'
    atomic_json(root/'study.json',{'active_run':'development'})
    atomic_json(root/'development/status.json',{'state':'running','pid':999999999})
    assert summary(tmp_path)['status']['state']=='needs_verification'


def test_completed_budget_is_separate_from_failure_or_unexpected_pause(tmp_path):
    root=tmp_path/'recognition-improvement-20261116'
    atomic_json(root/'study.json',{'active_run':'regional-weighted'})
    atomic_json(root/'regional-decision.json',{'gate_passed':False})
    status={'state':'paused','completed_epoch':3,'reason':'本次运行epoch预算用尽'}
    atomic_json(root/'regional-weighted/status.json',status)
    result=summary(tmp_path)
    assert result['status']['state']=='completed_budget' and result['status']['raw_state']=='paused'
    atomic_json(root/'regional-weighted/status.json',{**status,'state':'failed'})
    assert summary(tmp_path)['status']['state']=='failed'


def test_paired_contrast_progress_reports_saved_checkpoints_across_arms(tmp_path):
    output = tmp_path / 'out'
    study = output / 'recognition-improvement-20261118/fl-local-contrast-01'
    atomic_json(study / 'protocol.json', {'arms': ['control', 'contrast'], 'budget_epochs_per_arm': 3})
    atomic_json(study / 'control/run/status.json', {'state': 'completed', 'completed_epoch': 3})
    atomic_json(study / 'control/run/metrics.json', [
        {'epoch': 3, 'train_loss': .1, 'validation': {'dice': .4}, 'seconds': 20},
    ])
    completed = summary(output)
    assert completed['active_run'] == 'control'
    assert completed['status']['state'] == 'completed_budget'
    assert completed['history'][0]['dice'] == .4
    atomic_json(study / 'contrast/run/status.json', {'state': 'running', 'completed_epoch': 1, 'pid': 999999999})
    checkpoint = summary(output)
    assert checkpoint['active_run'] == 'contrast'
    assert checkpoint['status']['state'] == 'needs_verification'
    assert checkpoint['status']['completed_epoch'] == 1
