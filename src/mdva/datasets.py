"""分类示例数据集：航空航天、医学影像、计算机视觉分割。

平台叫"多模态科研数据"平台，就必须拿得出多个学科的真实数据形态，
而不是只有一种图像。三类数据各自压不同的能力：

- aerospace       工程遥测(时序) + 告警(事件) + 载荷(FITS 单通道高动态) + 太阳序列(帧序列)
                  压的是位深拉伸、跨模态时间联动、离散量不能插值
- medical         CT 断层(npy 帧栈 uint16 HU) + 生理信号(多通道含棘波) + 病理(RGB 彩色)
                  压的是彩色帧不能走伪彩、窗宽窗位、长时序降采样保尖峰
- cv_segmentation 同一批场景的多个模型预测输出 + 真值掩膜 + 指标表
                  压的是跨目录并行对比、批量算子、标注导出

全部为程序合成，无外部数据许可问题；每类都带真值，因此算法可以量化评测。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional

import numpy as np

from . import synth
from .io.timeutil import iso_from_utc

__all__ = ["CATEGORIES", "build_all", "build_aerospace", "build_cv_segmentation", "build_medical"]

CATEGORIES = {
    "aerospace": {
        "title": "航空航天 / 空间科学数据",
        "short": "航空航天",
        "modalities": ["series", "event", "image", "cube", "video"],
        "stress": "高动态位深、跨模态时间联动、离散量语义",
    },
    "medical": {
        "title": "医学影像与生理信号",
        "short": "医学",
        "modalities": ["video", "series", "event", "image"],
        "stress": "窗宽窗位、彩色帧、长时序尖峰保留",
    },
    "cv_segmentation": {
        "title": "计算机视觉图像分割",
        "short": "图像分割",
        "modalities": ["image"],
        "stress": "多模型输出跨目录并行对比、批量算子、标注导出",
    },
}


# --------------------------------------------------------------------------- #
# 航空航天
# --------------------------------------------------------------------------- #

def solar_disk(shape: tuple = (384, 384), n_flares: int = 4, seed: int = 0) -> tuple:
    """太阳全日面盘面：临边昏暗 + 黑子 + 耀斑区。"""
    rng = np.random.default_rng(seed)
    h, w = shape
    yy, xx = np.mgrid[0:h, 0:w]
    cy, cx = h / 2, w / 2
    r = np.hypot(yy - cy, xx - cx) / (min(h, w) / 2 * 0.86)

    disk = np.clip(1.0 - r, 0.0, 1.0)
    # 临边昏暗：中心亮边缘暗，幂律近似
    intensity = 900.0 * disk ** 0.42
    intensity = intensity + rng.normal(0, 3.0, shape) * (disk > 0)

    spots = []
    for _ in range(5):
        ang, rad = rng.uniform(0, 2 * math.pi), rng.uniform(0.05, 0.6)
        sy, sx = cy + rad * (min(h, w) / 2 * 0.86) * math.sin(ang), cx + rad * (min(h, w) / 2 * 0.86) * math.cos(ang)
        size = rng.uniform(4, 11)
        dark = np.exp(-((yy - sy) ** 2 + (xx - sx) ** 2) / (2 * size ** 2))
        intensity -= 520 * dark
        spots.append((float(sx), float(sy), float(size)))

    flares = []
    for _ in range(n_flares):
        ang, rad = rng.uniform(0, 2 * math.pi), rng.uniform(0.2, 0.8)
        fy, fx = cy + rad * (min(h, w) / 2 * 0.86) * math.sin(ang), cx + rad * (min(h, w) / 2 * 0.86) * math.cos(ang)
        size = rng.uniform(6, 16)
        intensity += rng.uniform(2500, 9000) * np.exp(-((yy - fy) ** 2 + (xx - fx) ** 2) / (2 * size ** 2))
        flares.append((float(fx), float(fy)))

    return np.clip(intensity, 0, None).astype(np.uint16), {"spots": spots, "flares": flares}


def orbit_ground_track(n: int = 4320, start: float = 1735689600.0, period_s: float = 5800.0,
                       dt: float = 1.0) -> tuple:
    """星下点轨迹与若干轨道派生量，作为"轨道/姿态"类结构化时序。"""
    t = start + np.arange(n, dtype=np.float64) * dt
    phase = 2 * math.pi * (t - start) / period_s
    incl = math.radians(97.4)  # 太阳同步轨道
    lat = np.degrees(np.arcsin(np.sin(incl) * np.sin(phase)))
    lon = np.degrees(np.arctan2(np.cos(incl) * np.sin(phase), np.cos(phase))) - 360 * (t - start) / 86400.0
    lon = (lon + 180) % 360 - 180
    alt = 720.0 + 12.0 * np.sin(2 * phase) + np.random.default_rng(0).normal(0, 0.35, n)
    beta = 22.0 * np.sin(2 * math.pi * (t - start) / 86400.0 / 4.0)
    values = np.stack([lat, lon, alt, beta], axis=1)
    return t, values, ["sub_lat", "sub_lon", "altitude_km", "beta_angle_deg"], ["deg", "deg", "km", "deg"]


def build_aerospace(root, seed: int = 0, n_telemetry: int = 20000, n_frames: int = 16) -> dict:
    root = Path(root) / "aerospace"
    for sub in ("telemetry", "payload", "sequence"):
        (root / sub).mkdir(parents=True, exist_ok=True)

    written = {}

    # 工程遥测 + 标定表 + 事件
    data, calib, truth = synth.telemetry(n=n_telemetry, seed=seed)
    start = float(data[0][0])
    written["telemetry"] = str(synth.write_telemetry_csv(root / "telemetry" / "engineering.csv", data, calib))
    fracs = (0.32, 0.55, 0.72, 0.85)
    events = [
        (fracs[0] * n_telemetry, "加热器控制指令下发", 1, 0),
        (fracs[1] * n_telemetry, "动量轮 A 组切换", 2, 0),
        (fracs[2] * n_telemetry, "数传链路重配置", 2, 0),
        (fracs[3] * n_telemetry, "载荷进入安全模式", 3, 600),
    ]
    written["events"] = str(synth.write_events(root / "telemetry" / "operations.json", start, events))

    # 轨道星下点：采样周期放大到覆盖整段遥测，否则时间窗一拖出轨道段就一路空白
    track_dt = max(1.0, n_telemetry / 720.0)
    t, values, names, units = orbit_ground_track(n=720, start=start, dt=track_dt)
    import csv as _csv

    with (root / "telemetry" / "ground_track.csv").open("w", encoding="utf-8", newline="") as fh:
        w = _csv.writer(fh)
        w.writerow(["utc"] + names)
        for i in range(t.size):
            w.writerow([f"{t[i]:.3f}"] + [f"{values[i, j]:.5f}" for j in range(len(names))])
    written["ground_track"] = str(root / "telemetry" / "ground_track.csv")

    # 载荷观测时刻刻意与指令事件对齐：一个时间窗就能同时框住遥测、事件、图像和帧序列，
    # 这才是"跨模态时间联动"的演示素材，而不是各自漂在各的时间上
    exposure = {"xray": 32.0, "solar": 8.0, "cube": 384.0}
    obs = {
        "xray": start + fracs[0] * n_telemetry,
        "solar": start + fracs[1] * n_telemetry,
        "cube": start + fracs[2] * n_telemetry,
    }

    img, star_truth = synth.star_field(shape=(512, 512), n_sources=60, n_cosmic=40, seed=seed + 1)
    written["payload_xray"] = str(synth.write_fits(root / "payload" / "xray_field.fits", img, {
        "TELESCOP": "SIM-XRT", "INSTRUME": "HXI-SIM", "OBJECT": "SYNTH-FIELD",
        "DATE-OBS": iso_from_utc(obs["xray"]), "EXPTIME": exposure["xray"], "BUNIT": "counts",
    }))
    sun, sun_truth = solar_disk(seed=seed + 2)
    written["payload_solar"] = str(synth.write_fits(root / "payload" / "solar_disk.fits", sun, {
        "TELESCOP": "SIM-SOLAR", "INSTRUME": "FXI-SIM", "OBJECT": "SUN-DISK",
        "DATE-OBS": iso_from_utc(obs["solar"]), "EXPTIME": exposure["solar"], "BUNIT": "DN/s",
    }))
    written["payload_cube"] = str(synth.write_fits(root / "payload" / "solar_cube.fits",
                                                   synth.cube((12, 256, 256), seed=seed + 3), {
        "TELESCOP": "SIM-SOLAR", "INSTRUME": "LST-SIM", "OBJECT": "SYNTH-CUBE",
        "DATE-OBS": iso_from_utc(obs["cube"]), "EXPTIME": exposure["cube"],
        "NAXIS3": 12, "BUNIT": "counts",
    }))

    # 帧序列目录：太阳演化序列，压视频模态
    from PIL import Image

    from .io import video as _video

    seq_dir = root / "sequence" / "solar_evolution"
    seq_dir.mkdir(parents=True, exist_ok=True)
    interval = 2.5
    for i in range(n_frames):
        frame, _ = solar_disk(shape=(256, 256), n_flares=3, seed=seed + 100 + i)
        arr = _video.to_display_uint8(np.asarray(frame))
        Image.fromarray(np.dstack([arr] * 3)).save(seq_dir / f"frame_{i:04d}.png")
    (seq_dir / "meta.json").write_text(json.dumps({
        "fps": round(1.0 / interval, 6),
        "t0": obs["xray"],
        "frame_interval_s": interval,
        "note": "耀斑区演化序列，与 X 射线载荷同一次观测",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    written["sequence_dir"] = str(seq_dir)

    truth_out = {"injected_anomalies": synth.anomaly_truth(truth),
                 "xray_sources": len(star_truth["centroids"]), "xray_cosmics": len(star_truth["cosmic"]),
                 "solar_flares": len(sun_truth["flares"]), "n_frames": n_frames,
                 "t0": start, "n_telemetry": n_telemetry,
                 # 逐目标真值：后端评测要算查准/查全，只有计数是不够的
                 "payload_truth": {
                     "xray_field": {"centroids": [list(c) for c in star_truth["centroids"]],
                                    "cosmic": [list(c) for c in star_truth["cosmic"]],
                                    "bg": star_truth["bg"], "sigma": star_truth["sigma"]},
                     "solar_disk": {"flares": [list(f) for f in sun_truth["flares"]],
                                    "spots": [list(s) for s in sun_truth["spots"]]}},
                 "linkage_window": [obs["xray"] - 10.0, obs["xray"] + exposure["xray"]]}
    (root / "truth.json").write_text(json.dumps(truth_out, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"category": "aerospace", "root": str(root), "written": written, "truth": truth_out}


# --------------------------------------------------------------------------- #
# 医学
# --------------------------------------------------------------------------- #

def ct_head_stack(n_slices: int = 48, size: int = 160, seed: int = 0) -> tuple:
    """头部断层幻影，uint16 存储 HU 偏移。返回 (stack, 元信息)。"""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    cy = cx = size / 2
    stack = np.full((n_slices, size, size), -1024.0, dtype=np.float64)

    tumor_y, tumor_x = cy + rng.uniform(-14, 14), cx + rng.uniform(-14, 14)
    tumor_r = rng.uniform(7, 12)
    for k in range(n_slices):
        z = (k - n_slices / 2) / (n_slices / 2)
        rx = (size * 0.40) * math.sqrt(max(1e-3, 1 - z * z * 0.82))
        ry = (size * 0.46) * math.sqrt(max(1e-3, 1 - z * z * 0.82))
        skull = ((yy - cy) / ry) ** 2 + ((xx - cx) / rx) ** 2
        slice_img = np.full((size, size), -1024.0)
        brain = skull <= 1.0
        bone = (skull > 0.90) & brain
        slice_img[brain & ~bone] = 24.0 + rng.normal(0, 1.6, (brain & ~bone).sum())
        slice_img[bone] = 900.0 + rng.normal(0, 40, bone.sum())
        # 脑室
        vent = (((yy - cy) / (ry * 0.16)) ** 2 + ((xx - cx * 0.86) / (rx * 0.09)) ** 2) <= 1.0
        vent |= (((yy - cy) / (ry * 0.16)) ** 2 + ((xx - cx * 1.14) / (rx * 0.09)) ** 2) <= 1.0
        slice_img[vent & brain] = 12.0
        # 病灶只在部分层面出现，且随 z 衰减
        if abs(z) < 0.45:
            blob = ((yy - tumor_y) ** 2 + (xx - tumor_x) ** 2) <= (tumor_r * (1 - abs(z) / 0.45)) ** 2
            slice_img[blob] = 55.0 + rng.normal(0, 3.0, blob.sum())
        stack[k] = slice_img

    # HU 平移到 uint16：HU + 1024
    encoded = np.clip(stack + 1024.0, 0, 65535).astype(np.uint16)
    return encoded, {"hu_intercept": -1024, "tumor": (float(tumor_x), float(tumor_y), float(tumor_r)),
                     "n_slices": n_slices}


def physiological(n: int = 12000, fs: float = 250.0, seed: int = 0) -> tuple:
    """ECG 三导联 + 呼吸 + 一过性低氧事件。返回 (t, values, channels, units, events)。"""
    rng = np.random.default_rng(seed)
    t = np.arange(n, dtype=np.float64) / fs
    hr_bpm = 72.0 + 6.0 * np.sin(2 * math.pi * t / 90.0)
    beat_phase = np.cumsum(2 * math.pi * hr_bpm / 60.0 / fs)

    def ecg(shape_scale, noise):
        p = 0.12 * np.exp(-((np.mod(beat_phase, 2 * math.pi) - 1.1) ** 2) / (2 * 0.10 ** 2))
        qrs = shape_scale * (np.exp(-((np.mod(beat_phase, 2 * math.pi) - 2.0) ** 2) / (2 * 0.030 ** 2))
                             - 1.6 * np.exp(-((np.mod(beat_phase, 2 * math.pi) - 2.09) ** 2) / (2 * 0.022 ** 2)))
        tw = 0.28 * np.exp(-((np.mod(beat_phase, 2 * math.pi) - 2.9) ** 2) / (2 * 0.22 ** 2))
        return p + qrs + tw + rng.normal(0, noise, n)

    spo2 = 97.0 + 0.9 * np.sin(2 * math.pi * t / 1.1) + rng.normal(0, 0.25, n)
    # 一过性低氧：中段掉到 88 再恢复
    dip0, dip1 = int(0.44 * n), int(0.50 * n)
    spo2[dip0:dip1] -= 8.5 * np.sin(np.linspace(0, math.pi, dip1 - dip0))
    resp = 0.6 * np.sin(2 * math.pi * t / 4.2) + rng.normal(0, 0.05, n)

    values = np.stack([ecg(1.0, 0.030), ecg(0.72, 0.035), ecg(-0.55, 0.040), spo2, resp], axis=1)
    channels = ["ECG_II", "ECG_V5", "ECG_aVF", "SpO2", "Resp"]
    units = ["mV", "mV", "mV", "%", "a.u."]
    events = [((dip0 + dip1) / 2 / fs, "SpO2 desaturation episode", 3, (dip1 - dip0) / fs)]
    return t, values, channels, units, events


def pathology_patch(size: int = 256, seed: int = 0) -> tuple:
    """H&E 风格病理裁剪：RGB，背景基质 + 腺体腔 + 核团。返回 (rgb, mask)。"""
    rng = np.random.default_rng(seed)
    rgb = np.zeros((size, size, 3), dtype=np.uint8)
    # 基质：嗜伊红粉红
    rgb[:, :, 0] = 205 + rng.normal(0, 12, (size, size))
    rgb[:, :, 1] = 150 + rng.normal(0, 14, (size, size))
    rgb[:, :, 2] = 175 + rng.normal(0, 12, (size, size))
    mask = np.zeros((size, size), dtype=np.uint8)

    yy, xx = np.mgrid[0:size, 0:size]
    for _ in range(7):  # 腺体腔（类 1）
        cy, cx = rng.uniform(20, size - 20), rng.uniform(20, size - 20)
        r = rng.uniform(10, 26)
        lumen = ((yy - cy) / r) ** 2 + ((xx - cx) / (r * rng.uniform(0.6, 1.4))) ** 2 <= 1.0
        rgb[lumen] = (245, 235, 240)
        mask[lumen] = 1
    for _ in range(260):  # 核（类 2），苏木精深蓝紫
        cy, cx = rng.uniform(4, size - 4), rng.uniform(4, size - 4)
        r = rng.uniform(1.6, 4.2)
        nucleus = ((yy - cy) ** 2 + (xx - cx) ** 2) <= r ** 2
        rgb[nucleus] = (95, 60, 140)
        mask[nucleus] = 2
    return np.clip(rgb, 0, 255).astype(np.uint8), mask


def build_medical(root, seed: int = 0) -> dict:
    root = Path(root) / "medical"
    for sub in ("ct", "physiological", "pathology"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    written = {}

    stack, ct_truth = ct_head_stack(seed=seed)
    np.save(root / "ct" / "head_series.npy", stack)
    # 断层序列没有绝对采集时刻，只给层面顺序；窗宽窗位默认值放在侧车里供界面上色
    (root / "ct" / "head_series.meta.json").write_text(json.dumps({
        "fps": 1.0, "t0": None, "modality_note": "CT 断层，层面序号即帧号",
        "window": {"soft_tissue": (40.0, 80.0), "bone": (300.0, 1500.0)},
        **ct_truth,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    written["ct_stack"] = str(root / "ct" / "head_series.npy")

    t, values, channels, units, events = physiological(seed=seed)
    import csv as _csv

    t0 = 1735689600.0
    with (root / "physiological" / "icu_record.csv").open("w", encoding="utf-8", newline="") as fh:
        w = _csv.writer(fh)
        w.writerow(["utc"] + channels)
        for i in range(t.size):
            w.writerow([f"{t0 + t[i]:.4f}"] + [f"{values[i, j]:.5f}" for j in range(len(channels))])
    (root / "physiological" / "calib.json").write_text(json.dumps(
        {c: {"type": "analog"} for c in channels}, ensure_ascii=False, indent=2), encoding="utf-8")
    synth.write_events(root / "physiological" / "alerts.json", t0, events)
    written["icu"] = str(root / "physiological" / "icu_record.csv")
    written["alerts"] = str(root / "physiological" / "alerts.json")

    from PIL import Image

    for k in range(6):
        rgb, mask = pathology_patch(seed=seed + 30 + k)
        Image.fromarray(rgb).save(root / "pathology" / f"slide_{k:02d}_rgb.png")
        _save_labels(root / "pathology" / f"slide_{k:02d}_gt.png", mask, PATHOLOGY_PALETTE)
    written["pathology_dir"] = str(root / "pathology")

    truth = {"ct": ct_truth, "n_pathology": 6, "desaturation_event": events[0][1],
             "pathology_labels": {"0": "背景", "1": "腺体腔", "2": "核",
                                  "colors": [list(c) for c in PATHOLOGY_PALETTE]},
             "payload_truth": {"head_series": {
                 "centroids": [[ct_truth["tumor"][0], ct_truth["tumor"][1]]],
                 "radius": ct_truth["tumor"][2], "slice": "mid"}}}
    (root / "truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"category": "medical", "root": str(root), "written": written, "truth": truth}


# --------------------------------------------------------------------------- #
# 计算机视觉分割
# --------------------------------------------------------------------------- #

CLASSES = {1: "blob", 2: "stripe", 3: "ring"}
# 掩膜用调色板 PNG 存：类别编号是标签不是强度，灰度存法会让渲染层把它
# 当连续量拉伸，三个类别糊成三种灰，看不出漏了哪一类
CLASS_COLORS = {1: (219, 77, 64), 2: (51, 184, 89), 3: (64, 102, 224)}
LABEL_PALETTE = [(0, 0, 0)] + [CLASS_COLORS[c] for c in sorted(CLASSES)]
_NORM = {c: tuple(v / 255.0 for v in rgb) for c, rgb in CLASS_COLORS.items()}
PATHOLOGY_PALETTE = [(0, 0, 0), (245, 235, 240), (95, 60, 140)]


def _save_labels(path, mask: np.ndarray, palette) -> Path:
    from PIL import Image

    flat = [v for rgb in palette for v in rgb]
    img = Image.fromarray(mask.astype(np.uint8), mode="P")
    img.putpalette(flat)
    img.save(str(path))
    return Path(path)


def segmentation_scene(size: int = 192, seed: int = 0) -> tuple:
    """合成场景：三类目标 + 纹理背景。返回 (rgb, gt_mask, 真值统计)。"""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:size, 0:size]
    rgb = np.empty((size, size, 3), dtype=np.float64)
    base = rng.uniform(0.35, 0.6)
    rgb[:, :, 0] = base + 0.10 * np.sin(xx / 9.0) + rng.normal(0, 0.03, (size, size))
    rgb[:, :, 1] = base + 0.10 * np.cos(yy / 11.0) + rng.normal(0, 0.03, (size, size))
    rgb[:, :, 2] = base + 0.06 * np.sin((xx + yy) / 13.0) + rng.normal(0, 0.03, (size, size))
    mask = np.zeros((size, size), dtype=np.uint8)

    counts = {1: 0, 2: 0, 3: 0}
    # 后画的类不覆盖先画的：颜色与标签必须严格一致，否则真值掩膜自己就自相矛盾
    for _ in range(rng.integers(2, 5)):  # 类 1 团块
        cy, cx = rng.uniform(20, size - 20), rng.uniform(20, size - 20)
        r = rng.uniform(12, 26)
        sel = (((yy - cy) ** 2 + (xx - cx) ** 2) <= r ** 2) & (mask == 0)
        rgb[sel] = _NORM[1]
        mask[sel] = 1
        counts[1] += int(sel.any())
    for _ in range(rng.integers(1, 4)):  # 类 2 条带
        cy, cx = rng.uniform(20, size - 20), rng.uniform(20, size - 20)
        ang = rng.uniform(0, math.pi)
        length, width = rng.uniform(40, 90), rng.uniform(6, 14)
        u = (xx - cx) * math.cos(ang) + (yy - cy) * math.sin(ang)
        v = -(xx - cx) * math.sin(ang) + (yy - cy) * math.cos(ang)
        sel = (np.abs(u) <= length / 2) & (np.abs(v) <= width / 2) & (mask == 0)
        rgb[sel] = _NORM[2]
        mask[sel] = 2
        counts[2] += int(sel.any())
    for _ in range(rng.integers(1, 3)):  # 类 3 环
        cy, cx = rng.uniform(28, size - 28), rng.uniform(28, size - 28)
        r = rng.uniform(14, 24)
        dist = np.hypot(yy - cy, xx - cx)
        sel = (dist <= r) & (dist >= r * 0.55) & (mask == 0)
        rgb[sel] = _NORM[3]
        mask[sel] = 3
        counts[3] += int(sel.any())

    return (np.clip(rgb * 255, 0, 255).astype(np.uint8), mask, counts)


def _dilate_erode(mask: np.ndarray, radius: int, grow: bool) -> np.ndarray:
    from scipy import ndimage

    foot = np.ones((2 * radius + 1, 2 * radius + 1), dtype=bool)
    out = np.zeros_like(mask)
    for c in np.unique(mask):
        if c == 0:
            continue
        sel = mask == c
        out[sel] = c
        if grow:
            out[ndimage.binary_dilation(sel, foot) & (mask == 0)] = c
        else:
            holes = sel & ~ndimage.binary_erosion(sel, foot)
            out[holes] = 0
    return out


def predict_variant(gt: np.ndarray, rng, kind: str) -> np.ndarray:
    """三种典型失败模式，模拟不同模型的预测输出。

    刻意让每个模型"擅长的方面"不同，这样并行对比才有信息量：
    只看单一指标会得出完全相反的结论。
    """
    pred = gt.copy()
    if kind == "unet":
        # 边界抖动：整体略胀，小目标丢失
        pred = _dilate_erode(pred, 2, grow=True)
        for c in np.unique(pred):
            if c == 0:
                continue
            sel = pred == c
            if sel.sum() < 140:
                pred[sel] = 0
    elif kind == "deeplabv3":
        # 过平滑：细节被抹掉，环状目标退化为实心
        from scipy import ndimage

        for c in list(np.unique(pred)):
            if c == 0:
                continue
            sel = ndimage.binary_opening(pred == c, np.ones((3, 3)))
            pred[pred == c] = 0
            pred[sel] = c
    else:  # segformer_s：局部块状漏检 + 类间混淆
        h, w = pred.shape
        by, bx = rng.integers(0, max(1, h - 24)), rng.integers(0, max(1, w - 24))
        pred[by:by + 24, bx:bx + 24] = 0
        confuse = pred == 2
        pred[confuse] = 1
    return pred


def _iou(a: np.ndarray, b: np.ndarray, c: int) -> float:
    inter = np.logical_and(a == c, b == c).sum()
    union = np.logical_or(a == c, b == c).sum()
    return float(inter / union) if union else float("nan")


def build_cv_segmentation(root, seed: int = 0, n_scenes: int = 12,
                          models=("unet", "deeplabv3", "segformer_s")) -> dict:
    root = Path(root) / "cv_segmentation"
    from PIL import Image

    dirs = {"images": root / "images", "mask_gt": root / "mask_gt"}
    for m in models:
        dirs[f"pred_{m}"] = root / "pred" / m
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)

    rng = np.random.default_rng(seed)
    rows = []
    for k in range(n_scenes):
        rgb, gt, counts = segmentation_scene(seed=seed + k)
        name = f"scene_{k:03d}"
        Image.fromarray(rgb).save(dirs["images"] / f"{name}.png")
        _save_labels(dirs["mask_gt"] / f"{name}.png", gt, LABEL_PALETTE)
        for m in models:
            pred = predict_variant(gt, rng, m)
            _save_labels(dirs[f"pred_{m}"] / f"{name}.png", pred, LABEL_PALETTE)
            rec = {"scene": name, "model": m}
            for c, label in CLASSES.items():
                rec[f"iou_{label}"] = round(_iou(gt, pred, c), 4)
            rec["miou"] = round(float(np.nanmean([rec[f"iou_{l}"] for l in CLASSES.values()])), 4)
            rows.append(rec)

    import csv as _csv

    with (root / "metrics.csv").open("w", encoding="utf-8", newline="") as fh:
        w = _csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    summary = {}
    for m in models:
        sel = [r for r in rows if r["model"] == m]
        summary[m] = {
            "mean_miou": round(float(np.mean([r["miou"] for r in sel])), 4),
            **{f"mean_iou_{l}": round(float(np.mean([r[f"iou_{l}"] for r in sel])), 4)
               for l in CLASSES.values()},
        }
    (root / "truth.json").write_text(json.dumps({
        "n_scenes": n_scenes, "models": list(models), "classes": CLASSES,
        "label_palette": {str(c): list(v) for c, v in CLASS_COLORS.items()},
        "per_model": summary}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"category": "cv_segmentation", "root": str(root),
            "written": {"images": str(dirs["images"]), "mask_gt": str(dirs["mask_gt"]),
                        "metrics": str(root / "metrics.csv")},
            "truth": {"per_model": summary, "n_scenes": n_scenes}}


# --------------------------------------------------------------------------- #

BUILDERS = {
    "aerospace": build_aerospace,
    "medical": build_medical,
    "cv_segmentation": build_cv_segmentation,
}


def build_all(root, categories: Optional[list] = None, seed: int = 0, **knobs) -> dict:
    """生成数据集。`knobs` 是各类别生成器的可调参数，只转发对方接受的键。

    三类的参数表天然不同（时序长度只对航空航天有意义，场景数只对分割有意义），
    所以这里按签名过滤而不是要求统一签名。
    """
    import inspect

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    results = {}
    for name in (categories or list(BUILDERS)):
        if name not in BUILDERS:
            print(f"[warn] 未知类别 {name}，可用: {sorted(BUILDERS)}")
            continue
        fn = BUILDERS[name]
        accepted = set(inspect.signature(fn).parameters)
        results[name] = fn(root, seed=seed, **{k: v for k, v in knobs.items() if k in accepted})
        print(f"[{name}] -> {results[name]['root']}")

    manifest = {
        "generated_by": "mdva.datasets",
        "seed": seed,
        "categories": {name: {**CATEGORIES[name],
                              "root": str(Path(root) / name),
                              "truth": results.get(name, {}).get("truth", {})}
                       for name in results},
    }
    (Path(root) / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return results
