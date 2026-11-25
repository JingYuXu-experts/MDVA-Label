import json
import pytest
from mdva.training.scss_main import prepare


def test_full_training_rejects_failed_learning_gate_without_creating_output(tmp_path):
    data=tmp_path/'dataset';data.mkdir();(data/'sources.json').write_text('{}')
    convergence=tmp_path/'convergence';convergence.mkdir()
    (convergence/'decision.json').write_text(json.dumps({'gate_passed':False}))
    output=tmp_path/'main'
    with pytest.raises(ValueError,match='门槛未过'):prepare(data,convergence,output)
    assert not output.exists()


def test_full_trainer_rejects_nan_gradient_and_restores_engine(tmp_path,monkeypatch):
    import torch
    from mdva.training import scss_main as main
    from mdva.training.runtime import digest
    data=tmp_path/'dataset';data.mkdir();(data/'sources.json').write_text('{}');(data/'manifest-hashes.json').write_text('{}')
    convergence=tmp_path/'convergence';(convergence/'rate-control').mkdir(parents=True)
    protocol=convergence/'rate-control/protocol.json';protocol.write_text('{}')
    (convergence/'decision.json').write_text(json.dumps({'gate_passed':True,'protocol_sha256':digest(protocol)}))
    study=tmp_path/'study';main.prepare(data,convergence,study)
    architecture=main.engine.architecture
    def fake_train(*args,**kwargs):
        scaler=main.engine.torch.amp.GradScaler('cuda',enabled=True)
        assert not scaler.is_enabled()
        parameter=torch.nn.Parameter(torch.ones(1));parameter.grad=torch.full((1,),float('nan'))
        optimizer=main.engine.torch.optim.Adam([parameter],lr=.001)
        optimizer.step()
    monkeypatch.setattr(main.engine,'train',fake_train)
    with pytest.raises(ValueError,match='非有限梯度'):main.train(study)
    assert main.engine.architecture is architecture and main.engine.torch is torch
