import json

import pytest

from mdva.web.training import detail, summary


def test_localization_summary_ignores_unfinished_reports(tmp_path):
    from mdva.web.training import localization_report
    from mdva.training.durable import atomic_json
    atomic_json(tmp_path/'model-localization/01/summary.json',{'state':'completed','scope':'complete_validation_only','models':{'AR':{}}})
    atomic_json(tmp_path/'model-localization/02/summary.json',{'state':'running','scope':'complete_validation_only'})
    assert localization_report(tmp_path)['models']=={'AR':{}}


def test_no_experiments_is_honest_empty_state(tmp_path):
    assert summary(tmp_path)['available'] is False
    with pytest.raises(ValueError): detail(tmp_path, '../../private')


def test_completed_release_does_not_display_pending_test_confirmation(tmp_path):
    from mdva.training.durable import atomic_json
    folder=tmp_path/'out/training-release'
    atomic_json(folder/'protocol.json',{'recognition':[],'resources':{}})
    atomic_json(folder/'approved-revision.json',{'order':[]})
    atomic_json(folder/'closeout.json',{'state':'completed'})
    atomic_json(folder/'recognition-test-state.json',{'state':'completed','completed':12})
    atomic_json(folder/'recognition-test-authorization.json',{'state':'confirmed'})
    atomic_json(folder/'selection-lock.json',{})
    result=summary(tmp_path)
    phases={row['id']:row for row in result['phases']}
    assert phases['M5']['state']=='partial'  # FL remains sealed.
    assert '已确认' in phases['M8']['gate']
    assert result['progress']['phase']=='M11'
    assert '独立测试已完成' in result['progress']['next_step']


@pytest.mark.parametrize('state',['paused','failed','completed'])
def test_stopped_training_keeps_last_run_checkpoint_and_metrics(tmp_path,state):
    folder=tmp_path/'out/training-saved';run=folder/'runs/AR-A0-s17';run.mkdir(parents=True)
    (folder/'protocol.json').write_text(json.dumps({'recognition':[{'id':'AR-A0-s17'}],'resources':{}}))
    (folder/'configs').mkdir()
    (folder/'configs/AR-A0-s17.json').write_text(json.dumps({'id':'AR-A0-s17'}))
    (folder/'queue-state.json').write_text(json.dumps({'active':'AR-A0-s17'}))
    (run/'status.json').write_text(json.dumps({'id':'AR-A0-s17','state':state,'completed_epoch':7,
        'best_validation_dice':.8,'updated_utc':'2026-11-15T00:00:00+00:00'}))
    (run/'metrics.json').write_text(json.dumps([{'epoch':7}]))
    (run/'last.pt').write_bytes(b'checkpoint metadata fixture')
    value=summary(tmp_path)['progress']
    assert value['active'] is None
    assert value['last_run']['id']=='AR-A0-s17' and value['last_run']['state']==state
    assert value['checkpoint']['exists'] and value['checkpoint']['completed_epoch']==7
    assert detail(tmp_path,'AR-A0-s17')['metrics']==[{'epoch':7}]


def test_dashboard_distinguishes_stale_process_unrun_and_test(tmp_path):
    folder = tmp_path/'out/training-20261112-test'; folder.mkdir(parents=True)
    config = {'id': 'FL-F2-s17'}
    (folder/'protocol.json').write_text(json.dumps({'recognition': [config], 'resources': {}}))
    run = folder/'runs/FL-F2-s17'; run.mkdir(parents=True)
    (run/'status.json').write_text(json.dumps({'id': 'FL-F2-s17', 'state': 'running', 'completed_epoch': 1,
                                            'updated_utc': '2020-01-01T00:00:00+00:00'}))
    value = summary(tmp_path)
    assert value['runs'][0]['state'] == 'needs_verification'
    assert value['completed'] == 0 and not value['test_unlocked']
    assert not value['models']


def test_routes_use_configured_output_without_opening_annotation_database(tmp_path):
    from mdva.web.deps import Workspaces
    from mdva.web.routes import call
    output = tmp_path/'custom-results'; folder = output/'training-20261112-test'
    folder.mkdir(parents=True)
    (folder/'protocol.json').write_text(json.dumps({'recognition': [], 'resources': {}}))
    ws = Workspaces(tmp_path/'missing-data', out_dir=output)
    response = call(ws, 'GET', '/api/training/summary')
    assert response.status == 200 and response.json_body['available']
    assert not ws._hubs
    assert call(ws, 'GET', '/api/training/run', {'id': '../private'}).status == 400
    assert call(ws, 'POST', '/api/training/summary').status == 405


def test_local_logs_can_be_previewed_downloaded_and_do_not_accept_paths(tmp_path):
    from mdva.web.deps import Workspaces
    from mdva.web.routes import call
    from mdva.training.journal import append
    output = tmp_path/'results'; folder = output/'training-test'; folder.mkdir(parents=True)
    (folder/'protocol.json').write_text(json.dumps({'recognition': [], 'resources': {}}))
    (folder/'训练进度与接续.md').write_text('# 本地进度\n\n下一步继续训练。', encoding='utf-8')
    append(folder, 'handoff', '轮次检查点已保存', '下次从本地继续。')
    ws = Workspaces(tmp_path/'data', out_dir=output)
    preview = call(ws, 'GET', '/api/training/file', {'id': 'handoff'})
    assert preview.status == 200 and '下一步' in preview.json_body['text']
    download = call(ws, 'GET', '/api/training/file', {'id': 'handoff', 'download': '1'})
    assert download.data == (folder/'训练进度与接续.md').read_bytes()
    assert 'attachment' in download.headers['Content-Disposition']
    for name in ('../../private', 'C:\\Windows\\win.ini', '@ssafd/secret'):
        assert call(ws, 'GET', '/api/training/file', {'id': name}).status == 400
    journal = call(ws, 'GET', '/api/training/journal', {'kind': 'handoff'})
    assert journal.json_body['total'] == 1
    assert call(ws, 'GET', '/api/training/journal', {'page': '-1'}).status == 400
    assert not ws._hubs
