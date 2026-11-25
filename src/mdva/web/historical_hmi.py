"""Registered JSOC hourly LOS source for local research audits, not public URL proxy."""
from datetime import datetime
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


def source_url(nominal_tai):
    try:stamp=datetime.strptime(nominal_tai,'%Y%m%d_%H%M%S')
    except (ValueError,TypeError):raise ValueError('HMI名义TAI时刻格式无效') from None
    if not 2010<=stamp.year<=2026 or stamp.minute or stamp.second:
        raise ValueError('仅允许登记历史整点HMI资料')
    filename=f'hmi.M_720s.{nominal_tai}_TAI.fits'
    return f'https://jsoc1.stanford.edu/data/hmi/fits/{stamp:%Y/%m/%d}/{filename}',filename


class HmiRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        target=urlparse(newurl)
        if (target.scheme!='https' or target.hostname!='jsoc1.stanford.edu'
                or target.port not in (None,443) or target.username or target.password
                or not target.path.startswith('/data/hmi/fits/')):
            raise ValueError('HMI资料发生未登记跳转')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def download_hmi_hourly(nominal_tai):
    url,filename=source_url(nominal_tai)
    with build_opener(HmiRedirect()).open(Request(url,headers={'User-Agent':'MDVA-local-research/1.0'}),timeout=25) as response:
        target=urlparse(response.geturl())
        if target.scheme!='https' or target.hostname!='jsoc1.stanford.edu' or target.port not in (None,443):
            raise ValueError('HMI资料来源身份不符')
        length=response.headers.get('Content-Length');raw=response.read(16*1024*1024+1)
    if len(raw)>16*1024*1024 or length is not None and len(raw)!=int(length):
        raise ValueError('HMI资料下载长度异常')
    if not raw.startswith(b'SIMPLE  '):raise ValueError('响应不是HMI FITS')
    return raw,{'url':url,'filename':filename,'nominal_tai':nominal_tai,
        'kind':'historical hourly HMI source; processing/cadence/resolution and operational availability require header review'}
