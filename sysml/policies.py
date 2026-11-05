"""Shared cache controller. Callers serialize mutations; storage holds only result bytes."""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import math
import operator
import time

from .common import ms

COMPARE = {
    "=": operator.eq,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}


@dataclass
class Config:
    normal: str = "N2"
    invalid: str = "I1"
    refresh: str = "R0"
    evict: str = "E1"
    capacity: int | None = None
    refresh_budget: int | None = 4
    admission: str = "lazy"
    tau: float = 60
    horizon: float = 30
    ablate: str = ""
    min_admit: float = 1.0
    direct: bool = False
    audit_hits: bool = False
    weights: dict = field(
        default_factory=lambda: dict(
            freq=1.0, cost=1.0, recent=1.0, decay=1.0, reuse=1.0
        )
    )
    refresh_threshold: float = 0.0

    def __post_init__(self):
        for val, prefix, count in [
            (self.normal, "N", 4),
            (self.invalid, "I", 5),
            (self.refresh, "R", 8),
            (self.evict, "E", 9),
        ]:
            if val not in [prefix + str(i) for i in range(count)]:
                raise ValueError(val)
        if self.invalid == "I4" and self.refresh != "R0":
            raise ValueError(
                "I4 lazy revision checks require R0; fix I1 or I2 for refresh comparisons"
            )
        if set(self.weights) != {"freq", "cost", "recent", "decay", "reuse"} or any(
            w < 0 for w in self.weights.values()
        ):
            raise ValueError("expected five nonnegative heuristic weights")
        if (
            self.admission not in {"lazy", "reconsider"}
            or self.tau <= 0
            or self.horizon <= 0
            or (self.capacity is not None and self.capacity < 0)
            or (self.refresh_budget is not None and self.refresh_budget < 0)
        ):
            raise ValueError("invalid policy configuration")


@dataclass
class History:
    analysis: object
    request: dict
    first: float
    last: float
    freq: int = 0
    decay: float = 0
    writes: int = 0
    cost: float = 0
    invalidated: bool = False
    write_origin: int = 0

    def features(self, t, cfg):
        age = max(0, t - self.last)
        decay = self.decay * math.exp(-age / cfg.tau)
        lam = decay / cfg.tau
        mu = self.writes / max(cfg.horizon, t - self.first)
        reuse = min(lam * cfg.horizon, lam / max(mu, 1e-12))
        return {
            "freq": self.freq,
            "cost": self.cost,
            "recent": math.exp(-age / cfg.tau),
            "decay": decay,
            "reuse": reuse,
            "survive": lam / (lam + mu) if lam + mu else 0,
        }


@dataclass
class Entry:
    size: int
    birth: int
    stamp: tuple
    gds: float
    proactive: float | None


def affected(a, change, policy):
    if policy == "I0":
        return True
    if change["table"] not in a.tables:
        return False
    if policy == "I1" or change["op"] == "TRUNCATE":
        return True
    if (
        policy in {"I2", "I3"}
        and change["op"] == "UPDATE"
        and a.columns is not None
        and set(change["columns"]).isdisjoint(a.columns)
    ):
        return False
    if policy == "I3" and a.predicate:
        col, op, val = a.predicate
        for image in (change.get("old"), change.get("new")):
            if image is None:
                continue
            if col not in image or (
                image[col] is not None and type(image[col]) is not int
            ):
                return True
            if image[col] is not None and COMPARE[op](image[col], val):
                return True
        return False
    return True


