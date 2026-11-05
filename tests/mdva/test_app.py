"""桌面查看器的冒烟测试。

会真的开一个窗口，所以默认跳过；要跑就 `MDVA_GUI_TESTS=1 pytest tests/mdva/test_app.py`。
无显示的环境（CI、纯命令行服务器）上即使设了变量也会跳过。
"""

import os

import pytest

tk = pytest.importorskip("tkinter")

requires_display = pytest.mark.skipif(
    not os.environ.get("MDVA_GUI_TESTS"),
    reason="会弹窗，需要显式设 MDVA_GUI_TESTS=1 才跑",
)


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    pytest.importorskip("astropy")
    from mdva import datasets

    root = tmp_path_factory.mktemp("appdata")
    datasets.build_aerospace(root, seed=5, n_telemetry=1200, n_frames=4)
    return root / "aerospace"


@pytest.fixture(scope="module")
def three_domains(tmp_path_factory):
    """三个学科各建一份，分组浏览才有东西可分。"""
    pytest.importorskip("astropy")
    from mdva import datasets

    root = tmp_path_factory.mktemp("domains")
    datasets.build_all(root, seed=3, n_telemetry=1200, n_frames=4, n_scenes=2)
    return root


def _new_viewer(autoload=False):
    from mdva.app import Viewer

    try:
        root = tk.Tk()
    except tk.TclError as exc:            # 没有可用显示
        pytest.skip(f"无法创建窗口：{exc}")
    return root, Viewer(root, autoload=autoload)


def _pump(root, n=6):
    for _ in range(n):
        root.update()


@requires_display
def test_viewer_lists_every_modality_found_by_scan(dataset):
    from mdva import io
    from mdva.app import MODALITY_CN

    root, viewer = _new_viewer()
    try:
        viewer.load(str(dataset))
        _pump(root)
        assert len(viewer.items) == len(io.scan(str(dataset)))
        shown = {viewer.tree.set(str(i), "modality") for i in range(len(viewer.items))}
        assert shown >= {MODALITY_CN["series"], MODALITY_CN["event"], MODALITY_CN["video"]}
        assert "扫描失败" not in viewer.status.cget("text")
    finally:
        root.destroy()


@requires_display
def test_viewer_renders_selected_tiles(dataset):
    root, viewer = _new_viewer()
    try:
        viewer.load(str(dataset))
        _pump(root)
        viewer.check_all(True)
        _pump(root)
        # 每个选中项都要有一格画面，且状态栏报告的是渲染成功而不是失败
        assert len(viewer.selected()) == len(viewer.items)
        assert len(viewer.photos) == len(viewer.selected())
        assert "渲染失败" not in viewer.status.cget("text")
    finally:
        root.destroy()


@requires_display
def test_only_a_limited_subset_is_auto_checked(dataset):
    """82 项一进来全画会卡死，默认只勾前若干项。"""
    from mdva.app import MAX_AUTO_CHECK

    root, viewer = _new_viewer()
    try:
        viewer.load(str(dataset))
        _pump(root)
        assert len(viewer.selected()) == min(MAX_AUTO_CHECK, len(viewer.items))
    finally:
        root.destroy()


@requires_display
def test_items_are_grouped_by_domain_then_folder(three_domains):
    """分组的第一层必须是学科，第二层是学科内的目录。"""
    from mdva import datasets

    root, viewer = _new_viewer()
    try:
        viewer.load(str(three_domains))
        _pump(root)
        tops = list(viewer.tree.get_children())
        titles = {viewer.group_label[i] for i in tops}
        assert titles == {meta["short"] for meta in datasets.CATEGORIES.values()}

        leaves = viewer._leaves()
        assert len(leaves) == len(viewer.items)
        # 叶子必须真的挂在对应学科下面
        aerospace = next(i for i in tops if viewer.group_label[i].startswith("航空航天"))
        under = viewer._leaves(aerospace)
        assert under and all(viewer.items[int(i)].uri.find("aerospace") >= 0 for i in under)
    finally:
        root.destroy()


@requires_display
def test_clicking_a_group_selects_the_whole_subtree(three_domains):
    root, viewer = _new_viewer()
    try:
        viewer.load(str(three_domains))
        _pump(root)
        viewer.check_all(False)
        top = viewer.tree.get_children()[0]
        viewer.tree.focus(top)
        viewer.check_group(True)
        _pump(root)
        assert len(viewer.selected()) == len(viewer._leaves(top))
        assert viewer.tree.item(top, "text").startswith("☑")

        viewer._set_leaves(viewer._leaves(top)[:1], False)
        viewer._sync_groups()
        assert viewer.tree.item(top, "text").startswith("◪")
    finally:
        root.destroy()


