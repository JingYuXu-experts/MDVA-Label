import json
import pytest
import torch
from mdva.training import fl_main as main
from mdva.training.runtime import digest


def test_full_pilot_refuses_failed_learning_gate(tmp_path):
    rate=tmp_path/'rate';rate.mkdir();(rate/'summary.json').write_text(json.dumps({'state':'completed','learning_gate_passed':False}))
    with pytest.raises(ValueError,match='门槛未通过'):main.prepare(rate,tmp_path/'full')
    assert not (tmp_path/'full').exists()


def test_full_pilot_uses_source_lr_no_scaler_skips_and_restores_engine(tmp_path,monkeypatch):
    study=tmp_path/'study';study.mkdir()
    monkeypatch.setattr(main.engine,'data_identity',lambda _: {})
    p={'code':main.identity(),'data_identity':{},'dataset':str(tmp_path/'dataset'),'source_provenance_sha256':digest(main.SOURCE/'provenance.json'),'config':{}}
    (study/'protocol.json').write_text(json.dumps(p))
    old_arch=main.engine.architecture;old_loss=main.engine.loss_value
    def fake_train(*a,**kw):
        param=torch.nn.Parameter(torch.ones(1));optimizer=main.engine.torch.optim.Adam([param],lr=1e-3)
        assert optimizer.param_groups[0]['lr']==1e-4
        assert not main.engine.torch.amp.GradScaler('cuda',enabled=True).is_enabled()
        param.grad=torch.full_like(param,float('nan'));optimizer.step()
    monkeypatch.setattr(main.engine,'train',fake_train)
    with pytest.raises(ValueError,match='非有限梯度'):main.train(study)
    assert main.engine.architecture is old_arch and main.engine.loss_value is old_loss and main.engine.torch is torch
