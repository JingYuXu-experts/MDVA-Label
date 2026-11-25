import json

import pytest

from mdva.training.durable import atomic_json
from mdva.training import recognition_release as release


@pytest.fixture
def scope(tmp_path,monkeypatch):
    dataset=tmp_path/'data';dataset.mkdir()
    for name in ('manifest-hashes.json','source-files.jsonl'):(dataset/name).write_text('{}')
    (dataset/'segmentation-ready.jsonl').write_text('\n'.join(json.dumps(
        {'eligible':True,'category':category,'split':'test'}) for category in ('AR','CH')))
    experiment=tmp_path/'experiment';experiment.mkdir()
    atomic_json(experiment/'protocol.json',{'dataset':str(dataset),
        'recognition':[{'id':i} for i in release.EVALUATION_IDS]})
    monkeypatch.setattr(release,'checkpoint_evidence',lambda _p,i:{'checkpoint_sha256':i,'identity_sha256':'identity'})
    release.prepare_protocol(experiment)
    return experiment


def test_scope_requires_separate_exact_confirmation_and_rejects_changed_protocol(scope):
    from mdva.training.delivery import selection_lock,evaluate_run
    from mdva.training.runtime import digest
    assert release.prepare_protocol(scope)['model_image_evaluations']==12
    with pytest.raises(ValueError,match='单独确认'):selection_lock(scope)
    with pytest.raises(ValueError,match='单独确认'):evaluate_run(scope,'AR-A0-s17','test')
    assert not (scope/'selection-lock.json').exists()
    with pytest.raises(ValueError,match='SHA'):release.authorize(scope,'wrong')
    release.authorize(scope,digest(scope/release.PROTOCOL))
    lock=selection_lock(scope)
    assert set(lock['recognition'])==set(release.EVALUATION_IDS)
    assert lock['candidates']==release.CANDIDATES
    with pytest.raises(ValueError):selection_lock(scope)
    for identifier,split in [('FL-F0-s17','test'),('CH-C0-s17','test'),('AR-A0-s17','calibration')]:
        with pytest.raises(ValueError,match='封存'):evaluate_run(scope,identifier,split)
    protocol=json.loads((scope/release.PROTOCOL).read_text())
    protocol['threshold']=.3;atomic_json(scope/release.PROTOCOL,protocol)
    with pytest.raises(ValueError,match='单独确认'):release.validate_authorized_scope(scope)


def test_freeze_draft_is_idempotent_and_never_overwrites_changed_evidence(scope,monkeypatch):
    before=(scope/release.PROTOCOL).read_bytes()
    release.prepare_protocol(scope)
    assert (scope/release.PROTOCOL).read_bytes()==before
    monkeypatch.setattr(release,'checkpoint_evidence',lambda *_:{'checkpoint_sha256':'changed'})
    with pytest.raises(ValueError,match='已存在'):release.prepare_protocol(scope)
    assert (scope/release.PROTOCOL).read_bytes()==before


def test_approved_counts_keep_deferred_separate():
    p={'recognition':[{'id':'AR-A0-s17'},{'id':'CH-C0-s17'}]}
    runs=[{'id':'AR-A0-s17','state':'completed'},{'id':'CH-C0-s17','state':'not_started'}]
    result=release.scope_counts(p,runs,{'order':['AR-A0-s17']})
    assert result['approved_completed']==result['approved_total']==1
    assert result['deferred_total']==1 and result['approved_scope_complete']


def test_primary_pairing_rejects_duplicates_and_missing_runs():
    from mdva.training.recognition_closeout import primary_statistics
    rows=[]
    for family in release.FAMILIES:
        for seed in release.SEEDS:
            for i in range(4):
                rows.append({'run_id':f'{family}-s{seed}','split':'validation','sample_id':str(i),'uri':f'{i}.jpg',
                             'time_utc':f'2021-0{i+1}-01T00:00:00+00:00',
                             'dice':.7+(.05 if family.endswith(('A1','C2')) else 0)})
    result=primary_statistics(rows)
    assert len(result)==2 and all(r['images']==4 for r in result)
    assert all(r['difference_b_minus_a']==pytest.approx(.05) for r in result)
    assert all(0<=r['p_holm']<=1 for r in result)
    with pytest.raises(ValueError,match='重复'):primary_statistics(rows+[rows[0]])


def test_cli_status_reports_approved_counts_without_hiding_original(tmp_path):
    from mdva.training.__main__ import status
    atomic_json(tmp_path/'protocol.json',{'recognition':[{'id':'AR-A0-s17'},{'id':'CH-C0-s17'}]})
    atomic_json(tmp_path/'approved-revision.json',{'revision':'R1','order':['AR-A0-s17']})
    atomic_json(tmp_path/'runs/AR-A0-s17/status.json',{'id':'AR-A0-s17','state':'completed'})
    atomic_json(tmp_path/'label-audit.json',{'gates':{}})
    value=status(tmp_path)
    assert value['complete']==1 and value['total']==2
    assert value['approved_completed']==value['approved_total']==1 and value['deferred_total']==1
