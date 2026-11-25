"""Shared device routing and estimates for project batches."""

from collections import defaultdict
from datetime import datetime, timezone
from math import isfinite
from statistics import median

from .solar_device import automatic, matches

PENDING = {"queued", "running", "waiting_gpu", "interrupted"}


def targets(mode, devices):
    if mode == "auto":
        return [automatic(devices)]
    if mode == "all-gpus":
        gpus = [device for device in devices if device["device"] == "cuda" and device["available"]]
        if gpus:
            return gpus
        raise ValueError("没有兼容的显卡可用于识别")
    for device in devices:
        if device["id"] == mode:
            if device["available"]:
                return [device]
            raise ValueError(device.get("reason", "此设备不可用"))
    raise ValueError("所选处理设备不可用，请重新选择")


def job_device(job, devices):
    selected = job.get("target_device")
    if selected is None:
        return automatic(devices)
    return next((d for d in devices if d["available"] and matches(d, selected)), None)


def ordered_pending(jobs):
    return sorted(
        (j for j in jobs if j["state"] in PENDING),
        key=lambda j: (j["state"] != "running", j["created_utc"], j["id"]),
    )


def positive(value):
    return isinstance(value, (int, float)) and isfinite(value) and value > 0


class Timing:
    def __init__(self, jobs, devices, stamp):
        self.devices = devices
        self.stamp = stamp
        self.work = ordered_pending(jobs)
        self.rates = defaultdict(list)
        self.load = defaultdict(list)
        self.startup = defaultdict(list)
        for job in sorted(jobs, key=lambda j: j.get("updated_utc", ""), reverse=True):
            if positive(job.get("load_seconds")):
                device = self.measured_device(job)
                if device:
                    key = (device["id"], job["model_id"])
                    self.load[key].append(job["load_seconds"])
                    if positive(job.get("worker_startup_seconds")):
                        self.startup[device["id"]].append(job["worker_startup_seconds"])
            for row in reversed(job["results"]):
                if row["state"] != "completed" or not positive(row.get("seconds")):
                    continue
                device = self.measured_device(row if row.get("device_id") else job)
                if device:
                    self.rates[device["id"], job["model_id"]].append(row["seconds"])

    def measured_device(self, record):
        for device in self.devices:
            if record.get("device_id") == device["id"] and record.get("hardware") == device["name"]:
                return device
        if record.get("device_id") is not None:
            return None
        candidates = [
            d
            for d in self.devices
            if d["device"] == record.get("device")
            and (d["device"] == "cpu" or d["name"] == record.get("hardware"))
        ]
        return candidates[0] if len(candidates) == 1 else None

    def rate(self, device, model):
        values = self.rates[device, model]
        return median(values[:24]) if values else None

    def loading(self, device, model):
        return median(self.load[device, model][:12] or [0])

    def job_remaining(self, job, device):
        count = max(0, job["total"] - job["completed"])
        if not count:
            return 0
        key, model = device["id"], job["model_id"]
        rate = self.rate(key, model)
        if rate is None:
            return None
        seconds = count * rate
        if job["state"] != "running":
            return seconds + self.loading(key, model)
        started = job.get("load_started_at") or job.get("active_started_at")
        if started:
            elapsed = max(0, self.stamp - started)
            if job.get("load_started_at"):
                seconds += max(0, self.loading(key, model) - elapsed)
            else:
                seconds -= min(elapsed, rate)
        return max(0.5, seconds)

    def backlog(self, device, until=None):
        ahead, seconds = 0, 0
        for job in self.work:
            if until is not None and job["id"] == until:
                break
            assigned = job_device(job, self.devices)
            if assigned is None or assigned["id"] != device["id"]:
                continue
            ahead += 1
            duration = self.job_remaining(job, device)
            seconds = None if seconds is None or duration is None else seconds + duration
        return ahead, seconds

    def plan(self, counts, devices):
        """Distribute photos using observed speeds and the current device queues."""
        if len(devices) == 1:
            return {model: [{"device": devices[0], "count": count}] for model, count in counts.items()}
        loads = {d["id"]: self.backlog(d)[1] or 0 for d in devices}
        allocations = {}
        used = set()
        for model, count in counts.items():
            assigned = {d["id"]: 0 for d in devices}
            measured = [rate for d in devices if (rate := self.rate(d["id"], model)) is not None]
            fallback = median(measured) if measured else 1.0
            costs = {d["id"]: self.rate(d["id"], model) or fallback for d in devices}
            for _ in range(count):
                candidates = [d for d in devices if d["id"] not in used] or devices
                device = min(candidates, key=lambda d: loads[d["id"]] + costs[d["id"]])
                key = device["id"]
                if assigned[key] % 200 == 0:
                    loads[key] += self.loading(key, model)
                assigned[key] += 1
                loads[key] += costs[key]
                used.add(key)
            allocations[model] = [
                {"device": d, "count": assigned[d["id"]]} for d in devices if assigned[d["id"]]
            ]
        return allocations

    def estimate(self, plans, blocked):
        details = []
        for device, jobs in plans:
            key = device["id"]
            actual = jobs[-1].get("id")
            ahead, seconds = self.backlog(device, until=actual)
            if actual is not None:
                # The backlog includes earlier jobs of this batch on the same device.
                duration = self.job_remaining(jobs[-1], device)
                batch_ids = {job["id"] for job in jobs}
                ahead -= sum(j["id"] in batch_ids and j["id"] != actual for j in self.work)
            else:
                duration = 0
                for job in jobs:
                    rate = self.rate(key, job["model_id"])
                    if rate is None:
                        duration = None
                        break
                    duration += job["total"] * rate + self.loading(key, job["model_id"])
            waiting = device["device"] == "cuda" and blocked
            if seconds is not None and duration is not None and not waiting:
                seconds += duration
                if not self.work:
                    seconds += median(self.startup[key][:12] or [0])
            else:
                seconds = None
            details.append(
                {
                    "id": key,
                    "name": device["name"],
                    "ahead": max(0, ahead),
                    "remaining": sum(j["total"] - j.get("completed", 0) for j in jobs),
                    "seconds": None if seconds is None else round(seconds, 1),
                    "blocked": waiting,
                }
            )
        seconds = (
            max((d["seconds"] for d in details), default=0)
            if all(d["seconds"] is not None for d in details)
            else None
        )
        return {
            "seconds": seconds,
            "finish_utc": (
                datetime.fromtimestamp(self.stamp + seconds, timezone.utc).isoformat()
                if seconds is not None
                else None
            ),
            "ahead": sum(d["ahead"] for d in details),
            "devices": details,
        }
