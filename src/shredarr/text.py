"""How things are written for people: sizes, durations, episode ranges, rules."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .config import Rule


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.1f} {u}" if u in ("GB", "TB") else f"{n:.0f} {u}"
        n /= 1024
    raise AssertionError("unreachable")


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' * (n != 1)}"


def duration(sec: float) -> str:
    sec = max(0, int(sec))
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{max(m, 1)}m"


def episodes_label(eps: Iterable[dict[str, Any]]) -> str:
    """Sonarr episodes -> "S01E01-E08 (8 episodes), S02E03"."""
    eps = list(eps)
    out = []
    for sn in sorted({e["seasonNumber"] for e in eps}):
        nums = sorted(e["episodeNumber"] for e in eps if e["seasonNumber"] == sn)
        out.append(f"S{sn:02d}E{nums[0]:02d}" + (f"-E{nums[-1]:02d} ({len(nums)} episodes)" if len(nums) > 1 else ""))
    return ", ".join(out)


def episodes_text(eps: Iterable[Iterable[int]]) -> str:
    """[[season, episode], ...] -> episodes_label."""
    return episodes_label([{"seasonNumber": s, "episodeNumber": e} for s, e in (tuple(x) for x in eps)])


def rule_text(r: Rule) -> str:
    """A seeding rule in a few words: "14 days or ratio 2"."""
    if r.get("mode") == "never":
        return "never delete"
    days, ratio = r.get("min_seed_days"), r.get("min_ratio")
    p = ([f"{days:g} days"] if r["mode"] != "ratio" and days is not None else []) + \
        ([f"ratio {ratio:g}"] if r["mode"] != "days" and ratio is not None else [])
    return (" and " if r["mode"] == "both" else " or ").join(p) or "no rule"


def tail(path: str) -> str:
    """Last two path parts (folder/file). Matches the same file in Jellyfin and here even when mounted at different paths."""
    return "/".join(path.replace("\\", "/").rstrip("/").split("/")[-2:])