@requires_display
def test_time_span_follows_the_current_selection(three_domains):
    """三个学科各有各的时间纪元，只勾医学时时间轴必须收到医学那一段。"""
    from mdva import io

    root, viewer = _new_viewer()
    try:
        viewer.load(str(three_domains))
        _pump(root)
        viewer.check_all(False)
        medical = next(i for i in viewer.tree.get_children()
                       if viewer.group_label[i].startswith("医学"))
        viewer.tree.focus(medical)
        viewer.check_group(True)
        _pump(root)
        expect = io.common_span(viewer.selected())
        assert abs(viewer.t_lo - expect[0]) < 1e-6
        assert abs(viewer.t_hi - expect[1]) < 1e-6
    finally:
        root.destroy()


@requires_display
def test_index_mode_moves_through_slices_and_channels(dataset):
    root, viewer = _new_viewer()
    try:
        viewer.load(str(dataset))
        _pump(root)
        viewer.mode.set("切片序号")
        viewer._on_mode()
        _pump(root)
        sl = viewer.window()
        assert sl.t0 == float("-inf") and sl.t1 == float("inf")   # 不再收时间窗
        assert sl.index == 0
        viewer.slider.set(2)
        viewer._on_slider("2")
        _pump(root)
        assert viewer.window().index == 2
        assert viewer._max_views() > 2
    finally:
        root.destroy()


@requires_display
def test_rotate_and_flip_change_the_pixels(dataset):
    import numpy as np

    root, viewer = _new_viewer()
    try:
        viewer.load(str(dataset))
        _pump(root)
        base = viewer.photos[0]
        viewer.rotate.set("90 度")
        _pump(root)
        turned = viewer.photos[0]
        assert np.asarray(turned).shape != np.asarray(base).shape or \
            not np.array_equal(np.asarray(turned), np.asarray(base))
        viewer.rotate.set("不旋转")
        viewer.flip.set("水平")
        _pump(root)
        assert not np.array_equal(np.asarray(viewer.photos[0]), np.asarray(base))
    finally:
        root.destroy()


@requires_display
def test_double_click_opens_a_zoom_window_with_controls(dataset):
    root, viewer = _new_viewer()
    try:
        viewer.load(str(dataset))
        _pump(root)
        cube = next(i for i, it in enumerate(viewer.items) if it.n_views > 1)
        viewer.zoom(cube)
        _pump(root)
        assert len(viewer.zooms) == 1
        win = viewer.zooms[0]
        win._render(float(win.slider["to"]))
        _pump(root)
        assert win.item.n_views > 1
        win.top.destroy()
    finally:
        root.destroy()


@requires_display
def test_cursor_drag_only_repaints_tiles_that_move(dataset, monkeypatch):
    """单帧图像跟时间游标无关，拖游标时重画它纯属浪费——这是拖动卡顿的主因。"""
    from mdva import app as appmod

    root, viewer = _new_viewer()
    try:
        viewer.load(str(dataset))
        _pump(root)
        calls = []
        real = appmod.render.render_item

        def spy(item, *a, **kw):
            calls.append(item.id)
            return real(item, *a, **kw)

        monkeypatch.setattr(appmod.render, "render_item", spy)
        viewer.paint(all_tiles=True)
        calls.clear()
        viewer.slider.set(float(viewer.slider["to"]) * 0.7)
        viewer.paint(all_tiles=False)
        moving = {i.id for i in viewer.selected() if viewer._moves_with_cursor(i)}
        static = {i.id for i in viewer.selected()} - moving
        assert moving and static, "这组数据没有同时含「会动」和「不动」的格子，测不出选择性重画"
        assert set(calls) <= moving
        assert not (set(calls) & static)
    finally:
        root.destroy()


@requires_display
def test_repaint_requests_are_coalesced(dataset):
    """一次拖动会派发几十个事件，必须合并成一次重画。"""
    root, viewer = _new_viewer()
    try:
        viewer.load(str(dataset))
        _pump(root)
        painted = []
        viewer.paint = lambda all_tiles=False: painted.append(all_tiles)
        for step in range(30):
            viewer.request("cursor")
        assert painted == [], "事件还没泵就重画了，等于没合并"
        root.update_idletasks()
        assert len(painted) == 1
        # 中途来了"重建"级请求，就不能被降级成一次普通重画
        viewer.request("cursor")
        viewer.request("build")
        root.update_idletasks()
        assert painted == [False, True]
    finally:
        root.destroy()


