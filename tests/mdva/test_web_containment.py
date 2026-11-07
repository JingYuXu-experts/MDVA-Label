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


@pytest.mark.parametrize("declared", [True, False])
def test_out_dir_follows_startup_declaration_only(tmp_path, declared):
    """`out_dir` 判据的两对照（PM seq=764 §三、seq=782 派单）：扩大允许集只属于启动参数。

    同一个靶——工作区之外、且**事先不存在**的目录——只在 `--out` 声明与否上分两形：
    声明 ⇒ 200 且真落盘（含目录尚未创建）；未声明 ⇒ 400 且一个目录都不许建。
    未声明那形还顺带钉住一条脆机关：`Hub` 的隐式默认落点（库旁 `out/`）**不得无条件进
    roots**——`services.py:177` 用的是入参、`:178` 才补默认，谁把这两行调换顺序这条就红。
    """
    data_root = tmp_path / "data" / "ws"
    target = tmp_path / "挂载盘out"                    # 在工作区之外，故意不预先创建
    _png(data_root / "imgs" / "a.png", (200, 20, 20))
    Store(data_root / "mdva.sqlite").close()
    ws = Workspaces(data_root, out_dir=str(target) if declared else None)
    try:
        hub = ws.hub(None)
        created = call(ws, "POST", "/api/datasets", None,
                       {"name": "两形", "type": "detection", "root": str(data_root / "imgs")})
        assert created.status == 200, created.json_body
        res = call(ws, "POST", "/api/export", None, {"version": created.json_body["version_id"],
                                                     "format": "coco",
                                                     "out_dir": str(target / "sub")})
        text = str(res.json_body.get("error", "")) if isinstance(res.json_body, dict) else ""
        if declared:
            assert res.status == 200, (res.status, text)
            assert (target / "sub").is_dir(), "200 但目录没建出来"
            assert list((target / "sub").glob("*.json")), "200 但没有真落盘产物"
        else:
            assert res.status == 400 and "越界" in text, (res.status, text)
            assert not target.exists(), "拒了却顺手把目录建出来了"
            assert Path(hub.out_dir).resolve() not in hub.store.roots      # 隐式默认没进允许根
            assert hub.out_dir == data_root / "out"                        # 缺省落点照旧
    finally:
        ws.close()


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


def test_subworkspace_context_refuses_outside_payloads(tmp_path):
    """W27-9 覆盖面：`ws=<子工作区>` 与越界载荷的**组合**（此前所有拒侧断言都在顶层 ws 语境）。

    数据分析 seq=779 §二 实测确认的唯一真缺口：正对照（子工作区可读）在 `:269` 已有，
    缺的是"子工作区语境下越界仍被拒"。允许根 = `lab` 自己的 base ∪ 启动声明的 data_root，
    所以 data_root 之外的载荷照样必须 400，而且要拒在遍历之前、原文可诊断、不回显服务端路径。
    """
    data_root = tmp_path / "data"
    sub = data_root / "lab"
    _png(sub / "cases" / "one.png", (11, 220, 33))
    Store(sub / "mdva.sqlite").close()                               # 建库即完成登记
    leak = _png(tmp_path / "elsewhere" / "LEAK.png")                 # 真文件：拒的必须是边界
    ws = Workspaces(data_root)
    try:
        created = call(ws, "POST", "/api/datasets", {"ws": "lab"},
                       {"name": "子工作区边界", "type": "detection", "root": str(sub / "cases")})
        assert created.status == 200, created.json_body
        ok = call(ws, "GET", "/api/frame", {"ws": "lab", "uri": "cases/one.png",
                                            "width": 32, "height": 32})
        assert ok.status == 200 and ok.data and ok.data.startswith(b"\x89PNG")   # 正对照

        rel = os.path.relpath(leak, sub).replace("\\", "/")           # ../.. /elsewhere/LEAK.png
        assert rel.startswith("..")
        for label, value in (("绝对路径", str(leak)), ("相对上跳", rel)):
            res = call(ws, "GET", "/api/frame", {"ws": "lab", "uri": value,
                                                 "width": 32, "height": 32})
            text = str(res.json_body.get("error", "")) if isinstance(res.json_body, dict) else ""
            assert res.status == 400, (label, res.status, text)
            assert "越界" in text and "未知工作区" not in text, (label, text)   # 答其所问
            assert str(data_root) not in text, f"{label} 回显了服务端路径"        # W27-7
            assert not (res.data or b"").startswith(b"\x89PNG")                   # 内容没出去

        scan = call(ws, "GET", "/api/scan", {"ws": "lab", "root": str(leak.parent)})
        stext = str(scan.json_body.get("error", "")) if isinstance(scan.json_body, dict) else ""
        assert scan.status == 400 and "越界" in stext, (scan.status, stext)
    finally:
        ws.close()


