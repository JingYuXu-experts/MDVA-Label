"""Read-only SSAFD sources, header-only indexing and immutable mask references.

Source paths are registered at startup. Public URIs are @source/relative/path;
source pixels are never rewritten, including when polygons are edited.
"""
from __future__ import annotations

import io
import json
import random
import time
import uuid
from pathlib import Path

import numpy as np
from PIL import Image

CATEGORIES = ('AR', 'CH', 'FL')
COLORS = ((76, 141, 246), (56, 195, 148), (245, 178, 66))


def _header(path):
    with path.open('rb') as stream:
        version = np.lib.format.read_magic(stream)
        shape, _, dtype = np.lib.format._read_array_header(stream, version)
    if len(shape) != 2 or dtype.kind not in 'biu':
        raise ValueError('掩膜必须为二维整型实例数组')
    return tuple(shape), str(dtype)


def mask_array(store, record):
    if not record.get('mask_uri'):
        raise ValueError('此图像没有配对的原始掩膜')
    path = store.require_within(record['mask_uri'])
    arr = np.load(path, mmap_mode='r', allow_pickle=False)
    if arr.ndim != 2 or arr.dtype.kind not in 'biu' or arr.shape != (record['height'], record['width']):
        raise ValueError('原始掩膜的形状或类型与索引不一致，请重新预检数据源')
    if arr.min() < 0:
        raise ValueError('实例编号必须为非负整数')
    return arr


def _noaa(root):
    """File name + mask instance ID join. Conflicting mappings stay unresolved."""
    table = root / 'AR' / 'MASK-NOAA.xlsx'
    if not table.is_file():
        return {}, [], {'file': '', 'rows': 0}
    import openpyxl
    workbook = openpyxl.load_workbook(table, read_only=True, data_only=True)
    mapping, conflicts, n = {}, [], 0
    try:
        sheet = workbook.active
        rows = sheet.iter_rows(values_only=True)
        columns = [str(v or '').strip().upper() for v in next(rows)]
        if not {'FILENAME', 'MASK_NUM', 'NOAA_AR'} <= set(columns):
            raise ValueError('NOAA 表缺少 FILENAME / MASK_NUM / NOAA_AR 列')
        for row in rows:
            obj = dict(zip(columns, row))
            if not obj.get('FILENAME') or obj.get('MASK_NUM') is None:
                continue
            n += 1
            stem = Path(str(obj['FILENAME']).replace('\\', '/')).stem
            key = stem + ':' + str(int(obj['MASK_NUM']))
            value = str(obj['NOAA_AR']) if obj.get('NOAA_AR') is not None else None
            if key in mapping and mapping[key] != value:
                conflicts.append({'kind': 'noaa_conflict', 'key': key})
                mapping[key] = None
            else:
                mapping[key] = value
    finally:
        workbook.close()
    return mapping, conflicts, {'file': 'AR/MASK-NOAA.xlsx', 'rows': n}


