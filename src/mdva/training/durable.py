"""Bounded, same-directory atomic commits; never overwrite the previous good file."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time


def commit(path, producer, attempts=8):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.'+path.name+'.', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            producer(stream)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    for attempt in range(attempts):
        try:
            os.replace(temporary, path)
            return
        except PermissionError as exc:
            if attempt+1 == attempts:
                raise PermissionError(f'有界提交重试失败，旧文件保留；待提交文件：{temporary}') from exc
            time.sleep(min(.025*2**attempt, .8))


def atomic_json(path, value):
    data = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode('utf-8')
    commit(path, lambda stream: stream.write(data))


def atomic_checkpoint(path, value):
    import torch
    commit(path, lambda stream: torch.save(value, stream))


def early_stop_met(epoch, bad_epochs, config):
    return epoch >= config.get('min_epochs', 20) and bad_epochs >= config.get('patience', 12)
