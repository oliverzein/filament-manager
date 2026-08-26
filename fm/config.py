# fm/config.py
import os
from dataclasses import dataclass
from typing import Mapping
from .errors import FmError

@dataclass(frozen=True)
class Config:
    printer_ip: str; ssh_user: str; ssh_password: str; db_remote_path: str
    ws_port: int = 9999; version_override: int = 9876543210
    id_range_start: int = 99001; agent_token: str = ""
_REQUIRED = ("FM_PRINTER_IP", "FM_SSH_USER", "FM_SSH_PASSWORD",
             "FM_DB_REMOTE_PATH", "FM_AGENT_TOKEN")

def load_config(env: Mapping[str, str] = os.environ) -> Config:
    missing = [k for k in _REQUIRED if not env.get(k)]
    if missing:
        raise FmError(1, f"missing required env vars: {', '.join(missing)}")
    return Config(
        printer_ip=env["FM_PRINTER_IP"], ssh_user=env["FM_SSH_USER"],
        ssh_password=env["FM_SSH_PASSWORD"],
        db_remote_path=env.get("FM_DB_REMOTE_PATH",
            "/mnt/UDISK/creality/userdata/box/material_database.json"),
        ws_port=int(env.get("FM_WS_PORT", "9999")),
        version_override=int(env.get("FM_VERSION_OVERRIDE", "9876543210")),
        id_range_start=int(env.get("FM_ID_RANGE_START", "99001")),
        agent_token=env.get("FM_AGENT_TOKEN", ""))
