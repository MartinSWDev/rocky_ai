"""Shared configuration + secret loading for Rocky.

A single source of truth used by both the server (PC) and the client (Mac):

  * non-secret settings come from ``config.yaml`` (path overridable with the
    ``ROCKY_CONFIG`` env var, otherwise next to this file);
  * secrets (Jira token) come from a ``.env`` file or the process environment.

Importing this module never touches the network and has only one third-party
dependency (PyYAML), so the server stays light.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover - import guard
    raise SystemExit(
        "PyYAML is required. Install deps with:\n"
        "  pip install -r requirements-server.txt   (PC)\n"
        "  pip install -r requirements-client.txt   (Mac)"
    ) from exc

_HERE = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no external dependency). Real env vars win."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def expand(path: str | None) -> str | None:
    """Expand ``~`` and ``$VARS`` in a path string."""
    if not path:
        return path
    return os.path.expanduser(os.path.expandvars(path))


class Config:
    """Thin wrapper over the parsed YAML giving dotted-path access."""

    def __init__(self, data: dict, source: Path):
        self._data = data
        self.source = source

    def get(self, dotted: str, default=None):
        node = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted: str):
        val = self.get(dotted, _MISSING)
        if val is _MISSING:
            raise SystemExit(f"Missing required config key: {dotted} (in {self.source})")
        return val


_MISSING = object()


def load_config() -> Config:
    """Load .env then config.yaml and return a :class:`Config`."""
    _load_dotenv(_HERE / ".env")
    cfg_path = Path(os.environ.get("ROCKY_CONFIG", _HERE / "config.yaml"))
    if not cfg_path.exists():
        example = _HERE / "config.example.yaml"
        raise SystemExit(
            f"Config not found at {cfg_path}.\n"
            f"Create it from the example:\n"
            f"  cp {example} {cfg_path}"
        )
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    return Config(data, cfg_path)
