"""数据整理：把勾选的数据项复制或移动到目标目录，并留下可整批撤销的清单。

"并行挑选 + 一键保存"是界面上最高频的动作，也是唯一会动到磁盘上原始文件的动作，
所以每次操作都写一份清单到目标目录，撤销时逆序回放，并且删之前先核对
大小与修改时间——不能把别人放进来的文件当自己的删掉。
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

MANIFEST_NAME = "mdva_manifest.json"


@dataclass
class Entry:
    src: str
    dst: str
    action: str            # copy | move
    is_dir: bool
    size: int
    mtime: float
    renamed: bool = False  # 目标已有同名文件，落到了带序号的新名字


@dataclass
class Collection:
    target: str
    mode: str
    started: float = 0.0
    entries: List[Entry] = field(default_factory=list)
    created: List[str] = field(default_factory=list)   # 本次新建的目录，撤销时清掉
    skipped: List[str] = field(default_factory=list)

    @property
    def manifest_path(self) -> Path:
        return Path(self.target) / MANIFEST_NAME

    def save(self) -> Path:
        self.manifest_path.write_text(
            json.dumps({"target": self.target, "mode": self.mode, "started": self.started,
                        "entries": [asdict(e) for e in self.entries],
                        "created": self.created, "skipped": self.skipped},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        return self.manifest_path

    @classmethod
    def load(cls, path) -> "Collection":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        entries = [Entry(**e) for e in payload.get("entries", [])]
        return cls(target=payload["target"], mode=payload["mode"],
                   started=payload.get("started", 0.0), entries=entries,
                   created=payload.get("created", []), skipped=payload.get("skipped", []))

    def summary(self) -> dict:
        return {"target": self.target, "mode": self.mode, "n": len(self.entries),
                "copied": sum(1 for e in self.entries if e.action == "copy"),
                "moved": sum(1 for e in self.entries if e.action == "move"),
                "skipped": len(self.skipped)}


def uri_of(item) -> str:
    """DataItem 和裸路径都能传。"""
    return str(getattr(item, "uri", item))


def _unique(dst: Path, taken=()) -> Tuple[Path, bool]:
    """同名就加序号。静默覆盖会让两张 scene_000.png 只剩一张。

    占用判断要同时看磁盘和本批已分配的目标：一批里有两张同名图时，
    第一张还没落盘，只查磁盘就会给两张算出同一个目标。
    """
    busy = {str(t).lower() for t in taken}

    def free(p: Path) -> bool:
        return not p.exists() and str(p).lower() not in busy

    if free(dst):
        return dst, False
    stem, suffix = dst.stem, dst.suffix
    for k in range(1, 10000):
        cand = dst.with_name(f"{stem}_{k}{suffix}")
        if free(cand):
            return cand, True
    raise RuntimeError(f"{dst} 同名文件过多，无法自动编号")


def _plan(items: Iterable, target, flatten: bool = False,
          overwrite: bool = False) -> List[Tuple[Path, Path, bool]]:
    """内部预览：(源, 目标, 是否被编号过)。编号必须在落盘前就定下来，
    否则一批里的两张同名图会算出同一个目标。"""
    target = Path(target)
    out = []
    taken: List[str] = []
    for item in items:
        src = Path(uri_of(item))
        rel = src.name if flatten else Path(src.parent.name) / src.name
        dst = target / rel
        if overwrite:
            renamed = False
        else:
            dst, renamed = _unique(dst, taken)
        taken.append(str(dst))
        out.append((src, dst, renamed))
    return out


def plan(items: Iterable, target, flatten: bool = False) -> List[Tuple[Path, Path]]:
    """只算不做的预览：返回 (源, 目标) 列表，供界面弹确认框。"""
    return [(src, dst) for src, dst, _ in _plan(items, target, flatten)]


def collect(items: Iterable, target, mode: str = "copy", flatten: bool = False,
            overwrite: bool = False) -> Collection:
    """把选中的项复制或移动到 target。返回的 Collection 可直接 undo。

    默认按"来源目录名/文件名"落位：多模型输出的文件名往往完全相同，
    摊平到一层就只剩最后一张，比对的前提都没了。
    """
    if mode not in ("copy", "move"):
        raise ValueError(f"mode 只能是 copy 或 move，收到 {mode!r}")
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    coll = Collection(target=str(target), mode=mode, started=time.time())

    for src, dst, renamed in _plan(items, target, flatten, overwrite):
        if not src.exists():
            coll.skipped.append(f"{src} 不存在")
            continue
        was_dir = src.is_dir()
        if overwrite and dst.exists():
            (shutil.rmtree if dst.is_dir() else dst.unlink)()
        dst.parent.mkdir(parents=True, exist_ok=True)
        if str(dst.parent).lower() not in [c.lower() for c in coll.created]:
            coll.created.append(str(dst.parent))

        if was_dir:
            (shutil.copytree if mode == "copy" else shutil.move)(str(src), str(dst))
            stat_size, stat_mtime = _dir_stamp(dst)
        elif mode == "copy":
            shutil.copy2(str(src), str(dst))
            stat = dst.stat()
            stat_size, stat_mtime = stat.st_size, stat.st_mtime
        else:
            shutil.move(str(src), str(dst))
            stat = dst.stat()
            stat_size, stat_mtime = stat.st_size, stat.st_mtime

        coll.entries.append(Entry(src=str(src), dst=str(dst), action=mode, is_dir=was_dir,
                                  size=stat_size, mtime=stat_mtime, renamed=renamed))

    coll.save()
    return coll


def _dir_stamp(path: Path) -> Tuple[int, float]:
    total = 0
    newest = 0.0
    for f in path.rglob("*"):
        if f.is_file():
            st = f.stat()
            total += st.st_size
            newest = max(newest, st.st_mtime)
    return total, newest


def undo(manifest, keep_dirs: bool = True, force: bool = False) -> dict:
    """逆序回放一次整理操作：复制的删掉、移动的搬回原处。

    删之前核对大小与修改时间，对不上就说明那个文件不是本次放进去的（或已被改过），
    默认跳过而不是删掉；force=True 才强行处理。
    """
    p = Path(str(manifest))
    coll = (manifest if isinstance(manifest, Collection)
            else Collection.load(p if p.is_file() else p / MANIFEST_NAME))
    removed, restored, refused = 0, 0, 0

    for entry in reversed(coll.entries):
        dst, src = Path(entry.dst), Path(entry.src)
        if not dst.exists():
            refused += 1
            continue
        if entry.action == "copy":
            if not force and not _matches(dst, entry):
                refused += 1
                continue
            _rm(dst)
            removed += 1
        else:
            if not force and not _matches(dst, entry):
                refused += 1
                continue
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dst), str(src))
            restored += 1

    if not keep_dirs:
        for d in sorted(set(coll.created), key=len, reverse=True):
            p = Path(d)
            if p.is_dir() and not any(p.iterdir()):
                p.rmdir()

    if coll.manifest_path.is_file():
        coll.manifest_path.unlink()
    return {"removed": removed, "restored": restored, "refused": refused,
            "target": coll.target, "mode": coll.mode}


def _matches(path: Path, entry: Entry) -> bool:
    if entry.is_dir or path.is_dir():
        size, mtime = _dir_stamp(path)
        return size == entry.size
    stat = path.stat()
    return stat.st_size == entry.size and abs(stat.st_mtime - entry.mtime) < 1e-6


def _rm(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def latest_manifest(target) -> Optional[Path]:
    path = Path(target) / MANIFEST_NAME
    return path if path.is_file() else None


__all__ = ["Collection", "Entry", "MANIFEST_NAME", "collect", "latest_manifest",
           "plan", "undo", "uri_of"]
