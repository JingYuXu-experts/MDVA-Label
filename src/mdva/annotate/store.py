"""数据集/版本/标注的 SQLite 门面。

单一真源是数据库；`manifest.json` 是**派生产物**（由版本 DAG 拓扑排序重新生成），
这样"谱系可审、可重放"和"查询快"不用二选一，也不会双写打架。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Iterable, Optional

from . import state as ST
from .model import Ann

SCHEMA_VERSION = 2

_DDL = """
CREATE TABLE IF NOT EXISTS dataset(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL, root TEXT NOT NULL,
  created REAL NOT NULL, meta TEXT NOT NULL DEFAULT '{}');

CREATE TABLE IF NOT EXISTS version(
  id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, parent_id TEXT,
  op TEXT NOT NULL, params TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL,
  stats TEXT NOT NULL DEFAULT '{}');

CREATE TABLE IF NOT EXISTS label(
  dataset_id TEXT NOT NULL, name TEXT NOT NULL, color TEXT NOT NULL DEFAULT '',
  hotkey TEXT NOT NULL DEFAULT '', ord INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(dataset_id, name));

CREATE TABLE IF NOT EXISTS annotation(
  id TEXT PRIMARY KEY, version_id TEXT NOT NULL, item_uri TEXT NOT NULL,
  geom TEXT NOT NULL, coords TEXT NOT NULL, label TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'manual', confidence REAL NOT NULL DEFAULT 1.0,
  backend TEXT NOT NULL DEFAULT '', slice INTEGER, level INTEGER NOT NULL DEFAULT 0,
  color TEXT, note TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'draft', role TEXT NOT NULL DEFAULT '',
  ts REAL NOT NULL, revised_from TEXT);

CREATE TABLE IF NOT EXISTS task(
  id TEXT PRIMARY KEY, dataset_id TEXT, version_in TEXT, version_out TEXT,
  kind TEXT NOT NULL, params TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL DEFAULT 'pending', report TEXT NOT NULL DEFAULT '{}',
  created REAL NOT NULL, updated REAL NOT NULL);

CREATE TABLE IF NOT EXISTS review(
  id INTEGER PRIMARY KEY AUTOINCREMENT, annotation_id TEXT NOT NULL,
  dataset_id TEXT, version_id TEXT, item_uri TEXT,
  role TEXT NOT NULL, action TEXT NOT NULL, from_state TEXT, to_state TEXT,
  reason TEXT NOT NULL DEFAULT '', ts REAL NOT NULL);

