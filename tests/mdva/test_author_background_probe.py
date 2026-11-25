import pytest
from mdva.training.author_background_probe import select


def rows():
    return [{'uri':str(i),'split':'train','category':'CH','mask_statistics':{'pixels':{'0':10000-i,'1':i}}} for i in range(90)]


def test_shared_stratified_training_selection_is_fixed_and_complete():
    data=rows();a=select(data);b=select(list(reversed(data)))
    assert a==b and len(a)==30 and len({r['uri'] for r in a})==30
    assert all(sum(lo<=int(r['uri'])<hi for r in a)==10 for lo,hi in [(0,30),(30,60),(60,90)])


def test_validation_or_other_task_cannot_enter_training_pilot():
    for key,value in [('split','validation'),('split','test'),('category','AR')]:
        data=rows();data[0][key]=value
        with pytest.raises(ValueError,match='训练'):select(data)