@requires_display
def test_stale_request_after_rebuild_does_not_crash(dataset):
    """rebuild 拆掉控件后，排队中的重画必须静默作废，而不是把异常抛进 Tk 回调。"""
    root, viewer = _new_viewer()
    try:
        viewer.load(str(dataset))
        _pump(root)
        viewer.request("cursor")
        viewer.rebuild()
        root.update_idletasks()
        root.update()
        assert viewer.cells
    finally:
        root.destroy()


@requires_display
def test_knobs_that_cannot_affect_selection_are_greyed_out(dataset):
    """用户抱怨"应用了没有任何区别"：影响不到当前勾选的参数就该是灰的，并且要说清楚。"""
    root, viewer = _new_viewer()
    try:
        viewer.load(str(dataset))
        _pump(root)
        viewer.check_all(False)
        only_series = [i for i in viewer._leaves()
                       if viewer.items[int(i)].modality == "series"]
        viewer._set_leaves(only_series[:1], True)
        viewer.rebuild()
        _pump(root)
        assert str(viewer.box_interval.cget("state")) == "disabled"
        assert str(viewer.box_colormap.cget("state")) == "disabled"
        assert "不受影响" in viewer.status.cget("text") or "点不动" in viewer.status.cget("text")

        viewer.check_all(False)
        viewer._set_leaves([i for i in viewer._leaves()
                            if viewer.items[int(i)].modality == "image"][:1], True)
        viewer.rebuild()
        _pump(root)
        assert str(viewer.box_interval.cget("state")) == "readonly"
    finally:
        root.destroy()


@requires_display
def test_demo_lands_on_the_linkage_window(three_domains):
    """一键演示要停在"四种模态同时有内容"的那一段，而不是随便一个中间值。"""
    import json

    root, viewer = _new_viewer()
    try:
        viewer.load(str(three_domains))
        _pump(root)
        truth = json.loads((three_domains / "aerospace" / "truth.json")
                           .read_text(encoding="utf-8"))
        win = truth["linkage_window"]
        viewer.demo()
        _pump(root)
        t = viewer.cursor()
        assert win[0] - 1e-6 <= t <= win[1] + 1e-6, f"游标 {t} 没落在联动窗 {win} 内"
        assert viewer.selected(), "演示没有勾上任何数据"
    finally:
        root.destroy()


@requires_display
def test_advanced_panel_starts_collapsed(dataset):
    """默认只给四个动作，其余收进"显示高级参数"，一屏十几个控件没人会用。"""
    root, viewer = _new_viewer()
    try:
        viewer.load(str(dataset))
        _pump(root)
        assert not viewer.adv.winfo_manager()
        viewer.adv_toggle.invoke()
        root.update_idletasks()
        assert viewer.adv.winfo_manager() == "pack"
    finally:
        root.destroy()


@requires_display
def test_unchecking_items_clears_the_grid(dataset):
    root, viewer = _new_viewer()
    try:
        viewer.load(str(dataset))
        _pump(root)
        viewer.check_all(False)
        _pump(root)
        assert viewer.photos == []
        assert viewer.selected() == []
    finally:
        root.destroy()


@requires_display
def test_missing_directory_reports_instead_of_raising(tmp_path):
    root, viewer = _new_viewer()
    try:
        viewer.load(str(tmp_path / "does-not-exist"))
        assert "失败" in viewer.status.cget("text") or "没有可识别" in viewer.status.cget("text")
    finally:
        root.destroy()


@requires_display
def test_startup_autoloads_the_bundled_demo_data(monkeypatch, tmp_path):
    """仓库里已经有 data/ 示例时，开窗口就该直接看到图，不用先点"打开目录"。"""
    from mdva import app as appmod

    demo = tmp_path / "data" / "aerospace" / "telemetry"
    demo.mkdir(parents=True)
    (demo / "t.csv").write_text("utc,v\n1000,1.0\n1001,2.0\n", encoding="utf-8")
    monkeypatch.setattr(appmod, "_REPO", tmp_path)

    root, viewer = _new_viewer(autoload=True)
    try:
        viewer._autoload()
        _pump(root)
        assert len(viewer.items) == 1
        assert viewer._leaves()
        assert viewer.group_label[viewer.tree.get_children()[0]] == "航空航天"
    finally:
        root.destroy()
