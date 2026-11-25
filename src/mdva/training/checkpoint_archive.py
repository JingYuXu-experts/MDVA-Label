"""Preserve exact evaluated bytes, rather than a hash of a mutable training path."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .durable import commit
from .runtime import digest


def archive_checkpoint(source, destination, expected_sha256):
    """Call while holding the GPU mutex; reject mutation or conflicting archives."""
    source, destination = Path(source), Path(destination)
    if source.is_symlink() or destination.is_symlink():
        raise ValueError('检查点归档不允许符号链接')
    if source.resolve() == destination.resolve():
        raise ValueError('归档必须独立于训练检查点')
    if not source.is_file() or digest(source) != expected_sha256:
        raise ValueError('检查点归档前身份不符')
    if destination.exists():
        if not destination.is_file() or digest(destination) != expected_sha256:
            raise ValueError('已有检查点归档冲突，不覆盖')
        return destination

    def copy_verified(stream):
        copied = hashlib.sha256()
        with source.open('rb') as original:
            for block in iter(lambda: original.read(1024 * 1024), b''):
                copied.update(block)
                stream.write(block)
        if copied.hexdigest() != expected_sha256 or digest(source) != expected_sha256:
            raise ValueError('复制期间检查点改变，拒绝归档')

    commit(destination, copy_verified)
    if digest(destination) != expected_sha256:
        raise ValueError('检查点归档回读不一致')
    return destination
