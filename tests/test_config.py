# tests/test_config.py
import pytest
from fm.config import load_config
from fm.errors import FmError

BASE = {"FM_PRINTER_IP": "192.168.0.101", "FM_SSH_USER": "root",
        "FM_SSH_PASSWORD": "pw", "FM_DB_REMOTE_PATH": "/mnt/UDISK/x.json"}

def test_loads_with_defaults():
    c = load_config(BASE)
    assert c.ws_port == 9999 and c.version_override == 9876543210
    assert c.id_range_start == 99001
    assert c.agent_token == ""  # optional, defaults to empty

def test_overrides():
    c = load_config({**BASE, "FM_WS_PORT": "8888"})
    assert c.ws_port == 8888

def test_agent_token_optional():
    """FM_AGENT_TOKEN is optional — omitted → empty string default."""
    c = load_config(BASE)
    assert c.agent_token == ""

def test_agent_token_when_provided():
    """FM_AGENT_TOKEN when provided is stored."""
    c = load_config({**BASE, "FM_AGENT_TOKEN": "mytoken"})
    assert c.agent_token == "mytoken"

@pytest.mark.parametrize("missing", list(BASE))
def test_missing_required_fails(missing):
    env = {k: v for k, v in BASE.items() if k != missing}
    with pytest.raises(FmError):
        load_config(env)
