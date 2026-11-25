import pytest
from mdva.web.space_weather import download_cnn193_browser_asset,download_cnn193_demo_image
from mdva.training.author_model_snapshot import tree_digest,PINNED_TREE_SHA256


@pytest.mark.parametrize('name',['../model.json','group24-shard1of1','group1-shard3of3','https://example.com/model.json',''])
def test_weight_downloader_rejects_unregistered_path_without_network(name,monkeypatch):
    def forbidden(*args,**kwargs):raise AssertionError('Should reject before network')
    monkeypatch.setattr('urllib.request.urlopen',forbidden)
    with pytest.raises(ValueError):download_cnn193_browser_asset(name)


def test_demo_downloader_rejects_cross_host_redirect_and_unlisted_examples(monkeypatch):
    with pytest.raises(ValueError):download_cnn193_demo_image('../image.jpg')
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def geturl(self):return 'https://unregistered.example/data'
    monkeypatch.setattr('urllib.request.urlopen',lambda *args,**kwargs:Response())
    with pytest.raises(ValueError,match='跳转'):download_cnn193_demo_image('30c4bdc6b5b012ac7523fb3216664adc.jpg')


def test_pinned_tree_does_not_trust_commit_string_alone():
    assert tree_digest([{'path':'keras/model.json','sha':'bad','size':28820,'type':'blob'}])!=PINNED_TREE_SHA256
    a=[{'path':'b','sha':'b','size':2,'type':'blob'},{'path':'a','sha':'a','size':1,'type':'blob'}]
    assert tree_digest(a)==tree_digest(list(reversed(a)))