CREATE INDEX IF NOT EXISTS idx_ann_version ON annotation(version_id, item_uri);
CREATE INDEX IF NOT EXISTS idx_version_dataset ON version(dataset_id, created);
CREATE INDEX IF NOT EXISTS idx_task_dataset ON task(dataset_id, kind);
CREATE INDEX IF NOT EXISTS idx_review_target ON review(version_id, annotation_id);
"""

def _migrate_v1_noop(store: "Store") -> None:
    """v0 -> v1：建表由 `executescript(_DDL)` 负责，这里没有额外动作。"""


def _migrate_v2_relative_uri(store: "Store") -> None:
    """v1 -> v2（D3）：库内绝对路径 -> 相对工作区根（mdva.sqlite 所在目录）。

    只改写得出相对形式的行（路径在工作区之下）；指不到工作区的历史行保持原样——
    `to_abs` 对绝对路径原样放行，旧库照常可读，绝不因为迁移丢一条标注。
    """
    db = store.db
    for table in ("annotation", "review"):
        rows = db.execute(f"SELECT rowid AS rid, item_uri FROM {table}").fetchall()
        for row in rows:
            if not row["item_uri"]:
                continue
            rel = store.to_rel(row["item_uri"])
            if rel != row["item_uri"]:
                db.execute(f"UPDATE {table} SET item_uri=? WHERE rowid=?", (rel, row["rid"]))
    for row in db.execute("SELECT id, root FROM dataset").fetchall():
        if not row["root"]:
            continue
        rel = store.to_rel(row["root"])
        if rel != row["root"]:
            db.execute("UPDATE dataset SET root=? WHERE id=?", (rel, row["id"]))


#: 迁移列表：`user_version` 从 n 升到 n+1 就跑 `_MIGRATIONS[n]`。毕设规模不上 alembic。
_MIGRATIONS: list = [_migrate_v1_noop, _migrate_v2_relative_uri]


def _new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _loads(text, default=None):
    if text in (None, ""):
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


class Store:
    """一个数据集根目录对应一个库文件（`mdva.sqlite`）。"""

    def __init__(self, db_path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        #: 工作区根 = 库文件所在目录。D3 之后库内 uri 一律相对这里存，
        #: 整个目录拷走 / 换盘符，标注照样能定位到文件。
        self.base = self.path.parent.resolve()
        # FastAPI 在线程池里跑同步 handler，连接必须跨线程复用 -> 一把全局写锁
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.init()

    # ------------------------------------------------------------------ 生命周期

    def init(self) -> None:
        with self._lock, self.db:
            self.db.executescript(_DDL)
            version = int(self.db.execute("PRAGMA user_version").fetchone()[0])
            if version < SCHEMA_VERSION:
                for step in _MIGRATIONS[version:SCHEMA_VERSION]:
                    step(self)
                self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ------------------------------------------------------------------ uri 归一（D3）

    def to_rel(self, uri) -> str:
        """入库/发布归一：工作区根之下的绝对路径 -> posix 相对路径。

        指不到工作区的路径（别的盘、别的机器遗留）保持原样绝对——宁可留着一条
        能看出问题的绝对路径，也不造一个解析不回原文件的相对路径。
        """
        text = str(uri or "")
        if not text:
            return text
        p = Path(text)
        if not p.is_absolute():
            return p.as_posix()
        try:
            return p.resolve().relative_to(self.base).as_posix()
        except (ValueError, OSError):
            return text

    def to_abs(self, uri) -> str:
        """文件访问前的还原：相对 uri 拼回工作区根；绝对路径原样放行（旧库兼容）。"""
        text = str(uri or "")
        if not text:
            return text
        p = Path(text)
        if p.is_absolute():
            return text
        return str(self.base / p)

    def relativize(self, text) -> str:
        """出站载荷归一：只把 base 之下的绝对路径转相对，其余字符串一律不动。"""
        value = str(text)
        if not value or not Path(value).is_absolute():
            return value
        return self.to_rel(value)

    # ------------------------------------------------------------------ 数据集

    def create_dataset(self, name: str, type: str, root, meta: Optional[dict] = None) -> str:
        if type not in DATASET_TYPES:
            raise ValueError(f"未知数据集类型 {type!r}，可用 {DATASET_TYPES}")
        did = _new_id("ds")
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO dataset(id,name,type,root,created,meta) VALUES(?,?,?,?,?,?)",
                (did, name, type, self.to_rel(Path(root).resolve()), time.time(),
                 json.dumps(meta or {}, ensure_ascii=False)))
        return did

    def get_dataset(self, dataset_id: str) -> Optional[dict]:
        row = self.db.execute("SELECT * FROM dataset WHERE id=?", (dataset_id,)).fetchone()
        return _row_dataset(row, self.base) if row else None

    def list_datasets(self) -> list[dict]:
        return [_row_dataset(r, self.base) for r in
                self.db.execute("SELECT * FROM dataset ORDER BY created DESC")]

    # ------------------------------------------------------------------ 版本 DAG

    def put_version(self, dataset_id: str, parent_id: Optional[str], op: str,
                    params: Optional[dict] = None, stats: Optional[dict] = None) -> str:
        if self.get_dataset(dataset_id) is None:
            raise KeyError(f"未知数据集 {dataset_id}")
        if parent_id and self.get_version(parent_id) is None:
            raise KeyError(f"父版本 {parent_id} 不存在")
        vid = _new_id("v")
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO version(id,dataset_id,parent_id,op,params,created,stats) "
                "VALUES(?,?,?,?,?,?,?)",
                (vid, dataset_id, parent_id, op,
                 json.dumps(params or {}, ensure_ascii=False), time.time(),
                 json.dumps(stats or {}, ensure_ascii=False)))
        return vid

    def get_version(self, version_id: str) -> Optional[dict]:
        row = self.db.execute("SELECT * FROM version WHERE id=?", (version_id,)).fetchone()
        return _row_version(row) if row else None

    def list_versions(self, dataset_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM version WHERE dataset_id=? ORDER BY created", (dataset_id,)).fetchall()
        out = [_row_version(r) for r in rows]
        counts = {v["id"]: n for v, n in zip(out, self._annotation_counts([v["id"] for v in out]))}
        for v in out:
            v["n_annotations"] = counts.get(v["id"], 0)
        return out

    def head_version(self, dataset_id: str) -> Optional[str]:
        rows = self.list_versions(dataset_id)
        return rows[-1]["id"] if rows else None

    def lineage(self, version_id: str) -> list[dict]:
        """从给定版本回溯到根，用于复现重放。"""
        out = []
        cur = self.get_version(version_id)
        seen = set()
        while cur and cur["id"] not in seen:
            seen.add(cur["id"])
            out.append(cur)
            cur = self.get_version(cur["parent_id"]) if cur["parent_id"] else None
        return list(reversed(out))

    def diff(self, v1: str, v2: str) -> dict:
        a = {self._key(r) for r in self._rows(v1)}
        b = {self._key(r) for r in self._rows(v2)}
        return {"added": sorted(b - a), "removed": sorted(a - b),
                "n_added": len(b - a), "n_removed": len(a - b),
                "n_common": len(a & b)}

    @staticmethod
    def _key(row: sqlite3.Row) -> str:
        return f"{row['item_uri']}|{row['geom']}|{row['label']}|{row['coords']}"

    def _rows(self, version_id: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM annotation WHERE version_id=? ORDER BY item_uri, id",
            (version_id,)).fetchall()

    def _annotation_counts(self, version_ids: list[str]) -> list[int]:
        if not version_ids:
            return []
        marks = ",".join("?" * len(version_ids))
        rows = self.db.execute(
            f"SELECT version_id, COUNT(*) c FROM annotation WHERE version_id IN ({marks}) "
            f"GROUP BY version_id", version_ids).fetchall()
        table = {r["version_id"]: r["c"] for r in rows}
        return [table.get(v, 0) for v in version_ids]

    # ------------------------------------------------------------------ 标签组

    def set_labels(self, dataset_id: str, labels: Iterable[dict]) -> None:
        """整体替换标签组。界面拖了顺序就是一次全量写，简单且原子。"""
        payload = [(dataset_id, l["name"], l.get("color", ""), l.get("hotkey", ""), i)
                   for i, l in enumerate(labels)]
        names = [p[1] for p in payload]
        if len(set(names)) != len(names):
            raise ValueError("标签名重复")
        with self._lock, self.db:
            self.db.execute("DELETE FROM label WHERE dataset_id=?", (dataset_id,))
            self.db.executemany(
                "INSERT INTO label(dataset_id,name,color,hotkey,ord) VALUES(?,?,?,?,?)", payload)

    def list_labels(self, dataset_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM label WHERE dataset_id=? ORDER BY ord", (dataset_id,)).fetchall()
        return [{"name": r["name"], "color": r["color"], "hotkey": r["hotkey"],
                 "order": r["ord"]} for r in rows]

    # ------------------------------------------------------------------ 标注

    def put_annotation(self, version_id: str, item_uri: str, ann: Ann,
                       role: str = "", state: str = "draft",
                       annotation_id: Optional[str] = None) -> str:
        if self.get_version(version_id) is None:
            raise KeyError(f"未知版本 {version_id}")
        if state not in ST.STATES:
            raise ValueError(f"未知状态 {state}")
        aid = annotation_id or _new_id("a")
        payload = (aid, version_id, self.to_rel(item_uri), ann.geom, json.dumps(ann.coords),
                   ann.label, ann.source, float(ann.confidence), ann.backend,
                   ann.slice, int(ann.level),
                   json.dumps(list(ann.color)) if ann.color else None, ann.note,
                   state, role, time.time())
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO annotation(id,version_id,item_uri,geom,coords,label,source,"
                "confidence,backend,slice,level,color,note,state,role,ts) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET coords=excluded.coords, label=excluded.label, "
                "source=excluded.source, confidence=excluded.confidence, "
                "backend=excluded.backend, slice=excluded.slice, level=excluded.level, "
                "color=excluded.color, note=excluded.note, state=excluded.state, "
                "role=excluded.role, ts=excluded.ts", payload)
        return aid

    def get_annotation(self, annotation_id: str) -> Optional[dict]:
        row = self.db.execute("SELECT * FROM annotation WHERE id=?", (annotation_id,)).fetchone()
        return _row_ann(row) if row else None

    def list_annotations(self, version_id: str, item_uri: Optional[str] = None,
                         state: Optional[str] = None, source: Optional[str] = None) -> list[dict]:
        sql, args = "SELECT * FROM annotation WHERE version_id=?", [version_id]
        if item_uri is not None:
            sql += " AND item_uri=?"
            args.append(self.to_rel(item_uri))     # 查询口径与入库口径一致，绝对/相对都能命中
        if state:
            sql += " AND state=?"
            args.append(state)
        if source:
            sql += " AND source=?"
            args.append(source)
        sql += " ORDER BY item_uri, id"
        return [_row_ann(r) for r in self.db.execute(sql, args)]

    def annotated_items(self, version_id: str) -> set:
        """有标注的数据项集合。返回**绝对路径**：调用方都拿它去磁盘定位文件，
        库里存的相对形式只是存储细节，不外泄给文件访问代码。"""
        return {self.to_abs(r["item_uri"]) for r in
                self.db.execute("SELECT DISTINCT item_uri FROM annotation WHERE version_id=?",
                                (version_id,))}

    def delete_annotation(self, annotation_id: str, role: str = "admin",
                          reason: str = "") -> bool:
        row = self.db.execute("SELECT * FROM annotation WHERE id=?", (annotation_id,)).fetchone()
        if row is None:
            return False
        with self._lock, self.db:
            self.db.execute("DELETE FROM annotation WHERE id=?", (annotation_id,))
            self._log_review(row, role, "delete", row["state"], "deleted", reason)
        return True

    def set_state(self, annotation_id: str, role: str, action: str,
                  reason: str = "") -> str:
        """角色 + 当前状态决定能否迁移；任何迁移都写 review 留痕。"""
        row = self.db.execute("SELECT * FROM annotation WHERE id=?", (annotation_id,)).fetchone()
        if row is None:
            raise KeyError(f"未知标注 {annotation_id}")
        target = ST.next_state(role, action, row["state"])
        with self._lock, self.db:
            self.db.execute("UPDATE annotation SET state=?, role=?, ts=? WHERE id=?",
                            (target, role, time.time(), annotation_id))
            self._log_review(row, role, action, row["state"], target, reason)
        return target

    def submit_many(self, version_id: str, item_uri: str, role: str,
                    reason: str = "") -> int:
        ids = [r["id"] for r in self.db.execute(
            "SELECT id,state FROM annotation WHERE version_id=? AND item_uri=?",
            (version_id, self.to_rel(item_uri)))]
        moved = 0
        for aid in ids:
            cur = self.get_annotation(aid)
            if cur and ST.can(role, "submit", cur["state"]):
                self.set_state(aid, role, "submit", reason)
                moved += 1
        return moved

    def review_bulk(self, version_id: str, role: str, action: str, reason: str = "",
                    max_confidence: Optional[float] = None) -> int:
        """按置信度批量验收：只动高置信的预标注，人工标的仍走逐条。"""
        sql = "SELECT id,state FROM annotation WHERE version_id=? AND state='submitted'"
        args: list = [version_id]
        if max_confidence is not None:
            sql += " AND confidence>=?"
            args.append(float(max_confidence))
        moved = 0
        for row in self.db.execute(sql, args):
            if ST.can(role, action, row["state"]):
                self.set_state(row["id"], role, action, reason)
                moved += 1
        return moved

    def queue(self, version_id: str, role: str) -> list[str]:
        """该角色待办的样本（按 item_uri 聚合）。"""
        wanted = ST.queue_states(role)
        if not wanted:
            return []
        marks = ",".join("?" * len(wanted))
        rows = self.db.execute(
            f"SELECT DISTINCT item_uri FROM annotation WHERE version_id=? "
            f"AND state IN ({marks}) ORDER BY item_uri", [version_id, *wanted])
        return [r["item_uri"] for r in rows]

    def review_log(self, annotation_id: Optional[str] = None,
                   version_id: Optional[str] = None) -> list[dict]:
        sql, args = "SELECT * FROM review WHERE 1=1", []
        if annotation_id:
            sql += " AND annotation_id=?"
            args.append(annotation_id)
        if version_id:
            sql += " AND version_id=?"
            args.append(version_id)
        sql += " ORDER BY id"
        return [{"annotation_id": r["annotation_id"], "dataset_id": r["dataset_id"],
                 "version_id": r["version_id"], "item_uri": r["item_uri"],
                 "role": r["role"], "action": r["action"],
                 "from": r["from_state"], "to": r["to_state"], "reason": r["reason"],
                 "ts": r["ts"]} for r in self.db.execute(sql, args)]

    def _log_review(self, row, role, action, from_state, to_state, reason) -> None:
        self.db.execute(
            "INSERT INTO review(annotation_id,dataset_id,version_id,item_uri,role,action,"
            "from_state,to_state,reason,ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (row["id"], _dataset_of_version(self.db, row["version_id"]), row["version_id"],
             row["item_uri"], role, action, from_state, to_state, reason, time.time()))

    def progress(self, version_id: str) -> dict:
        rows = self.db.execute(
            "SELECT state, COUNT(*) c FROM annotation WHERE version_id=? GROUP BY state",
            (version_id,)).fetchall()
        by_state = {r["state"]: r["c"] for r in rows}
        total = sum(by_state.values())
        return {"total": total, "by_state": by_state,
                "approved": by_state.get("approved", 0),
                "ratio": round(by_state.get("approved", 0) / total, 4) if total else 0.0}

    def label_counts(self, version_id: str) -> dict:
        rows = self.db.execute(
            "SELECT label, COUNT(*) c FROM annotation WHERE version_id=? GROUP BY label",
            (version_id,)).fetchall()
        return {r["label"] or "(空标签)": r["c"] for r in rows}

    # ------------------------------------------------------------------ 任务

    def create_task(self, kind: str, dataset_id: Optional[str] = None,
                    version_in: Optional[str] = None, version_out: Optional[str] = None,
                    params: Optional[dict] = None) -> str:
        tid = _new_id("t")
        now = time.time()
        with self._lock, self.db:
            self.db.execute(
                "INSERT INTO task(id,dataset_id,version_in,version_out,kind,params,state,"
                "report,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (tid, dataset_id, version_in, version_out, kind,
                 json.dumps(params or {}, ensure_ascii=False), "running", "{}", now, now))
        return tid

    def update_task(self, task_id: str, state: str, report: Optional[dict] = None,
                    version_out: Optional[str] = None) -> None:
        with self._lock, self.db:
            self.db.execute(
                "UPDATE task SET state=?, report=?, version_out=COALESCE(?,version_out), "
                "updated=? WHERE id=?",
                (state, json.dumps(report or {}, ensure_ascii=False), version_out,
                 time.time(), task_id))

    def get_task(self, task_id: str) -> Optional[dict]:
        row = self.db.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return None
        return {"id": row["id"], "dataset_id": row["dataset_id"], "kind": row["kind"],
                "version_in": row["version_in"], "version_out": row["version_out"],
                "params": _loads(row["params"], {}), "state": row["state"],
                "report": _loads(row["report"], {}),
                "created": row["created"], "updated": row["updated"]}

    def list_tasks(self, dataset_id: Optional[str] = None, kind: Optional[str] = None) -> list[dict]:
        sql, args = "SELECT id FROM task WHERE 1=1", []
        if dataset_id:
            sql += " AND dataset_id=?"
            args.append(dataset_id)
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY created DESC"
        return [self.get_task(r["id"]) for r in self.db.execute(sql, args)]

    # ------------------------------------------------------------------ 谱系导出

    def export_manifest(self, dataset_id: str, write: bool = True) -> dict:
        ds = self.get_dataset(dataset_id)
        if ds is None:
            raise KeyError(f"未知数据集 {dataset_id}")
        versions = self.list_versions(dataset_id)
        by_id = {v["id"]: v for v in versions}
        order = _topo(versions, by_id)
        manifest = {
            "dataset": ds["name"], "dataset_id": dataset_id, "type": ds["type"],
            "root": ds["root"], "labels": self.list_labels(dataset_id),
            "versions": [{"id": v["id"], "parent": v["parent_id"], "op": v["op"],
                          "params": v["params"], "n_annotations": v["n_annotations"],
                          "created": v["created"]} for v in order],
            "exports": [t["report"] for t in self.list_tasks(dataset_id, "export")
                        if t and t["state"] == "done"],
            "schema_version": SCHEMA_VERSION,
        }
        if write and ds["root"]:
            Path(ds["root"]).mkdir(parents=True, exist_ok=True)
            (Path(ds["root"]) / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest


DATASET_TYPES = ("classification", "detection", "segmentation", "series_interval",
                 "event_level", "mixed")


def _dataset_of_version(db, version_id: str):
    row = db.execute("SELECT dataset_id FROM version WHERE id=?", (version_id,)).fetchone()
    return row["dataset_id"] if row else None


def _row_dataset(row: sqlite3.Row, base: Optional[Path] = None) -> dict:
    root = row["root"]
    if root and base is not None and not Path(root).is_absolute():
        root = str(base / root)      # 库里存相对（可移植），读出来永远是绝对（可直接用）
    return {"id": row["id"], "name": row["name"], "type": row["type"], "root": root,
            "created": row["created"], "meta": _loads(row["meta"], {})}


def _row_version(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "dataset_id": row["dataset_id"], "parent_id": row["parent_id"],
            "op": row["op"], "params": _loads(row["params"], {}), "created": row["created"],
            "stats": _loads(row["stats"], {})}


def _row_ann(row: sqlite3.Row) -> dict:
    ann = Ann.from_json({"geom": row["geom"], "coords": _loads(row["coords"], []),
                         "label": row["label"], "source": row["source"],
                         "confidence": row["confidence"], "backend": row["backend"],
                         "slice": row["slice"], "level": row["level"],
                         "color": _loads(row["color"]), "note": row["note"]})
    return {"id": row["id"], "version_id": row["version_id"], "item_uri": row["item_uri"],
            "state": row["state"], "role": row["role"], "ts": row["ts"], "ann": ann}


def _topo(versions: list[dict], by_id: dict) -> list[dict]:
    """按 parent->child 排序；孤儿版本排在最后但绝不丢。"""
    done: set = set()
    out: list[dict] = []

    def visit(v: dict, guard: int = 0) -> None:
        if v["id"] in done or guard > len(versions):
            return
        parent = by_id.get(v["parent_id"]) if v["parent_id"] else None
        if parent is not None:
            visit(parent, guard + 1)
        if v["id"] not in done:
            done.add(v["id"])
            out.append(v)

    for v in sorted(versions, key=lambda x: x["created"]):
        visit(v)
    return out


__all__ = ["Store", "DATASET_TYPES", "SCHEMA_VERSION"]
