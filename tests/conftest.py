# tests/conftest.py
import pytest
from fm.config import Config

@pytest.fixture
def cfg():
    return Config(printer_ip="192.168.0.101", ssh_user="root",
                  ssh_password="pw", db_remote_path="/mnt/UDISK/x.json",
                  ws_port=9999, version_override=9876543210,
                  id_range_start=99001, agent_token="testtoken")
