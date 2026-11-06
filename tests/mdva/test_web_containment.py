"""REQ-W27：入站路径边界与跨源策略——访问控制面的覆盖。

为什么单独一个文件：`test_offline.py` 那 7 条管的全是**出站**（不发外部请求），
入站"谁能读、能读到哪儿"这一面此前**零覆盖**（PM seq=731 §四 W27-5 / 清单 §4）。

命名守新组规（⑤第三变体在测试层的实物）：名字里写 refuses / never / before 的，
体内必须真的对**那个参数**做了断言——`test_web.py::test_live_serves_frontend_and_refuses_traversal`
就是因为名字承诺 refuses、体内只测 URL 路径面而被登记成假覆盖，本文件不许重犯。

落点（PM seq=754 定层）：**请求边界（Web 层按 ws + 启动参数判）**，不下沉 `Store`/`to_abs`/
通用导出层——所以本文件既断"接口越界必拒"，也断"命令行/进程内调用照旧可用"，
后者是那四条已验收能力（两个建包脚本、`mdva export/batch/augment --out`、
`test_store.py::test_d3_uris_outside_base_stay_absolute`）不变的证据。
"""

import json
import os
import threading
import time
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from PIL import Image

from mdva.annotate.store import OutsideWorkspace, Store
from mdva.web.deps import Workspaces
from mdva.web.routes import call
from mdva.web.server import make_server

EVIL_ORIGIN = "http://evil.invalid"
LEAK_COLOR = (7, 130, 45)         # 越界对照图的特征色：它出现在响应里就等于数据出域


def _png(path: Path, color=LEAK_COLOR) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (24, 18), color).save(path, "PNG")
    return path


@pytest.fixture(scope="module")
def arena(tmp_path_factory):
    """`ws/` = 允许根之内；`outside/` = 同级真实目录 = 允许根之外。

    越界靶必须是**写得出来、能被引擎解码的真文件**（②-c：拿不存在的 ghost 证明的是
    not-found，不是 containment——测试员 seq=737 §一 5-b 点名的就是这一族）。
    """
    root = tmp_path_factory.mktemp("w27")
    ws, outside = root / "ws", root / "outside"
    inside_a = _png(ws / "imgs" / "a.png", (200, 20, 20))
    _png(ws / "imgs" / "b.png", (20, 20, 200))
    secret_png = _png(outside / "DO-NOT-LEAK.png")
    secret_csv = outside / "DO-NOT-LEAK.csv"
    secret_csv.write_text("t,v\n0,1\n1,2\n", encoding="utf-8")
    secret_npy = outside / "DO-NOT-LEAK.npy"
    secret_npy.write_bytes(b"\x93NUMPY" + b"\x00" * 96)          # 存在即可，边界不该走到解码
    manifest = outside / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    return SimpleNamespace(root=root, ws=ws, outside=outside, secret_png=secret_png,
                           secret_csv=secret_csv, secret_npy=secret_npy, manifest=manifest,
                           inside=inside_a, inside_dir=ws / "imgs")


@pytest.fixture(scope="module")
def live(arena, tmp_path_factory):
    """`--data = arena.ws` 起一次真服务器：参数面走 HTTP，与浏览器看到的完全同一条路。"""
    static = tmp_path_factory.mktemp("w27static")
    (static / "index.html").write_text("<!doctype html><title>mdva</title>", encoding="utf-8")
    ws = Workspaces(arena.ws)
    res = call(ws, "POST", "/api/datasets", None,
               {"name": "边界内", "type": "detection", "root": str(arena.inside_dir)})
    assert res.status == 200, res.json_body
    httpd = make_server(ws, "127.0.0.1", 0, static_dir=static)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(ws=ws, port=httpd.server_address[1], did=res.json_body["dataset_id"],
                          vid=res.json_body["version_id"])
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)
    ws.close()


