"""Train-only official FITS/source-domain audit; never aligns or relabels masks."""
from __future__ import annotations
import argparse,json,warnings
from datetime import datetime
from pathlib import Path
import numpy as np
from PIL import Image
from .datasets import SegmentationDataset
from .durable import atomic_json
from .runtime import digest,now,output_path


def ones_sum(raw):
    if len(raw)%4:raise ValueError('FITS校验需要完整4字节字组')
    value=int(np.frombuffer(raw,dtype='>u4').astype(np.uint64).sum(dtype=np.uint64))
    while value>>32:value=(value&0xffffffff)+(value>>32)
    return value


def mapping(image,header):
    import cv2
    from scipy.ndimage import zoom
    image=np.asarray(image)
    if image.shape!=(1024,1024):raise ValueError('仅核验已登记的1024同源显示流程')
    scale=float(header['CDELT1']);radius=float(header['RSUN_OBS'])/scale
    if not np.isfinite(radius) or scale<=0 or not 300<radius<500:raise ValueError('太阳视半径或比例无效')
    vmax=float(np.nanpercentile(image,99.9))
    if not np.isfinite(vmax) or vmax<=0:raise ValueError('显示归一化分位数无效')
    image=np.sqrt(np.clip(np.flip(image,axis=0),0,vmax)/vmax)
    # Preserve the supplied script's coordinate arithmetic for provenance testing.
    # This is NOT a claim of corrected FITS1-based -> image0-based WCS alignment.
    affine=np.float32([[1,0,512-float(header['CRPIX1'])],[0,1,512-(1024-float(header['CRPIX2']))]])
    image=cv2.warpAffine(image,affine,(1024,1024),borderMode=cv2.BORDER_CONSTANT,borderValue=np.nan)
    half=int(radius*1.1);image=image[512-half:512+half,512-half:512+half]
    image=zoom(image,(512/(half*2),)*2,order=1)
    result=np.full((512,512),255,np.uint8);finite=np.isfinite(image)
    # Approximate the 256-level gray colormap, not JPEG or renderer bitwise parity.
    result[finite]=np.clip(image[finite]*256,0,255).astype('uint8')
    return result,{'vmax_percentile99_9':vmax,'crop_half_pixels':half,'physical_radius_pixels':radius,
        'mapping':'provided script flip/percentile/sqrt/affine/crop/zoom; gray LUT approximation; no disk mask inferred'}


