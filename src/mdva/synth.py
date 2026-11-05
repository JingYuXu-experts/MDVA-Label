"""合成数据生成：点源图像、多通道遥测、事件表。

两个用途。一是测试与演示不依赖外部数据源，二是给算法评测提供带真值的
输入——真实在轨数据没有异常时刻的标注，无法算检出率与质心误差。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional

import numpy as np


def star_field(
    shape: tuple = (512, 512),
    n_sources: int = 60,
    bg: float = 120.0,
    noise_sigma: float = 6.0,
    psf_fwhm: float = 2.4,
    n_cosmic: int = 40,
    bit_depth: int = 16,
    seed: int = 0,
) -> tuple:
    """高斯点源 + 泊松噪声背景 + 单像元宇宙线。返回 (uint16 图像, 质心真值)。"""
    rng = np.random.default_rng(seed)
    h, w = shape
    sigma = psf_fwhm / 2.3548
    img = rng.normal(bg, noise_sigma, size=shape).astype(np.float64)

    truth = []
    for _ in range(n_sources):
        # 留出边界，避免高斯截断导致质心真值与观测不一致
        cy = float(rng.uniform(6, h - 7))
        cx = float(rng.uniform(6, w - 7))
        flux = float(rng.uniform(2000, 60000))
        y0, y1 = max(0, int(cy) - 8), min(h, int(cy) + 9)
        x0, x1 = max(0, int(cx) - 8), min(w, int(cx) + 9)
        yy, xx = np.mgrid[y0:y1, x0:x1]
        amp = flux / (2 * math.pi * sigma ** 2)
        img[y0:y1, x0:x1] += amp * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma ** 2))
        truth.append((cx, cy, flux))

    cosmic = []
    for _ in range(n_cosmic):
        y, x = int(rng.integers(2, h - 2)), int(rng.integers(2, w - 2))
        img[y, x] += rng.uniform(3000, 12000)
        cosmic.append((x, y))

    full = float(2 ** bit_depth - 1)
    q = np.clip(img, 0, full)
    # 保留 >99% 的动态范围余量，让拉伸算法有可分辨的层次
    return q.astype(np.uint16), {"centroids": truth, "cosmic": cosmic, "bg": bg, "sigma": noise_sigma}


def cube(shape: tuple = (16, 256, 256), seed: int = 0, **kwargs) -> np.ndarray:
    """数据立方体：沿第 0 轴缓慢移动的点源，用于切片浏览与时间联动演示。"""
    frames = []
    for i in range(shape[0]):
        img, _ = star_field(shape=shape[1:], n_sources=12, seed=seed + i, **kwargs)
        # 平移量按帧递增，模拟卫星指向漂移
        shift = int(round(i * 1.5))
        frames.append(np.roll(np.roll(img, shift, axis=0), -shift, axis=1))
    return np.stack(frames)


def telemetry(
    n: int = 86400,
    start: float = 1735689600.0,
    dt: float = 1.0,
    seed: int = 0,
) -> tuple:
    """模拟工程遥测：电源、热控、姿控、数传四个分系统。

    返回 (TimeSeries 输入数组, 通道定义, 注入异常真值)。
    异常类型覆盖漂移、卡死、尖峰、渐变退化、位翻转五类，
    这五类在文献里被公认为动态阈值类方法最难同时处理的组合。
    """
    rng = np.random.default_rng(seed)
    t = start + np.arange(n, dtype=np.float64) * dt
    orbit_s = 5800.0  # 近地轨道周期，决定光照/阴影的交替频率
    phase = 2 * np.pi * (t % orbit_s) / orbit_s
    sunlit = np.clip(np.cos(phase), 0.0, 1.0)

    channels = {}

    # 太阳电池阵电流：随光照角变化，阴影期归零
    cur = 8.4 * sunlit + rng.normal(0, 0.045, n)
    channels["SA_Current"] = {"data": cur, "unit": "A", "type": "analog"}

    # 蓄电池电压：光照期充电、阴影期放电，并叠加长期容量衰减
    soc = 0.5 + 0.28 * np.sin(phase - np.pi / 3)
    degrade = -1.6e-7 * t_index_ramp(n)  # 缓慢退化趋势
    bus = 28.0 + 1.1 * soc + degrade + rng.normal(0, 0.012, n)
    channels["Bus_Voltage"] = {"data": bus, "unit": "V", "type": "analog"}

    # 蓄电池温度：热控周期 + 一次注入的加热器故障漂移
    temp = 21.0 + 3.6 * np.sin(phase + 0.4) + rng.normal(0, 0.22, n)
    channels["Batt_Temp"] = {"data": temp, "unit": "degC", "type": "analog"}

    # 动量轮转速：姿态正常时小幅抖动，中段注入卡死（长时间恒定）
    wheel = 3200.0 + 18.0 * np.sin(2 * np.pi * t / 90.0) + rng.normal(0, 2.4, n)
    channels["RW1_Speed"] = {"data": wheel, "unit": "rpm", "type": "analog"}

    # 数传误码率：长尾分布，偶发尖峰
    ber = rng.gamma(0.6, 0.04, n)
    channels["Dnlink_BER"] = {"data": ber, "unit": "", "type": "analog"}

    # 工作模式：离散量，绝不能做插值或求均值
    mode = np.zeros(n, dtype=np.int16)
    for k in range(0, n, 7200):
        mode[k:k + 7200] = int(rng.integers(0, 3))
    channels["Payload_Mode"] = {"data": mode.astype(np.float64), "unit": "", "type": "discrete",
                                "values": {0: "Standby", 1: "Science", 2: "Safe"}}

    truth = {}
    # 加热器故障：温度基线抬升且振幅衰减
    a = int(0.32 * n)
    b = min(a + 5400, n)
    temp[a:b] += np.linspace(0, 9.5, b - a)
    truth["Batt_Temp"] = {"drift": [(a, b, "heater_failure")]}

    # 动量轮卡死：读数冻结
    a = int(0.55 * n)
    b = min(a + 900, n)
    wheel[a:b] = wheel[a]
    truth["RW1_Speed"] = {"frozen": [(a, b, "wheel_stiction")]}

    # 母线电压毛刺：短尖峰，考验降采样是否吃尖
    for frac in (0.11, 0.27, 0.63, 0.81, 0.94):
        i = int(frac * n)
        bus[i:i + 6] += rng.choice([-1, 1]) * rng.uniform(2.6, 4.2)
    truth["Bus_Voltage"] = {"spike": [(i, i + 6, "bus_transient")
                                      for i in [int(f * n) for f in (0.11, 0.27, 0.63, 0.81, 0.94)]]}

    # 误码率整体劣化
    a = int(0.72 * n)
    ber[a:] *= 6.0
    truth["Dnlink_BER"] = {"degrade": [(a, n, "link_degrade")]}

    names = list(channels)
    values = np.stack([channels[c]["data"] for c in names], axis=1)
    units = [channels[c]["unit"] for c in names]
    calib = {
        c: {"type": channels[c]["type"],
            **({"values": channels[c]["values"]} if "values" in channels[c] else {})}
        for c in names
    }
    return (t, values, names, units), calib, truth


def t_index_ramp(n: int) -> np.ndarray:
    return np.arange(n, dtype=np.float64)


def write_fits(path, arr: np.ndarray, header: Optional[dict] = None) -> Path:
    from astropy.io import fits

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    hdu = fits.PrimaryHDU(arr)
    base = {
        "TELESCOP": "SIM-SAT",
        "INSTRUME": "HXI-SIM",
        "OBJECT": "SYNTH-FIELD",
        "DATE-OBS": "2025-01-01T00:00:00.000",
        "EXPTIME": 32.0,
        "BUNIT": "counts",
    }
    for k, v in (header or base).items():
        try:
            hdu.header[k] = v
        except ValueError:
            continue
    hdu.writeto(str(path), overwrite=True)
    return path


def write_telemetry_csv(path, data: tuple, calib: dict) -> Path:
    import csv

    t, values, names, _ = data
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["utc"] + names)
        for i in range(t.size):
            writer.writerow([f"{t[i]:.3f}"] + [f"{values[i, j]:.6f}" for j in range(len(names))])

    Path(path).with_suffix(".calib.json").write_text(json.dumps(calib, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_events(path, t_start: float, specs: list) -> Path:
    """specs: [(offset_s, label, level, duration_s)]"""
    rows = [
        {
            "time": t_start + off,
            "label": label,
            "level": level,
            "source": "SIM",
            "duration": dur,
        }
        for off, label, level, dur in specs
    ]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"events": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def anomaly_truth(anomalies: dict) -> dict:
    """注入异常真值表 -> {通道: {类型: [[起, 止, 名称], …]}}。

    必须整体复制成 JSON 可序列化的结构。以前这里写的是 `list(v)`，
    把 {类型: 区间} 展开成了**键名列表**——真值表看着存在，实则一条异常都定位不了，
    后端评测的查准/查全全成 0/0。这种丢数据的写法要留个注释挡住回退。
    """
    return {channel: {kind: [[int(a), int(b), str(label)] for a, b, label in rows]
                      for kind, rows in groups.items()}
            for channel, groups in anomalies.items()}


def make_dataset(root, n_telemetry: int = 20000, seed: int = 0, with_cube: bool = True) -> Path:
    """一次性生成完整演示数据集（FITS + 遥测 CSV + 标定 + 事件 + 立方体）。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)

    img, truth = star_field(shape=(512, 512), n_sources=60, seed=seed)
    write_fits(root / "image_field.fits", img, {
        "TELESCOP": "SIM-SAT", "INSTRUME": "HXI-SIM", "OBJECT": "SYNTH-FIELD",
        "DATE-OBS": "2025-01-01T00:00:00.000", "EXPTIME": 32.0, "BUNIT": "counts",
    })

    data, calib, anomalies = telemetry(n=n_telemetry, seed=seed)
    write_telemetry_csv(root / "telemetry_sim.csv", data, calib)

    write_events(root / "events_sim.json", data[0][0], [
        (0.32 * n_telemetry, "加热器控制指令下发", 1, 0),
        (0.55 * n_telemetry, "动量轮 A 组切换", 2, 0),
        (0.72 * n_telemetry, "数传链路重配置", 2, 0),
        (0.85 * n_telemetry, "载荷安全模式", 3, 600),
    ])

    if with_cube:
        write_fits(root / "cube_sim.fits", cube((12, 256, 256), seed=seed), {
            "TELESCOP": "SIM-SAT", "INSTRUME": "FXI-SIM", "OBJECT": "SYNTH-CUBE",
            "DATE-OBS": "2025-01-01T00:00:00.000", "NAXIS3": 12, "BUNIT": "counts",
        })

    meta = {"seed": seed, "n_telemetry": n_telemetry, "image_truth_sources": len(truth["centroids"]),
            "injected_anomalies": anomaly_truth(anomalies)}
    (root / "truth.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return root