def http(method, port, path, body=None, headers=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=30)
    heads = dict(headers or {})
    payload = None
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        heads["Content-Type"] = "application/json"
    conn.request(method, path, payload, heads)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, dict(resp.getheaders()), data


def error_of(raw: bytes) -> str:
    try:
        return str(json.loads(raw.decode("utf-8")).get("error", ""))
    except Exception:
        return raw.decode("utf-8", "replace")[:200]


# ---------------------------------------------------------------- 键 × 收口点（逐键独立断言）
# 数据分析 seq=751 那张表的实物版：`uri`/`uris`/`target`/`dir` 经 `to_abs`，
# `root`/`out_dir`/`manifest`/`calibration` 不经——所以每一键各算一条判据、各给一个 id，
# 不靠"某一个单点"一句话覆盖全部。

@pytest.mark.parametrize("key,method,path,query,body", [
    ("uri", "GET", "/api/frame", {"uri": "OUTSIDE_PNG"}, None),
    ("uris", "GET", "/api/thumbs", {"uris": ["OUTSIDE_PNG"], "cell": "32,32"}, None),
    ("root", "GET", "/api/scan", {"root": "OUTSIDE_DIR"}, None),
    ("target", "GET", "/api/collect/latest", {"target": "OUTSIDE_DIR"}, None),
    ("dir", "POST", "/api/export/validate", None, {"dir": "OUTSIDE_DIR"}),
    ("out_dir", "POST", "/api/export", None, {"format": "coco", "out_dir": "OUTSIDE_DIR"}),
    ("manifest", "POST", "/api/collect/undo", None, {"manifest": "OUTSIDE_MANIFEST"}),
    ("calibration", "POST", "/api/clean/preview", None,
     {"uris": ["INSIDE"], "specs": [{"name": "normalize"}], "calibration": "OUTSIDE_NPY"}),
])
def test_boundary_refuses_each_path_key_independently(live, arena, key, method, path,
                                                      query, body):
    """W27-1（键 × 收口点）：八个带磁盘路径的键，每一个都必须被请求边界单独拒掉。

    断言三件：状态 400、原文可诊断（含「越界」且不吐允许根清单）、被拒目标的内容
    一个字节都不在响应里。走 `routes.call` 是因为它和真服务器用的是同一个入口，
    载荷形态（真数组 / 逗号串 / 绝对 / 相对）不会被 URL 编码二次改写——
    数据分析 seq=751 §四 那条"探针形态错会同时伪造拒绝与放行"的教训照做。
    """
    subs = {"OUTSIDE_PNG": str(arena.secret_png), "OUTSIDE_DIR": str(arena.outside),
            "OUTSIDE_MANIFEST": str(arena.manifest), "OUTSIDE_NPY": str(arena.secret_npy),
            "INSIDE": str(arena.inside)}

    def fill(value):
        if isinstance(value, str):
            for mark, real in subs.items():
                value = value.replace(mark, real)
            return value
        if isinstance(value, list):
            return [fill(v) for v in value]
        if isinstance(value, dict):
            return {k: fill(v) for k, v in value.items()}
        return value

    qs, bd = fill(query or {}), fill(body or {})
    if "version" not in qs and "version" not in bd and method == "POST":
        bd["version"] = live.vid
    res = call(live.ws, method, path, qs or None, bd or None)
    text = str(res.json_body.get("error", "")) if isinstance(res.json_body, dict) else ""
    assert res.status == 400, (key, res.status, text)
    assert "越界" in text, (key, text)                                   # 可诊断，不是答非所问
    assert "未知工作区" not in text, key
    assert str(arena.ws) not in text, f"{key} 的回显把允许根清单吐出去了"    # W27-7
    assert not (res.data or b"").startswith(b"\x89PNG")                  # 内容没出去


