"""Replay the same full-length trace solo and paired on two isolated service stacks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from sysml.common import DSN, REDIS
import run_low_concurrency as campaign


def run_one(directory, clients):
    job = next(j for j in campaign.plan([32, 64])
               if j["seed"] == 11 and j["scenario"] == "stable"
               and j["policy"] == "I1" and j["clients"] == clients)
    data = campaign.trial(job, campaign.materialize(job), directory)
    campaign.save(directory / "result.json", data)


def start(root, mode, clients):
    directory = root / f"{mode}-{clients}"
    directory.mkdir()
    env = os.environ.copy()
    if clients == 64:
        env.update(SYSML_DSN=DSN.replace(":55432/", ":55433/"),
                   SYSML_REDIS=REDIS.replace(":56379/", ":56380/"))
        if env["SYSML_DSN"] == DSN or env["SYSML_REDIS"] == REDIS:
            raise ValueError("Expected the benchmark's local service endpoints")
    with (directory / "worker.log").open("w") as log:
        return subprocess.Popen([sys.executable, "-B", __file__, str(directory), str(clients)],
                                env=env, stdout=log, stderr=subprocess.STDOUT)


def probe(root):
    root.mkdir()
    durations = {}
    for mode in ["solo", "paired"]:
        start_time = time.monotonic()
        children = []
        try:
            for clients in (32, 64):
                child = start(root, mode, clients)
                children.append(child)
                if mode == "solo" and child.wait():
                    raise RuntimeError("Solo trial failed")
        finally:
            codes = [child.wait() for child in children]
        if any(codes):
            raise RuntimeError("Paired trial failed")
        durations[mode] = time.monotonic() - start_time

    rows = []
    for clients in (32, 64):
        def metrics(mode):
            r = json.loads((root / f"{mode}-{clients}" / "result.json").read_bytes())
            return r["metrics"]["all"]
        solo, pair = metrics("solo"), metrics("paired")
        rows.append(dict(clients=clients, solo_p95_ms=solo["latency_ms"]["p95"],
                         paired_p95_ms=pair["latency_ms"]["p95"],
                         db_ms_ratio=pair["demand_db_ms"] / solo["demand_db_ms"],
                         scheduled_p95_ratio=pair["scheduled_latency_ms"]["p95"] / solo["scheduled_latency_ms"]["p95"]))
    campaign.save(root / "comparison.json", dict(seconds=durations, comparisons=rows))


if __name__ == "__main__":
    if len(sys.argv) == 3:
        run_one(Path(sys.argv[1]), int(sys.argv[2]))
    else:
        probe(Path(sys.argv[1]))
