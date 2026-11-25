import json
import pytest
from mdva.training import scss_review25 as review
from mdva.training.runtime import digest


@pytest.mark.parametrize(('state','epoch'), [('running',25),('paused',20),('paused',24)])
def test_refinement_review_rejects_partial_or_live_stage_before_pixels(tmp_path,state,epoch):
    study=tmp_path/'study';(study/'run').mkdir(parents=True)
    dataset=tmp_path/'dataset';dataset.mkdir();(dataset/'sources.json').write_text('{}')
    (study/'protocol.json').write_text(json.dumps({'dataset':str(dataset)}))
    (study/'refinement-25.json').write_text('{}')
    plan={'training_protocol_sha256':digest(study/'protocol.json'),
        'stage_sha256':digest(study/'refinement-25.json'),'evaluator_sha256':digest(review.__file__),
        'evaluation_code':{},'checkpoints':['best','last']}
    (study/'evaluation-25-protocol.json').write_text(json.dumps(plan))
    (study/'run/status.json').write_text(json.dumps({'state':state,'completed_epoch':epoch}))
    with pytest.raises(ValueError,match='25轮完整保存'):
        review.run(study,tmp_path/'review')
    assert not (tmp_path/'review').exists()