def test_relative_escape_forms_and_encoding_variants_all_refused(live, arena):
    """uri 面 × 五种载荷变体 + 双重编码：真实存在于工作区外的图，一条都不许 200。

    载荷集与测试员第三轮取证 / 数据分析 §一 同形态（直穿、`..%2F`、`%2e%2e%2F`、
    反斜杠、绝对路径），外加 `%252e`——数据分析已落定 404/213B（只解一次码），
    这里把它当**回归项**钉住：修后仍不得是 200。
    """
    rel = os.path.relpath(arena.secret_png, arena.ws).replace("\\", "/")
    once = rel.replace(".", "%2e").replace("/", "%2F")
    probes = [rel, rel.replace("/", "%2F"), once, rel.replace("/", "\\"),
              str(arena.secret_png), once.replace("%", "%25")]        # 最后一条 = 双重编码
    for probe in probes:
        status, _, raw = http("GET", live.port,
                              "/api/frame?uri=" + quote(probe, safe="") + "&width=64&height=64")
        assert status in (400, 404), (probe, status)
        assert b"\x89PNG" not in raw
        assert status == 400 or "越界" not in error_of(raw)          # 双重编码走的是 not-found


def test_non_decodable_suffix_outside_is_refused_by_root_not_by_sniff(live, arena):
    """边界是"允许根"，不是"后缀能否被 sniff 判成模态"（数据分析 §二-C 的 400 老结论作废）。

    修前 `.csv` 也能 200/7,908B 出域，因为当时判的是模态；现在 CSV 与 PNG 一样在边界上就被拒。
    """
    rel = os.path.relpath(arena.secret_csv, arena.ws).replace("\\", "/")
    status, _, raw = http("GET", live.port, "/api/frame?uri=" + quote(rel, safe=""))
    assert status == 400 and "越界" in error_of(raw)
    # 报错原文会点出调用方自己送进来的文件名（那是 W27-7 允许的一半），
    # 所以要验的是**文件内容**没出去，不是文件名没出现。
    assert b"0,1" not in raw and b"t,v" not in raw


def test_refusal_happens_before_any_directory_walk(live, arena):
    """W27-6：拒绝必须发生在遍历之前——`root=C:\\Windows` 实测曾 90 秒不返回，最终 4xx 不算过。

    这条同时断 W27-7 的另一半：修前那种 `403 [Errno 13] Permission denied` + 一长串
    系统路径的答非所问，现在既不该出现耗时，也不该出现 OS 原文。
    """
    system_root = Path(os.environ.get("SystemRoot") or r"C:\Windows")
    for target, label in ((arena.outside, "outside"), (system_root, "systemroot")):
        started = time.perf_counter()
        status, _, raw = http("GET", live.port, "/api/scan?root=" + quote(str(target)))
        spent = time.perf_counter() - started
        text = error_of(raw)
        assert status == 400, (label, status, text)
        assert spent < 3.0, f"{label} 拒了 {spent:.1f}s：遍历没被停住"
        assert "越界" in text and "Permission denied" not in text
        assert str(target) in text and str(arena.ws) not in text


def test_relative_root_resolves_against_workspace_not_process_cwd(live, arena):
    """W27-3 追加项：`.` 的基准是工作区 base，不是服务进程的 cwd（两处 `or "."` 的收口）。"""
    status, _, raw = http("GET", live.port, "/api/scan?root=.")
    body = json.loads(raw.decode("utf-8"))
    assert status == 200, error_of(raw)
    assert Path(body["root"]).resolve() == arena.ws.resolve()
    assert Path.cwd() != arena.ws                                   # 前提：两者确实不同
    assert set(body["modalities"]) == {"image"}                     # 工作区里只有图，不外泄别的


