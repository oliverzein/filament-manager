# fm/agent/cli.py
"""fm-agent — companion agent CLI that drives the agent job queue.

Polls the filament-manager API (``GET /api/agent/jobs`` with a Bearer agent
token), executes the claimed job against the local OrcaSlicer user tree, and
posts the outcome back to ``POST /api/agent/results``.

Subcommands
-----------
* ``poll [--base-url URL] [--token TOK]`` — one shot: claim and process at most
  one queued job, then exit.
* ``watch`` — poll loop every ``POLL_INTERVAL`` seconds (v1 simple loop).

Configuration
-------------
Agent credentials live in ``~/.config/filament-manager/agent.json`` as
``{"base_url": ..., "token": ...}``. ``--base-url`` / ``--token`` override the
file. ``FM_ORCASLICER_CONFIG_DIR`` overrides OrcaSlicer config-dir discovery
(otherwise ``find_orca_user_dir`` falls back to its default
``~/.config/OrcaSlicer``).

Job handling
------------
* ``apply_preset`` — refuse (post an ``error`` result) when
  :func:`is_orcaslicer_running` is true; otherwise write ``<name>.json`` and
  ``<name>.info`` into the first OrcaSlicer user filament dir and post ``done``.
* ``report_presets`` — scan the *first* user filament dir only (Global
  Constraint 10) and post ``done`` with ``{"presets": [...]}``. Each preset
  carries the human-facing fields ``{name, filament_type, filament_vendor,
  nozzle_temperature, nozzle_temperature_initial_layer, inherits, from}`` plus
  the internal ``_path`` / ``_system`` / ``filament_id`` keys that
  :func:`fm.presets.matching.temp_sync_targets` needs to match and read temps —
  so the stored result is directly consumable by the temp-sync handler.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

from fm.presets.matching import find_orca_user_dir, is_orcaslicer_running

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AGENT_CONFIG_DIR = Path.home() / ".config" / "filament-manager"
AGENT_CONFIG_FILE = AGENT_CONFIG_DIR / "agent.json"

POLL_INTERVAL = 60  # seconds (watch loop)


def load_config(base_url: str | None = None, token: str | None = None) -> tuple[str, str]:
    """Resolve agent credentials.

    ``--base-url`` / ``--token`` win over the config file; both are required.
    """
    cfg: dict = {}
    if AGENT_CONFIG_FILE.exists():
        try:
            cfg = json.loads(AGENT_CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            cfg = {}
    base_url = base_url or cfg.get("base_url")
    token = token or cfg.get("token")
    if not base_url or not token:
        raise SystemExit(
            "fm-agent: base_url and token are required "
            "(set in ~/.config/filament-manager/agent.json or pass --base-url/--token)"
        )
    return base_url, token


def _orca_config_dir() -> str | None:
    """Return the OrcaSlicer config dir override, or None for the default."""
    env = os.environ.get("FM_ORCASLICER_CONFIG_DIR")
    return env if env else None


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

def _get_jobs(base_url: str, token: str) -> list[dict]:
    r = requests.get(
        f"{base_url}/api/agent/jobs",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("jobs", [])


def _post_result(
    base_url: str, token: str, job_id: int, status: str, result: dict
) -> dict:
    r = requests.post(
        f"{base_url}/api/agent/results",
        headers={"Authorization": f"Bearer {token}"},
        json={"job_id": job_id, "status": status, "result": result},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Job handlers
# ---------------------------------------------------------------------------

def _handle_apply_preset(job: dict) -> tuple[str, dict]:
    """Write preset JSON + .info into the first user filament dir.

    Returns ``(status, result)`` to post back. Refuses with an error result
    when OrcaSlicer is running.
    """
    if is_orcaslicer_running():
        return "error", {"error": "OrcaSlicer running"}

    payload = job.get("payload") or {}
    name = payload.get("name")
    preset = payload.get("preset")
    info = payload.get("info", "")
    if not name or preset is None:
        return "error", {"error": "apply_preset payload missing name or preset"}

    filament_dir = find_orca_user_dir(_orca_config_dir())
    if filament_dir is None:
        return "error", {"error": "no OrcaSlicer user filament directory found"}
    filament_dir.mkdir(parents=True, exist_ok=True)

    (filament_dir / f"{name}.json").write_text(json.dumps(preset, indent=2))
    (filament_dir / f"{name}.info").write_text(info if info is not None else "")
    return "done", {"name": name, "files": [f"{name}.json", f"{name}.info"]}


def _preset_report(d: dict, path: Path) -> dict:
    """Build a report-presets entry consumable by temp_sync_targets.

    Human-facing fields per the brief plus the internal ``_path`` / ``_system``
    / ``filament_id`` keys that :func:`match_presets` and
    :func:`temp_sync_targets` require.
    """
    return {
        "name": d.get("name", ""),
        "filament_type": d.get("filament_type", ""),
        "filament_vendor": d.get("filament_vendor", ""),
        "nozzle_temperature": d.get("nozzle_temperature"),
        "nozzle_temperature_initial_layer": d.get("nozzle_temperature_initial_layer"),
        "inherits": d.get("inherits", ""),
        "from": "user",
        "filament_id": d.get("filament_id", ""),
        "_path": str(path),
        "_system": False,
        "pressure_advance": d.get("pressure_advance"),
        "enable_pressure_advance": d.get("enable_pressure_advance"),
        "filament_flow_ratio": d.get("filament_flow_ratio"),
        "filament_max_volumetric_speed": d.get("filament_max_volumetric_speed"),
        "filament_retraction_length": d.get("filament_retraction_length"),
        "filament_retraction_speed": d.get("filament_retraction_speed"),
        "fan_min_speed": d.get("fan_min_speed"),
        "fan_max_speed": d.get("fan_max_speed"),
        "cool_plate_temp": d.get("cool_plate_temp"),
        "eng_plate_temp": d.get("eng_plate_temp"),
        "hot_plate_temp": d.get("hot_plate_temp"),
    }


def _handle_report_presets(job: dict) -> tuple[str, dict]:
    """Scan the first user filament dir only and report its presets."""
    filament_dir = find_orca_user_dir(_orca_config_dir())
    if filament_dir is None or not filament_dir.is_dir():
        return "done", {"presets": []}

    presets: list[dict] = []
    for p in sorted(filament_dir.glob("*.json")):
        try:
            d = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if d.get("type", "filament") != "filament":
            continue
        presets.append(_preset_report(d, p))
    return "done", {"presets": presets}


_AGENT_HANDLERS = {
    "apply_preset": _handle_apply_preset,
    "report_presets": _handle_report_presets,
}


def _process_job(base_url: str, token: str, job: dict) -> None:
    """Dispatch one claimed job to its handler and post the result back."""
    kind = job.get("kind")
    handler = _AGENT_HANDLERS.get(kind)
    if handler is None:
        # Unknown kind: post an error so the job is not left running forever.
        _post_result(base_url, token, job["job_id"], "error",
                     {"error": f"unknown job kind: {kind!r}"})
        return
    status, result = handler(job)
    _post_result(base_url, token, job["job_id"], status, result)


def poll_once(base_url: str, token: str) -> int:
    """Claim and process at most one queued job. Returns 0 on success."""
    jobs = _get_jobs(base_url, token)
    for job in jobs:
        _process_job(base_url, token, job)
    return 0


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_poll(args: argparse.Namespace) -> int:
    base_url, token = load_config(args.base_url, args.token)
    return poll_once(base_url, token)


def cmd_watch(args: argparse.Namespace) -> int:
    base_url, token = load_config(args.base_url, args.token)
    interval = args.interval
    while True:
        try:
            poll_once(base_url, token)
        except Exception as exc:  # noqa: BLE001 — watch must survive errors
            print(f"fm-agent: poll error: {exc}", file=sys.stderr)
        time.sleep(interval)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fm-agent", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)

    def add_conn(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--base-url", help="API base URL (overrides agent.json)")
        sp.add_argument("--token", help="agent bearer token (overrides agent.json)")

    sp_poll = sub.add_parser("poll", help="claim and process one queued job, then exit")
    add_conn(sp_poll)
    sp_poll.set_defaults(func=cmd_poll)

    sp_watch = sub.add_parser("watch", help="poll loop every 60 s")
    add_conn(sp_watch)
    sp_watch.add_argument("--interval", type=int, default=POLL_INTERVAL,
                          help=f"poll interval in seconds (default {POLL_INTERVAL})")
    sp_watch.set_defaults(func=cmd_watch)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