def run(dataset,folder):
    from astropy.io import fits
    dataset=Path(dataset);folder=output_path(folder,dataset)
    if (folder/'mapping-review.json').exists():raise ValueError('保留旧审计，使用新版本')
    audit=json.loads((folder/'source-audit.json').read_text(encoding='utf-8'))
    data=SegmentationDataset(dataset,'CH','train');known={r['uri']:r for r in data.rows}
    if audit['train_uri'] not in known:raise ValueError('只允许冻结训练图像')
    source=data.source(audit['train_uri'])
    if digest(source)!=audit['train_sha256']:raise ValueError('输入图像身份改变')
    images={};headers={};checks={};warning_records=[]
    for item in audit['sources']:
        if item['state']!='downloaded_verified':raise ValueError('下载未完成')
        filename=item['filename'];path=(folder/filename).resolve()
        if path.parent!=folder.resolve() or digest(path)!=item['sha256']:raise ValueError('FITS资源路径或身份改变')
        raw=path.read_bytes();channel=item['channel'];rows=[]
        with warnings.catch_warnings(record=True) as warning_log:
            warnings.simplefilter('always')
            with fits.open(path,disable_image_compression=True,memmap=False) as hdus:
                for h in hdus:
                    if 'CHECKSUM' not in h.header:continue
                    info=h.fileinfo();part=raw[info['hdrLoc']:info['datLoc']+info['datSpan']]
                    rows.append({'raw_hdu_sum':ones_sum(part),'raw_hdu_valid':ones_sum(part)==0xffffffff,
                                 'datasum_valid':h.verify_datasum()==1})
            with fits.open(path,memmap=False) as hdus:
                h=next(h for h in hdus if h.data is not None and h.data.ndim==2)
                needed=('WAVELNTH','QUALITY','LVL_NUM','T_OBS','CRPIX1','CRPIX2','CDELT1','CDELT2','RSUN_OBS',
                        'CRVAL1','CRVAL2','CROTA2','CTYPE1','CTYPE2','CUNIT1','CUNIT2')
                # Unrelated legacy nan cards can be unparsable. Do not normalize
                # the entire header or alter raw bytes to obtain needed metadata.
                header={key:h.header.get(key) for key in needed};image=h.data.copy()
            warning_records.extend({'channel':channel,'type':w.category.__name__,'message':str(w.message)} for w in warning_log)
        if not rows or not all(v['raw_hdu_valid'] and v['datasum_valid'] for v in rows):raise ValueError('原始HDU校验失败')
        if str(header.get('WAVELNTH'))!=channel or header.get('QUALITY')!=0 or header.get('LVL_NUM')!=1.5:raise ValueError('通道/质量/处理级别不符')
        if digest(path)!=item['sha256']:raise ValueError('读取过程中原FITS被改变')
        images[channel]=image;headers[channel]=header;checks[channel]=rows
    if set(images)!={'193','211'}:raise ValueError('只核验已登记193/211配对')
    rendered,parameters=mapping(images['193'],headers['193']);gray=np.asarray(Image.open(source).convert('L'))
    if gray.shape!=rendered.shape:raise ValueError('JPG原尺寸不同')
    y,x=np.ogrid[:512,:512];core=np.hypot(y-256,x-256)<180
    correlation=float(np.corrcoef(rendered[core],gray[core])[0,1]);mae=float(np.abs(rendered[core].astype(float)-gray[core]).mean())
    stamps=[]
    for channel in ('193','211'):
        value=headers[channel].get('T_OBS','')
        if not value.endswith('Z'):raise ValueError('观测时刻缺少可核验的UTC标识')
        stamps.append(datetime.fromisoformat(value.replace('Z','+00:00')))
    grid_keys=('CRPIX1','CRPIX2','CDELT1','CDELT2','CRVAL1','CRVAL2','CROTA2','CTYPE1','CTYPE2','CUNIT1','CUNIT2')
    grid={key:{c:headers[c].get(key) for c in headers} for key in grid_keys}
    result={'state':'one_training_source_core_comparison_completed','created_utc':now(),
        'source_audit_sha256':digest(folder/'source-audit.json'),'code_sha256':digest(__file__),
        'train_uri':audit['train_uri'],'raw_hdu_integrity':checks,'warnings':warning_records,
        'mapping':parameters,'core_radius_fixed_pixels':180,'core_correlation':correlation,'core_mae_8bit':mae,
        'all_image_correlation':float(np.corrcoef(rendered.ravel(),gray.ravel())[0,1]),
        'signed_211_minus193_seconds':(stamps[1]-stamps[0]).total_seconds(),'wcs_grid_metadata':grid,
        'limits':['One training example only; no full-dataset source equivalence.',
            'Off-limb corona remains in this formula, while downloaded JPEG has white outside background; supplied script does not explain that step.',
            'Core agreement is not complete WCS/pixel alignment certification; no geometric or intensity fit was performed.',
            'Historical definitive products are delayed; they do not establish operational real-time availability.'],
        'training_changed':False,'mask_read':False,'test_accessed':False,'multichannel_training_allowed':False,
        'integrity_note':'Raw HDU checksum is checked before parser normalization of nonstandard nan header cards; parsed-header CHECKSUM can differ.'}
    Image.fromarray(rendered).save(folder/'fixed-mapping-193.png');atomic_json(folder/'mapping-review.json',result)
    return result


if __name__=='__main__':
    from .runtime import configure_environment
    configure_environment();p=argparse.ArgumentParser();p.add_argument('--dataset',required=True);p.add_argument('--folder',required=True);a=p.parse_args();r=run(a.dataset,a.folder)
    print(json.dumps({k:r[k] for k in ('core_correlation','core_mae_8bit','signed_211_minus193_seconds','multichannel_training_allowed')}))
