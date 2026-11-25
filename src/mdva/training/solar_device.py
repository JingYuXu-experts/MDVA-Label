"""Enumerate the devices supported by the installed Torch build."""

import json

CPU = {"id": "cpu", "device": "cpu", "name": "CPU", "available": True}


def inventory(cuda=None):
    if cuda is None:
        import torch

        cuda = torch.cuda
    devices = [CPU.copy()]
    if not cuda.is_available():
        return devices

    architectures = []
    for arch in cuda.get_arch_list():
        kind, _, digits = arch.partition("_")
        if digits.isdigit():
            architectures.append((kind, int(digits)))
    for index in range(cuda.device_count()):
        try:
            major, minor = cuda.get_device_capability(index)
            target = major * 10 + minor
            compatible = any(
                build <= target and (kind == "compute" or (kind == "sm" and build // 10 == major))
                for kind, build in architectures
            )
            device = {
                "id": f"cuda:{index}",
                "device": "cuda",
                "name": cuda.get_device_name(index),
                "index": index,
                "available": compatible,
            }
            if not compatible:
                device["reason"] = "当前 PyTorch 不支持这张显卡的 CUDA 架构"
            devices.append(device)
        except (RuntimeError, OSError):
            devices.append(
                {
                    "id": f"cuda:{index}",
                    "device": "cuda",
                    "name": f"GPU {index}",
                    "index": index,
                    "available": False,
                    "reason": "无法读取显卡状态",
                }
            )
    return devices


def automatic(devices):
    return next((d for d in devices if d["device"] == "cuda" and d["available"]), CPU)


def matches(left, right):
    return left["id"] == right["id"] and left["name"] == right["name"]


if __name__ == "__main__":
    print(json.dumps(inventory()))
