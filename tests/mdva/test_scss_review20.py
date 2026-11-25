import json
import pytest
from mdva.training import scss_review20 as review
from mdva.training.runtime import digest


def setup(study,tmp_path,state):
    (study/'run').mkdir(parents=True);dataset=tmp_path/'dataset';dataset.mkdir();(dataset/'sources.json').write_text('{}')
    (study/'protocol.json').write_text(json.dumps({'dataset':str(dataset)}));(study/'active-stage.json').write_text('{}')
    (study/'evaluation-20-protocol-v2.json').write_text(json.dumps({'training_protocol_sha256':digest(study/'protocol.json'),
        'stage_sha256':digest(study/'active-stage.json'),'evaluator_sha256':digest(review.__file__),
        'evaluation_code':{},'checkpoints':['best','last']}))
    (study/'run/status.json').write_text(json.dumps({'state':state,'completed_epoch':20}))


def test_twentieth_epoch_still_running_cannot_be_reviewed(tmp_path):
    study=tmp_path/'study';setup(study,tmp_path,'running');out=tmp_path/'out'
    with pytest.raises(ValueError,match='训练停止'):review.run(study,out)
    assert not out.exists()


def test_arbitrary_checkpoint_or_test_selector_is_rejected(tmp_path):
    study=tmp_path/'study';setup(study,tmp_path,'paused')
    with pytest.raises(ValueError,match='best/last'):review.run(study,tmp_path/'out',checkpoint_kind='test')