# ------------------------------------------------- 消费点覆盖面：不经 PATH_KEYS 的路径值（W27-9）

@pytest.mark.parametrize("door", ["body", "query", "params_nested"])
def test_inbound_path_value_outside_is_refused_whichever_door_it_enters(live, arena, door,
                                                                        monkeypatch):
    """`source_uri` 不在八键里，却经 `rest()` 与 `params` 两个门进 `to_abs`（判据附件 v1.0 §2）。

    覆盖面按**消费点**判、不按键名判（PM seq=794 §一 / seq=799 §一 例外句）：三个门各一条，
    只补顶层仍会漏嵌套门。断言两件事：400 可诊断原文 + **引擎一次都没 open 那枚区外文件**
    （状态码证明不了读没读到，所以钉一把 `builtins.open` 记录器）。
    """
    from mdva.web import routes

    inside, leak = str(arena.inside), str(arena.secret_png)
    box = [2, 2, 18, 12]
    if door == "body":
        query, body = None, {"uri": inside, "kind": "box", "coords": box, "source_uri": leak}
    elif door == "query":
        query = {"uri": inside, "kind": "box", "coords": "2,2,18,12", "source_uri": leak}
        body = None
    else:                                          # 门 3：params 整包嵌套位
        query, body = None, {"uri": inside, "kind": "box", "coords": box,
                             "params": {"source_uri": leak}}

    opened = []
    real_open = open

    def spy(*args, **kwargs):
        try:
            opened.append(os.path.normcase(str(args[0])))
        except Exception:
            pass
        return real_open(*args, **kwargs)

    monkeypatch.setattr("builtins.open", spy)
    res = call(live.ws, "POST", "/api/smart/same_image", query, body)
    text = str(res.json_body.get("error", "")) if isinstance(res.json_body, dict) else ""
    assert res.status == 400, (door, res.status, text)
    assert "越界" in text and "未知工作区" not in text, (door, text)
    assert sum(1 for p in opened if os.path.normcase(leak) in p) == 0, \
        f"{door} 门拒了但文件已经被读过一次"
    assert routes.PATH_KEYS and "source_uri" not in routes.PATH_KEYS, \
        "PATH_KEYS 里出现 source_uri ⇒ 本条的覆盖面口径要跟着改（键名清单已不是八个）"


@pytest.mark.parametrize("door", ["specs_prompts", "specs_reference", "inside_positive"])
def test_clean_side_doors_use_the_same_ruler(live, arena, door):
    """清洗侧三道门（判据附件 §4 的 d/e/f）：`specs[]` 里除 `name/op/params` 外的**任意键**都会被
    `clean/task.py:51-53` 并进算子参数 ⇒ **字段名永远追不完**，门 5 的 `reference` 就是证明。

    d/e 期望 400（拒在 handler 之前），f 是区内正对照：同形状指区内文件必须 200，否则就是把
    已验收的"模板钉死"用法判死了——与 `out_dir`、`source_uri` 那两条同形的另一半。
    """
    inside, leak = str(arena.inside), str(arena.secret_png)
    target = inside if door == "inside_positive" else leak
    prompt = {"kind": "box", "coords": [2, 2, 18, 12]}
    if door in ("specs_prompts", "inside_positive"):
        prompt["source_uri"] = target
    spec = {"name": "content_filter", "rule": "keep_if_matched", "prompts": [dict(prompt)]}
    if door == "specs_reference":
        spec["reference"] = target
    res = call(live.ws, "POST", "/api/clean/preview", None, {"uris": [inside], "specs": [spec]})
    text = str(res.json_body.get("error", "")) if isinstance(res.json_body, dict) else ""
    if door == "inside_positive":
        assert res.status == 200, (res.status, text)
        assert "越界" not in text
    else:
        assert res.status == 400 and "越界" in text, (door, res.status, text)
        assert "未知工作区" not in text, (door, text)


def test_unregistered_knob_carrying_an_absolute_path_is_refused(live, arena):
    """形态层背板（挡"下一枚没人登记过的名字"的那一层）：字段名谁都没登记过，值长得像区外路径就照样拒。

    这条不是在测 `reference`——它测的是**下一次**有人在 `specs[]`/`params` 里加一枚新名字而没
    进任何清单时的行为。判据附件 §2 的结论是"按键名穷举不收敛"，所以实现不能只靠名单。
    """
    res = call(live.ws, "POST", "/api/clean/preview", None,
               {"uris": [str(arena.inside)],
                "specs": [{"name": "content_filter", "rule": "keep_if_matched",
                           "prompts": [{"kind": "box", "coords": [2, 2, 18, 12]}],
                           "brand_new_field_nobody_registered": str(arena.secret_png)}]})
    text = str(res.json_body.get("error", "")) if isinstance(res.json_body, dict) else ""
    assert res.status == 400 and "越界" in text, (res.status, text)


