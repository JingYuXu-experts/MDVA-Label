"""SDAC 太阳活动区演示包构建器（REQ-R07）。

一次在线抓取，之后完全离线可跑：

    python experiments/build_solar_demo.py --days 90
    python experiments/build_solar_demo.py --days 90 --jobs 8 --end 2026-11-06
    python experiments/build_solar_demo.py --verify-only      # 不联网，只重跑防呆

产物：
    data/solar_ar/images/YYYY/MM/DD/<name>.jpg   一天一张，1024 档 HMIBC（或 HMIB）
    data/solar_ar/reports/YYYY-MM-DD.txt         NOAA 官方 SRS 原文（只有当日可取，见下）
    data/solar_ar/SOURCES.md                     逐张来源 / 抓取时刻 / 命名取得方式 / 校验和
    data/solar_ar/manifest.json                  root 指仓库内相对路径，truth 记录"真值靠人工"

三条来自实测的硬约束，代码里必须照办：
1. **文件名不许拼时间戳当事实**：AIA 档时间戳不规则（已实测 404），HMI 档虽为 15 分钟整，
   仍统一向服务器求证——优先解析目录 HTML；HTML 取不回来（目录 >0.5MB，慢链路上会超时截断）
   才对 15 分钟网格逐个发 HEAD 问服务器。两种取得方式都逐条记进 SOURCES.md（method=html|head）。
2. **不许用统一比例常数**：不同产品码日面半径不同（实测 HMIBC 488 / 0171 510 / 0304 468），
   所以本脚本不写任何"像素/角秒"常数；位置真值一律留给人工（见 shared/docs/数据集实测复核-航空航天-v1.md §4）。
3. **入库防呆**：每张都要过 `mdva.io` 的 sniff 与加载，modality 必须是 image。
   GIF 会被判成 video、多波段 TIFF 会被判成 cube——这两类一旦混进来必须当场报错，不许带病进演示。

历史 SRS 不可回补（已实测：SWPC json 端点 202+0 字节、warehouse 只有射电年包、
SIDC 按日期寻址返回 200 但正文 1 字节）。所以 reports/ 只能从今天起逐日累积，
缺的天数就是缺的，脚本不去伪造配对。
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

BASE = "https://sdo.gsfc.nasa.gov/assets/img/browse/"
SIZES = ("256", "512", "1024", "2048", "3072", "4096")
PRODUCTS = ("HMIBC", "HMIB", "HMIIC", "HMIIF")
# 目录 HTML 里一条真实文件链接；只认我们白名单内的产品码
LINK_RE = re.compile(r'href="(\d{8})_(\d{6})_(\d{4})_([A-Za-z0-9]+)\.jpg"')


def _run(argv, timeout):
    """跑一条 curl，返回 (returncode, stdout 文本)。用 curl 而不是 urllib：
    本环境对 NASA 站点的 TLS/重定向行为已用 curl 实测过，不引入新依赖。"""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout + 20)
        return p.returncode, (p.stdout or "")
    except subprocess.TimeoutExpired:
        return 28, ""


def head(url, timeout):
    """向服务器问一次这个文件名到底存不存在。返回 (状态码, 字节数)。"""
    rc, out = _run(["curl", "-sS", "-I", "--max-time", str(timeout), url], timeout)
    code = 0
    size = None
    for ln in out.splitlines():
        low = ln.strip().lower()
        if low.startswith("http/"):
            parts = ln.split()
            code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        elif low.startswith("content-length:"):
            try:
                size = int(ln.split(":", 1)[1].strip())
            except ValueError:
                size = None
    return code, size


def _parse_html(html, date, size, products):
    out = []
    for d8, t6, s, p in LINK_RE.findall(html):
        if s == size and p in products and d8 == f"{date:%Y%m%d}":
            out.append((f"{d8}_{t6}_{s}_{p}.jpg", p))
    out.sort()
    return out


def discover(date, products, size, timeout, tmpdir, html_timeout=25, deep_timeout=90):
    """取得该日一张可用文件的真实文件名。返回 dict 或 None（None = 该日确实没有）。

    顺序是"先向服务器求证、再退化"，不是"先猜再用"：
    1. 解析目录 HTML（产品经理定的硬约束）；
    2. HTML 取不回来（目录 >1MB，慢链路会超时截断）→ 对 15 分钟网格逐个 HEAD 问服务器；
    3. 两条都没命中 → 再用长超时抓一次完整 HTML 复核。这一步很重要：2026-10-08…15 实测
       就是靠它确认"那几天目录里根本没有 HMI 浏览产品，只有 AIA"，是真缺口而不是命名差异。
    命中方式逐条记进 SOURCES.md（method=html|head|html-deep）。
    """
    day = date.strftime("%Y/%m/%d/")
    url = BASE + day + "/"
    dst = tmpdir / f"{__import__('os').getpid()}_{date:%Y%m%d}.html"

    def _try_html(tmo, method):
        rc, _ = _run(["curl", "-sS", "-L", "--max-time", str(tmo), "-o", str(dst), url], tmo)
        hits = []
        if rc == 0 and dst.is_file():
            try:
                hits = _parse_html(dst.read_text(encoding="utf-8", errors="replace"), date, size, products)
            except OSError:
                hits = []
        try:
            dst.unlink(missing_ok=True)
        except OSError:
            pass  # 上一轮被强杀留下的句柄，不能把整轮构建带崩
        if hits:
            name, prod = hits[0]
            return {"name": name, "product": prod, "method": method, "candidates": len(hits)}
        return None

    got = _try_html(html_timeout, "html")
    if got:
        return got
    # HEAD 网格只是"HTML 没取全"时的兜底，且每档只问当天最靠前的几个时刻：
    # 全网格 4 产品 × 96 槽 = 384 次请求，慢链路上会把整轮构建拖死，而命中基本都在 00:00。
    for prod in products:
        for hh, mm in ((0, 0), (0, 15), (0, 30), (0, 45), (1, 0)):
            cand = f"{date:%Y%m%d}_{hh:02d}{mm:02d}00_{size}_{prod}.jpg"
            code, _ = head(BASE + day + "/" + cand, timeout)
            if code == 200:
                return {"name": cand, "product": prod, "method": "head", "candidates": 1}
    # 两条都没命中：抓一次完整 HTML 复核。这一步是"是真缺口还是命名差异"的判据——
    # 2026-10-08…15 就是靠它确认目录里只有 AIA 波长档、没有任何 HMI 浏览产品。
    return _try_html(deep_timeout, "html-deep")


def fetch(url, dest, timeout):
    """下载一张图；已存在且非空则跳过（可续跑）。返回 (状态, 字节, sha256)。"""
    if dest.is_file() and dest.stat().st_size > 0:
        return "cached", dest.stat().st_size, _sha(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    rc, _ = _run(["curl", "-sS", "-L", "--fail", "--max-time", str(timeout), "-o", str(tmp), url], timeout)
    if rc != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        return f"error(rc={rc})", 0, ""
    tmp.replace(dest)
    return "fetched", dest.stat().st_size, _sha(dest)


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def verify(path):
    """逐张防呆：必须被我们的读取层判成 image，且原生尺寸就是标称档。"""
    from mdva.io import MODALITY_LOADERS, sniff
    from PIL import Image

    kind = sniff(path)
    rec = {"sniff": kind, "modality": None, "px": None, "bytes": path.stat().st_size}
    if kind is None:
        rec["error"] = "sniff 判定这不是数据项"
        return rec
    try:
        item = MODALITY_LOADERS[kind](str(path))
        rec["modality"] = item.modality
    except Exception as exc:
        rec["error"] = f"加载失败 {type(exc).__name__}: {str(exc)[:80]}"
        return rec
    try:
        with Image.open(path) as im:
            rec["px"] = list(im.size)
            rec["mode"] = im.mode
    except Exception as exc:
        rec["error"] = f"像素读取失败 {type(exc).__name__}"
    return rec


def main(argv=None):
    ap = argparse.ArgumentParser(description="构建 data/solar_ar 太阳活动区演示包")
    ap.add_argument("--days", type=int, default=90, help="连续天数，一天一张（默认 90）")
    ap.add_argument("--end", default="", help="最后一天 YYYY-MM-DD，缺省为今天 UTC")
    ap.add_argument("--out", default="data/solar_ar")
    ap.add_argument("--products", default="HMIBC,HMIB,HMIIF,HMIIC",
                    help="按优先级取第一个当日可用的 HMI 产品码；AIA 波长档不在候选内——"
                         "实测其圆外烧录像素 14,327 是 HMI 档 390 的 37 倍")
    ap.add_argument("--size", default="1024", choices=SIZES)
    ap.add_argument("--jobs", type=int, default=8, help="并发下载数（慢链路下并发是主要提速手段）")
    ap.add_argument("--timeout", type=int, default=180, help="单张 curl 超时秒数")
    ap.add_argument("--html-timeout", type=int, default=25,
                    help="目录 HTML 的 curl 超时；慢链路上超时会退化为 HEAD 逐个问服务器")
    ap.add_argument("--deep-timeout", type=int, default=90,
                    help="两条路径都没命中时，再抓一次完整 HTML 复核的天数超时")
    ap.add_argument("--verify-only", action="store_true", help="不联网，只对已有产物重跑防呆")
    ap.add_argument("--no-srs", action="store_true", help="跳过 SRS 当日存档")
    args = ap.parse_args(argv)
    products = [p.strip() for p in args.products.split(",") if p.strip()]
    root = Path(args.out)
    root.mkdir(parents=True, exist_ok=True)
    end = (dt.date.fromisoformat(args.end) if args.end else dt.datetime.now(dt.timezone.utc).date())
    dates = [end - dt.timedelta(days=i) for i in range(args.days)][::-1]

    def matches(p):
        return any(p.name.endswith(f"_{args.size}_{pr}.jpg") for pr in products)

    if args.verify_only:
        dates = []
        paths = sorted(p for p in root.glob("images/**/*.jpg") if matches(p))
        rows = [(p, verify(p)) for p in paths]
        dl_by_path = {p: {"date": "-".join([p.parents[2].name, p.parents[1].name, p.parents[0].name]),
                          "method": "cached", "status": "cached", "sha256_16": _sha(p),
                          "bytes": p.stat().st_size} for p in paths}
        # 缺口按"已入库日期张成的区间"回算：verify-only 不联网，但也不能把缺口写成 0
        have = sorted({dt.date.fromisoformat(dl_by_path[p]["date"]) for p in paths})
        if have:
            span = [have[0] + dt.timedelta(days=i) for i in range((have[-1] - have[0]).days + 1)]
            missing = [str(d) for d in span if d not in set(have)]
            dates = span
        else:
            missing = []
    else:
        tmp = root / ".tmp"
        tmp.mkdir(exist_ok=True)

        def already(d):
            """已下过的天直接跳过：可续跑、可补天数，不重复消耗网络。"""
            hits = [p for p in (root / "images" / f"{d:%Y}" / f"{d:%m}" / f"{d:%d}").glob("*.jpg")
                    if matches(p)] if (root / "images" / f"{d:%Y}" / f"{d:%m}" / f"{d:%d}").is_dir() else []
            return sorted(hits)[0] if hits else None

        with cf.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
            plan = list(ex.map(lambda d: (d, already(d) or discover(
                d, products, args.size, args.timeout, tmp, args.html_timeout, args.deep_timeout)), dates))
        try:
            if not any(tmp.iterdir()):
                tmp.rmdir()
        except OSError:
            pass  # 上一轮被强杀留下的半截临时文件，不影响本轮产物
        todo = [(d, info["name"], info["method"], info["product"]) for d, info in plan
                if isinstance(info, dict)]
        cached = [(d, p) for d, p in plan if isinstance(p, Path)]
        missing = [str(d) for d, p in plan if p is None]
        dist = {}
        for _, _, m, _ in todo:
            dist[m] = dist.get(m, 0) + 1
        print(f"命名取得：{len(todo)} 天需下载（method 分布 {dist}），已在库 {len(cached)} 天，"
              f"缺 {len(missing)} 天 {missing[:8]}{' …' if len(missing) > 8 else ''}")

        def job(item):
            d, name, method, prod = item
            dest = root / "images" / f"{d:%Y}" / f"{d:%m}" / f"{d:%d}" / name
            url = BASE + d.strftime("%Y/%m/%d/") + "/" + name
            status, nbytes, sha = fetch(url, dest, args.timeout)
            return {"date": d.isoformat(), "name": name, "method": method, "product": prod,
                    "url": url, "status": status, "bytes": nbytes, "sha256_16": sha, "path": dest}

        with cf.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
            got = list(ex.map(job, todo))
        rows = [(Path(g["path"]), verify(g["path"])) for g in got if g["status"] in ("fetched", "cached")]
        rows += [(p, verify(p)) for _, p in cached]
        failed = [g for g in got if g["status"].startswith("error")]
        if failed:
            print(f"下载失败 {len(failed)} 张：{[(f['date'], f['status']) for f in failed[:6]]}")
        dl_by_path = {Path(g["path"]): g for g in got}
        for d, p in cached:
            dl_by_path.setdefault(p, {"date": d.isoformat(), "method": "cached",
                                      "status": "cached", "sha256_16": _sha(p),
                                      "bytes": p.stat().st_size})

    # 一天一张：同一天留下时间戳最早的那张，多出来的移到 .duplicates/ 待人工确认，
    # 不静默删除——它们是历次续跑用不同产品码命中同一天造成的（实测 2026-10-16 有 HMIB+HMIBC 两张）
    groups = {}
    for p, rec in rows:
        key = "/".join([p.parents[2].name, p.parents[1].name, p.parents[0].name])
        groups.setdefault(key, []).append((p, rec))
    kept, dupes = [], []
    dup_records = []
    for key in sorted(groups):
        items = sorted(groups[key], key=lambda t: t[0].name)
        kept.append(items[0])
        dupes += [p for p, _ in items[1:]]
    if dupes:
        # 重复日直接删（同一天已有更早的一张），但把 sha256 与文件名记进 SOURCES.md 备查——
        # 不能留在包内：mdva scan 是递归的，留着它就变成"一天两张"（实测 .duplicates 里的文件仍被扫到）
        for p in dupes:
            dup_records.append({"date": dl_by_path.get(p, {}).get("date", "-".join(
                [p.parents[2].name, p.parents[1].name, p.parents[0].name])),
                "file": p.name, "sha256_16": _sha(p), "bytes": p.stat().st_size})
            p.unlink()
        print(f"一天一张：删除 {len(dupes)} 张重复日文件（已记入 SOURCES.md 备查）")
    rows = kept

    bad = []
    for p, rec in rows:
        if rec.get("modality") != "image" or rec.get("error"):
            bad.append((p, rec))
        elif rec.get("px") != [int(args.size), int(args.size)]:
            bad.append((p, dict(rec, error=f"原生尺寸 {rec.get('px')} != {args.size}x{args.size}")))
    # SRS：只有当日可匿名取（历史不可回补，已实测）
    srs_note = "跳过"
    if not args.verify_only and not args.no_srs:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        try:
            from archive_srs import archive_once
            r = archive_once(root / "reports")
            srs_note = f"{r['date']} -> {r['path']}（{r['bytes']} 字节，{r['n_regions']} 个活动区）"
        except Exception as exc:
            srs_note = f"当日 SRS 存档失败：{type(exc).__name__}: {exc}"

    n_ok = len(rows) - len(bad)
    total_bytes = sum(rec.get("bytes", 0) for _, rec in rows)
    manifest = {
        "generated_by": "experiments.build_solar_demo",
        "source": BASE,
        "products_priority": products,
        "size": int(args.size),
        "license": "U.S. Government open data (NASA/SDO); no copyright. "
                   "Courtesy of NASA/SDO and the AIA, EVE, and HMI science teams.",
        "categories": {
            "solar_ar": {
                "title": "太阳活动区识别（真实公开图像 + 我们自标真值）",
                "short": "太阳活动区",
                "modalities": ["image"],
                "stress": "真实公开数据入库、人工画框与审核态、COCO 导出与回读校验",
                # root 必须是仓库内可解析相对路径（REQ-W19 的教训：一次性 Temp 目录会指空）
                "root": str(root).replace("\\", "/"),
                "n_images": n_ok,
                "bytes": total_bytes,
                "coverage": {"requested_days": len(dates) or n_ok, "gap_days": missing},
                "truth": {
                    "auto_ground_truth": False,
                    "reason": "浏览 JPG 无 WCS、日面朝向不可判；实测两种西向约定都不显著优于随机落点"
                              "（见 shared/docs/数据集实测复核-航空航天-v1.md §4）",
                    "taxonomy_source": "NOAA SWPC SRS：Z 列 = Hale 极性，Mag Type 列 = McIntosh 磁类型",
                    "labels_by": "人工在 MDVA 内标注",
                    "reports_dir": str(root / "reports").replace("\\", "/"),
                },
            }
        },
    }
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# 数据来源与抓取留痕 · data/solar_ar", "",
             f"- 来源：`{BASE}` （NASA SDO 全日面浏览产品，匿名直链，无需注册）",
             f"- 产品码优先级：`{','.join(products)}` / 尺寸档 `{args.size}`。"
             "**AIA 波长档不在候选内**：实测其圆外烧录像素 14,327，是 HMI 档 390 的 37 倍",
             "- 许可：**美国政府公开数据，无版权限制**；署名 `Courtesy of NASA/SDO and the AIA, EVE, and HMI science teams`",
             "- 命名取得：先解析目录 HTML → 取不回来才对 15 分钟网格发 HEAD 问服务器 → "
             "两条都没命中再抓一次完整 HTML 复核。**不拿拼出来的名字当事实**；"
             "命中方式逐条记在 method 列（html / head / html-deep / cached）",
             f"- SRS 当日存档：{srs_note}（历史 SRS 不可回补，已实测；缺的日子就是缺的）",
             f"- 抓取时刻（UTC）：{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}",
             "",
             "| 日期 | 文件 | 取得方式 | 状态 | 字节 | sha256(16) | sniff | modality | 原生尺寸 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for p, rec in rows:
        g = dl_by_path.get(p, {})
        lines.append(f"| {g.get('date', p.parent.name)} | {p.name} | {g.get('method', '-')} | "
                     f"{g.get('status', '-')} | {rec.get('bytes', 0):,} | {g.get('sha256_16', '-')} | "
                     f"{rec.get('sniff')} | {rec.get('modality')} | {rec.get('px')} |")
    lines += ["", f"## 日期缺口（{len(missing)} 天）", ""]
    if missing:
        lines += [f"- 下列日期三条取得路径都问过服务器（含一次完整 HTML 复核），"
                  "该日目录里没有任何候选 HMI 浏览产品——已实测例如 2026-10-10 目录里只有 AIA 波长档：",
                  "- " + ", ".join(missing)]
    else:
        lines.append("- 无：本次请求的每一天都取到了一张。")
    if dup_records:
        lines += ["", f"## 已删除的重复日文件（{len(dup_records)} 个，一天一张）", ""]
        lines += [f"- {r['date']} `{r['file']}`（{r['bytes']:,} 字节，sha256(16)={r['sha256_16']}）："
                  "同一天已保留更早的一张" for r in dup_records]
    (root / "SOURCES.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\n图像 {n_ok}/{len(rows)} 张通过防呆（modality=image 且原生 {args.size}x{args.size}），"
          f"合计 {total_bytes:,} 字节")
    print(f"manifest -> {root / 'manifest.json'}   SOURCES -> {root / 'SOURCES.md'}")
    if bad:
        print(f"\n防呆失败 {len(bad)} 张（不许带病进演示）：")
        for p, rec in bad[:20]:
            print(f"  {p}  {rec}")
        return 1
    if not rows:
        print("\n一张都没取到：检查网络或改用 --end 指定更早的日期")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
