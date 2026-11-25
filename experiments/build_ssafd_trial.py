# -*- coding: utf-8 -*-
"""REQ-R08 / SSAFD 采纳前置（P2b）：离线格式转换 + 逐张防呆 + 掩膜→poly + 导出对账。

SSAFD 落地页逐字实测（见 shared/docs/数据集实测复核-航空航天-v1.md §11）决定了本脚本三条设计：
1. **类别来自源目录，不来自像素值**。官方原文："In AR folder, a correspondence file between mask
   values and NOAA AR is also provided"；"a value of 0 represents background pixels and values of
   other than zero correspond to **individual structural instances**"；"AR, coronal hole, and
   filament datasets are stored as **independent submodules**"。
   ⇒ 掩膜非零值是**实例编号**（AR 那一份可通过对应表 join 到 NOAA 号），类别由 AR/CH/FL 目录决定。
   ⇒ 早先的 `1/2/3 → AR/CH/FL` 占位映射**语义就是错的**，本脚本不再有它；默认 `--class-from dir`。
2. **掩膜不进被扫描目录**：2 维 `.npy` 会被静默错读成 series 并入库（REQ-W25），含 3 维数组的
   `.npz` 会被 imageio 伪造视频元数据（REQ-W24）；而 PNG 掩膜若放进被扫描目录会成为可浏览条目
   （实测 n_items = 影像数 + 掩膜数），PM seq=638 裁定"不改代码、改包结构"。
   ⇒ 默认 `--mask-layout outside`：掩膜落与被扫描目录**平级**的 `_ssafd_masks_src/`，
     交付包里只有影像，`mdva scan` 的 n_items 必须等于影像张数（正例判据，不满足就非零退出）。
3. **几何载体只有 poly**：box 撞 REQ-W22（实测 512 上合法框误杀 5/8；真实写路径上 4 条全在图内的
   box 被 `Hub.put_annotations` 拒），poly 同批全通过。box 仅作 W22 复现证据，不写进产物。

掩膜搬出被扫描目录的代价已实测量化（不是推测）：`_mask_for` 靠目录约定找不到平级目录里的掩膜 ⇒
`include_masks=True` 的**栅格**掩膜会静默为 0；但专家掩膜不丢——它以 `segmentation` 多边形随 poly
进 COCO。`--export-probe` 每次跑都把这两个数一起打出来，免得"掩膜搬出去了但没人说导出会少东西"。

硬边界（PM seq=627 §四 / seq=638 §三）：
- 产物只落 `--out`（默认 `data/solar_ar_ssafd_trial`），**绝不允许指向已验证的 `data/solar_ar`**；
- **绝不读取任何 Downloads 目录**（发起人的下载进程不许干扰）；
- 被扫描目录出现 `.npy`/`.npz` → 当场拒绝并非零退出（P2b **负对照**）；
- 被扫描目录 `n_items != 影像张数` → 判不通过（P2b **正例判据**）。

用法：
    set PYTHONPATH=src
    E:/ANACONDA/envs/mdva/python.exe experiments/build_ssafd_trial.py --selftest
    E:/ANACONDA/envs/mdva/python.exe experiments/build_ssafd_trial.py --src <SSAFD解包目录>
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import sys
import traceback
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import mdva                                                     # noqa: E402
from mdva.io import MODALITY_LOADERS, sniff                      # noqa: E402
from mdva.annotate.model import Ann                              # noqa: E402

DEFAULT_OUT = ROOT / "data" / "solar_ar_ssafd_trial"
PROTECTED = (ROOT / "data" / "solar_ar",)          # 已验证的 SDAC 包，一个字节都不许动
FORBIDDEN_SRC = (Path.home() / "Downloads",)       # 发起人的下载目录，一行都不读
ARRAY_SUFFIXES = (".npy", ".npz")                  # 被扫描目录里一律不许出现
IMAGE_IN_SUFFIXES = (".fits", ".fit", ".fts", ".png", ".jpg", ".jpeg", ".tif", ".tiff")
MASK_IN_SUFFIXES = ARRAY_SUFFIXES + (".png", ".tif", ".tiff")
COMPONENT_LABELS = ("AR", "CH", "FL")              # 官方：three independent submodules
LICENSE = ("CC BY 4.0（数据集 JSON-LD 的 license 字段逐字为 "
           "https://creativecommons.org/licenses/by/4.0/）——有署名义务，引用串见 cite_as")
CITE_AS = ("@data{df62b0bfc7f847c1bb223a30032c5c88, title={Solar Situation Awareness and Flare "
           "Forecasting Dataset for Operational Applications}, author={LI MING and Jingjing Wang "
           "and Bingxian Luo}, year={2026}, month={July}, publisher={Science Data Bank}, "
           "doi={10.57760/sciencedb.space.03541}}")
MIN_POLY_COORDS = 6        # annotate/model.py:68 poly 需 >=3 顶点且坐标成对


# --------------------------------------------------------------------- 路径守卫

def guard(path: Path, kind: str, must_exist: bool = False) -> Path:
    p = Path(path).resolve()
    for bad in FORBIDDEN_SRC:
        try:
            bp = bad.resolve()
        except OSError:
            continue
        if p == bp or bp in p.parents:
            raise SystemExit(f"拒绝{kind} Downloads 目录：{p}（发起人的下载进程不许干扰）")
    if kind.startswith("产物"):
        for keep in PROTECTED:
            kp = keep.resolve()
            if p == kp or kp in p.parents or p in kp.parents:
                raise SystemExit(f"拒绝把产物写进已验证的 SDAC 包：{p} ↔ {kp}")
    if must_exist and not p.is_dir():
        raise SystemExit(f"{kind}目录不存在：{p}")
    return p


# ----------------------------------------------------------------- 读入 / 写出

def _squeeze(a) -> np.ndarray:
    arr = np.asarray(a)
    while arr.ndim > 2 and arr.shape[-1] in (1, 3, 4) and arr.ndim == 3:
        arr = arr[..., 0]
    if arr.ndim > 2:
        arr = arr.reshape(-1, arr.shape[-1]) if 1 in arr.shape else arr[0]
    return np.asarray(arr)


def read_array(path: Path) -> np.ndarray:
    """只用来读**源**（源目录不是被扫描目录，允许 .npy）；掩膜/影像取 2D。"""
    suffix = path.suffix.lower()
    if suffix == ".npy":
        return _squeeze(np.load(str(path), mmap_mode="r"))
    if suffix == ".npz":
        with np.load(str(path)) as zf:
            cands = [(zf[k].shape, k) for k in zf.files]
            two_d = [k for sh, k in cands if len(sh) == 2]
            pick = two_d[0] if two_d else max(cands)[1]
            return _squeeze(zf[pick])
    from PIL import Image
    with Image.open(str(path)) as im:
        return _squeeze(np.array(im))


def write_mask_png16(arr: np.ndarray, dst: Path) -> dict:
    """标签图 → 16-bit PNG。实测 `Image.fromarray(uint16)` 自动 mode='I;16'、**不传 mode**
    （传了 Pillow 12.3 已出 DeprecationWarning、13 会移除），且值 301/65535 不丢
    —— 正好覆盖"实例编号可能是 4 位 NOAA 号"这一形态。"""
    from PIL import Image
    mx = int(np.nanmax(arr)) if arr.size else 0
    if mx > 65535:
        raise ValueError(f"掩膜值 {mx} 超出 16-bit PNG 上限 65535，需换单波段 2D FITS")
    dst.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr.astype(np.uint16)).save(str(dst))
    return _fingerprint(dst)


def write_image_fits(arr: np.ndarray, dst: Path) -> dict:
    """影像 → 单波段 2D FITS，保 float32 动态范围（实测 sniff=image、native 正确）。"""
    from astropy.io import fits
    dst.parent.mkdir(parents=True, exist_ok=True)
    out = np.asarray(arr)
    if out.dtype.kind in "iub" and out.dtype.itemsize < 4:
        out = out.astype(np.int32)
    elif out.dtype.kind == "f":
        out = out.astype(np.float32)
    fits.PrimaryHDU(out).writeto(str(dst), overwrite=True)
    return _fingerprint(dst)


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _fingerprint(path: Path) -> dict:
    return {"file": path.name, "bytes": path.stat().st_size, "sha256_16": _sha(path)}


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")


# ------------------------------------------------------------------ 逐张防呆

def verify_tree(out: Path) -> tuple:
    """正对照口径：被扫描目录里每个文件必须 sniff 出 image 且真能 load；
    出现任何 .npy/.npz 直接判失败（REQ-W24/W25 的防呆，不是建议）。"""
    offenders, rows = [], []
    for p in sorted(out.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(out).as_posix()
        if p.suffix.lower() in ARRAY_SUFFIXES:
            offenders.append(rel)
            continue
        if p.suffix.lower() not in IMAGE_IN_SUFFIXES:
            continue
        s = sniff(str(p))
        rec = {"file": rel, "sniff": s, "native": "-", "bytes": p.stat().st_size,
               "sha256_16": _sha(p), "error": ""}
        if s != "image":
            rec["error"] = f"判成 {s or '非数据项'}，不是 image"
        else:
            try:
                item = MODALITY_LOADERS[s](str(p))
                sh = tuple(item.shape or ())
                rec["native"] = f"{int(sh[-1])}x{int(sh[-2])}" if len(sh) >= 2 else "?"
                np.asarray(item.load())
            except Exception as exc:
                rec["error"] = f"load 抛错 {type(exc).__name__}: {str(exc)[:80]}"
        rows.append(rec)
    return offenders, rows


def assert_store_clean(offenders: list, bad_rows: list) -> None:
    """P2b 硬防呆：被扫描目录里有 .npy/.npz，或有任何文件没判成 image —— 直接拒绝建包。
    写成异常而不是警告，是因为 REQ-W25 的教训：静默收下就等于把损坏伪装成成功。"""
    if offenders:
        raise SystemExit(
            f"被扫描目录里出现 {'、'.join(ARRAY_SUFFIXES)} 共 {len(offenders)} 个：{offenders[:5]}。"
            f"实测这类文件会被静默错读（2D→series 且入库零警告；含 3D 的 .npz→video 并由 imageio "
            f"伪造 fps/n_frames/宽高，见 REQ-W24/W25）。掩膜请转 16-bit PNG 并放在被扫描目录之外"
            f"（--mask-layout outside）。")
    if bad_rows:
        raise SystemExit(f"逐张防呆未 100% 判 image：{[(r['file'], r['error']) for r in bad_rows][:5]}")


# ------------------------------------------------------------- 掩膜 → poly/box

def mask_to_polys(arr: np.ndarray, component: str, ar_map: dict, tol: float = 2.0,
                  class_from: str = "dir") -> tuple:
    """标签图 → poly（扁平顶点、x=列 y=行）+ box（角点，仅作 W22 复现证据）。

    类别名默认取**源目录**（component），掩膜非零值当实例编号记进 note；
    AR 若在包内拿到 "mask values ↔ NOAA AR" 对应表，才在 note 里追加 noaa=。
    """
    from skimage import measure
    polys, boxes, n_bad_geom = [], [], 0
    for value in sorted(int(v) for v in np.unique(arr) if int(v) != 0):
        label = (ar_map.get(str(value)) or f"class{value}") if class_from == "value" else component
        note = f"instance={value}"
        if component == "AR" and str(value) in (ar_map or {}):
            note += f" noaa={ar_map[str(value)]}"
        m = (np.asarray(arr) == value)
        rows_, cols_ = np.nonzero(m)
        if rows_.size == 0:
            continue
        bb = [float(cols_.min()), float(rows_.min()), float(cols_.max()), float(rows_.max())]
        boxes.append({"geom": "box", "coords": bb, "label": label, "source": "import",
                      "value": value, "note": note})
        best = max(measure.find_contours(m.astype(np.float64), 0.5), key=len)
        ap = measure.approximate_polygon(best, tolerance=tol)
        coords = []
        for r_, c_ in ap:
            coords += [round(float(c_), 3), round(float(r_), 3)]
        if len(coords) < MIN_POLY_COORDS or len(coords) % 2:
            n_bad_geom += 1
            continue
        polys.append({"geom": "poly", "coords": coords, "label": label, "source": "import",
                      "value": value, "note": note, "n_vertices": len(coords) // 2})
    return polys, boxes, n_bad_geom


# ------------------------------------------------- 真入库校验（走产品自己的写路径）

def _hub(out: Path, sub: str):
    from mdva.web.services import Hub
    d = out / sub
    d.mkdir(parents=True, exist_ok=True)
    return Hub(d / "mdva.sqlite", out_dir=d / "out"), d


def _build_version(hub, out: Path, labels: list):
    scan = hub.scan(str(out))
    ds = hub.create_dataset("ssafd_trial", "segmentation", out, category="solar_ar",
                            labels=[{"name": v} for v in labels] or [{"name": "AR"}])
    return scan, ds["version_id"]


def _anns(payload: list, want_box: bool = False) -> list:
    return [Ann(geom=p["geom"], coords=p["coords"], label=p["label"], source="import",
                note=p.get("note", ""))
            for p in payload if p["geom"] == "poly" or (p["geom"] == "box" and want_box)]


def ingest_check(out: Path, anns_by_stem: dict, labels: list, n_images: int,
                 want_box: bool = False) -> dict:
    """用 Hub.create_dataset + Hub.put_annotations 真跑一遍入库校验。
    比直接调 `_pixel_violation` 强：走 services.py:648 的真实写路径；
    并把 PM seq=638 的正例判据（`n_items` == 影像张数）在这里当场判掉。"""
    hub, hub_dir = _hub(out, "_hubcheck")
    report = {"items": 0, "poly_accepted": 0, "poly_rejected": 0,
              "box_accepted": 0, "box_rejected": 0, "samples": []}
    try:
        scan, vid = _build_version(hub, out, labels)
        report["version_id"] = vid
        report["scan_modalities"] = scan.get("modalities")
        report["scan_n_items"] = scan.get("n_items")
        report["n_images_expected"] = n_images
        report["n_items_equals_images"] = (scan.get("n_items") == n_images)
        for row in scan.get("items") or []:
            uri = str(row["uri"])
            payload = anns_by_stem.get(Path(uri).stem)
            if not payload:
                continue                     # 掩膜不是被标注对象；outside 布局下它压根不在树里
            report["items"] += 1
            item = hub.load_item(uri)
            shape = tuple(item.shape or ())
            nw, nh = (int(shape[-1]), int(shape[-2])) if len(shape) >= 2 else (0, 0)
            anns = _anns(payload, want_box)
            try:
                hub.put_annotations(vid, uri, anns, role="annotator", state="draft")
                for a in anns:
                    report[f"{a.geom}_accepted"] += 1
            except ValueError:
                for a in anns:               # 整批被拒 -> 逐条定位是谁
                    try:
                        hub.put_annotations(vid, uri, [a], role="annotator", state="draft")
                        report[f"{a.geom}_accepted"] += 1
                    except ValueError as one:
                        report[f"{a.geom}_rejected"] += 1
                        if len(report["samples"]) < 6:
                            report["samples"].append({"uri": Path(uri).name, "geom": a.geom,
                                                      "coords": [round(float(c), 1)
                                                                 for c in a.coords[:4]],
                                                      "native": f"{nw}x{nh}", "label": a.label,
                                                      "error": str(one)[:170]})
    finally:
        hub.close()
        shutil.rmtree(hub_dir, ignore_errors=True)
    return report


# ----------------------------------------------------- 导出对账（掩膜搬出的代价）

def export_probe(out: Path, anns_by_stem: dict, labels: list, scratch: Path) -> dict:
    """实测两件事，把"掩膜搬出被扫描目录"的代价量化，不含推测：
    ① poly 随 COCO 的 `segmentation` 走 ⇒ 专家掩膜以矢量形态不丢；
    ② `_mask_for` 找不到平级目录里的掩膜 ⇒ `include_masks=True` 的**栅格**掩膜为 0。
    注：这里只导 poly（box 被 REQ-W22 拦，导了也是白导），不是全流程闭环彩排。
    """
    from mdva.export.base import _mask_for
    hub, hub_dir = _hub(out, "_hubexport")
    rep = {}
    try:
        scan, vid = _build_version(hub, out, labels)
        n_put = 0
        for row in scan.get("items") or []:
            uri = str(row["uri"])
            payload = anns_by_stem.get(Path(uri).stem)
            if not payload:
                continue
            anns = _anns(payload)                 # 只 poly
            hub.put_annotations(vid, uri, anns, role="annotator", state="draft")
            n_put += len(anns)
        rep["polys_written"] = n_put
        shutil.rmtree(scratch, ignore_errors=True)
        (scratch / "coco").mkdir(parents=True, exist_ok=True)
        res = hub.export(vid, "coco", {"include_masks": True, "split_ratios": "1/0/0",
                                       "segmentation": True}, out_dir=str(scratch / "coco"))
        rep["counts"] = (res or {}).get("counts") or {}
        rep["export_top_keys"] = sorted(list(res or {}))[:12]
        for k in ("n_images", "n_annotations", "n_categories", "n_skipped",
                  "spatial_annotations"):
            rep[k] = (res or {}).get(k)
        rep["issues"] = len((res or {}).get("issues") or [])
        rep["skipped"] = (res or {}).get("skipped") or []
        coco = scratch / "coco"
        # COCO 的标注文件实名 `instances.json`（另有 _train/_val/_test 切分），不是 *.coco.json
        jf = next((p for p in (coco / "instances.json",) if p.is_file()),
                  next((p for p in sorted(coco.glob("instances*.json"))), None))
        if jf is not None:
            j = json.loads(jf.read_text(encoding="utf-8"))
            anns = j.get("annotations") or []
            segs = [a for a in anns if a.get("segmentation")]
            rep["coco_file"] = jf.name
            rep["coco_annotations"] = len(anns)
            rep["coco_with_segmentation"] = len(segs)
            rep["coco_seg_sample_vertices"] = (len(segs[0]["segmentation"][0]) // 2
                                               if segs and segs[0].get("segmentation") else 0)
            rep["coco_categories"] = [c.get("name") for c in j.get("categories") or []]
            rep["coco_image_bytes_total"] = sum(p.stat().st_size for p in (coco / "images").glob("*")
                                                if (coco / "images").is_dir())
        rep["raster_masks_dir_written"] = (coco / "masks").is_dir()
        first = (scan.get("items") or [{}])[0].get("uri")
        rep["mask_for_returns"] = (_mask_for(hub.load_item(str(first)), out)
                                   if first else "无条目") or "None（平级目录不在搜索面）"
    finally:
        hub.close()
        shutil.rmtree(hub_dir, ignore_errors=True)
        shutil.rmtree(scratch, ignore_errors=True)
    return rep


# ------------------------------------------------------------------- 留痕写出

def write_records(out: Path, rows: list, conv: list, ing: dict, exp: dict,
                  source_desc: dict, stats: dict, masks_dir: Path, inside: bool) -> None:
    manifest = {
        "generated_by": "experiments.build_ssafd_trial",
        "source": source_desc["text"],
        "source_doi": "10.57760/sciencedb.space.03541",
        "source_version": "3.0.0",
        "source_size_bytes": 4720273868,
        "cite_as": CITE_AS,
        "license": LICENSE,
        "categories": {
            "solar_ar": {
                "title": "太阳活动区（SSAFD 专家掩膜 → 我们转换后的可标注包）",
                "short": "太阳活动区",
                "modalities": ["image"],
                "stress": "真实公开数据入库、专家掩膜→poly、审核态、COCO 导出与回读校验",
                "root": _rel(out),
                "n_images": stats["n_images"],
                "bytes": stats["bytes"],
                "coverage": {"days": sorted(stats["days"]), "gap_days": stats["gap_days"]},
                "provenance": source_desc["provenance"],
                "truth": {
                    "auto_ground_truth": False,
                    "provider_labels": stats["mode"] == "real",
                    "labels_by": ("SSAFD 专家分割掩膜（数据作者标注，随包分发）+ "
                                  "我们做的 掩膜→poly 几何转换" if stats["mode"] == "real"
                                  else "本机自造标签 blob（P2b 预验用），**不是 SSAFD 专家标注**"),
                    "class_source": "源目录（AR/CH/FL），**不是像素值**",
                    "class_source_quote": "AR, coronal hole, and filament datasets are stored as "
                                          "independent submodules …; values of other than zero "
                                          "correspond to individual structural instances",
                    "instance_id_in": "annotation.note（instance=<掩膜值>；AR join 成功时再加 noaa=）",
                    "ar_map_joined": stats["ar_map_joined"],
                    "ar_map_note": "SSAFD 只在 AR 目录提供 mask 值↔NOAA AR 对应表（官方未给文件名）；"
                                   "CH/FL 官方未提同类表 ⇒ 其掩膜值只能当实例编号",
                    "geometry_carrier": "poly",
                    "geometry_carrier_note": "box 在 REQ-W22 修复后才可用；实测 512 上合法框误杀 5/8，"
                                             "poly 同批 8/8 通过",
                    "taxonomy_source": "NOAA SWPC SRS：Z 列 = Hale 极性，Mag Type 列 = McIntosh；"
                                       "SSAFD 的 AR_parameters 另含 Hale/McIntosh 列",
                },
                "conversion": {"n_converted": len(conv),
                               "target_mask": "16-bit PNG，落在被扫描目录**之外**",
                               "target_image": "单波段 2D FITS（整型影像保留 int32）",
                               "masks_dir": _rel(masks_dir),
                               "masks_inside_scanned_root": inside,
                               "array_suffixes_forbidden_in_store": list(ARRAY_SUFFIXES)},
                "ingest_check": ing,
                "export_probe": exp,
            }
        },
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    lines = ["# 数据来源与转换留痕 · " + _rel(out), "",
             f"- 来源：{source_desc['text']}",
             "- 版本 / DOI：**3.0.0**（2026-02-25 发布）/ `10.57760/sciencedb.space.03541`；"
             "官方登记总大小 4,720,273,868 字节；作者 LI MING / Jingjing Wang / Bingxian Luo；"
             "conditionsOfAccess = PUBLIC",
             f"- 许可：{LICENSE}",
             f"- 引用串（署名义务的具体写法）：`{CITE_AS}`",
             "- 形态前置（P2b）：掩膜 = 16-bit PNG 且落**被扫描目录之外**；影像 = 单波段 2D FITS。"
             "被扫描目录内既不允许出现 `.npy`/`.npz`（会被静默错读，REQ-W24/W25），"
             "也不允许出现掩膜 PNG（会成为条目 ⇒ `n_items != 影像张数`）。",
             "- 类别口径：AR/CH/FL 由**源目录**决定；掩膜非零值是**实例编号**，"
             "AR 可通过 SSAFD 自带对应表 join 到 NOAA 号（join 未证实前不外说）。",
             "- 几何载体：poly（box 等 REQ-W22 落地）",
             f"- 生成时刻（UTC）：{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}",
             "",
             "| 文件 | sniff | 字节 | sha256(16) | 状态 |",
             "|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['file']} | {r['sniff']} | {r['bytes']:,} | {r['sha256_16']} | "
                     f"{r['error'] or 'ok，native ' + r['native']} |")
    lines += ["", "## 转换对（源 → 产物）", "",
              "| 组件 | 源（影像 + 掩膜） | → 影像（树内） | → 掩膜（树外） | 掩膜字节 | 实例数 |",
              "|---|---|---|---|---|---|"]
    for c in conv:
        lines.append(f"| {c['component']} | {c['src']} | {c['image']} | {c['mask']} | "
                     f"{c.get('mask_bytes', 0):,} | {c['n_instances']} |")
    lines += ["", "## 入库校验（走 Hub.put_annotations 真实写路径）", "",
              f"- `mdva scan`：modalities=`{ing.get('scan_modalities')}`，"
              f"n_items={ing.get('scan_n_items')}，影像张数={ing.get('n_images_expected')} ⇒ "
              f"**n_items == 影像张数：{ing.get('n_items_equals_images')}**",
              f"- 被标注条目：{ing.get('items')}",
              f"- poly：接受 {ing.get('poly_accepted', 0)} / 拒绝 {ing.get('poly_rejected', 0)}",
              f"- box：接受 {ing.get('box_accepted', 0)} / 拒绝 {ing.get('box_rejected', 0)}"
              f"（拒绝项 = REQ-W22 复现证据，不进产物）"]
    if ing.get("samples"):
        lines += ["", "  被拒样本原文：", ""]
        for s in ing["samples"]:
            lines.append(f"  - `{s['uri']}` {s['geom']}{s['coords']} @ {s['native']} → {s['error']}")
    if exp:
        lines += ["", "## 导出对账（掩膜搬出树外的代价，实测非推测）", "",
                  f"- 写入 poly：{exp.get('polys_written')} 条；COCO 文件 `{exp.get('coco_file')}` "
                  f"标注 {exp.get('coco_annotations')} 条，其中带 `segmentation` 多边形 "
                  f"**{exp.get('coco_with_segmentation')} 条**"
                  f"（样例 {exp.get('coco_seg_sample_vertices')} 顶点）⇒ **专家掩膜以矢量形态不丢**",
                  f"- COCO 类别：{exp.get('coco_categories')}",
                  f"- `include_masks=True` 时**栅格** `masks/` 是否落盘：**{exp.get('raster_masks_dir_written')}**；"
                  f"`_mask_for` 返回 `{exp.get('mask_for_returns')}`",
                  f"- 导出 counts：{json.dumps(exp.get('counts'), ensure_ascii=False)}；"
                  f"n_images={exp.get('n_images')} n_annotations={exp.get('n_annotations')} "
                  f"n_skipped={exp.get('n_skipped')} issues={exp.get('issues')}",
                  f"- COCO 里图片重写一份的合计字节：{exp.get('coco_image_bytes_total'):,}"
                  if exp.get("coco_image_bytes_total") is not None else "- （未统计 COCO 图片字节）"]
    (out / "SOURCES.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------- 自造源

def synth_source(dir_: Path, n=3, size=256) -> int:
    """按官方结构造源：AR/CH/FL 三个 independent submodules，各含 images/ + masks/。
    掩膜非零值 = 实例编号；**AR 故意用 4 位号**（官方判据：值域出现 4 位数就是 NOAA 编号），
    CH/FL 用个位号，正好把"join 只可能发生在 AR"这条走一遍。"""
    dir_.mkdir(parents=True, exist_ok=True)
    yy, xx = np.mgrid[0:size, 0:size]
    plan = {"AR": [4566, 4571], "CH": [1, 2], "FL": [7]}
    made = 0
    for comp, ids in plan.items():
        for i in range(n):
            lab = np.zeros((size, size), np.int32)
            # k=0 放左上、k>=1 放右下：box 的正例（应放行）与负例（被 W22 误杀）两侧都有
            for k, v in enumerate(ids):
                if k == 0:
                    cx, cy = 40 + 9 * i, 40 + 7 * i
                else:
                    cx = min(size - 26, 52 + 14 * i + 11 * k + (5 if comp == "FL" else 0))
                    cy = max(30, size - 58 - 13 * i - 9 * k)
                r = 18 + 6 * k + 3 * i
                lab[((xx - cx) ** 2 + (yy - cy) ** 2) <= r * r] = v
            stem = f"{20260701 + i:08d}_{comp.lower()}{i:02d}"
            di, dm = dir_ / comp / "images", dir_ / comp / "masks"
            di.mkdir(parents=True, exist_ok=True)
            dm.mkdir(parents=True, exist_ok=True)
            np.save(str(di / f"{stem}.npy"),
                    (np.sin(xx / 17.0 + i) * np.cos(yy / 21.0) * 1500.0).astype(np.float32))
            np.save(str(dm / f"{stem}.npy"), lab)
            made += 1
    return made


def find_ar_map(src: Path) -> dict:
    """找 SSAFD 的 "mask values ↔ NOAA AR" 对应表。官方只说它在 AR 目录、**没给文件名**
    （落地页逐字复核过），所以按形状猜候选并把命中的条数报出来；没命中就返回空 ⇒
    `ar_map_joined=false`，join 这句话不外说。"""
    ar = src / "AR"
    cands = [p for p in sorted(ar.rglob("*"))
             if p.is_file() and p.suffix.lower() in (".csv", ".txt", ".json")
             and re.search(r"(noaa|corr|map|relation|table|label)", p.stem, re.I)] if ar.is_dir() else []
    out = {}
    for p in cands[:1]:
        if p.suffix.lower() == ".json":
            try:
                out = {str(k): str(v) for k, v in json.loads(p.read_text(encoding="utf-8-sig")).items()}
            except Exception:
                out = {}
            break
        for ln in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            cells = [c.strip() for c in re.split(r"[,;\t]", ln) if c.strip()]
            if len(cells) >= 2 and cells[0].lstrip("-").isdigit():
                out[cells[0]] = cells[1]
    return {"path": _rel(cands[0]) if cands else "", "n": len(out),
            "candidates": [_rel(p) for p in cands], "map": out}


# ------------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", default="", help="SSAFD 解包目录（内含 AR/CH/FL 子模块）")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="被扫描的交付包目录")
    ap.add_argument("--masks-dir", default="", help="掩膜落盘目录（默认与 --out 平级的 _ssafd_masks_src）")
    ap.add_argument("--mask-layout", choices=("outside", "inside"), default="outside",
                    help="outside=掩膜在被扫描目录外（PM 裁定，默认）；inside=旧摆放，仅供对照")
    ap.add_argument("--components", default=",".join(COMPONENT_LABELS), help="参与构建的源子模块")
    ap.add_argument("--class-from", choices=("dir", "value"), default="dir",
                    help="dir=类别来自源目录（官方口径）；value=按掩膜值映射（仅当 SSAFD 真给类别码时）")
    ap.add_argument("--ar-map", default="", help="AR 的 mask 值↔NOAA 号 JSON；缺省时自动在源里找对应表")
    ap.add_argument("--selftest", action="store_true", help="用本机自造样本当源，不读任何外部目录")
    ap.add_argument("--size", type=int, default=256, help="自造样本边长")
    ap.add_argument("--n-per-component", type=int, default=3, help="每个子模块自造几张")
    ap.add_argument("--tolerance", type=float, default=2.0, help="轮廓简化容差（像素）")
    ap.add_argument("--with-box-evidence", action="store_true",
                    help="入库校验时附带 box，复现 REQ-W22 拒绝原文（box 不写进产物）")
    ap.add_argument("--export-probe", action="store_true", default=True,
                    help="跑一次 COCO 导出，实测掩膜搬出树外的代价")
    ap.add_argument("--negative-control", action="store_true", default=True,
                    help="建完后往被扫描目录偷放一个 .npy，验证防呆当场拒绝（默认开）")
    args = ap.parse_args()

    out = guard(Path(args.out), "产物", False)
    comps = [c.strip() for c in args.components.split(",") if c.strip()]
    if args.masks_dir:
        masks_dir = Path(args.masks_dir).resolve()
    elif args.mask_layout == "inside":
        # 真正的"旧摆放"= 与影像同级的 <stem>_mask.png，也就是 _mask_for 会命中的那一种；
        # 放 out/_masks_inside 是另一种摆放，_mask_for 同样找不到，测不出对照差异（踩过）
        masks_dir = (out / "images").resolve()
    else:
        masks_dir = (out.parent / "_ssafd_masks_src").resolve()
    inside = str(masks_dir).startswith(str(out) + "\\") or masks_dir == out
    stats = {"mode": "selftest", "days": [], "gap_days": [], "ar_map_joined": False}
    print(f"运行自证：mdva.__file__ = {mdva.__file__}")
    print(f"          numpy = {np.__version__}  python = {sys.version.split()[0]}")
    print(f"          被扫描目录 = {out}")
    print(f"          掩膜目录   = {masks_dir}（在树内={inside}，layout={args.mask_layout}）")
    print(f"          组件 = {comps}  类别来源 = --class-from {args.class_from}")

    protected_before = {}
    for keep in PROTECTED:
        mf = keep / "manifest.json"
        if mf.is_file():
            j = json.loads(mf.read_text(encoding="utf-8"))
            protected_before[str(keep)] = {k: v["n_images"] for k, v in j.get("categories", {}).items()}
            protected_before[str(keep) + ":bytes"] = sum(
                v["bytes"] for v in j.get("categories", {}).values())
            protected_before[str(keep) + ":sha"] = _sha(mf)
    if protected_before:
        print(f"已验证 SDAC 包基线：{protected_before}")

    if args.selftest or not args.src:
        src = ROOT / "experiments" / "probes" / "_ssafd_synth_src"
        if src.exists():
            shutil.rmtree(str(src))
        k = synth_source(src, n=args.n_per_component, size=args.size)
        source_desc = {
            "text": "本机自造样本（P2b 预验）：按 SSAFD 官方结构造 AR/CH/FL 三个子模块，"
                    "掩膜 int32 实例编号（AR 用 4 位号形状），影像 float32 .npy",
            "provenance": {"dataset": "本机自造（模拟 SSAFD 结构）", "version": "",
                           "acquired_by": "本机 experiments 生成", "fetched_by_us": False}}
        print(f"自造源：{src}（{k} 组）")
    else:
        src = guard(Path(args.src), "源", True)
        source_desc = {
            "text": f"SSAFD 本机解包目录 {src}（由发起人下载，我方未发起任何网络请求）",
            "provenance": {"dataset": "SSAFD (NSSC, National Space Science Center)",
                           "version": "3.0.0", "acquired_by": "发起人本机下载",
                           "fetched_by_us": False, "doi": "10.57760/sciencedb.space.03541"}}
        stats["mode"] = "real"
        print(f"源目录：{src}")

    ar_map = json.loads(args.ar_map) if args.ar_map else {}
    if not ar_map and not args.selftest:
        found = find_ar_map(src)
        if found["n"]:
            ar_map, stats["ar_map_joined"] = found["map"], True
            print(f"  AR 对应表命中：{found['path']}（{found['n']} 条，已 join）")
        else:
            print(f"  AR 对应表未命中（候选 {found['candidates']}）⇒ ar_map_joined=false，join 不外说；"
                  f"CH/FL 官方无同类表")

    conv, anns_by_stem = [], {}
    n_img_bytes = n_mask_bytes = 0
    for comp in comps:
        img_dir, mask_dir = src / comp / "images", src / comp / "masks"
        if not img_dir.is_dir():
            print(f"  跳过组件 {comp}：源里没有 {comp}/images 目录")
            continue
        masks = ({p.stem: p for p in sorted(mask_dir.iterdir())
                  if p.is_file() and p.suffix.lower() in MASK_IN_SUFFIXES}
                 if mask_dir.is_dir() else {})
        for ip in sorted(img_dir.iterdir()):
            if not ip.is_file() or ip.suffix.lower() not in IMAGE_IN_SUFFIXES + ARRAY_SUFFIXES:
                continue
            mp = masks.get(ip.stem)
            if mp is None:
                print(f"  跳过 {comp}/{ip.name}：找不到配对掩膜")
                continue
            arr = read_array(ip) if ip.suffix.lower() in ARRAY_SUFFIXES else None
            if arr is not None and arr.ndim != 2:
                print(f"  跳过 {comp}/{ip.name}：影像不是 2D（形如 {arr.shape}），不自动摊平")
                continue
            mask_arr = read_array(mp)
            if mask_arr.ndim != 2:
                print(f"  跳过 {comp}/{ip.stem}：掩膜不是 2D（形如 {mask_arr.shape}）")
                continue
            stem = f"{comp}_{ip.stem}"
            if arr is None:                         # 源本身是影像文件：原样复制，不重编码
                dst = out / "images" / f"{stem}{ip.suffix.lower()}"
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(str(ip), str(dst))
                fp = _fingerprint(dst)
            else:                                   # .npy 影像 → 单波段 2D FITS
                dst = out / "images" / f"{stem}.fits"
                fp = write_image_fits(arr, dst)
            n_img_bytes += fp["bytes"]
            mfp = write_mask_png16(mask_arr, masks_dir / f"{stem}_mask.png")
            n_mask_bytes += mfp["bytes"]
            polys, boxes, n_bad = mask_to_polys(mask_arr, comp, ar_map, args.tolerance,
                                                args.class_from)
            if n_bad:
                print(f"  警告 {comp}/{ip.stem}：{n_bad} 个轮廓顶点不足 {MIN_POLY_COORDS}，已丢弃")
            conv.append({"component": comp, "src": f"{comp}/{ip.name} + {comp}/{mp.name}",
                         "image": f"images/{dst.name}",
                         "mask": _rel(masks_dir / f"{stem}_mask.png"),
                         "mask_bytes": mfp["bytes"], "n_instances": len(polys),
                         "mask_native": f"{mask_arr.shape[1]}x{mask_arr.shape[0]}"})
            anns_by_stem[stem] = polys + (boxes if args.with_box_evidence else [])
            d = re.search(r"(\d{8})", ip.stem)
            if d:
                stats["days"].append(f"{d.group(1)[:4]}-{d.group(1)[4:6]}-{d.group(1)[6:]}")
    stats["n_images"] = len(conv)
    stats["bytes"] = n_img_bytes + n_mask_bytes
    if not conv:
        raise SystemExit("没有任何影像+掩膜配对成功，包不产出（别交空包）")

    offenders, rows = verify_tree(out)
    bad_rows = [r for r in rows if r["error"]]
    n_mask_in_tree = sum(1 for r in rows if "_mask" in r["file"])
    print(f"\n一、逐张防呆：{len(rows)} 个入库文件，判 image = {len(rows) - len(bad_rows)}/{len(rows)}，"
          f"树内 .npy/.npz = {len(offenders)}，树内掩膜条目 = {n_mask_in_tree}")
    for r in bad_rows:
        print(f"    FAIL {r['file']}：{r['error']}")
    assert_store_clean(offenders, bad_rows)

    labels = sorted({str(v) for v in (ar_map.values() if stats["ar_map_joined"] else [])}) or comps
    ing = ingest_check(out, anns_by_stem, labels, stats["n_images"],
                       want_box=args.with_box_evidence)
    print(f"\n二、入库校验：scan modalities={ing.get('scan_modalities')} "
          f"n_items={ing.get('scan_n_items')} / 影像张数={ing.get('n_images_expected')} "
          f"=> n_items==影像张数：{ing.get('n_items_equals_images')}；被标注条目={ing['items']}")
    print(f"    poly 接受 {ing['poly_accepted']} / 拒绝 {ing['poly_rejected']}；"
          f"box 接受 {ing['box_accepted']} / 拒绝 {ing['box_rejected']}")
    for s in ing.get("samples", []):
        print(f"    被拒原文 [{s['geom']}] {s['uri']} {s['coords']} @ {s['native']}：{s['error']}")

    exp = {}
    if args.export_probe:
        exp = export_probe(out, anns_by_stem, labels,
                           ROOT / "experiments" / "probes" / "_synth" / "_export_probe")
        print(f"\n三、导出对账：写入 poly {exp.get('polys_written')} 条 → COCO "
              f"`{exp.get('coco_file')}` 标注 {exp.get('coco_annotations')} 条，"
              f"带 segmentation {exp.get('coco_with_segmentation')} 条"
              f"（样例 {exp.get('coco_seg_sample_vertices')} 顶点），类别 {exp.get('coco_categories')}")
        print(f"    include_masks=True 时栅格 masks/ 落盘 = {exp.get('raster_masks_dir_written')}；"
              f"`_mask_for` 返回 `{exp.get('mask_for_returns')}`")
        print(f"    导出 counts：{json.dumps(exp.get('counts'), ensure_ascii=False)[:200]}；"
              f"n_images={exp.get('n_images')} n_annotations={exp.get('n_annotations')} "
              f"n_skipped={exp.get('n_skipped')} issues={exp.get('issues')}")

    write_records(out, rows, conv, ing, exp, source_desc, stats, masks_dir, inside)

    nc_msg = ""
    if args.negative_control and conv:
        bad = out / "images" / "mask_negative_control.npy"
        np.save(str(bad), np.zeros((8, 8), np.int32))
        try:
            off2, rows2 = verify_tree(out)
            assert_store_clean(off2, [r for r in rows2 if r["error"]])
            nc_msg = "负对照**失败**：放进 .npy 后建包流程没拒绝（防呆没生效）"
        except SystemExit as exc:
            nc_msg = f"负对照通过，拒绝原文：{exc}"
        finally:
            bad.unlink(missing_ok=True)
        print(f"\n四、P2b 负对照：{nc_msg}")

    polluted = []
    for keep in PROTECTED:
        mf = keep / "manifest.json"
        if mf.is_file():
            j = json.loads(mf.read_text(encoding="utf-8"))
            now = {k: v["n_images"] for k, v in j.get("categories", {}).items()}
            bts = sum(v["bytes"] for v in j.get("categories", {}).values())
            sha = _sha(mf)
            before = (protected_before.get(str(keep)), protected_before.get(str(keep) + ":bytes"),
                      protected_before.get(str(keep) + ":sha"))
            if before[0] and (now != before[0] or bts != before[1] or sha != before[2]):
                polluted.append(f"{keep}: {before} -> {now}/{bts}/{sha}")
    print(f"\n五、已验证 SDAC 包未被污染："
          f"{'是（跑前跑后 manifest 逐字节相同）' if not polluted else '否 ' + str(polluted)}")
    print(f"\n产物：{stats['n_images']} 张影像进包（{n_img_bytes:,} 字节）；"
          f"树外掩膜 {len(conv)} 个 / {n_mask_bytes:,} 字节（不计入交付包体积）")
    print(f"manifest -> {out / 'manifest.json'}   SOURCES -> {out / 'SOURCES.md'}")

    fails = []
    if not ing.get("n_items_equals_images"):
        fails.append(f"n_items({ing.get('scan_n_items')}) != 影像张数({ing.get('n_images_expected')})")
    if sorted(k for k in (ing.get("scan_modalities") or {}) if k != "image"):
        fails.append(f"scan 出现非 image 模态 {ing.get('scan_modalities')}")
    if int(ing.get("poly_rejected", 0)) > 0:
        fails.append(f"poly 有被拒条目 {ing['poly_rejected']} 条")
    if int(ing.get("items", 0)) != stats["n_images"]:
        fails.append(f"被标注条目 {ing.get('items')} != 影像张数 {stats['n_images']}")
    if exp and exp.get("coco_with_segmentation", 0) != exp.get("polys_written"):
        fails.append(f"COCO segmentation 条数({exp.get('coco_with_segmentation')}) != "
                     f"写入 poly({exp.get('polys_written')})")
    if "没生效" in nc_msg:
        fails.append("负对照没拦住 .npy")
    if polluted:
        fails.append("污染了已验证的 SDAC 包")
    if fails:
        print("\n结论：P2b **未通过** → " + "；".join(fails))
        return 2
    print("\n结论：P2b 通过（逐张判 image、n_items==影像张数、模态只有 image、poly 全部入库、"
          "COCO segmentation 与 poly 数一致、负对照被当场拒绝、SDAC 包逐字节未变）")
    return 0


if __name__ == "__main__":
    # 注意：sys.exit() 自己也抛 SystemExit。v1 写成 try: sys.exit(main()) 会把
    # "正常退出 0" 当成拒绝处理（实测 exit=3），先取返回值再退出。
    try:
        code = main()
    except SystemExit as exc:
        print(f"拒绝：{exc}", file=sys.stderr)
        code = 3
    except Exception:
        traceback.print_exc()
        code = 4
    sys.exit(code)
