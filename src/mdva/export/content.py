"""字节内容分组（REQ-W17 的核心判据，REQ-W13 的只读提示复用同一实现）。

为什么不能借 `load_item` 的解码缓存：解码后的数组丢掉了容器信息——同像素的
两张 PNG 可能字节完全不同（压缩器/元数据不同），字节相同的两份 FITS 也可能
被存成不同文件名。W17 要封死的是"逐字节重复文件被分到 train/val 两侧"这种
同源泄漏，判据必须落在原始字节上，所以这里单独建一层**字节缓存**，失效语义
与解码缓存同款：(路径, size, mtime_ns) 指纹一变即重算。

成本控制是需求里限死的形态（PM seq=390 约束 2）：
1. 先按文件 size 分桶——size 唯一的文件连打开都不会打开；
2. 只有同 size 的候选才逐块算 sha256；
3. 结果按指纹缓存在进程内，热导出零重读。
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict, defaultdict
from pathlib import Path
from typing import Iterable

CHUNK = 1024 * 1024
CACHE_MAX = 2048

# (绝对路径, size, mtime_ns) -> sha256 hex；指纹变了自然失效，同指纹不重算
_sha_cache: "OrderedDict[tuple, str]" = OrderedDict()
_lock = threading.Lock()      # serve 是多线程的，缓存的逐出/挪尾必须串行


def _fingerprint(path: Path):
    try:
        st = path.stat()
        return (str(path), st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def _file_sha(fp: tuple) -> str:
    with _lock:
        hit = _sha_cache.get(fp)
        if hit is not None:
            _sha_cache.move_to_end(fp)
            return hit
    # 锁外读文件：并发同算一个文件只会浪费一次 IO，结果一致且后写覆盖无害；
    # 把大文件读进临界区反而会把整条导出串行化。
    digest = hashlib.sha256()
    with open(fp[0], "rb") as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            digest.update(block)
    with _lock:
        _sha_cache[fp] = digest.hexdigest()
        while len(_sha_cache) > CACHE_MAX:
            _sha_cache.popitem(last=False)
    return digest.hexdigest()


def content_map(uris: Iterable[str]) -> dict:
    """返回 {uri: 组令牌}，只收录"至少两个文件逐字节相同"的成员。

    令牌取 sha256 前 16 位——分组只看相等性，不需要全长；同令牌 = 同内容。
    读不到的文件（权限/已删除）静默跳过分组，交给上层按组名键兜底，
    分组判据不该把一次导出变成一次 IO 报错。
    """
    by_size: dict = defaultdict(list)
    # 先去重：同一文件的多个样本（stack 切片）不是"两个重复文件"——
    # 它们本来就该同侧，且由样本名键保证；这里判的是不同路径的逐字节副本。
    for u in dict.fromkeys(str(x) for x in uris):
        fp = _fingerprint(Path(u))
        if fp is not None:
            by_size[fp[1]].append((u, fp))
    out: dict = {}
    for members in by_size.values():
        if len(members) < 2:
            continue                          # size 唯一：连 sha 都不算
        by_sha: dict = defaultdict(list)
        for uri, fp in members:
            by_sha[_file_sha(fp)].append(uri)
        for sha, group in by_sha.items():
            if len(group) >= 2:
                for uri in group:
                    out[uri] = sha[:16]
    return out


def group_stats(uris: Iterable[str]) -> dict:
    """给报告用的可见判据（约束 4）：这次划分是否感知重复内容、感知到几组。"""
    cm = content_map(uris)
    sizes: dict = defaultdict(int)
    for token in cm.values():
        sizes[token] += 1
    return {"content_aware": True,
            "n_content_groups": len(sizes),
            "n_files_in_content_groups": len(cm)}