class Cache:
    def __init__(self, cfg, delete=lambda key: None):
        self.cfg = cfg
        self.delete = delete
        self.entries = {}
        self.hist = {}
        self.index = defaultdict(set)
        self.rev = Counter()
        self.point_cols = defaultdict(set)
        self.stats = Counter()
        self.used = self.peak = self.serial = 0
        self.age = 0.0
        self.epoch = 0

    def access(self, a, op, t):
        if a.key not in self.hist:
            self.hist[a.key] = History(a, op, t, t, write_origin=self.write_count(a))
        h = self.hist[a.key]
        h.writes = self.write_count(a) - h.write_origin
        h.decay = h.features(t, self.cfg)["decay"] + 1
        h.freq += 1
        h.last = t
        if a.predicate and a.predicate[1] == "=":
            for table in a.tables:
                self.point_cols[table].add(a.predicate[0])
        if a.key in self.entries:
            e = self.entries[a.key]
            e.gds = self.age + h.freq * h.cost / max(1, e.size)
        return h

    def versions(self, a):
        if a.predicate and a.predicate[1] == "=":
            t = next(iter(a.tables))
            col, _, v = a.predicate
            return (self.rev[t, "wild"], self.rev[t, col, v])
        return tuple(self.rev[t, "all"] for t in sorted(a.tables))

    def write_count(self, a):
        return sum(self.versions(a))

    def stamp(self, a):
        return self.versions(a) if self.cfg.invalid == "I4" else ()

    def valid(self, k):
        e = self.entries.get(k)
        if e and e.stamp != self.stamp(self.hist[k].analysis):
            self.hist[k].invalidated = True
            self.remove(k, "revision_miss")
            return False
        return e is not None

    def hit(self, k):
        e = self.entries[k]
        if e.proactive is not None:
            self.stats["refresh_used"] += 1
            e.proactive = None

    def remove(self, k, reason):
        e = self.entries.pop(k)
        self.delete(k)
        self.used -= e.size
        for t in self.hist[k].analysis.tables:
            self.index[t].discard(k)
        self.stats[reason] += 1
        if e.proactive is not None:
            self.stats["refresh_wasted"] += 1
            self.stats["refresh_wasted_db_ms"] += e.proactive

    def priority(self, k, t, kind):
        h = self.hist[k]
        h.writes = self.write_count(h.analysis) - h.write_origin
        f = self.hist[k].features(t, self.cfg)
        cfg = self.cfg
        if cfg.ablate:
            f[cfg.ablate] = 1.0
            if cfg.ablate == "reuse":
                f["survive"] = 1.0
        w = cfg.weights
        combined = (
            f["cost"] ** w["cost"]
            * (math.log1p(f["freq"]) * w["freq"] + f["decay"] * w["decay"])
            * f["recent"] ** w["recent"]
            * (f["reuse"] * f["survive"]) ** w["reuse"]
        )
        if kind == "refresh":
            return {
                "R1": 1,
                "R2": f["freq"],
                "R3": f["cost"],
                "R4": f["recent"],
                "R5": f["decay"],
                "R6": f["reuse"],
                "R7": combined - f["cost"],
            }[cfg.refresh]
        e = self.entries[k]
        return [
            e.birth,
            self.hist[k].last,
            f["freq"],
            f["decay"],
            f["cost"],
            f["freq"] * f["cost"],
            e.gds,
            f["reuse"],
            combined / max(1, e.size),
        ][int(cfg.evict[1:])]

    def publish(self, k, size, cost, t, proactive=False):
        h = self.hist[k]
        h.cost = 0.8 * h.cost + 0.2 * cost if h.cost else cost
        cfg = self.cfg
        if cfg.capacity is not None and size > cfg.capacity:
            return False
        if (
            h.invalidated
            and not proactive
            and cfg.admission == "reconsider"
            and h.features(t, cfg)["reuse"] < cfg.min_admit
        ):
            self.stats["admission_rejected"] += 1
            return False
        start = time.perf_counter()
        while cfg.capacity is not None and self.used + size > cfg.capacity:
            victim = min(
                self.entries,
                key=lambda q: (self.priority(q, t, "evict"), self.entries[q].birth, q),
            )
            if cfg.evict == "E6":
                self.age = self.entries[victim].gds
            self.remove(victim, "evictions")
        self.stats["replacement_ms"] += ms(start)
        self.serial += 1
        self.entries[k] = Entry(
            size,
            self.serial,
            self.stamp(h.analysis),
            self.age + h.freq * h.cost / max(1, size),
            cost if proactive else None,
        )
        self.used += size
        self.peak = max(self.peak, self.used)
        h.invalidated = False
        for table in h.analysis.tables:
            self.index[table].add(k)
        return True

    def changes(self, events, t):
        if not events:
            return set(), []
        start = time.perf_counter()
        self.epoch += 1
        selected = set()
        for change in events:
            table = change["table"]
            self.rev[table, "all"] += 1
            if change["op"] == "TRUNCATE":
                self.rev[table, "wild"] += 1
            else:
                for col in self.point_cols[table]:
                    vals = {
                        row[col]
                        for row in (change.get("old"), change.get("new"))
                        if row is not None and col in row
                    }
                    for v in vals:
                        self.rev[table, col, v] += 1
            if self.cfg.invalid == "I4":
                continue
            candidates = self.entries if self.cfg.invalid == "I0" else self.index[table]
            self.stats["examined"] += len(candidates)
            selected.update(
                k
                for k in candidates
                if affected(self.hist[k].analysis, change, self.cfg.invalid)
            )
        for k in selected:
            self.hist[k].invalidated = True
            self.remove(k, "invalidations")
        self.stats["selection_ms"] += ms(start)
        start = time.perf_counter()
        cfg = self.cfg
        chosen = []
        if cfg.refresh != "R0":
            ranked = sorted(
                [(self.priority(k, t, "refresh"), k) for k in selected],
                key=lambda x: (-x[0], x[1]),
            )
            chosen = [
                k
                for score, k in ranked
                if cfg.refresh != "R7" or score > cfg.refresh_threshold
            ][: cfg.refresh_budget]
        self.stats["scheduling_ms"] += ms(start)
        return selected, chosen
