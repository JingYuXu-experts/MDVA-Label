import copy
import numpy as np
import pytest

torch = pytest.importorskip('torch')
from mdva.training.author_tfjs import BrowserCNN193, NativeTranspose, native_binary, read_snapshot


def tiny_graph():
    layers = [{'name':'input','class_name':'InputLayer','config':{'batch_input_shape':[None,256,256,1]},'inbound_nodes':[]}]
    weights = {}
    def conv(name, previous, inputs, outputs):
        layers.append({'name':name,'class_name':'Conv2D','config':{'kernel_size':[3,3],'padding':'same',
            'activation':'linear','use_bias':True,'data_format':'channels_last','strides':[1,1],'filters':outputs},
            'inbound_nodes':[[[previous,0,0,{}]]]})
        weights[name+'/kernel'] = np.zeros((3,3,inputs,outputs),np.float32)
        weights[name+'/bias'] = np.full(outputs,-.25,np.float32)
    conv('first','input',1,1)
    for i in range(64):
        layers.append({'name':f'elu{i}','class_name':'Activation','config':{'activation':'elu'},
                       'inbound_nodes':[[['first' if i == 0 else f'elu{i-1}',0,0,{}]]]})
    conv('last','elu63',1,1)
    model = {'modelTopology':{'model_config':{'config':{'layers':layers,'input_layers':[['input',0,0]],'output_layers':[['last',0,0]]}}}}
    return model,weights


def test_native_output_keeps_linear_values_and_is_inference_only():
    graph,weights = tiny_graph();model = BrowserCNN193(graph,weights)
    with torch.inference_mode():
        result=model(torch.zeros(1,1,32,32),synthetic=True)
    assert torch.equal(result,torch.full_like(result,-.25))
    assert not any(p.requires_grad for p in model.parameters())
    with pytest.raises(ValueError,match='只允许推理'):model.train()
    with pytest.raises(ValueError,match='原生256'):model(torch.zeros(1,1,32,32))
    with pytest.raises(ValueError,match='数值'):model(torch.full((1,1,256,256),float('nan')))


def test_hwio_mapping_and_unknown_graph_rejected():
    graph,weights = tiny_graph();weights['first/kernel']=np.arange(9,dtype=np.float32).reshape(3,3,1,1)
    model=BrowserCNN193(graph,weights)
    assert np.array_equal(model.layers['first'].weight.detach().numpy()[0,0],np.arange(9).reshape(3,3))
    extra=copy.deepcopy(weights);extra['unclaimed']=np.zeros(1,np.float32)
    with pytest.raises(ValueError,match='未完全消费'):BrowserCNN193(graph,extra)
    graph['modelTopology']['model_config']['config']['layers'][2]['class_name']='Lambda'
    with pytest.raises(ValueError,match='不支持'):BrowserCNN193(graph,weights)


def test_native_transpose_alignment_matches_impulse():
    op=NativeTranspose(1,1,3,stride=2)
    with torch.no_grad():op.weight.fill_(1);op.bias.zero_()
    x=torch.zeros(1,1,2,2);x[0,0,0,0]=1
    expected=torch.zeros(1,1,4,4);expected[:,:,:3,:3]=1
    assert torch.equal(op(x),expected)


def test_transpose_hwoi_becomes_iohw_without_spatial_flip():
    graph,weights=tiny_graph();layer=graph['modelTopology']['model_config']['config']['layers'][1]
    layer['class_name']='Conv2DTranspose';layer['config']['strides']=[2,2]
    layer['config']['filters']=3
    original=np.arange(3*3*3,dtype=np.float32).reshape(3,3,3,1)
    weights['first/kernel']=original
    weights['first/bias']=np.zeros(3,np.float32)
    weights['last/kernel']=np.zeros((3,3,3,1),np.float32)
    model=BrowserCNN193(graph,weights)
    assert np.array_equal(model.layers['first'].weight.detach().numpy(),original.transpose(3,2,0,1))


def test_rounding_is_browser_semantics_without_sigmoid():
    values=np.array([-10,-.5,0,.499,.5,1,1.499,1.5,3],np.float32)
    assert native_binary(values).tolist()==[False,False,False,False,True,True,True,False,False]
    with pytest.raises(ValueError,match='非有限'):native_binary([np.inf])


def test_snapshot_rejects_unregistered_origin_before_loading_weights(tmp_path):
    import json
    (tmp_path/'snapshot.json').write_text(json.dumps({'commit':'wrong','model_git_blob':'wrong'}))
    with pytest.raises(ValueError,match='登记'):read_snapshot(tmp_path)
