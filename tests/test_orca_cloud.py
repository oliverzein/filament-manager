# tests/test_orca_cloud.py
"""OrcaCloudClient tests — mocked requests, no real API calls."""
from __future__ import annotations

import base64
import json
from unittest.mock import patch, MagicMock

import pytest

from fm.orca_cloud import OrcaCloudClient, OrcaCloudConflict, OrcaCloudError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_jwt(payload: dict) -> str:
    """Create a fake JWT with the given payload (no signature)."""
    header = base64.urlsafe_b64encode(
        b'{"alg":"HS256","typ":"JWT"}'
    ).rstrip(b'=').decode()
    body = base64.urlsafe_b64encode(
        json.dumps(payload).encode()
    ).rstrip(b'=').decode()
    return f"{header}.{body}.fake-signature"


def _mock_response(status_code=200, json_data=None, text=""):
    """Create a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = status_code < 400
    resp.text = text or json.dumps(json_data or {})
    resp.json.return_value = json_data or {}
    return resp


# ---------------------------------------------------------------------------
# _refresh
# ---------------------------------------------------------------------------

class TestRefresh:
    def test_extracts_access_token(self):
        jwt = _make_jwt({"sub": "user-uuid-123"})
        resp = _mock_response(200, {
            "access_token": jwt,
            "refresh_token": "new-refresh",
        })
        client = OrcaCloudClient(refresh_token="old-refresh")
        with patch("fm.orca_cloud.requests.post", return_value=resp):
            token = client._refresh()
        assert token == jwt

    def test_calls_on_token_rotated(self):
        jwt = _make_jwt({"sub": "user-uuid-123"})
        resp = _mock_response(200, {
            "access_token": jwt,
            "refresh_token": "new-refresh",
        })
        rotated: list[str] = []
        client = OrcaCloudClient(
            refresh_token="old-refresh",
            on_token_rotated=lambda t: rotated.append(t),
        )
        with patch("fm.orca_cloud.requests.post", return_value=resp):
            client._refresh()
        assert rotated == ["new-refresh"]

    def test_no_callback_when_token_unchanged(self):
        jwt = _make_jwt({"sub": "user-uuid-123"})
        resp = _mock_response(200, {
            "access_token": jwt,
            "refresh_token": "old-refresh",
        })
        rotated: list[str] = []
        client = OrcaCloudClient(
            refresh_token="old-refresh",
            on_token_rotated=lambda t: rotated.append(t),
        )
        with patch("fm.orca_cloud.requests.post", return_value=resp):
            client._refresh()
        assert rotated == []

    def test_raises_on_non_200(self):
        resp = _mock_response(401, text="Unauthorized")
        client = OrcaCloudClient(refresh_token="bad-token")
        with patch("fm.orca_cloud.requests.post", return_value=resp):
            with pytest.raises(OrcaCloudError) as exc:
                client._refresh()
        assert exc.value.status == 401


# ---------------------------------------------------------------------------
# sync_push
# ---------------------------------------------------------------------------

class TestSyncPush:
    def test_sends_correct_body(self):
        jwt = _make_jwt({"sub": "43d7af43-fe15-4d11-be6b-59a88db20355"})
        refresh_resp = _mock_response(200, {
            "access_token": jwt, "refresh_token": "new-refresh",
        })
        push_resp = _mock_response(200, {
            "updated_time": 12345, "created_time": 12345,
        })
        client = OrcaCloudClient(refresh_token="refresh")
        with patch("fm.orca_cloud.requests.post", return_value=refresh_resp), \
             patch("fm.orca_cloud.requests.request", return_value=push_resp) as mock_req:
            result = client.sync_push(
                profile_id="Pabc123",
                name="Test Profile",
                content={"name": "Test Profile", "filament_type": "PLA"},
            )
        assert result["updated_time"] == 12345
        assert result["created_time"] == 12345
        call_args = mock_req.call_args
        assert call_args[0][0] == "POST"
        assert "sync/push" in call_args[0][1]
        json_body = call_args[1]["json"]
        assert json_body["id"] == "cad4d71f-3bca-51e4-9d81-8c6a6eeb82b8"
        assert json_body["name"] == "Test Profile"
        assert json_body["content"]["filament_type"] == "PLA"
        assert "original_updated_time" not in json_body

    def test_includes_original_updated_time_for_updates(self):
        jwt = _make_jwt({"sub": "43d7af43-fe15-4d11-be6b-59a88db20355"})
        refresh_resp = _mock_response(200, {
            "access_token": jwt, "refresh_token": "new-refresh",
        })
        push_resp = _mock_response(200, {"updated_time": 99999, "created_time": 12345})
        client = OrcaCloudClient(refresh_token="refresh")
        with patch("fm.orca_cloud.requests.post", return_value=refresh_resp), \
             patch("fm.orca_cloud.requests.request", return_value=push_resp) as mock_req:
            client.sync_push(
                profile_id="Pabc123",
                name="Test Profile",
                content={"name": "Test Profile"},
                original_updated_time="12345",
            )
        json_body = mock_req.call_args[1]["json"]
        assert json_body["original_updated_time"] == "12345"

    def test_raises_conflict_on_409(self):
        jwt = _make_jwt({"sub": "43d7af43-fe15-4d11-be6b-59a88db20355"})
        refresh_resp = _mock_response(200, {
            "access_token": jwt, "refresh_token": "new-refresh",
        })
        server_profile = {"id": "Pabc123", "name": "Test", "updated_time": 99999}
        conflict_resp = _mock_response(409, {"server_profile": server_profile})
        client = OrcaCloudClient(refresh_token="refresh")
        with patch("fm.orca_cloud.requests.post", return_value=refresh_resp), \
             patch("fm.orca_cloud.requests.request", return_value=conflict_resp):
            with pytest.raises(OrcaCloudConflict) as exc:
                client.sync_push("Pabc123", "Test", {"name": "Test"})
        assert exc.value.server_profile == server_profile

    def test_raises_error_on_500(self):
        jwt = _make_jwt({"sub": "43d7af43-fe15-4d11-be6b-59a88db20355"})
        refresh_resp = _mock_response(200, {
            "access_token": jwt, "refresh_token": "new-refresh",
        })
        error_resp = _mock_response(500, text="Internal Server Error")
        client = OrcaCloudClient(refresh_token="refresh")
        with patch("fm.orca_cloud.requests.post", return_value=refresh_resp), \
             patch("fm.orca_cloud.requests.request", return_value=error_resp):
            with pytest.raises(OrcaCloudError) as exc:
                client.sync_push("Pabc123", "Test", {"name": "Test"})
        assert exc.value.status == 500


# ---------------------------------------------------------------------------
# sync_pull
# ---------------------------------------------------------------------------

class TestSyncPull:
    def test_returns_upserts_and_deletes(self):
        jwt = _make_jwt({"sub": "43d7af43-fe15-4d11-be6b-59a88db20355"})
        refresh_resp = _mock_response(200, {
            "access_token": jwt, "refresh_token": "new-refresh",
        })
        pull_data = {
            "next_cursor": 42,
            "upserts": [{"id": "P1", "name": "P1", "content": {"name": "P1"},
                         "updated_time": "100"}],
            "deletes": ["P2"],
        }
        pull_resp = _mock_response(200, pull_data)
        client = OrcaCloudClient(refresh_token="refresh")
        with patch("fm.orca_cloud.requests.post", return_value=refresh_resp), \
             patch("fm.orca_cloud.requests.request", return_value=pull_resp) as mock_req:
            result = client.sync_pull(cursor=10)
        assert result["next_cursor"] == 42
        assert len(result["upserts"]) == 1
        assert result["deletes"] == ["P2"]
        assert mock_req.call_args[1]["params"] == {"cursor": 10}

    def test_410_triggers_full_resync(self):
        jwt = _make_jwt({"sub": "43d7af43-fe15-4d11-be6b-59a88db20355"})
        refresh_resp = _mock_response(200, {
            "access_token": jwt, "refresh_token": "new-refresh",
        })
        gone_resp = _mock_response(410, text="Gone")
        ok_resp = _mock_response(200, {"next_cursor": 0, "upserts": [], "deletes": []})
        client = OrcaCloudClient(refresh_token="refresh")
        with patch("fm.orca_cloud.requests.post", return_value=refresh_resp), \
             patch("fm.orca_cloud.requests.request",
                   side_effect=[gone_resp, ok_resp]) as mock_req:
            result = client.sync_pull(cursor=99)
        assert result["next_cursor"] == 0
        first_call = mock_req.call_args_list[0]
        second_call = mock_req.call_args_list[1]
        assert first_call[1]["params"] == {"cursor": 99}
        assert second_call[1]["params"] is None

    def test_no_cursor_omits_param(self):
        jwt = _make_jwt({"sub": "43d7af43-fe15-4d11-be6b-59a88db20355"})
        refresh_resp = _mock_response(200, {
            "access_token": jwt, "refresh_token": "new-refresh",
        })
        pull_resp = _mock_response(200, {"next_cursor": 0, "upserts": [], "deletes": []})
        client = OrcaCloudClient(refresh_token="refresh")
        with patch("fm.orca_cloud.requests.post", return_value=refresh_resp), \
             patch("fm.orca_cloud.requests.request", return_value=pull_resp) as mock_req:
            client.sync_pull()
        assert mock_req.call_args[1]["params"] is None


# ---------------------------------------------------------------------------
# sync_delete
# ---------------------------------------------------------------------------

class TestSyncDelete:
    def test_sends_correct_body(self):
        jwt = _make_jwt({"sub": "43d7af43-fe15-4d11-be6b-59a88db20355"})
        refresh_resp = _mock_response(200, {
            "access_token": jwt, "refresh_token": "new-refresh",
        })
        delete_resp = _mock_response(200, {"status": "ok", "deleted": 2})
        client = OrcaCloudClient(refresh_token="refresh")
        with patch("fm.orca_cloud.requests.post", return_value=refresh_resp), \
             patch("fm.orca_cloud.requests.request", return_value=delete_resp) as mock_req:
            result = client.sync_delete(["P1", "P2"])
        assert result["deleted"] == 2
        call_args = mock_req.call_args
        assert call_args[0][0] == "DELETE"
        assert "sync/delete" in call_args[0][1]
        assert call_args[1]["params"] == {"resource": "profiles"}
        assert call_args[1]["json"] == {"ids": ["P1", "P2"]}


# ---------------------------------------------------------------------------
# get_user_id
# ---------------------------------------------------------------------------

class TestGetUserId:
    def test_decodes_jwt_sub(self):
        user_uuid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        jwt = _make_jwt({"sub": user_uuid})
        refresh_resp = _mock_response(200, {
            "access_token": jwt, "refresh_token": "new-refresh",
        })
        client = OrcaCloudClient(refresh_token="refresh")
        with patch("fm.orca_cloud.requests.post", return_value=refresh_resp):
            uid = client.get_user_id()
        assert uid == user_uuid

    def test_raises_on_malformed_token(self):
        refresh_resp = _mock_response(200, {
            "access_token": "justonepart",
            "refresh_token": "new-refresh",
        })
        client = OrcaCloudClient(refresh_token="refresh")
        with patch("fm.orca_cloud.requests.post", return_value=refresh_resp):
            with pytest.raises(OrcaCloudError, match="malformed"):
                client.get_user_id()

    def test_raises_on_no_sub_claim(self):
        jwt = _make_jwt({"other": "claim"})
        refresh_resp = _mock_response(200, {
            "access_token": jwt, "refresh_token": "new-refresh",
        })
        client = OrcaCloudClient(refresh_token="refresh")
        with patch("fm.orca_cloud.requests.post", return_value=refresh_resp):
            with pytest.raises(OrcaCloudError, match="no sub"):
                client.get_user_id()


# ---------------------------------------------------------------------------
# is_connected
# ---------------------------------------------------------------------------

class TestIsConnected:
    def test_connected_with_token(self):
        client = OrcaCloudClient(refresh_token="some-token")
        assert client.is_connected() is True

    def test_not_connected_without_token(self):
        client = OrcaCloudClient(refresh_token="")
        assert client.is_connected() is False

# ---------------------------------------------------------------------------
# login_with_password
# ---------------------------------------------------------------------------

class TestLoginWithPassword:
    def test_returns_refresh_token_and_user_id(self):
        jwt = _make_jwt({"sub": "user-uuid-456"})
        resp = _mock_response(200, {
            "access_token": jwt,
            "refresh_token": "fresh-refresh-token",
        })
        with patch("fm.orca_cloud.requests.post", return_value=resp):
            refresh, user_id = OrcaCloudClient.login_with_password(
                "user@example.com", "password123"
            )
        assert refresh == "fresh-refresh-token"
        assert user_id == "user-uuid-456"

    def test_raises_on_bad_credentials(self):
        resp = _mock_response(401, {"error": "Invalid credentials"})
        with patch("fm.orca_cloud.requests.post", return_value=resp):
            with pytest.raises(OrcaCloudError, match="login failed"):
                OrcaCloudClient.login_with_password("bad", "creds")

