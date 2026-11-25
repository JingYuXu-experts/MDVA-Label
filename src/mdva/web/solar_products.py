"""Lossless machine annotations and image-plane measurements; no physical-event inference."""
from __future__ import annotations

import numpy as np
from scipy import ndimage


def rle(mask):
    """COCO uncompressed RLE, column-major, beginning with a background run."""
    flat=np.asarray(mask,dtype=np.uint8).ravel(order='F')
    edges=np.flatnonzero(np.diff(flat))+1
    counts=np.diff(np.concatenate(([0],edges,[len(flat)]))).astype(int).tolist()
    if flat[0]:counts.insert(0,0)
    return {'size':list(mask.shape),'counts':counts}


def regions(mask):
    mask=np.asarray(mask)
    if mask.ndim!=2 or not mask.size or not np.isin(mask,[0,255]).all():
        raise ValueError('机器掩膜必须是非空二维0/255数组')
    labels,count=ndimage.label(mask>0,structure=np.ones((3,3)))
    output=[]
    for index,box in enumerate(ndimage.find_objects(labels),1):
        if box is None:continue
        crop=labels[box]==index
        ys,xs=np.nonzero(crop);y0,x0=box[0].start,box[1].start
        full=np.zeros(mask.shape,dtype=bool);full[box]=crop
        area=int(crop.sum())
        output.append({'component_id':index,'bbox':[x0,y0,box[1].stop-x0,box[0].stop-y0],
            'area_pixels':area,'image_fraction':area/mask.size,
            'centroid_pixels':[float(xs.mean()+x0),float(ys.mean()+y0)],
            'has_holes':bool((ndimage.binary_fill_holes(crop)&~crop).any()),
            'segmentation':rle(full)})
    assert len(output)==count
    return output
