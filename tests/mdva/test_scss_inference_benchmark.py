import json

import pytest

from mdva.training.scss_inference_benchmark import ready
from mdva.training.runtime import digest


def test_training_in_progress_blocks_benchmark_before_loading_model(tmp_path):
    study=tmp_path/'study'; (study/'run').mkdir(parents=True)
    (study/'run/status.json').write_text(json.dumps({'state':'running','completed_epoch':20}))
    with pytest.raises(ValueError, match='不抢占'):
        ready(study, tmp_path/'absent-review')


def test_reject_tampered_archived_model_and_test_reports(tmp_path):
    study=tmp_path/'study'; (study/'run').mkdir(parents=True)
    (study/'run/status.json').write_text(json.dumps({'state':'paused','completed_epoch':20}))
    (study/'protocol.json').write_text('{}')
    review=tmp_path/'review'; review.mkdir(); (review/'checkpoint.pt').write_bytes(b'weights')
    sha=digest(review/'checkpoint.pt')
    report={'state':'completed','exact_replay':True,'test_accessed':False,'calibration_accessed':False,
        'identity':{'checkpoint_kind':'best','training_protocol_sha256':digest(study/'protocol.json'),'checkpoint_sha256':sha},
        'checkpoint_archive':{'file':'checkpoint.pt','sha256':sha,'verified':True}}
    (review/'summary.json').write_text(json.dumps(report))
    assert ready(study,review)==report
    (review/'checkpoint.pt').write_bytes(b'tampered')
    with pytest.raises(ValueError,match='归档身份'):
        ready(study,review)
    report['test_accessed']=True
    (review/'summary.json').write_text(json.dumps(report))
    with pytest.raises(ValueError,match='测试或校准'):
        ready(study,review)
