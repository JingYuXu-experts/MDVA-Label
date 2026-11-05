"""标注几何与审核状态机的单元测试。"""

import pytest

from mdva.annotate.model import Ann, GEOMS, merge
from mdva.annotate import state as ST
from mdva.render import overlay as OV


def test_box_geometry_basics():
    a = Ann("box", [10, 20, 60, 80], "flare")
    assert a.bbox() == (10, 20, 60, 80)
    assert a.area() == 50 * 60
    assert a.duration() == 0.0


def test_box_must_be_ordered_and_finite():
    with pytest.raises(ValueError):
        Ann("box", [60, 80, 10, 20])
    with pytest.raises(ValueError):
        Ann("box", [0, 0, float("inf"), 10])


def test_unknown_geom_and_source_rejected():
    with pytest.raises(ValueError):
        Ann("ellipse", [1, 2, 3, 4])
    with pytest.raises(ValueError):
        Ann("box", [0, 0, 1, 1], source="ai")


def test_interval_and_level_carry_time_not_pixels():
    iv = Ann("interval", [100.0, 250.0], "drift")
    assert iv.duration() == 150.0
    assert iv.bbox() is None
    assert iv.overlaps_time(200, 300) and not iv.overlaps_time(0, 50)
    with pytest.raises(ValueError):
        Ann("interval", [250.0, 100.0])


def test_level_geom_requires_level_field():
    lv = Ann("level", [10.0, 20.0], label="告警", level=3)
    obj = lv.to_overlay()
    assert isinstance(obj, OV.VInterval) and obj.level == 3


def test_iou_and_time_iou():
    a = Ann("box", [0, 0, 10, 10])
    b = Ann("box", [5, 0, 15, 10])
    assert abs(a.iou(b) - 50 / 150) < 1e-9
    t1, t2 = Ann("interval", [0, 10]), Ann("interval", [5, 15])
    assert abs(t1.time_iou(t2) - 5 / 15) < 1e-9


def test_poly_and_point_convert_to_overlay():
    poly = Ann("poly", [0, 0, 10, 0, 10, 10])
    assert isinstance(poly.to_overlay(), OV.Polyline)
    assert isinstance(Ann("point", [3, 4]).to_overlay(), OV.Point)
    assert isinstance(Ann("box", [0, 0, 1, 1]).to_overlay(), OV.Box)


def test_overlay_roundtrip_preserves_semantics():
    src = Ann("box", [1.0, 2.0, 30.0, 40.0], "star", source="preannotate",
              backend="star_detect", confidence=0.87)
    back = Ann.from_overlay(src.to_overlay(), source="preannotate", backend="star_detect")
    assert back.geom == "box" and back.coords == src.coords
    assert abs(back.confidence - 0.87) < 1e-9


def test_json_roundtrip_covers_every_field():
    a = Ann("box", [0, 0, 5, 5], "b", "visual_prompt", 0.5, "visual", slice=7, level=2,
            color=(1, 2, 3), note="备注")
    b = Ann.from_json(a.to_json())
    assert b == a


def test_scaled_and_flipped_keep_geometry_valid():
    a = Ann("box", [10, 20, 30, 40])
    assert a.scaled(0.5, 0.25).coords == [5, 5, 15, 10]
    flipped = a.flipped(horizontal=True, width=100)
    assert flipped.coords == [70, 20, 90, 40]
    # 时间区间没有"翻转像素"的语义，硬翻要报错而不是悄悄改坐标
    with pytest.raises(ValueError):
        Ann("interval", [0, 1]).flipped()


def test_merge_drops_near_duplicate_boxes():
    dup = [Ann("box", [0, 0, 10, 10], "x"), Ann("box", [0.2, 0.2, 10.2, 10.2], "x"),
           Ann("box", [50, 50, 60, 60], "x")]
    assert len(merge(dup)) == 2
    assert len(merge(dup, iou_thr=0.99)) == 3


def test_every_geom_kind_is_constructible():
    samples = {"box": [0, 0, 1, 1], "poly": [0, 0, 1, 0, 1, 1], "point": [0, 0],
               "interval": [0, 1], "level": [0, 1]}
    assert set(samples) == set(GEOMS)
    for geom, coords in samples.items():
        assert Ann(geom, coords).geom == geom


# ------------------------------------------------------------------ 状态机

def test_transitions_table_is_self_consistent():
    for (role, action), (allowed, target) in ST.TRANSITIONS.items():
        assert role in ST.ROLES and action in ST.ACTION_LABELS
        assert target in ST.STATES
        assert set(allowed) <= set(ST.STATES)


def test_annotator_cannot_approve_and_reviewer_cannot_submit():
    assert ST.can("annotator", "submit", "draft")
    assert not ST.can("annotator", "approve", "submitted")
    assert ST.can("reviewer", "approve", "submitted")
    assert not ST.can("reviewer", "submit", "draft")


def test_next_state_rejects_wrong_current_state():
    with pytest.raises(ST.PermissionDenied):
        ST.next_state("reviewer", "approve", "draft")


def test_next_state_rejects_unknown_role_and_action():
    with pytest.raises(ST.PermissionDenied):
        ST.next_state("visitor", "approve", "submitted")
    with pytest.raises(ST.PermissionDenied):
        ST.next_state("admin", "delete", "draft")


def test_rejected_can_be_revised_then_resubmitted():
    s = ST.next_state("annotator", "revise", "rejected")
    assert s == "draft"
    assert ST.next_state("annotator", "submit", s) == "submitted"


def test_admin_can_reopen_and_force():
    assert ST.next_state("admin", "reopen", "approved") == "draft"
    assert ST.next_state("admin", "approve", "draft") == "approved"


def test_queue_states_match_roles():
    assert ST.queue_states("annotator") == ("draft", "rejected")
    assert ST.queue_states("reviewer") == ("submitted",)


def test_describe_lists_only_that_roles_actions():
    text = ST.describe("reviewer")
    assert "通过" in text and "驳回" in text and "提交" not in text
