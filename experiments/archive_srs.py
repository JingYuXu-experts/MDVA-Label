"""NOAA SWPC SRS 每日存档（REQ-R07 第 4 条）。

为什么只能"从今天起攒"：历史批量我已实测取不到正文——SWPC 的 `json/srs` 端点三次返回
202 + 0 字节，`ftp.swpc.noaa.gov/pub/warehouse/` 匿名可用但里面是 1966 年起的射电频谱
年包（无 SRS 文件），SIDC 按日期寻址的 7 个入口全部返回 200 但正文 1 字节。
所以缺的日子补不回来，本脚本也**不去伪造配对**：一次运行只写当日一份，幂等可重复跑。

用法：
    python experiments/archive_srs.py                      # 存当日
    python experiments/archive_srs.py --dir data/solar_ar/reports
    python experiments/archive_srs.py --force              # 覆盖重写（默认不覆盖）

挂定时任务的建议：每天 01:00 UTC 之后跑一次（SRS 在 0030Z 签发），失败要留非零退出码，
不要静默跳过——漏一天就少一天，这是可观测的缺口，不该被脚本掩盖。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import re
import subprocess
import sys
from pathlib import Path

URL = "https://services.swpc.noaa.gov/text/srs.txt"
ISSUED_RE = re.compile(r"Issued at (\d{4})Z on (\d{2}) (\w{3}) (\d{4})")
VALID_RE = re.compile(r"Locations Valid at (\d{2})/(\d{4})Z")
# 表头 Nmbr Location Lo Area Z LL NN Mag Type —— Z 是 Hale 极性，Mag Type 是 McIntosh
ROW_RE = re.compile(r"^\s*(\d{4})\s+([NS]\d{2}[EW]\d{2,3})\s+(\d{3})\s+(\d{4})\s+"
                    r"(\w{3})\s+(\d{2})\s+(\d{2})\s+(.+?)\s*$")
MONTHS = {m: i for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), start=1)}


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


def fetch_raw(timeout=60):
    """取原文字节。宁可抛错也不返回半截文件。"""
    p = subprocess.run(["curl", "-sS", "-L", "--fail", "--max-time", str(timeout), URL],
                       capture_output=True, timeout=timeout + 20)
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError(f"SRS 抓取失败 rc={p.returncode} stderr={p.stderr.decode('utf-8', 'replace')[:160]}")
    return p.stdout


def parse(raw: bytes) -> dict:
    text = raw.decode("utf-8", "replace")
    regions = []
    for ln in text.splitlines():
        m = ROW_RE.match(ln)
        if m:
            noaa, loc, lo, area, hale, ll, nn, mag = m.groups()
            regions.append({"noaa_ar": noaa, "location": loc, "carrington_lo": int(lo),
                            "area_mhd": int(area), "hale": hale, "mcintosh": mag.strip(),
                            "ll": ll, "nn": nn})
    mi = ISSUED_RE.search(text)
    issued = None
    if mi:
        _hhmm, dd, mon, yyyy = mi.groups()
        issued = dt.date(int(yyyy), MONTHS[mon], int(dd))
    mv = VALID_RE.search(text)
    valid = None
    if mv and issued:
        dom, hhmm = mv.groups()
        # "Valid at 05/2400Z" 表示前一个日历日的 24:00 → 归属日期要退一天
        day = int(dom)
        valid = issued if day <= issued.day and hhmm != "2400" else issued - dt.timedelta(days=1)
        if hhmm == "2400":
            valid = issued - dt.timedelta(days=1)
    return {"issued": issued, "valid_as_of": valid, "n_regions": len(regions),
            "regions": regions, "lines": len(text.splitlines())}


def archive_once(dest_dir, force=False, timeout=60):
    """存当日一份 SRS 原文，并往 index.csv 追加一行元数据。返回记录 dict。"""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    raw = fetch_raw(timeout)
    info = parse(raw)
    today = dt.datetime.now(dt.timezone.utc).date()
    as_of = info["valid_as_of"] or info["issued"] or today
    path = dest / f"{as_of.isoformat()}.txt"
    if path.exists() and path.stat().st_size > 0 and not force:
        status = "skipped"
    else:
        path.write_bytes(raw)
        status = "written"
    idx = dest / "index.csv"
    new = not idx.exists()
    line = (f"{as_of.isoformat()},{today.isoformat()},{info['issued'].isoformat() if info['issued'] else ''},"
            f"{len(raw)},{_sha(raw)},{info['n_regions']},{status}\n")
    if new:
        idx.write_text("valid_as_of,archived_utc,issued_utc,bytes,sha256_16,n_regions,status\n",
                       encoding="utf-8")
    existing = idx.read_text(encoding="utf-8").splitlines()
    if not any(ln.startswith(f"{as_of.isoformat()},") for ln in existing[1:]):
        with open(idx, "a", encoding="utf-8", newline="") as fh:
            fh.write(line)
    return {"date": as_of.isoformat(), "path": str(path), "bytes": len(raw),
            "sha256_16": _sha(raw), "n_regions": info["n_regions"], "status": status,
            "issued": info["issued"].isoformat() if info["issued"] else ""}


def main(argv=None):
    ap = argparse.ArgumentParser(description="存档 NOAA SWPC 当日 SRS 原文")
    ap.add_argument("--dir", default="data/solar_ar/reports")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的当日文件")
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args(argv)
    r = archive_once(args.dir, force=args.force, timeout=args.timeout)
    print(f"SRS {r['date']}（签发 {r['issued']}）-> {r['path']}  {r['bytes']:,} 字节 "
          f"{r['sha256_16']} 活动区 {r['n_regions']} 个  [{r['status']}]")
    if r["n_regions"] == 0:
        print("警告：解析到 0 个活动区。可能是当日无黑子区，也可能是表头格式变了——"
              "请人工看一眼原文再决定是否用这一天", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
