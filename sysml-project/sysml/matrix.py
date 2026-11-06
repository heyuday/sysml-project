"""One calibrated trace per seed/scenario. Exhaustive sweeps are opt-in."""

from dataclasses import replace
from itertools import product
from .simulation import simulate
from .policies import Config


def cells(track, base, peak):
    if track == "rq1":
        for n in range(4):
            yield {"normalizer": "N" + str(n)}, replace(base, normal="N" + str(n))
    elif track == "rq2a":
        for i in range(5):
            yield {}, replace(base, invalid=f"I{i}", capacity=None)
    elif track == "rq2b":
        for r, b in product(range(8), [None, 4]):
            if r == 0 and b is not None:
                continue
            cfg = replace(base, refresh=f"R{r}", refresh_budget=b)
            yield {"budget": b}, cfg
            if r == 7:
                for feature in ("freq", "cost", "recent", "decay", "reuse"):
                    yield {"budget": b, "ablate": feature}, replace(cfg, ablate=feature)
            if r == 0:
                yield {"admission": "reconsider"}, replace(cfg, admission="reconsider")
    elif track == "rq3":
        for f, e in product((None, 0.5, 0.25, 0.1, 0.05, 0.01), range(9)):
            cfg = replace(
                base, capacity=None if f is None else int(peak * f), evict=f"E{e}"
            )
            yield {"capacity_fraction": f}, cfg
            if e == 8:
                for feature in ("freq", "cost", "recent", "decay", "reuse"):
                    yield (
                        {"capacity_fraction": f, "ablate": feature},
                        replace(cfg, ablate=feature),
                    )
    else:
        raise ValueError(track)


def sweep(ops, catalog, track, seed, scenario, split, warm=0.2, base=None):
    if base is None:
        base = Config()
    # Split chronologically before warmup/calibration. Each partition starts cold.
    boundary = int(0.4 * len(ops))
    part = ops[:boundary] if split == "tune" else ops[boundary:]
    if not part:
        raise ValueError("empty chronological split")
    if track in {"rq2a", "rq2b", "rq3"} and any(
        o.get("source") != "controlled-ch" for o in part
    ):
        raise ValueError("RQ2/RQ3 require controlled CH input")
    peak = (
        simulate(part, catalog, replace(base, capacity=None, refresh="R0"), warm)[
            "peak_value_bytes"
        ]
        if track == "rq3"
        else 0
    )
    for settings, cfg in cells(track, base, peak):
        r = simulate(part, catalog, cfg, warm)
        yield {
            **r,
            "seed": seed,
            "track": track,
            "split": split,
            "scenario": {**scenario, **settings},
            "working_set_bytes": peak,
        }
