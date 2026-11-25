"""Durable run state, source identities and local resource limits."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                   allow_nan=False), encoding='utf-8')
    os.replace(temporary, path)


def configure_environment():
    """Only affect this process, never base or global environment settings."""
    prefix = Path(sys.prefix).resolve()
    if prefix.name.lower() != 'mdva311' or prefix.parent.name.lower() != 'envs':
        raise RuntimeError('模型命令必须使用 Conda mdva311 环境')
    for key, leaf in [('TORCH_HOME', 'torch'), ('HF_HOME', 'huggingface'),
                      ('MPLCONFIGDIR', 'matplotlib'), ('TEMP', 'tmp'), ('TMP', 'tmp')]:
        os.environ.setdefault(key, str(prefix / '.cache' / leaf))
        if not Path(os.environ[key]).resolve().is_relative_to(prefix):
            raise RuntimeError(f'{key} 必须在 mdva311 中')
        Path(os.environ[key]).mkdir(parents=True, exist_ok=True)
    os.environ['PYTHONNOUSERSITE'] = '1'
    os.environ.setdefault('CUBLAS_WORKSPACE_CONFIG', ':4096:8')


def output_path(path, dataset):
    path = Path(path).resolve()
    roots = json.loads((Path(dataset) / 'sources.json').read_text(encoding='utf-8'))
    protected = [Path(dataset).resolve(), *(Path(p).resolve() for p in roots.values())]
    if any(path == p or path.is_relative_to(p) for p in protected):
        raise ValueError('训练输出不能位于原始数据或冻结清单目录内')
    return path


def code_identity():
    names = ('runtime.py', 'models.py', 'stable_models.py', 'metrics.py', 'datasets.py', 'train.py')
    return {n: digest(Path(__file__).parent / n) for n in names}


@contextmanager
def exclusive(path):
    """PID + process birth time prevents stale locks and PID reuse confusion."""
    import psutil
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                owner = json.loads(path.read_text(encoding='utf-8'))
                process = psutil.Process(owner['pid'])
                live = abs(process.create_time() - owner['birth']) < .01
            except (psutil.NoSuchProcess, KeyError):
                live = False
            except (ValueError, OSError, psutil.AccessDenied) as exc:
                raise RuntimeError(f'锁无法核验，拒绝抢占：{path}') from exc
            if live:
                raise RuntimeError(f'已有任务持有锁：{path}')
            path.unlink()
        else:
            break
    else:
        raise RuntimeError('无法获得任务锁')
    owner = {'pid': os.getpid(), 'birth': psutil.Process().create_time(), 'time': now()}
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(owner, stream)
    try:
        yield
    finally:
        if path.exists() and json.loads(path.read_text(encoding='utf-8')) == owner:
            path.unlink()


class ResourceLimit(RuntimeError):
    pass


class Resources:
    def __init__(self, device='cuda'):
        self.device = device
        self.peak_rss = 0
        self.peak_total_vram = 0
        self.start = time.perf_counter()

    def sample(self):
        import psutil
        process = psutil.Process()
        rss = process.memory_info().rss
        for child in process.children(recursive=True):
            try:
                rss += child.memory_info().rss
            except psutil.NoSuchProcess:
                pass
        available = psutil.virtual_memory().available
        self.peak_rss = max(self.peak_rss, rss)
        data = {'rss_gib': rss / 2**30, 'available_ram_gib': available / 2**30,
                'peak_rss_gib': self.peak_rss / 2**30,
                'seconds': time.perf_counter() - self.start}
        if rss > 20 * 2**30 or available < 4 * 2**30:
            raise ResourceLimit('内存门槛不满足：进程树20GiB/系统至少保留4GiB')
        if self.device == 'cuda':
            import torch
            free, total = torch.cuda.mem_get_info()
            self.peak_total_vram = max(self.peak_total_vram, total - free)
            reserved = torch.cuda.max_memory_reserved()
            data.update(peak_reserved_gib=reserved / 2**30,
                        peak_allocated_gib=torch.cuda.max_memory_allocated() / 2**30,
                        total_vram_used_gib=(total - free) / 2**30,
                        peak_total_vram_used_gib=self.peak_total_vram / 2**30)
            if reserved > 6 * 2**30 or total - free > 7.5 * 2**30:
                raise ResourceLimit('显存门槛不满足：进程预留6GiB/整机使用7.5GiB')
        return data
