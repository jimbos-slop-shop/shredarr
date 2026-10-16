"""Saving what's changed in the web UI: checked, saved to config.json, written to the activity log."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from ..config import (
    ARR_KINDS,
    BEHAVIOR_DEFAULTS,
    CLIENTS,
    JSON,
    MEDIA_SERVERS,
    MODES,
    NOTIFY_DEFAULTS,
    OPTIONAL,
    RETENTION_UNITS,
    SECRET_KEYS,
    SERVICES,
    ConfigStore,
    Rule,
    api_url,
    kind_of,
)
from ..repositories import OverridesRepo
from ..text import plural
from .activity import Activity
from .rules import rule_text
from .saved_torrents import retention_text


class Invalid(ValueError):
    """What the user entered can't be saved. str() says why, for the UI."""


def number(value: Any) -> float | None:
    """A number from a form field; blank means none."""
    return float(value) if value not in (None, "") else None


class SettingsEditor:
    def __init__(
        self, config: ConfigStore, overrides: OverridesRepo, activity: Activity, sweep_soon: Callable[[], object]
    ) -> None:
        self.config = config
        self.overrides = overrides
        self.activity = activity
        self.sweep_soon = sweep_soon  # rules changed: check the waiting torrents again

    def default_rule(self, form: dict[str, str]) -> None:
        """The dashboard's default rule form (also switches dry run)."""
        mode = form.get("mode", "either")
        rules: Rule = {
            "mode": mode if mode in MODES else "either",
            "min_seed_days": number(form.get("min_seed_days", "").strip()),
            "min_ratio": number(form.get("min_ratio", "").strip()),
        }
        c = self.config.update(lambda raw: raw.update(rules=rules, dry_run="dry_run" in form))
        self.activity.info(
            f"Default rule saved: {rule_text(rules)}, {MODES[rules['mode']]} · "
            + ("dry run on" if c.dry_run else "live")
        )
        self.sweep_soon()

    def torrent_rule(self, hashes: list[str], mode: str, days: Any, ratio: Any, label: str) -> None:
        """Torrents' own rule; a mode that isn't one of MODES puts them back on the default. label names them in
        the log."""
        d, r = number(days) if days is not None else None, number(ratio) if ratio is not None else None
        for h in hashes:
            if mode in MODES:
                self.overrides.set(h, mode, d, r)
            else:
                self.overrides.remove(h)
        what = (
            MODES[mode]
            if mode == "never"
            else rule_text({"mode": mode, "min_seed_days": d, "min_ratio": r}) + f" ({MODES[mode]})"
            if mode in MODES
            else "back to default"
        )
        self.activity.info(f"rule for {label}: {what}")
        self.sweep_soon()

    def rule_list(self, rules: list[JSON]) -> None:
        out = []
        for r in rules:
            if (
                r.get("type") not in ("category", "title", "tracker")
                or not str(r.get("match", "")).strip()
                or r.get("mode") not in MODES
            ):
                continue
            out.append(
                {
                    "type": r["type"],
                    "match": str(r["match"]).strip(),
                    "mode": r["mode"],
                    "min_seed_days": number(r.get("min_seed_days")),
                    "min_ratio": number(r.get("min_ratio")),
                }
            )
        self.config.update(lambda raw: raw.update(rule_list=out))
        self.activity.info(f"Seeding rules saved: {plural(len(out), 'rule')}")
        self.sweep_soon()

    def behavior(self, f: JSON) -> None:
        bh = {k: bool(f.get(k, v)) for k, v in BEHAVIOR_DEFAULTS.items()}
        self.config.update(lambda raw: raw.update(behavior=bh))
        self.activity.info(
            "Cleanup behavior saved: "
            + ", ".join(f"{k.replace('_', ' ')} {'on' if v else 'off'}" for k, v in bh.items())
        )

    def low_space(self, f: JSON) -> None:
        try:
            ls = {"enabled": bool(f.get("enabled")), "min_free_gb": max(1.0, float(f.get("min_free_gb") or 100))}
        except ValueError as e:
            raise Invalid("Minimum free space should be a number.") from e
        self.config.update(lambda raw: raw.update(low_space=ls))
        self.activity.info(
            f"Low disk space override {'on' if ls['enabled'] else 'off'} (minimum {ls['min_free_gb']:g} GB free)"
        )
        self.sweep_soon()

    def grace(self, f: JSON) -> None:
        try:
            value = max(0.0, min(8760.0, float(f.get("value") or 0)))
        except ValueError as e:
            raise Invalid("The delay should be a number.") from e
        unit = "days" if f.get("unit") == "days" else "hours"
        self.config.update(lambda raw: raw.update(grace={"value": value, "unit": unit}))
        self.activity.info(
            "Deletion delay saved: "
            + (
                f"{value:g} {unit[:-1] if value == 1 else unit}"
                if value
                else "none (delete as soon as the rule is met)"
            )
        )
        self.sweep_soon()

    def path_maps(self, maps: list[JSON]) -> None:
        out = [
            {"remote": m["remote"].strip().rstrip("/") + "/", "local": m["local"].strip().rstrip("/") + "/"}
            for m in maps
            if m.get("remote", "").strip() and m.get("local", "").strip()
        ]
        self.config.update(lambda raw: raw.update(path_maps=out))
        self.activity.info(f"Path mappings saved: {len(out)}")

    def torrent_retention(self, f: JSON) -> JSON:
        """How long saved .torrent files are kept. Returns the saved setting."""
        unit = f.get("unit")
        if unit not in RETENTION_UNITS:
            raise Invalid("Choose days, months, years or forever.")
        try:
            value = float(f.get("value") or 0) if unit != "forever" else 1.0
        except ValueError as e:
            raise Invalid("How long to keep them should be a number.") from e
        if value <= 0:
            raise Invalid("Keep them for more than 0, or choose forever.")
        keep = {"value": value, "unit": unit}
        self.config.update(lambda raw: raw.update(torrent_retention=keep))
        self.activity.info(
            f"Saved .torrent files are kept {'forever' if unit == 'forever' else 'for ' + retention_text(value, unit)}"
        )
        return keep

    def save_torrents(self, enabled: bool) -> None:
        self.config.update(lambda raw: raw.update(save_torrents=enabled))
        self.activity.info("Saving .torrent files for Redownload: " + ("on" if enabled else "off"))

    def restore(self, enabled: bool) -> None:
        self.config.update(lambda raw: raw.update(restore={"enabled": enabled}))
        self.activity.info("Restoring RAR releases with Undo: " + ("on" if enabled else "off"))

    def update_check(self, enabled: bool) -> None:
        self.config.update(lambda raw: raw.update(update_check=enabled))

    def password_policy(self, f: JSON) -> None:
        try:
            p = {
                "min_length": max(1, min(128, int(f.get("min_length") or 1))),
                "upper": bool(f.get("upper")),
                "digit": bool(f.get("digit")),
                "symbol": bool(f.get("symbol")),
            }
        except ValueError as e:
            raise Invalid("Minimum length should be a number.") from e
        self.config.update(lambda raw: raw.update(auth_policy=p))
        needs = [f"at least {p['min_length']} characters"] + [
            n for k, n in (("upper", "uppercase"), ("digit", "number"), ("symbol", "symbol")) if p[k]
        ]
        self.activity.info(
            "Password rules saved: "
            + (
                ", ".join(needs)
                if p != {"min_length": 1, "upper": False, "digit": False, "symbol": False}
                else "no requirements"
            )
        )

    def notifications(self, n: JSON) -> None:
        saved: JSON = {
            k: (bool(n[k]) if isinstance(v, bool) else str(n[k]).strip()) for k, v in NOTIFY_DEFAULTS.items() if k in n
        }
        if not re.fullmatch(r"\d\d:\d\d", saved.get("summary_time", "09:00")):
            raise Invalid("Summary time should look like 09:00.")
        new = {**NOTIFY_DEFAULTS, **saved}
        if new["discord_url"] and not re.match(r"https://(discord|discordapp)\.com/api/webhooks/", new["discord_url"]):
            raise Invalid("That doesn't look like a Discord webhook URL.")
        self.config.update(lambda raw: raw.update(notify=saved))
        self.activity.info("Notification settings saved")

    def connections(self, f: JSON) -> None:
        """Settings → Connections. A blank API key or password keeps the saved one."""

        def change(c: JSON) -> None:
            if f.get("torrent_client") in CLIENTS:
                c["torrent_client"] = f["torrent_client"]
            if f.get("media_server") in MEDIA_SERVERS:
                c["media_server"] = f["media_server"]
            if isinstance(f.get("extra_arrs"), list):
                c["extra_arrs"] = self._extra_arrs(f["extra_arrs"], c.get("extra_arrs", []))
            for svc, conf in f.items():
                if svc not in SERVICES or not isinstance(conf, dict):
                    continue
                old = c.get(svc) or {}
                new = {k: str(v).strip() for k, v in conf.items() if k in ("url", "api_key", "username", "password")}
                if not new.get("url"):
                    if svc in OPTIONAL:
                        c.pop(svc, None)
                    continue
                for k in SECRET_KEYS:
                    if k in new and not new[k] and old.get(k):
                        new[k] = old[k]
                new["url"] = api_url(svc, new["url"])
                c[svc] = new

        self.config.update(change)
        self.activity.info("Connection settings saved")

    @staticmethod
    def _extra_arrs(wanted: list[JSON], saved: list[JSON]) -> list[JSON]:
        """Extra Sonarr/Radarr instances. Each gets a key like "radarr-4k" from its name, kept when renamed."""
        old, out, used = {x["key"]: x for x in saved}, [], set(ARR_KINDS)
        for x in wanted:
            kind, url = x.get("kind"), str(x.get("url") or "").strip()
            if kind not in ARR_KINDS or not url:
                continue
            name = str(x.get("name") or "").strip() or f"{SERVICES[kind]} 2"
            if x.get("key") in old and kind_of(x["key"]) == kind:
                key = x["key"]
            else:
                slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
                key = kind + "-" + (re.sub(r"^" + kind + "-?", "", slug) or "2")
            while key in used:
                key += "-2"
            used.add(key)
            out.append(
                {
                    "key": key,
                    "kind": kind,
                    "name": name,
                    "url": api_url(kind, url),
                    "api_key": str(x.get("api_key") or "").strip() or old.get(key, {}).get("api_key", ""),
                }
            )
        return out