class Solar:
    def __init__(self, hub):
        self.hub = hub
        self.store = hub.store

    def sources(self):
        return [{'id': key, 'name': key, 'read_only': True, 'available': root.is_dir()}
                for key, root in self.store.source_roots.items()]

    def preflight(self, source_id):
        root = self.store.source_roots.get(source_id)
        if root is None:
            raise ValueError('未知数据源：请用 --source 名称=目录 启动服务')
        t0 = time.perf_counter()
        mapping, issues, noaa = _noaa(root)
        noaa_by_file = {}
        for key, value in mapping.items():
            stem, instance = key.rsplit(':', 1)
            noaa_by_file.setdefault(stem, {})[instance] = value
        entries, counts = [], {}
        for category in CATEGORIES:
            di, dm = root / category / (category + '_image'), root / category / (category + '_npy')
            images, masks = {}, {}
            for directory, suffix, found in ((di, '.jpg', images), (dm, '.npy', masks)):
                if not directory.is_dir():
                    issues.append({'kind': 'missing_directory', 'path': directory.relative_to(root).as_posix()})
                    continue
                for path in sorted(directory.iterdir()):
                    if path.suffix.lower() != suffix or not path.is_file():
                        continue
                    # Resolve links before reading even a header.
                    self.store.require_within(path)
                    if path.stem in found:
                        issues.append({'kind': 'duplicate', 'path': path.relative_to(root).as_posix()})
                    found[path.stem] = path
            matched = images.keys() & masks.keys()
            counts[category] = {'images': len(images), 'masks': len(masks), 'paired': len(matched),
                                'image_only': len(images.keys() - masks.keys()),
                                'mask_only': len(masks.keys() - images.keys())}
            for stem in sorted(masks.keys() - images.keys()):
                issues.append({'kind': 'mask_only', 'uri': self.store.to_rel(masks[stem]), 'category': category})
            for stem, path in images.items():
                rec = {'uri': self.store.to_rel(path), 'name': path.name, 'category': category,
                       'modality': 'image', 'width': 0, 'height': 0, 'mask_uri': None,
                       'source_id': source_id, 'noaa': {}, 'status': 'image_only'}
                try:
                    with Image.open(path) as image:
                        rec['width'], rec['height'] = image.size
                    if stem in masks:
                        shape, dtype = _header(masks[stem])
                        if shape != (rec['height'], rec['width']):
                            raise ValueError(f"图像与掩膜尺寸不一致：{rec['width']}×{rec['height']} / {shape}")
                        rec.update(mask_uri=self.store.to_rel(masks[stem]), mask_dtype=dtype, status='paired')
                    if category == 'AR':
                        # Indexed once below; avoid scanning the whole correspondence table per image.
                        rec['noaa'] = noaa_by_file.get(stem, {})
                except Exception as exc:
                    rec.update(status='invalid', error=str(exc))
                    issues.append({'kind': 'invalid', 'uri': rec['uri'], 'reason': str(exc)})
                if rec['status'] == 'image_only':
                    issues.append({'kind': 'image_only', 'uri': rec['uri'], 'category': category})
                entries.append(rec)
        totals = {k: sum(c[k] for c in counts.values()) for k in ('images', 'masks', 'paired', 'image_only', 'mask_only')}
        report = {'source_id': source_id, 'categories': counts, 'totals': totals,
                  'valid_images': sum(e['status'] != 'invalid' for e in entries),
                  'issues': issues, 'noaa': noaa, 'entries': entries,
                  'elapsed_ms': round((time.perf_counter() - t0) * 1000, 1)}
        scan_id = 'scan' + uuid.uuid4().hex[:12]
        with self.store.transaction():
            self.store.db.execute('INSERT INTO import_scan VALUES(?,?,?,?)',
                                  (scan_id, source_id, time.time(), json.dumps(report, ensure_ascii=False)))
        return {**self._summary(report), 'scan_id': scan_id}

    @staticmethod
    def _summary(report):
        return {k: v for k, v in report.items() if k != 'entries'}

    def execute(self, scan_id, name='', sample_per_category=0, seed=20261107):
        with self.store._lock:
            row = self.store.db.execute('SELECT payload FROM import_scan WHERE id=?', (scan_id,)).fetchone()
        if row is None:
            raise KeyError('预检记录不存在，请先预检')
        report = json.loads(row['payload'])
        entries = [e for e in report['entries'] if e['status'] != 'invalid']
        if sample_per_category:
            rng = random.Random(int(seed))
            entries = [e for cat in CATEGORIES for e in rng.sample(
                [r for r in entries if r['category'] == cat and r['mask_uri']],
                min(int(sample_per_category), sum(r['category'] == cat and bool(r['mask_uri']) for r in entries)))]
        if not entries:
            raise ValueError('没有可导入的图像')
        # Recheck roots before committing a potentially stale preflight plan.
        for entry in entries:
            self.store.require_within(entry['uri'])
            if entry['mask_uri']:
                self.store.require_within(entry['mask_uri'])
        tid = self.store.create_task('import', params={'scan_id': scan_id, 'source_id': report['source_id']})
        target = self.store.base / 'imports' / tid
        target.mkdir(parents=True, exist_ok=True)
        try:
            with self.store.transaction():
                did = self.store.create_dataset(name or 'SSAFD · 太阳结构分割', 'segmentation', target,
                                                {'source_id': report['source_id'], 'indexed': True, 'scan_id': scan_id})
                vid = self.store.put_version(did, None, 'import', {'scan_id': scan_id, 'seed': seed}, {'n_items': len(entries)})
                self.store.set_labels(did, [{'name': cat, 'color': '#%02x%02x%02x' % color}
                                            for cat, color in zip(CATEGORIES, COLORS)])
                self.store.db.executemany('INSERT INTO sample_index VALUES(?,?,?,?,?,?,?)',
                    [(did, e['uri'], e['category'], e['mask_uri'], e['width'], e['height'], json.dumps(e, ensure_ascii=False)) for e in entries])
            result = {**self._summary(report), 'dataset_id': did, 'version_id': vid,
                      'task_id': tid, 'n_items': len(entries), 'sample_per_category': sample_per_category}
            self.store.update_task(tid, 'done', result)
            self.store.export_manifest(did)
            return result
        except Exception as exc:
            self.store.update_task(tid, 'failed', {'error': str(exc)})
            raise

    def reference(self, uri):
        record = self.store.indexed_item(uri)
        if not record or not record.get('mask_uri'):
            return {'available': False, 'instances': [], 'uri': self.store.to_rel(uri)}
        arr = mask_array(self.store, record)
        values, counts = np.unique(arr, return_counts=True)
        instances = [{'id': int(v), 'pixels': int(n), 'category': record['category'],
                      'noaa': record.get('noaa', {}).get(str(int(v))),
                      'noaa_source': f"@{record['source_id']}/AR/MASK-NOAA.xlsx" if record['category']=='AR' else None,
                      'noaa_state': ('matched' if record.get('noaa', {}).get(str(int(v))) else 'unmatched')
                                    if record['category']=='AR' else 'not_applicable'}
                     for v, n in zip(values, counts) if v != 0]
        return {'available': True, 'uri': record['uri'], 'width': record['width'], 'height': record['height'],
                'category': record['category'], 'instances': instances, 'read_only': True}

    def overlay(self, uri, width=960, height=540, instance=0):
        record = self.store.indexed_item(uri)
        if not record:
            raise KeyError('条目不存在')
        arr = mask_array(self.store, record)
        rgba = np.zeros((*arr.shape, 4), dtype=np.uint8)
        active = arr == int(instance) if instance else arr > 0
        rgba[active, :3] = COLORS[CATEGORIES.index(record['category'])]
        rgba[active, 3] = 255
        image = Image.fromarray(rgba)
        width, height = max(1, min(2048, int(width))), max(1, min(2048, int(height)))
        scale = min(width / image.width, height / image.height)
        image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.Resampling.NEAREST)
        canvas = Image.new('RGBA', (width, height))
        canvas.paste(image, ((width - image.width) // 2, (height - image.height) // 2))
        stream = io.BytesIO()
        canvas.save(stream, format='PNG')
        return stream.getvalue()

    def polygons(self, uri, instance=0):
        """Editable approximations are separate from the lossless original mask."""
        from skimage.measure import find_contours, approximate_polygon
        record = self.store.indexed_item(uri)
        if not record:
            raise KeyError('条目不存在')
        arr = mask_array(self.store, record)
        anns = []
        for value in np.unique(arr):
            if value == 0 or (instance and int(value) != int(instance)):
                continue
            for contour in find_contours(np.pad((arr == value).astype(np.uint8), 1), 0.5):
                points = approximate_polygon(contour - 1, tolerance=1.0)
                points = np.clip(points, [0, 0], [arr.shape[0] - 1, arr.shape[1] - 1])
                if len(points) > 1 and np.array_equal(points[0], points[-1]):
                    points = points[:-1]
                if len(points) >= 3:
                    anns.append({'geom': 'poly', 'coords': points[:, ::-1].reshape(-1).tolist(),
                                 'label': record['category'], 'source': 'import',
                                 'note': f'原始实例 {int(value)} 的轮廓近似；孔洞与原始像素请参照只读掩膜'})
        return {'anns': anns, 'approximation': True, 'original_unchanged': True}
