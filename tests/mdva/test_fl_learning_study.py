import pytest
import torch
from mdva.training.fl_author_model import AuthorGraph, SOURCE, loss_value
from mdva.training.fl_learning_study import select_training


def test_author_graph_logit_adapter_is_exact_in_eval_and_controls_share_convs():
    if not (SOURCE / "provenance.json").is_file():
        pytest.skip("Requires the pinned DeepFilament author source snapshot")
    torch.set_num_threads(2);torch.manual_seed(2026);batch=AuthorGraph('batch')
    torch.manual_seed(2026);plain=AuthorGraph('none')
    convolution=lambda m:[v.detach() for x in m.modules() if isinstance(x,(torch.nn.Conv2d,torch.nn.ConvTranspose2d)) for v in x.parameters()]
    assert all(torch.equal(a,b) for a,b in zip(convolution(batch),convolution(plain)))
    assert sum(isinstance(m,torch.nn.BatchNorm2d) for m in batch.modules())==18
    assert not any(isinstance(m,torch.nn.BatchNorm2d) for m in plain.modules())
    batch.eval();x=torch.rand(2,1,32,32)
    with torch.inference_mode():assert torch.equal(batch(x).sigmoid(),batch.network(x))


def test_weighted_loss_changes_positive_gradient_without_changing_labels():
    logits=torch.zeros((1,1,32,32),requires_grad=True);target=torch.zeros_like(logits);target[0,0,12:14,12:14]=1
    a=loss_value(logits,target,1);ga=torch.autograd.grad(a,logits)[0]
    b=loss_value(logits,target,10);gb=torch.autograd.grad(b,logits)[0]
    assert torch.isfinite(gb).all() and gb[target==1].abs().sum()>ga[target==1].abs().sum()*3
    assert torch.equal(ga[target==0],gb[target==0]) and target.sum()==4


def test_learning_selection_refuses_heldout_images_and_duplicates():
    rows=[{'uri':str(i),'category':'FL','split':'train'} for i in range(16)]
    assert select_training({'probe_images':rows},rows)==rows
    with pytest.raises(ValueError):select_training({'probe_images':rows[:-1]+[rows[0]]},rows)
    for split in ('validation','calibration','test'):
        changed=[*rows];changed[0]={**rows[0],'split':split}
        with pytest.raises(ValueError):select_training({'probe_images':changed},rows)
