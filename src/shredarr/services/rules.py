"""Seeding rules: when a torrent whose media was deleted may go."""

from __future__ import annotations

from collections.abc import Callable

from ..clients.torrents import Torrent
from ..config import JSON, Config, Rule
from .trackers import tracker_host


def rule_for(t: Torrent, overrides: dict[str, Rule], config: Config, title: str | None,
             tracker_names: Callable[[], dict[str, str]]) -> Rule:
    """The seeding rule for torrent t, and where it came from. First match wins:
    this torrent's own rule > show/movie rule > tracker rule > category rule > default."""
    if t["hash"] in overrides:
        return {**overrides[t["hash"]], "source": "this torrent"}
    extra = config.rule_list
    host = tracker_host(t)
    tname = (tracker_names().get(t["hash"]) or "").casefold() if any(r.get("type") == "tracker" for r in extra) else ""

    def matches(kind: str, m: str) -> bool:
        if kind == "title":
            return bool(title) and m.casefold() == (title or "").casefold()
        if kind == "tracker":
            return bool(host and (host == m or host.endswith("." + m))) or bool(tname and tname == m.casefold())
        return t.get("category") == m

    for kind in ("title", "tracker", "category"):
        for r in extra:
            if r.get("type") == kind and r.get("match") and matches(kind, r["match"]):
                return {"mode": r["mode"], "min_seed_days": r.get("min_seed_days"), "min_ratio": r.get("min_ratio"), "source": r["match"]}
    return {**config.default_rule, "source": "default"}


def evaluate(r: Rule, t: Torrent) -> tuple[str, bool]:
    """(progress text, rule met) for torrent t under rule r."""
    mode = r["mode"]
    if mode == "never":
        return "protected: never deleted", False
    days, ratio = t["seeding_time"] / 86400, t["ratio"]
    need_days, need_ratio = r.get("min_seed_days"), r.get("min_ratio")
    need = [(f"seeded {days:.1f} of {need_days:g} days", days >= need_days)] if mode != "ratio" and need_days is not None else []
    need += [(f"ratio {ratio:.2f} of {need_ratio:g}", ratio >= need_ratio)] if mode != "days" and need_ratio is not None else []
    met = [ok for _, ok in need]
    done = t["progress"] >= 1 and (not met or (all(met) if mode == "both" else any(met)))
    return ", ".join(x for x, _ in need) or "no seeding rule", done


def met_parts(r: Rule, t: Torrent) -> JSON:
    """Which halves of the rule are met (None when a half doesn't apply), for the dashboard's meters."""
    days, ratio = r.get("min_seed_days"), r.get("min_ratio")
    return {"days_met": t["seeding_time"] / 86400 >= days if r["mode"] not in ("ratio", "never") and days is not None else None,
            "ratio_met": t["ratio"] >= ratio if r["mode"] not in ("days", "never") and ratio is not None else None}
