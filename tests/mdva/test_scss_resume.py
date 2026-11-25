import pytest
from mdva.training.scss_resume import remaining


def test_resume_uses_remaining_absolute_budget_not_whole_new_budget():
    stage={'starting_epoch':8,'target_epoch':20}
    assert remaining(stage,8)==12
    assert remaining(stage,15)==5
    assert remaining(stage,20)==0
    with pytest.raises(ValueError):remaining(stage,7)
    with pytest.raises(ValueError):remaining(stage,21)


def test_refinement_preserves_twentieth_epoch_and_refuses_budget_expansion(tmp_path):
    import json
    from mdva.training import scss_resume as resume
    from mdva.training.runtime import digest
    study=tmp_path/'study';(study/'run').mkdir(parents=True)
    snapshot=study/'stage20-snapshot/run';snapshot.mkdir(parents=True)
    history=[{'epoch':i,'validation':{'dice':.8}} for i in range(1,21)]
    (snapshot/'metrics.json').write_text(json.dumps(history));(study/'run/last.pt').write_bytes(b'original20')
    stage={'starting_epoch':20,'target_epoch':25,'additional_budget':5,
        'resume_code_sha256':digest(resume.__file__),'review_evidence':{},
        'history20_sha256':digest(snapshot/'metrics.json'),'checkpoint_at_start_sha256':digest(study/'run/last.pt')}
    resume.validate_refinement(study,stage,{'epoch':20,'history':history})
    assert remaining(stage,23)==2
    with pytest.raises(ValueError,match='20至25'):
        resume.validate_refinement(study,{**stage,'target_epoch':30},{'epoch':20,'history':history})
    changed=[*history];changed[0]={'epoch':1,'validation':{'dice':.9}}
    with pytest.raises(ValueError,match='轨迹改变'):
        resume.validate_refinement(study,stage,{'epoch':21,'history':changed})
    with pytest.raises(ValueError,match='证据身份'):
        resume.validate_refinement(study,{**stage,'review_evidence':{'../other.json':'0'*64}},{'epoch':20,'history':history})