@pytest.mark.parametrize("target", ["outside-dir", "process-cwd", "systemroot"])
def test_scan_refuses_every_outside_directory_shape(live, arena, target):
    """W27-3：工作区之外的目录形态一律拒；工作区之内照常 200（正对照同批，组规②）。"""
    value = {"outside-dir": str(arena.outside), "process-cwd": str(Path.cwd()),
             "systemroot": os.environ.get("SystemRoot") or r"C:\Windows"}[target]
    if Path(value).resolve() == arena.ws.resolve():
        pytest.skip(f"{target} 在这台机器上就是工作区，不构成越界载荷")
    status, _, raw = http("GET", live.port, "/api/scan?root=" + quote(value))
    assert status == 400 and "越界" in error_of(raw)
    inside = http("GET", live.port, "/api/scan?root=" + quote(str(arena.inside_dir)))
    assert inside[0] == 200 and json.loads(inside[2].decode())["n_items"] == 2


def test_post_datasets_refuses_undeclared_root(live, arena):
    """W27-8 反例（数据分析 §二-E 的靶）：登记外部 root 当场拒，不能先登记再合法读取。"""
    status, _, raw = http("POST", live.port, "/api/datasets",
                          {"name": "外部登记", "type": "detection", "root": str(arena.outside)})
    assert status == 400 and "越界" in error_of(raw)
    listed = json.loads(http("GET", live.port, "/api/datasets")[2].decode("utf-8"))
    assert "外部登记" not in json.dumps(listed, ensure_ascii=False)  # 库里没多出这一条


def test_write_side_export_refuses_outside_out_dir_and_keeps_default(live, arena):
    """写侧：`/api/export` 的 `out_dir` 越界 ⇒ 拒且不落盘；缺省落点（`--out` 之内）照常成功。"""
    away = arena.outside / "export-coco"
    status, _, raw = http("POST", live.port, "/api/export",
                          {"version": live.vid, "format": "coco", "out_dir": str(away)})
    assert status == 400 and "越界" in error_of(raw)
    assert not away.exists()
    call(live.ws, "POST", "/api/annotations", None,
         {"version": live.vid, "uri": str(arena.inside), "role": "annotator",
          "anns": [{"geom": "box", "coords": [1, 2, 10, 12], "label": "a"}]})
    ok, _, body = http("POST", live.port, "/api/export", {"version": live.vid, "format": "coco"})
    assert ok == 200, error_of(body)                                 # 正对照：默认落点没被误杀


# ---------------------------------------------------------------------- 正对照与子工作区

def test_inside_entries_still_render_as_real_png(live, arena):
    """W27-2 的 pytest 版：工作区内条目（相对与绝对两种写法）逐条仍 200 且真出 PNG。"""
    rows = json.loads(http("GET", live.port,
                           f"/api/items?dataset={live.did}&version={live.vid}")[2].decode())["items"]
    assert len(rows) == 2
    for row in rows:
        for probe in (row["uri"], str(arena.ws / row["uri"])):
            status, _, raw = http("GET", live.port,
                                  "/api/frame?uri=" + quote(probe) + "&width=32&height=32")
            assert status == 200 and raw.startswith(b"\x89PNG"), (probe, status)


def test_subworkspace_entries_remain_readable(tmp_path):
    """PM seq=749 §三 点名的"最容易先红"那条：已登记子工作区（MAX_DEPTH=2）的数据必须照旧可读。

    `discover()` 只在 `--data` 根之下扫描 ⇒ 子工作区必然在允许根之内。但构造性论证不等于
    执行验证，所以这条真建一个 `data/lab/mdva.sqlite`，走 `items` 与 `frame` 两面取 200。
    """
    data_root = tmp_path / "data"
    sub = data_root / "lab"
    _png(sub / "cases" / "one.png", (11, 220, 33))
    Store(sub / "mdva.sqlite").close()                               # 建库即完成登记
    ws = Workspaces(data_root)
    created = call(ws, "POST", "/api/datasets", {"ws": "lab"},
                   {"name": "子工作区", "type": "detection", "root": str(sub / "cases")})
    assert created.status == 200, created.json_body
    items = call(ws, "GET", "/api/items", {"ws": "lab",
                                           "dataset": created.json_body["dataset_id"],
                                           "version": created.json_body["version_id"]})
    assert items.status == 200, items.json_body
    assert [r["uri"] for r in items.json_body["items"]] == ["cases/one.png"]
    frame = call(ws, "GET", "/api/frame", {"ws": "lab", "uri": "cases/one.png",
                                           "width": 32, "height": 32})
    assert frame.status == 200 and frame.data and frame.data.startswith(b"\x89PNG")
    ws.close()


