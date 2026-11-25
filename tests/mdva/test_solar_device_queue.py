from threading import Barrier
from pathlib import Path

from mdva.training import solar_device, solar_worker
from mdva.training.durable import atomic_json
from mdva.training.solar_schedule import Timing


def gpu(index):
    return {
        "id": f"cuda:{index}",
        "device": "cuda",
        "index": index,
        "name": f"GPU {index}",
        "available": True,
    }


def job(identifier, state, hardware, **extra):
    return {
        "id": identifier,
        "state": state,
        "created_utc": identifier,
        "model_id": "AR",
        "target_device": hardware,
        "results": [],
        "total": 1,
        "completed": 0,
        **extra,
    }


def sample(hardware, rate):
    row = {
        "state": "completed",
        "seconds": rate,
        "device_id": hardware["id"],
        "hardware": hardware["name"],
    }
    history = job("history-" + hardware["id"], "completed", hardware)
    return {**history, **row, "results": [row], "completed": 1, "load_seconds": 2}


def test_parallel_eta_accounts_for_backlog_hardware_speed_and_elapsed_time():
    first, second = gpu(0), gpu(1)
    queue = [
        sample(first, 4),
        sample(second, 2),
        job("1", "running", first, active_started_at=95),
        job("2", "queued", second),
    ]
    timing = Timing(queue, [first, second], stamp=97)
    for device, expected in [(first, (1, 2)), (second, (1, 4))]:
        assert timing.backlog(device) == expected

    plan = timing.plan({"AR": 8}, [first, second])
    assert [shard["count"] for shard in plan["AR"]] == [3, 5]
    result = timing.estimate(
        [(shard["device"], [{"model_id": "AR", "total": shard["count"]}]) for shard in plan["AR"]],
        blocked=False,
    )
    assert result["seconds"] == 16  # max(2 + 3*4+2, 4 + 5*2+2)
    assert result["finish_utc"].endswith("00:01:53+00:00")
    assert result["ahead"] == 2
    blocked = timing.estimate([(first, [{"model_id": "AR", "total": 2}])], blocked=True)
    assert blocked["seconds"] is None and blocked["devices"][0]["blocked"]


def test_running_batch_estimate_does_not_count_its_shards_ahead():
    cpu = solar_device.CPU
    first = job("1", "running", cpu, active_started_at=9)
    second = job("2", "queued", cpu)
    batch = Timing([sample(cpu, 3), first, second], [cpu], stamp=10)
    estimate = batch.estimate([(cpu, [first, second])], blocked=False)
    assert estimate["seconds"] == 7  # 2 remaining in flight, then 2 load + 3 compute
    assert estimate["ahead"] == 0


def test_worker_runs_one_shard_per_device_concurrently(tmp_path, monkeypatch):
    import pytest

    pytest.importorskip("torch")
    from mdva.training import continuation

    devices = [solar_device.CPU, gpu(0), gpu(1)]
    monkeypatch.setattr(solar_device, "inventory", lambda: devices)
    monkeypatch.setattr(solar_worker, "ROOT", tmp_path)
    root = tmp_path / "solar-inference"
    for index, device in enumerate([*devices, gpu(0)]):
        identifier = str(index)
        atomic_json(root / identifier / "job.json", job(identifier, "queued", device))

    gate = Barrier(len(devices), timeout=15)
    seen = {}

    def simulate(folder, item, device):
        assert isinstance(folder, Path)
        if item["id"] == "3":
            assert solar_worker.read(root / "1" / "job.json")["state"] == "completed"
        else:
            gate.wait()
        if device["device"] == "cuda":
            assert continuation.lock_alive(tmp_path / ".mdva/training-gpu.lock") is True
        seen[item["id"]] = device["id"]
        item["state"] = "completed"
        atomic_json(folder / "job.json", item)

    monkeypatch.setattr(solar_worker, "run_on", simulate)
    solar_worker.run(tmp_path)
    assert seen == {"0": "cpu", "1": "cuda:0", "2": "cuda:1", "3": "cuda:0"}
    assert not (tmp_path / ".mdva/training-gpu.lock").exists()


def test_cpu_queue_runs_while_training_holds_the_gpu(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import pytest

    pytest.importorskip("torch")
    from mdva.training.runtime import exclusive

    cpu, cuda = solar_device.CPU, gpu(0)
    monkeypatch.setattr(solar_device, "inventory", lambda: [cpu, cuda])
    monkeypatch.setattr(solar_worker, "ROOT", tmp_path)
    root = tmp_path / "solar-inference"
    atomic_json(root / "0" / "job.json", job("0", "queued", cuda))
    atomic_json(root / "1" / "job.json", job("1", "queued", cpu))
    cpu_done = Event()
    monkeypatch.setattr(
        solar_worker, "persist", lambda folder, item: atomic_json(folder / "job.json", item)
    )

    def simulate(folder, item, device):
        atomic_json(folder / "job.json", {**item, "state": "completed"})
        if device["id"] == "cpu":
            cpu_done.set()

    monkeypatch.setattr(solar_worker, "run_on", simulate)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with exclusive(tmp_path / ".mdva/training-gpu.lock"):
            worker = pool.submit(solar_worker.run, tmp_path)
            assert cpu_done.wait(15)
            assert solar_worker.read(root / "0" / "job.json")["state"] == "waiting_gpu"
        worker.result(timeout=15)
    assert solar_worker.read(root / "0" / "job.json")["state"] == "completed"