def test_free_text_carrying_a_path_like_string_is_not_refused(tmp_path):
    """背板的边界（W27-2 同族的"修过头"防线）：人写的文字里出现路径字样不算越界。

    评审在驳回理由里打一句 `见 C:\\docs\\spec.png`、或数据集类别名里带 `/`，都是合法输入；
    形态层对 `anns`/`labels`/`reason` 这类自由文本载体不生效，只有登记名那一层管它们。
    """
    data_root = tmp_path / "data"
    _png(data_root / "imgs" / "a.png", (200, 20, 20))
    Store(data_root / "mdva.sqlite").close()
    ws = Workspaces(data_root)
    try:
        ok = call(ws, "POST", "/api/datasets", None,
                  {"name": "自由文本", "type": "detection", "root": str(data_root / "imgs"),
                   "labels": ["/home/data/类别", "C:\\docs\\spec.png", "见 ../note"]})
        assert ok.status == 200, (ok.status, ok.json_body)
    finally:
        ws.close()


def test_inbound_path_value_inside_still_works_as_template(live, arena):
    """两对照的另一半：`test_web.py:300` 的已验收用法（区内 `source_uri == uri`）不许误杀。"""
    inside = str(arena.inside)
    plain = call(live.ws, "POST", "/api/smart/same_image", None,
                 {"uri": inside, "kind": "box", "coords": [2, 2, 18, 12],
                  "source_uri": inside})
    assert plain.status == 200, (plain.status, plain.json_body)
    nested = call(live.ws, "POST", "/api/smart/same_image", None,
                  {"uri": inside, "kind": "box", "coords": [2, 2, 18, 12],
                   "params": {"source_uri": inside}})
    assert nested.status == 200, (nested.status, nested.json_body)


def test_path_value_registry_covers_web_consumers():
    """消费点尺的可执行版：web 层里"从入站参数字典取值 → 送进路径函数"的键名必须在清单里。

    机理是 `routes._smart_params` 的 `params.update(p.rest())`——除 `ROUTING_KEYS` 外全收，
    所以键名白名单结构上罩不住"其余全收"的入站面。这条门的作用就是：**下一次**再有人加一枚
    `params.get("某_uri")` 直接进 `to_abs` 而没登记，当场红（PM seq=799：写死键名第三次会
    再失效——第二次是 seq=754「经 to_abs 的算一格」，第三次是这次，根因同一个）。
    末了那条"对照 0"是尺子自身的否决权：扫描必须真的抓得到 `source_uri`，抓不到就说明
    这把尺已经瞎了，不许把"没红"写成"通过"。
    """
    import ast
    import re
    from mdva.web import routes

    root = Path(routes.__file__).resolve().parents[3]        # .../src/mdva/web/routes.py -> 仓根
    consumers = re.compile(r"to_abs\(|load_any\(|resolve_prompt\(|require_within\(")
    getter = re.compile(r"""params(?:\.get\(|\[)\s*["']([A-Za-z_][A-Za-z0-9_]*)["']""")
    reviewed_non_path = {"kind", "coords", "label", "role", "backend", "backend_params",
                         "workers", "executor", "state", "modality", "limit", "threshold",
                         "rule", "prompts", "name", "op", "nsigma",
                         "analyzer_params", "cell_height", "cell_width", "cols", "rows"}
    # 这份"已核非路径"名单**不是豁免面**：`_check_path_values` 的形态层（Tier B）照样扫这些键
    # 的值，往里塞绝对路径或 `..` 依然 400——所以登记缺失只影响"精确名"这一层，不影响背板。
    found, missing = set(), set()
    scan_files = ("src/mdva/web/services.py", "src/mdva/web/routes.py",
                  "src/mdva/clean/task.py", "src/mdva/clean/ops.py",
                  "src/mdva/annotate/prompts.py")
    for rel in scan_files:
        text = (root / rel).read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.get_source_segment(text, node) or ""
            if consumers.search(body):
                names = set(getter.findall(body))
                found |= names
                missing |= names - set(routes.PATH_VALUE_KEYS) - reviewed_non_path
    assert not missing, (f"这些入站键会到达文件系统却没登记进 PATH_VALUE_KEYS："
                         f"{sorted(missing)}（扫描面 {scan_files}）")
    # 对照 0（尺子自身的否决权）：这两枚都必须真被抓到，抓不到说明尺瞎了，不是"通过"。
    # `reference` 是判据附件 §2 门 5——它既不在 PATH_KEYS，也不在任何人列过的清单里。
    assert {"source_uri", "reference"} <= found, f"消费点尺没抓到这两枚键，扫描本身失效：{sorted(found)}"


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