# ---------------------------------------------------------------- 落点两半：边界内 vs 命令行

def test_in_process_callers_keep_absolute_uris(arena):
    """落点结论的另一半证据：不在请求边界上的调用，D3 的绝对 uri 与任意 `--out` 照旧可用。

    命令行是上一轮刚确立的唯一合法特权入口（`--out`/`--out-root`/`--data` 全是用户自声明），
    把它判成漏洞等于推翻 D3 的答案；被证伪的那句承诺讲的也只是接口。
    """
    from mdva.web.services import Hub

    store = Store(arena.ws / "in-process.sqlite")
    try:
        assert store.to_abs(str(arena.secret_png)) == str(arena.secret_png)   # 进程内不拒
    finally:
        store.close()
    scratch = arena.root / "scratch" / "coco"
    hub = Hub(arena.ws / "in-process.sqlite")
    try:
        created = hub.create_dataset("边界外落点", "detection", arena.inside_dir)
        hub.put_annotations(created["version_id"], str(arena.inside),
                            [{"geom": "box", "coords": [1, 2, 10, 12], "label": "a"}],
                            role="annotator")
        report = hub.export(created["version_id"], "coco", out_dir=str(scratch))
        assert report and scratch.is_dir()                # build_ssafd_trial.py 的那个形态
    finally:
        hub.close()


def test_boundary_ruler_accepts_declared_roots_only(tmp_path):
    """`require_within` 这把尺本身：相对的按 base 算、启动声明过的根才算允许根（W27-8 正例）。"""
    outer = tmp_path / "挂载盘"
    _png(outer / "a.png", (9, 9, 9))
    declared = Store(tmp_path / "svc" / "mdva.sqlite", extra_roots=(outer,))
    undeclared = Store(tmp_path / "svc" / "mdva.sqlite")
    try:
        assert declared.require_within(str(outer / "a.png")).is_file()      # 声明过 ⇒ 可进
        with pytest.raises(OutsideWorkspace):
            undeclared.require_within(str(outer / "a.png"))                # 没声明 ⇒ 拒
        assert undeclared.require_within(".") == undeclared.base            # cwd 进不来
        with pytest.raises(OutsideWorkspace):
            undeclared.require_within("..")
    finally:
        declared.close()
        undeclared.close()


# ---------------------------------------------------------------------- 跨源策略（W27-4 半边）

@pytest.mark.parametrize("path", ["/api/health", "/api/items", "/api/datasets", "/api/scan?root=."])
def test_live_api_never_echoes_request_origin(live, arena, path):
    """W27-4：带任意 Origin 重放，响应头不得出现 ACAO，也不得等于那个 Origin。

    浏览器侧"真读不到"是行为断言，pytest 只断服务端不再发通行证；行为那半边由
    CDP 双 origin 取证覆盖（成对证据落共享区）——口径⑤：字段存在≠行为发生。
    """
    status, heads, _ = http("GET", live.port, path, headers={"Origin": EVIL_ORIGIN})
    assert status == 200, (path, status)
    assert "Access-Control-Allow-Origin" not in heads
    assert EVIL_ORIGIN not in json.dumps(heads)
    pre = http("OPTIONS", live.port, "/api/annotations", headers={"Origin": EVIL_ORIGIN})
    assert pre[0] == 204 and "Access-Control-Allow-Origin" not in pre[1]
