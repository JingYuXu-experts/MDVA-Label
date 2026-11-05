"""增强策略：按学科/任务给出能直接用的默认配方，并拦住会破坏语义的写法。

这一层的价值不在"多几个开关"，而在傻瓜化：选完数据集就能一键增强，
不用用户去猜"星图能不能上下翻""遥测离散通道能不能加噪"。
`check()` 的每条警告都对应一次真会把数据集做坏的写法，宁可拦下来也别静默出成品。
"""

from __future__ import annotations

from typing import Optional, Sequence

from .lineage import Recipe

#: 学科预设：配方 + 为什么这么配（界面直接显示这句话，用户才知道默认值不是随便来的）
PRESETS: dict = {
    "aerospace": {
        "title": "航空航天（星图 / 太阳图 / 遥测）",
        "recipe": {"seed": 0, "factor": 2, "ops": [
            {"name": "flip", "params": {"p_horizontal": 0.5, "p_vertical": 0.0}},
            {"name": "rotate90", "params": {"k_choices": [0, 1, 2, 3], "p": 0.5}},
            {"name": "photometric", "params": {"brightness": 0.12, "contrast": 0.12,
                                               "noise": 0.008}},
            {"name": "series_jitter", "params": {"sigma": 0.005, "gain": 0.03}},
        ]},
        "reason": "星图与太阳图有真实的方向含义（晨昏线、自转方向），上下镜像会把南北颠倒，"
                  "所以只开左右镜像；遥测离散通道由 series_jitter 内部保护，不会加噪。",
    },
    "medical": {
        "title": "医学影像（CT / 病理）",
        "recipe": {"seed": 0, "factor": 3, "ops": [
            {"name": "flip", "params": {"p_horizontal": 0.5, "p_vertical": 0.5}},
            {"name": "rotate90", "params": {"k_choices": [0, 1, 2, 3], "p": 0.6}},
            {"name": "crop", "params": {"fraction": 0.9, "fraction_jitter": 0.06,
                                        "offset": 0.4}},
            {"name": "photometric", "params": {"brightness": 0.1, "contrast": 0.15,
                                               "noise": 0.006}},
        ]},
        "reason": "轴位/冠状切面没有绝对上下，镜像与 90° 旋转都是标准做法；"
                  "轻度裁剪模拟视野偏移，灰度扰动限制在不至于改判读的幅度内。",
    },
    "cv_segmentation": {
        "title": "计算机视觉（分割 / 检测）",
        "recipe": {"seed": 0, "factor": 4, "ops": [
            {"name": "flip", "params": {"p_horizontal": 0.5, "p_vertical": 0.2}},
            {"name": "crop", "params": {"fraction": 0.8, "fraction_jitter": 0.1,
                                        "offset": 0.6}},
            {"name": "photometric", "params": {"brightness": 0.2, "contrast": 0.2,
                                               "noise": 0.012}},
        ]},
        "reason": "自然图像看视角和光照，随机裁剪 + 光度扰动是 COCO 级别的常规配置；"
                  "掩膜走和图像完全相同的整数索引链，不需要也不会做插值。",
    },
    "series": {
        "title": "纯时序（遥测 / 工况）",
        "recipe": {"seed": 0, "factor": 2, "ops": [
            {"name": "series_jitter", "params": {"sigma": 0.01, "gain": 0.05}},
            {"name": "series_offset", "params": {"max_shift": 20.0}},
        ]},
        "reason": "时序条目没有像素，空间算子会被自动跳过；区间标注按绝对秒数存，"
                  "series_offset 把曲线和时间轴一起挪，标注跟着一起挪。",
    },
}

#: 任务类型 -> 预设名。数据集没标学科时按任务给默认，保证开箱即用。
TASK_PRESETS = {
    "classification": "cv_segmentation",
    "detection": "cv_segmentation",
    "segmentation": "cv_segmentation",
    "series_interval": "series",
    "event_level": "series",
    "mixed": "cv_segmentation",
}

SPATIAL = ("image", "cube", "video")
CATEGORY_KEYS = ("aerospace", "medical", "cv_segmentation")


def presets() -> list:
    return [{"name": name, "title": body["title"], "reason": body["reason"],
             "recipe": dict(body["recipe"])} for name, body in PRESETS.items()]


def get(name: str) -> dict:
    if name not in PRESETS:
        raise KeyError(f"未知增强预设 {name}，可用 {sorted(PRESETS)}")
    body = PRESETS[name]
    return {"name": name, "title": body["title"], "reason": body["reason"],
            "recipe": dict(body["recipe"])}


def suggest(dataset: Optional[dict] = None, items: Sequence = ()) -> dict:
    """按数据集挑一个预设：先认学科（meta.category），再认纯时序，最后认任务类型。

    优先学科是因为"能不能上下翻"由成像几何决定，而不是由标注任务决定。
    """
    meta = (dataset or {}).get("meta") or {}
    for key in CATEGORY_KEYS:
        if str(meta.get("category", "")) == key:
            return get(key)
    if items and not any(getattr(i, "modality", "") in SPATIAL for i in items):
        return get("series")
    return get(TASK_PRESETS.get((dataset or {}).get("type", ""), "cv_segmentation"))


def check(recipe, items: Sequence = (), category: str = "") -> list:
    """返回警告列表（空 = 可以放心跑）。每条都对应一次会让数据集坏掉的真实陷阱。"""
    from .ops import build

    recipe = Recipe.from_obj(recipe)
    operators = build(recipe.ops)
    warnings: list = []
    modalities = {getattr(i, "modality", "") for i in items} - {""}

    if modalities and not any(o.handles(m) for o in operators for m in modalities):
        warnings.append("配方里的算子处理不了这批条目的任何模态，跑完只会得到和原件一样的副本")
    for modality in sorted(modalities):
        if not any(o.handles(modality) for o in operators):
            warnings.append(f"{modality} 条目没有对应算子，会被跳过（不产副本）")

    vertical = next((float(o.param("p_vertical") or 0.0)
                     for o in operators if o.name == "flip"), 0.0)
    if vertical > 0 and category == "aerospace":
        warnings.append("星图/太阳图启用了上下镜像（p_vertical="
                        f"{vertical:g}），日冕与晨昏线方向会反")

    crops = [o for o in operators if o.name == "crop"]
    if crops:
        frac = min(float(o.param("fraction") or 1.0) for o in crops)
        if frac < 0.6:
            warnings.append(f"裁剪只保留 {frac:.0%} 边长，靠边的标注框会被切掉，"
                            "几何映射后可能退化成零面积框")
    if any(o.name == "series_jitter" for o in operators):
        warnings.append("series_jitter 只改连续通道，离散通道的码值原样保留；"
                        "增强后 CSV 的侧车里模拟通道已置 scale=1/offset=0，不要再叠加原标定")
    if int(recipe.factor) > 6:
        warnings.append(f"每张原图产 {recipe.factor} 个副本，同一场景会在训练集里占大头；"
                        "验证集若含同源图像，指标会虚高")
    return warnings
