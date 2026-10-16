from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from shredarr import __version__
from tests.fakes.lab import Lab
from tests.harness import Shredarr

GOLDEN = os.path.join(os.path.dirname(__file__), "golden")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--update-goldens", action="store_true", help="rewrite tests/golden from this run")


@pytest.fixture
def lab(tmp_path: Any) -> Iterator[Lab]:
    lab = Lab(str(tmp_path / "lab"))
    yield lab
    lab.stop()


StartFn = Callable[..., Shredarr]


@pytest.fixture
def start(lab: Lab, tmp_path: Any) -> Iterator[StartFn]:
    """start(**config overrides) -> a running Shredarr using the lab's apps. start(config=None) starts unconfigured."""
    running: list[Shredarr] = []

    def _start(
        config: dict[str, Any] | bool | None = True, env: dict[str, str] | None = None, **overrides: Any
    ) -> Shredarr:
        conf = lab.config(**overrides) if config is True else config or None
        s = Shredarr(str(tmp_path / f"shredarr{len(running)}"), conf, env)
        running.append(s)
        s.start()
        if conf and conf.get("webhook_token") and lab.jellyfin.webhook_target is None:
            lab.jellyfin.webhook_target = (s.base + "/webhook/jellyfin", conf["webhook_token"])
        return s

    yield _start
    for s in running:
        s.stop()


# ---------------------------------------------------------------------- golden files

TIME_KEYS = {"added_on", "ts", "delete_after", "redownloaded", "expires", "created"}
SHREDARR_VERSIONS = {"1.2.1", __version__}  # (the expected output was first recorded from 1.2.1)


def placeholder(key: str, value: Any) -> Any:
    if key in TIME_KEYS and isinstance(value, (int, float)):
        return "<TS>"
    if key == "free_gb" and isinstance(value, (int, float)):
        return "<GB>"
    if key in ("version", "latest") and value in SHREDARR_VERSIONS:
        return "<VERSION>"
    return drop_times(value)


def drop_times(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: placeholder(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [drop_times(v) for v in value]
    return value


def normalize(value: Any, lab: Lab | None = None) -> Any:
    """Make output comparable across runs: times, lab paths, fake app ports and versions become placeholders."""
    text = json.dumps(drop_times(value), sort_keys=True)
    if lab:
        for f in lab.fakes:
            text = text.replace(f.url, f"http://{f.name}")
        text = text.replace(lab.root, "<LAB>")
    return json.loads(text)


@pytest.fixture
def golden(request: pytest.FixtureRequest, lab: Lab) -> Callable[[str, Any], None]:
    """golden(label, data): compare data with tests/golden/<module>/<test>__<label>.json
    (written with --update-goldens)."""
    update = request.config.getoption("--update-goldens")
    module = request.node.module.__name__.rsplit(".", 1)[-1]
    test = re.sub(r"\W+", "_", request.node.name)

    def check(label: str, data: Any) -> None:
        path = os.path.join(GOLDEN, module, f"{test}__{label}.json")
        data = normalize(data, lab)
        if update:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                json.dump(data, f, indent=2, sort_keys=True)
                f.write("\n")
            return
        if not os.path.exists(path):
            pytest.fail(f"no golden file {os.path.relpath(path)}; run with --update-goldens and review the result")
        with open(path) as f:
            assert data == json.load(f), f"differs from {os.path.relpath(path)}"

    return check
