"""标注状态机与角色鉴权。

EasyData 的多人协作是"管理员/标注员/审核员"三角色。本地单机不做账号系统，
但把**权限矩阵**照搬下来：角色由界面切换、每次状态迁移都留痕。
这样"越权操作"是可测的行为，而不是靠约定。
"""

from __future__ import annotations

from typing import Optional

ROLES = ("admin", "annotator", "reviewer")

STATES = ("draft", "submitted", "approved", "rejected")

#: (角色, 动作) -> (允许的当前状态集合, 目标状态)
TRANSITIONS = {
    ("annotator", "submit"): (("draft", "rejected"), "submitted"),
    ("annotator", "revise"): (("draft", "rejected"), "draft"),
    ("reviewer", "approve"): (("submitted",), "approved"),
    ("reviewer", "reject"): (("submitted", "approved"), "rejected"),
    ("admin", "approve"): (STATES, "approved"),
    ("admin", "reject"): (STATES, "rejected"),
    ("admin", "reopen"): (("approved", "rejected"), "draft"),
}

ROLE_LABELS = {"admin": "管理员", "annotator": "标注员", "reviewer": "审核员"}
ACTION_LABELS = {"submit": "提交", "revise": "修订", "approve": "通过",
                 "reject": "驳回", "reopen": "重新打开"}


class PermissionDenied(RuntimeError):
    """角色无权执行该动作，或当前状态不允许该迁移。"""


def can(role: str, action: str, state: str) -> bool:
    rule = TRANSITIONS.get((role, action))
    return bool(rule and state in rule[0])


def next_state(role: str, action: str, state: str) -> str:
    if role not in ROLES:
        raise PermissionDenied(f"未知角色 {role!r}，可用 {ROLES}")
    if action not in ACTION_LABELS:
        raise PermissionDenied(f"未知动作 {action!r}")
    rule = TRANSITIONS.get((role, action))
    if rule is None:
        raise PermissionDenied(f"{ROLE_LABELS[role]} 不能执行「{ACTION_LABELS[action]}」")
    allowed, target = rule
    if state not in allowed:
        raise PermissionDenied(
            f"状态「{state}」不允许{ROLE_LABELS[role]}「{ACTION_LABELS[action]}」，"
            f"可操作的状态为 {list(allowed)}")
    return target


def queue_states(role: str) -> tuple:
    """该角色待办队列里应出现的状态。"""
    if role == "annotator":
        return ("draft", "rejected")
    if role == "reviewer":
        return ("submitted",)
    return STATES


def describe(role: str) -> str:
    """给界面用的一句话权限说明。"""
    acts = [ACTION_LABELS[a] for (r, a) in TRANSITIONS if r == role]
    return f"{ROLE_LABELS.get(role, role)}可执行：{'、'.join(sorted(set(acts)))}"


def assert_role(role: str, action: str, state: str) -> Optional[str]:
    return next_state(role, action, state)


__all__ = ["ROLES", "STATES", "TRANSITIONS", "ROLE_LABELS", "ACTION_LABELS",
           "PermissionDenied", "can", "next_state", "queue_states", "describe", "assert_role"]
