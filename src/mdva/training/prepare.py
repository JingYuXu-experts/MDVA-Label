"""Build audited, immutable-source solar research manifests. Never starts training.

Run with ``python -m mdva.training.prepare --source-root ... --output ...``.
Invalid or uncertain associations are quarantined, never silently repaired.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import csv
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import sys
import time

import numpy as np
import openpyxl
from PIL import Image
from scipy.ndimage import label as connected

from .provenance import (UTC, event_time, flare_level, integer, isolate_groups,
                         observation, resolve_noaa, split_for, segmentation_split)

NUMERIC = ('MGB_PIL', 'R', 'MGB', 'Total_Unsigned_Flux', 'Area')
SOURCES = {'ssafd': 'Situation Awareness Dataset',
           'solar_obs': 'Standardized Solar Observation Image Dataset',
           'solar_forecast': 'Solar Flare Forecasting Dataset', 'source_code': 'Code'}


def dump(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def write_rows(path, rows):
    with path.open('w', encoding='utf-8', newline='\n') as out:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')


class Preparation:
    def __init__(self, root, output):
        self.root, self.output = Path(root).resolve(), Path(output).resolve()
        if not self.root.is_dir():
            raise ValueError('数据父目录不存在')
        if self.output == self.root or self.output.is_relative_to(self.root):
            raise ValueError('输出目录必须位于原始数据目录以外')
        if (self.output / 'summary.json').exists():
            raise ValueError('此目录已有冻结报告；请使用新的输出目录')
        self.output.mkdir(parents=True, exist_ok=True)
        self.files, self.images, self.issues, self.segments, self.forecasts = {}, [], [], [], []
        self.mask_stats, self.image_by_uri = {}, {}
        self.mapping, self.parameters, self.reference_days = defaultdict(set), {}, defaultdict(set)
        self.started = time.time()

    def issue(self, kind, uri='', **details):
        self.issues.append({'kind': kind, 'uri': uri, **details})

    def path(self, source, relative):
        base = (self.root / SOURCES[source]).resolve()
        target = (base / relative).resolve()
        if not target.is_relative_to(base):
            raise ValueError('数据路径越界')
        return target

    def fingerprint(self, source, path):
        path = path.resolve()
        relative = path.relative_to((self.root / SOURCES[source]).resolve()).as_posix()
        uri = '@' + source + '/' + relative
        if uri in self.files:
            return uri
        before = path.stat()
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('读取期间源文件发生变化')
        self.files[uri] = {'uri': uri, 'bytes': before.st_size, 'mtime_ns': before.st_mtime_ns,
                           'sha256': digest.hexdigest()}
        return uri

    def time_info(self, name, uri):
        try:
            return observation(name)
        except (ValueError, OverflowError) as error:
            self.issue('unknown_observation_time', uri, reason=str(error))
            return {}

    def read_mapping_parameters(self):
        path = self.path('ssafd', 'AR/MASK-NOAA.xlsx')
        uri = self.fingerprint('ssafd', path)
        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            rows = book.active.iter_rows(values_only=True)
            headers = [str(x).upper().strip() for x in next(rows)]
            for row in rows:
                obj = dict(zip(headers, row))
                if not obj.get('FILENAME'):
                    continue
                stem = Path(str(obj['FILENAME']).replace('\\', '/')).stem
                ident, noaa = integer(obj.get('MASK_NUM')), integer(obj.get('NOAA_AR'))
                if ident is None or noaa is None or noaa < 10000:
                    self.issue('invalid_mapping', uri, stem=stem, row=str(row)); continue
                self.mapping[stem, ident].add(noaa)
                try:
                    day = observation(stem+'.jpg')['nominal_utc'][:10]
                    self.reference_days[day].add(noaa)
                except ValueError:
                    pass
        finally:
            book.close()
        for key, values in self.mapping.items():
            if len(values) != 1:
                self.issue('conflicting_noaa_mapping', uri, stem=key[0], instance_id=key[1], values=sorted(values))
        for path in sorted(self.path('solar_forecast', 'AR_parameters').glob('*.txt')):
            uri = self.fingerprint('solar_forecast', path)
            records = []
            with path.open(encoding='utf-8-sig', newline='') as stream:
                for line, row in enumerate(csv.DictReader(stream, delimiter='\t'), 2):
                    rec = {'instance_id': integer(row.get('Num')), 'noaa': integer(row.get('NOAA_AR')),
                           'parameters_uri': uri, 'line': line, 'raw': row, 'features': {}, 'exclusions': []}
                    rec['noaa_raw']=row.get('NOAA_AR')
                    canonical, basis=resolve_noaa(row.get('NOAA_AR'),self.mapping.get((path.stem,rec['instance_id']),set()))
                    rec['noaa'],rec['noaa_basis']=canonical,basis
                    for field in NUMERIC:
                        try:
                            value = float(row[field])
                            if not math.isfinite(value): raise ValueError()
                        except (ValueError, KeyError, TypeError):
                            value = None
                        rec['features'][field] = value
                        rec['features'][field+'_missing'] = int(value is None)
                    if rec['instance_id'] is None or rec['noaa'] is None:
                        rec['exclusions'].append('invalid_parameter_identity')
                    records.append(rec)
            self.parameters[path.stem] = records

    def scan_images(self):
        for source in ('ssafd', 'solar_obs'):
            for index, path in enumerate(sorted(self.path(source, '.').rglob('*.jpg')), 1):
                uri = self.fingerprint(source, path)
                row = {'uri': uri, **self.time_info(path.name, uri), 'valid': True}
                try:
                    with Image.open(path) as image:
                        image.load()  # Detect truncated streams, not only valid headers.
                        row.update(width=image.width, height=image.height, mode=image.mode,
                                   pixel_sha256=hashlib.sha256(f'{image.mode}:{image.size}:'.encode()+image.tobytes()).hexdigest())
                except Exception as error:
                    row.update(valid=False, error=str(error)); self.issue('invalid_image', uri, reason=str(error))
                self.images.append(row); self.image_by_uri[uri] = row
                if index % 1000 == 0: print(f'images {source}: {index}', flush=True)

    def scan_masks(self):
        for category in ('AR', 'CH', 'FL'):
            directory = self.path('ssafd', f'{category}/{category}_npy')
            image_directory = self.path('ssafd', f'{category}/{category}_image')
            masks = {p.stem: p for p in directory.glob('*.npy')}
            images = {p.stem: p for p in image_directory.glob('*.jpg')}
            for index, (stem, path) in enumerate(sorted(masks.items()), 1):
                uri = self.fingerprint('ssafd', path)
                try:
                    data = np.load(path, mmap_mode='r', allow_pickle=False)
                    if data.ndim != 2 or data.dtype.kind not in 'biu' or data.min() < 0:
                        raise ValueError('原始区域掩膜必须为二维非负整型')
                    values, counts = np.unique(data, return_counts=True)
                    components = {int(v): int(connected(data == v)[1]) for v in values if v != 0}
                    stats = {'shape': list(data.shape), 'dtype': str(data.dtype),
                             'pixels': {int(v): int(c) for v, c in zip(values, counts)}, 'components': components}
                    self.mask_stats[category, stem] = stats
                    if category != 'AR' and set(values) - {0, 1}:
                        self.issue('nonbinary_category_mask', uri, values=values.tolist())
                    if stem not in images:
                        self.issue('mask_without_image', uri, category=category)
                except Exception as error:
                    self.issue('invalid_mask', uri, reason=str(error))
                if index % 500 == 0: print(f'masks {category}: {index}', flush=True)
            for stem, path in sorted(images.items()):
                uri = '@ssafd/' + path.relative_to(self.path('ssafd', '.')).as_posix()
                image = self.image_by_uri[uri]
                row = {'uri': uri, 'category': category, 'split': 'excluded_invalid', 'exclusions': [],
                       'label_semantics': 'reference_instances' if category == 'AR' else 'semantic_region',
                       **{k: v for k, v in image.items() if k != 'valid'}}
                stats = self.mask_stats.get((category, stem))
                if not image['valid']: row['exclusions'].append('invalid_image')
                if stats is None: row['exclusions'].append('missing_or_invalid_mask')
                elif stats['shape'] != [image.get('height'), image.get('width')]:
                    row['exclusions'].append('mask_image_shape_mismatch')
                elif category != 'AR' and set(stats['pixels']) - {0, 1}:
                    row['exclusions'].append('unexpected_label_encoding')
                if stats:
                    row.update(mask_uri='@ssafd/'+masks[stem].relative_to(self.path('ssafd', '.')).as_posix(),
                               mask_statistics=stats)
                if image.get('time_utc'):
                    row['split'] = segmentation_split(image['time_utc'],category)
                else: row['exclusions'].append('unknown_time')
                row['noaa_ids'] = []
                if category == 'AR' and stats:
                    for ident in stats['pixels']:
                        if ident == 0: continue
                        ids = self.mapping.get((stem, ident), set())
                        if len(ids) == 1: row['noaa_ids'].extend(ids)
                        else: self.issue('mask_instance_no_mapping', uri, instance_id=ident)
                self.segments.append(row)

    def read_events(self):
        path = self.path('solar_forecast', 'flare list.xlsx')
        uri = self.fingerprint('solar_forecast', path)
        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
        events, seen = [], set()
        try:
            for index, row in enumerate(book.active.iter_rows(min_row=2, values_only=True), 2):
                if not any(x is not None for x in row): continue
                event = {'source_uri': uri, 'excel_row': index, 'raw_noaa': str(row[10]), 'raw_class': str(row[9]),
                         'raw_day': str(integer(row[2]))}
                try:
                    day = datetime.strptime(str(integer(row[2])), '%Y%m%d').replace(tzinfo=UTC)
                    event['day'] = day.date().isoformat()
                    start = event_time(day, row[0], row[1])
                    try: end = event_time(day, row[3], row[5])
                    except ValueError: end = None
                    try: peak = event_time(day, row[7], row[8])
                    except ValueError: peak = None
                    if end and end < start: end += timedelta(days=1)
                    if peak and peak < start: peak += timedelta(days=1)
                    cls, magnitude = flare_level(row[9])
                    candidates = set()
                    for shift in range(-2, 3): candidates |= self.reference_days[(day+timedelta(days=shift)).date().isoformat()]
                    noaa, basis = resolve_noaa(row[10], candidates)
                    event.update(start=start.isoformat(), end=end.isoformat() if end else None, peak=peak.isoformat() if peak else None,
                                 class_letter=cls, magnitude=magnitude, noaa=noaa, association_basis=basis,
                                 is_m_plus=cls in ('M', 'X'), valid=True,
                                 time_order_valid=bool(end and peak and start <= peak <= end))
                    if not event['time_order_valid']: self.issue('event_time_order', uri, excel_row=index)
                    key = (start, noaa, cls, magnitude, str(row[10]))
                    if key in seen: self.issue('duplicate_event', uri, excel_row=index); continue
                    seen.add(key)
                except (ValueError, TypeError, IndexError) as error:
                    event.update(valid=False, error=str(error)); self.issue('invalid_event', uri, excel_row=index, reason=str(error))
                events.append(event)
        finally:
            book.close()
        self.events = events
        self.events_by_noaa = defaultdict(list)
        self.unmatched_strong = []
        self.invalid_event_dates = set()
        for event in events:
            if event['valid'] and event['noaa']:
                self.events_by_noaa[event['noaa']].append(event)
            elif event['valid'] and event['is_m_plus']:
                self.unmatched_strong.append(datetime.fromisoformat(event['start']).timestamp())
            elif not event['valid']:
                # Unknown timing/class cannot safely support a negative target on this day.
                try: self.invalid_event_dates.add(datetime.strptime(event['raw_day'], '%Y%m%d').date().isoformat())
                except ValueError: self.invalid_event_dates.add('unknown')
        self.unmatched_strong.sort()
        dates = sorted(e['day'] for e in events if e.get('day'))
        self.event_coverage = (datetime.fromisoformat(dates[0]).replace(tzinfo=UTC),
                               datetime.fromisoformat(dates[-1]).replace(tzinfo=UTC)+timedelta(days=1)) if dates else None
        for records in self.events_by_noaa.values(): records.sort(key=lambda e: e['start'])

    def scan_forecasts(self):
        products = {'history_index': 'mask_flare_index_1024', 'C': 'mask_flare_label_1024/C-class or above',
                    'M': 'mask_flare_label_1024/M-class or above', 'X': 'mask_flare_label_1024/X-class'}
        associations, fi_past, fi_future = 0, 0, 0
        for index, ((category, stem), stats) in enumerate(sorted(self.mask_stats.items()), 1):
            if category != 'AR': continue
            mask_path = self.path('ssafd', 'AR/AR_npy/'+stem+'.npy')
            mask = np.load(mask_path, mmap_mode='r', allow_pickle=False)
            meta = observation(stem+'.jpg')
            t = datetime.fromisoformat(meta['time_utc']); until = t+timedelta(hours=24)
            arrays, array_uris = {}, {}
            for product, folder in products.items():
                path = self.path('solar_forecast', folder+'/'+stem+'.npy')
                if not path.is_file():
                    self.issue('forecast_array_missing', '@ssafd/AR/AR_npy/'+stem+'.npy', product=product); continue
                uri = self.fingerprint('solar_forecast', path)
                try:
                    a = np.load(path, mmap_mode='r', allow_pickle=False)
                    if a.shape != mask.shape or a.dtype.kind not in 'biuf' or not np.isfinite(a).all():
                        raise ValueError('预测资料形状/类型或有限值检查失败')
                    arrays[product] = a; array_uris[product] = uri
                except Exception as error: self.issue('invalid_forecast_array', uri, reason=str(error))
            parameters = self.parameters.get(stem, [])
            ids = {r['instance_id'] for r in parameters}
            for ident in stats['pixels']:
                if ident and ident not in ids: self.issue('mask_without_parameter_row', str(mask_path), instance_id=ident)
            for param in parameters:
                row = {**meta, 'stem': stem, 'instance_id': param['instance_id'], 'noaa': param['noaa'],
                       'noaa_raw':param['noaa_raw'],'noaa_basis':param['noaa_basis'],
                       'parameters_uri': param['parameters_uri'], 'parameter_line': param['line'],
                       'features': dict(param['features']), 'exclusions': list(param['exclusions']),
                       'split': split_for(meta['time_utc'], guard_days=4), 'forecast_horizon_hours': 24,
                       'target_start': t.isoformat(), 'target_end': until.isoformat(),
                       'availability_status': 'retrospective_event_time_only',
                       'excluded_input_fields': ['Num', 'NOAA_AR', 'provided_future_labels', 'provided_FI', 'Hale', 'McIntosh'],
                       'array_uris': array_uris}
                ident, noaa = row['instance_id'], row['noaa']
                area = stats['pixels'].get(ident, 0)
                if not area: row['exclusions'].append('parameter_instance_absent_in_mask')
                reference = self.mapping.get((stem, ident), set())
                if reference != {noaa}: row['exclusions'].append('noaa_mapping_missing_or_conflicting')
                values = {}
                if area:
                    region = mask == ident
                    for product, array in arrays.items(): values[product] = np.unique(array[region]).tolist()
                row['provided_region_values'] = values
                if len(values.get('M', [])) != 1 or values.get('M', [None])[0] not in (0, 1):
                    row['exclusions'].append('missing_or_mixed_m_label')
                else: row['provided_m_label'] = int(values['M'][0])
                if any(len(values.get(p, [])) != 1 or values[p][0] not in (0, 1) for p in ('C', 'X')):
                    row['exclusions'].append('missing_or_mixed_other_labels')
                elif 'provided_m_label' in row and not values['X'][0] <= row['provided_m_label'] <= values['C'][0]:
                    row['exclusions'].append('inconsistent_c_m_x_hierarchy')
                events = self.events_by_noaa.get(noaa, [])
                future = [e for e in events if t < datetime.fromisoformat(e['start']) <= until]
                row['event_m_label'] = int(any(e['is_m_plus'] for e in future))
                if row.get('provided_m_label') != row['event_m_label']:
                    row['exclusions'].append('provided_label_disagrees_with_event_reconstruction')
                elif 'provided_m_label' in row: associations += 1
                lo, hi = bisect_right(self.unmatched_strong, t.timestamp()), bisect_right(self.unmatched_strong, until.timestamp())
                if hi > lo and row.get('provided_m_label') == 0:
                    row['exclusions'].append('unassigned_m_plus_event_in_target_window')
                if 'unknown' in self.invalid_event_dates or any((t+timedelta(days=d)).date().isoformat() in self.invalid_event_dates for d in (0,1)):
                    row['exclusions'].append('invalid_event_in_target_days')
                if not self.event_coverage or t < self.event_coverage[0] or until > self.event_coverage[1]:
                    row['exclusions'].append('target_outside_event_catalogue_bounds')
                past = [e for e in events if t-timedelta(hours=24) < datetime.fromisoformat(e['start']) <= t
                        and e['time_order_valid'] and datetime.fromisoformat(e['end']) <= t]
                for cls in ('B', 'C', 'M', 'X'):
                    row['features']['past24_completed_'+cls] = sum(e['class_letter'] == cls for e in past)
                factors = {'A': 0, 'B': .1, 'C': 1, 'M': 10, 'X': 100}
                past_value = sum(factors[e['class_letter']]*e['magnitude'] for e in past)
                future_value = sum(factors[e['class_letter']]*e['magnitude'] for e in future)
                row['past24_completed_FI'] = past_value
                if len(values.get('history_index', [])) == 1:
                    value = values['history_index'][0]
                    fi_past += int(abs(value-past_value) < 1e-6)
                    fi_future += int(abs(value-future_value) < 1e-6)
                row['target_m_plus'] = row.get('provided_m_label')
                self.forecasts.append(row)
            if index % 500 == 0: print(f'forecast arrays: {index}', flush=True)
        self.forecast_audit = {'provided_vs_event_agreements': associations, 'provided_FI_equals_completed_past24': fi_past,
                              'provided_FI_equals_future24': fi_future,
                              'provided_FI_used_as_input': False, 'Hale_McIntosh_used_as_input': False,
                              'historical_publication_times_verified': False}
        known={r['stem'] for r in self.forecasts}
        for stem,parameters in self.parameters.items():
            if stem in known: continue
            for param in parameters:
                self.forecasts.append({**observation(stem+'.jpg'),'stem':stem,'instance_id':param['instance_id'],
                                       'noaa':param['noaa'],'noaa_raw':param['noaa_raw'],'noaa_basis':param['noaa_basis'],
                                       'features':param['features'],'parameters_uri':param['parameters_uri'],
                                       'split':split_for(observation(stem+'.jpg')['time_utc']),
                                       'exclusions':['invalid_or_missing_AR_reference_mask']})

    def finish(self):
        # Source observation groups prevent differently sized copies from leaking.
        exact_groups = defaultdict(list)
        for row in self.images:
            if row.get('valid'): exact_groups[row['pixel_sha256']].append(row['uri'])
        duplicates = [{'pixel_sha256': k, 'uris': v} for k, v in exact_groups.items() if len(v) > 1]
        def groups(row):
            keys = ['obs:'+row.get('observation_group', ''), 'pixel:'+row.get('pixel_sha256', row.get('uri', ''))]
            keys += ['NOAA:'+str(n) for n in row.get('noaa_ids', [])]
            return keys
        seg_conflicts = isolate_groups(self.segments, groups)
        forecast_conflicts = isolate_groups(self.forecasts, lambda r: ['NOAA:'+str(r['noaa'])])
        for rows in (self.segments, self.forecasts):
            for row in rows:
                if row['split'].startswith('excluded'): row['exclusions'].append('temporal_boundary_guard')
                row['exclusions'] = sorted(set(row['exclusions']))
                row['eligible'] = not row['exclusions']
        changes = []
        for uri, row in self.files.items():
            source, relative = uri[1:].split('/', 1)
            stat = self.path(source, relative).stat()
            if (stat.st_size, stat.st_mtime_ns) != (row['bytes'], row['mtime_ns']): changes.append(uri)
        if changes: raise RuntimeError('读取期间原始文件变更，报告不能冻结：'+str(changes[:3]))
        write_rows(self.output/'source-files.jsonl', sorted(self.files.values(), key=lambda x: x['uri']))
        write_rows(self.output/'observations.jsonl', self.images)
        write_rows(self.output/'segmentation-all.jsonl', self.segments)
        write_rows(self.output/'segmentation-ready.jsonl', (r for r in self.segments if r['eligible']))
        write_rows(self.output/'forecast-all.jsonl', self.forecasts)
        write_rows(self.output/'forecast-ready.jsonl', (r for r in self.forecasts if r['eligible']))
        write_rows(self.output/'events.jsonl', self.events)
        write_rows(self.output/'issues.jsonl', self.issues)
        write_rows(self.output/'duplicate-images.jsonl', duplicates)
        counts = lambda rows: dict(Counter(r['split'] for r in rows if r['eligible']))
        summary = {'completed': True, 'created_utc': datetime.now(UTC).isoformat(),
                   'source_root': str(self.root), 'source_files_fingerprinted': len(self.files),
                   'source_bytes_read': sum(r['bytes'] for r in self.files.values()),
                   'source_stat_changes': len(changes), 'images': len(self.images),
                   'invalid_images': sum(not r['valid'] for r in self.images), 'valid_masks': len(self.mask_stats),
                   'segmentation': {c: {'total': sum(r['category']==c for r in self.segments),
                                       'ready_by_split': counts([r for r in self.segments if r['category']==c])} for c in ('AR','CH','FL')},
                   'forecast': {'rows': len(self.forecasts), 'ready_by_split': counts(self.forecasts),
                                'positive_by_split': counts([r for r in self.forecasts if r.get('target_m_plus')==1]),
                                'excluded_reasons': dict(Counter(x for r in self.forecasts for x in r['exclusions'])),
                                'mode': 'retrospective_research_numeric_parameters', **self.forecast_audit},
                   'events': {'total': len(self.events), 'valid': sum(e['valid'] for e in self.events),
                              'unmatched_m_plus': len(self.unmatched_strong)},
                   'exact_pixel_duplicate_groups': len(duplicates),
                   'cross_split_group_conflicts': {'segmentation': seg_conflicts, 'forecast': forecast_conflicts},
                   'issue_counts': dict(Counter(x['kind'] for x in self.issues)),
                   'elapsed_seconds': round(time.time()-self.started, 2),
                   'training_started': False,
                   'split_protocol':{'AR_and_forecast':'train<=2020; validation2021; calibration2022; test2023-2024',
                                     'CH_FL':'train<=2018; validation2019-2020; calibration2021-2022; test2023-2024',
                                     'segmentation_guard_days':27,'forecast_guard_days':4,
                                     'reason':'实际标签覆盖核验后、训练开始前调整；三类独立训练，不共享拟合权重'},
                   'limitations': ['NOAA/SRS历史发布时间未核验，Hale/McIntosh不进输入',
                                   '事件历史只用T前已结束事件；缺少真正发布快照，限回顾性研究',
                                   '原始FI未作为输入；异常关联已隔离而非改写原数据',
                                   '附加观测保持未标注，不作为负样本；未自动生成伪标签']}
        dump(self.output/'summary.json', summary)
        hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(self.output.glob('*.jsonl'))}
        dump(self.output/'manifest-hashes.json', hashes)
        dump(self.output/'sources.json', {k: str(self.path(k, '.')) for k in SOURCES})
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        return summary

    def run(self):
        self.read_mapping_parameters(); self.scan_images(); self.scan_masks()
        self.read_events(); self.scan_forecasts()
        self.complete_inventory()
        return self.finish()

    def complete_inventory(self):
        for source in SOURCES:
            for path in self.path(source,'.').rglob('*'):
                if path.is_file(): self.fingerprint(source,path)

    def reuse_inventory(self,previous):
        """Reuse verified image/mask reads, then rerun all mutable association rules."""
        from .datasets import read_frozen
        previous=Path(previous)
        summary=json.loads((previous/'summary.json').read_text(encoding='utf-8'))
        if Path(summary['source_root']).resolve()!=self.root: raise ValueError('不能跨数据源复用核查缓存')
        self.files={r['uri']:r for r in read_frozen(previous,'source-files.jsonl')}
        self.images=read_frozen(previous,'observations.jsonl')
        self.image_by_uri={r['uri']:r for r in self.images}
        self.issues=[r for r in read_frozen(previous,'issues.jsonl') if r['kind'] in
                     ('invalid_image','invalid_mask','mask_without_image','unknown_observation_time','nonbinary_category_mask')]
        for row in read_frozen(previous,'segmentation-all.jsonl'):
            if 'mask_statistics' in row:
                stats=row['mask_statistics'];stats['pixels']={int(k):v for k,v in stats['pixels'].items()}
                self.mask_stats[row['category'],Path(row['uri']).stem]=stats
        for category in ('AR','CH','FL'):
            for path in self.path('ssafd',category+'/'+category+'_npy').glob('*.npy'):
                if (category,path.stem) in self.mask_stats: continue
                try:
                    a=np.load(path,mmap_mode='r',allow_pickle=False)
                    if a.ndim!=2 or a.dtype.kind not in 'biu' or a.min()<0: continue
                    values,counts=np.unique(a,return_counts=True)
                    self.mask_stats[category,path.stem]={'shape':list(a.shape),'dtype':str(a.dtype),
                        'pixels':{int(v):int(c) for v,c in zip(values,counts)},
                        'components':{int(v):int(connected(a==v)[1]) for v in values if v}}
                except Exception: continue
        self.read_mapping_parameters()
        # Reuse rows only as source metadata; recalculate split and associations.
        for row in read_frozen(previous,'segmentation-all.jsonl'):
            row['exclusions']=[x for x in row['exclusions'] if x not in ('cross_split_source_or_region','temporal_boundary_guard')]
            if row.get('time_utc'): row['split']=segmentation_split(row['time_utc'],row['category'])
            self.segments.append(row)
        self.read_events();self.scan_forecasts();self.complete_inventory()
        return self.finish()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--reuse-inventory',type=Path)
    args = parser.parse_args()
    if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    preparation=Preparation(args.source_root,args.output)
    if args.reuse_inventory: preparation.reuse_inventory(args.reuse_inventory)
    else: preparation.run()


if __name__ == '__main__': main()
