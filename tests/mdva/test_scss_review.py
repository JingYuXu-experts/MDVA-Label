import pytest
from mdva.training.scss_review import budget_gate

LIMITS={'validation_dice_at8':.75,'last4_minus_first4_mean_dice':.02,'or_validation_dice_at8':.79}
def history(values):return [{'epoch':i+1,'validation':{'dice':v}} for i,v in enumerate(values)]

def test_budget_requires_complete_epochs_and_finite_results():
    with pytest.raises(ValueError):budget_gate(history([.8]*7),LIMITS)
    with pytest.raises(ValueError):budget_gate(history([.8]*7+[float('nan')]),LIMITS)

def test_last_epoch_and_trend_decide_continuation_not_cherry_picked_best():
    assert budget_gate(history([.6,.65,.7,.7,.75,.76,.77,.78]),LIMITS)['gate_passed']
    assert budget_gate(history([.8]*8),LIMITS)['gate_passed']
    assert not budget_gate(history([.9,.8,.7,.7,.7,.7,.7,.7]),LIMITS)['gate_passed']
    assert not budget_gate(history([.76]*8),LIMITS)['gate_passed']


def test_review_refuses_live_training_before_touching_pixels_or_weights(tmp_path):
    import json
    from mdva.training.scss_review import run
    study=tmp_path/'study';(study/'run').mkdir(parents=True)
    dataset=tmp_path/'dataset';dataset.mkdir();(dataset/'sources.json').write_text('{}')
    (study/'protocol.json').write_text(json.dumps({'dataset':str(dataset)}))
    (study/'run/status.json').write_text(json.dumps({'state':'running','completed_epoch':7}))
    output=tmp_path/'review'
    with pytest.raises(ValueError,match='训练停止'):run(study,output)
    assert not output.exists()
