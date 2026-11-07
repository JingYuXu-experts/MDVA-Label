"""Lossless COCO instance masks from registered expert references (no polygon approximation)."""
from __future__ import annotations
import json
from pathlib import Path

import numpy as np

from ..annotate.model import Ann
from .base import Dataset, Sample, _load_item, _sample_name, write_sample_image, write_sample_mask, write_report


def read_mask(path):
    from PIL import Image
    if Path(path).suffix.lower() == '.npy':
        return np.load(path, mmap_mode='r', allow_pickle=False)
    with Image.open(path) as image:
        return np.array(image)


def rle_encode(binary):
    flat = np.asarray(binary, dtype=np.uint8).ravel(order='F')
    boundaries = np.flatnonzero(np.diff(flat)) + 1
    counts = np.diff(np.concatenate(([0], boundaries, [flat.size]))).astype(int).tolist()
    if flat.size and flat[0]:
        counts.insert(0, 0)
    return {'size': list(binary.shape), 'counts': counts}


def reference_dataset(store, version_id, only_labels=None):
    version = store.get_version(version_id)
    if not version:
        raise KeyError('未知版本')
    ds = Dataset(dataset=store.get_dataset(version['dataset_id']), version=version,
                 labels=store.list_labels(version['dataset_id']))
    ds.categories = [l['name'] for l in ds.labels] or ['AR', 'CH', 'FL']
    records = store.version_records(version_id)
    if not records:
        raise ValueError('此版本没有已索引的原始掩膜，请先导入真实数据源')
    for record in records:
        if only_labels and record['category'] not in only_labels:
            continue
        if not record.get('mask_uri'):
            ds.issues.append({'kind': 'missing_reference_mask', 'uri': record['uri']})
            continue
        mask_path = store.require_within(record['mask_uri'])
        arr = read_mask(mask_path)
        if arr.ndim != 2 or arr.dtype.kind not in 'biu' or arr.shape != (record['height'], record['width']) or arr.min() < 0:
            raise ValueError('原始掩膜尺寸或实例编号无效，请重新预检')
        uri = str(store.require_within(record['uri']))
        item = _load_item(uri, ds.issues, record['uri'])
        if item is None:
            continue
        anns, metas, values = [], [], []
        for value in np.unique(arr):
            if value == 0:
                continue
            ys, xs = np.nonzero(arr == value)
            anns.append(Ann('box', [int(xs.min()), int(ys.min()), int(xs.max())+1, int(ys.max())+1],
                            record['category'], source='import', note=f'原始实例 {int(value)}'))
            metas.append({'id': f"{record['uri']}#{int(value)}", 'state': 'reference', 'role': 'expert', 'ts': 0})
            values.append(int(value))
        ds.samples.append(Sample(uri=uri, name=_sample_name(record['uri'], 0, 'image'),
            width=record['width'], height=record['height'], item=item, anns=anns, ann_meta=metas,
            mask_uri=str(mask_path), meta={'record': record, 'instance_ids': values,
                'source_group': record.get('derived_from') or record['uri']}))
    return ds


def export_reference_coco(store, version_id, out_dir, **params):
    params = {**params, 'include_masks': True, 'segmentation': True}
    from .coco import _wrap, _dump
    from .split import plan_split, summary
    ds = reference_dataset(store, version_id, params.get('only_labels'))
    out = Path(out_dir)
    images_dir = str(params.get('images_dir', 'images'))
    ratios = [float(v) for v in str(params.get('split_ratios', '0.7/0.15/0.15')).split('/')]
    seed = int(params.get('seed', 0))
    assignment = plan_split(ds, ratios=ratios, seed=seed)
    images, annotations, masks = [], [], []
    for sample in ds.samples:
        placed = write_sample_image(sample, out / images_dir)
        mask_info = write_sample_mask(sample, out / 'masks')
        masks.append({'image': placed['file_name'], 'mask': mask_info['file_name'],
                      'uri': sample.meta['record']['uri'], 'source_mask': sample.meta['record']['mask_uri']})
        arr = read_mask(sample.mask_uri)
        images.append({'id': len(images)+1, 'file_name': placed['file_name'], 'width': sample.width,
                       'height': sample.height, 'mdva': {'uri': sample.meta['record']['uri'],
                       'split': assignment[sample.name], 'mask': True}})
        for ann, value in zip(sample.anns, sample.meta['instance_ids']):
            binary = arr == value
            x0, y0, x1, y1 = ann.coords
            annotations.append({'id': len(annotations)+1, 'image_id': len(images),
                'category_id': ds.categories.index(ann.label)+1, 'iscrowd': 0,
                'bbox': [x0, y0, x1-x0, y1-y0], 'area': int(binary.sum()),
                'segmentation': rle_encode(binary),
                'mdva': {'instance_id': value, 'source': 'original_mask',
                         'noaa_source': f"@{sample.meta['record']['source_id']}/AR/MASK-NOAA.xlsx" if ann.label=='AR' else None,
                         'noaa_state': ('matched' if sample.meta['record'].get('noaa', {}).get(str(value)) else 'unmatched')
                                       if ann.label=='AR' else 'not_applicable',
                         'noaa': sample.meta['record'].get('noaa', {}).get(str(value))}})
    files = [_dump(out / 'instances.json', _wrap(ds, images, annotations))]
    per_split = {}
    for fold in ('train', 'val', 'test'):
        subset = [im for im in images if im['mdva']['split'] == fold]
        ids = {im['id'] for im in subset}
        anns = [a for a in annotations if a['image_id'] in ids]
        files.append(_dump(out / f'instances_{fold}.json', _wrap(ds, subset, anns)))
        per_split[fold] = {'images': len(subset), 'annotations': len(anns)}
    (out / 'mask_index.json').write_text(json.dumps(masks, ensure_ascii=False, indent=2), encoding='utf-8')
    return write_report(out, ds, 'coco', {**params, 'annotation_source': 'original', 'images_dir': images_dir}, {
        'files': files, 'n_images': len(images), 'n_annotations': len(annotations), 'n_masks': len(masks),
        'n_categories': len(ds.categories), 'n_skipped': 0, 'skipped': [],
        'spatial_annotations': len(annotations), 'split': summary(assignment), 'per_split': per_split,
        'annotation_source': 'original', 'segmentation_encoding': 'COCO uncompressed RLE (column-major)',
        'note': '原始实例掩膜逐像素保留，含孔洞和不连通区域；检测框由实例像素边界派生'})
