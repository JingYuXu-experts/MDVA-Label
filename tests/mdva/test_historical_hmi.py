import pytest
from urllib.request import Request
from mdva.web.historical_hmi import source_url,HmiRedirect


def test_only_registered_historical_hourly_urls_are_constructed():
    url,name=source_url('20130709_060000')
    assert url=='https://jsoc1.stanford.edu/data/hmi/fits/2013/07/09/hmi.M_720s.20130709_060000_TAI.fits'
    assert name.endswith('_TAI.fits')
    for bad in ('20130709_060001','20130709_060200','20090101_000000','../20130709','https://example.com',None):
        with pytest.raises(ValueError):source_url(bad)


@pytest.mark.parametrize('target',['http://jsoc1.stanford.edu/data/hmi/fits/a','https://evil.test/data/hmi/fits/a','https://jsoc1.stanford.edu:444/data/hmi/fits/a','https://user@jsoc1.stanford.edu/data/hmi/fits/a','https://jsoc1.stanford.edu/other/a'])
def test_redirects_are_rejected_before_following_unregistered_sources(target):
    with pytest.raises(ValueError,match='未登记跳转'):
        HmiRedirect().redirect_request(Request(source_url('20130709_060000')[0]),None,302,'Found',{},target)
