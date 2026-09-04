# fm/orca_cloud.py
"""Direct Orca Cloud Sync API client.

Auth is via a Supabase refresh token.  The token rotates on every refresh;
the caller must persist the new token via the ``on_token_rotated`` callback.
"""
from __future__ import annotations

import base64
import json
import uuid
from typing import Callable

import requests


class OrcaCloudError(Exception):
    """Orca Cloud API error with status code and response body."""

    def __init__(self, status: int, body: str, message: str = ""):
        self.status = status
        self.body = body
        self.message = message or body
        super().__init__(self.message)


class OrcaCloudConflict(OrcaCloudError):
    """409 conflict — ``server_profile`` in body has the current version."""

    def __init__(self, body: str, server_profile: dict | None):
        super().__init__(409, body, "timestamp conflict")
        self.server_profile = server_profile


class OrcaCloudClient:
    """Direct Orca Cloud Sync API client.

    Auth via Supabase refresh token.  Token rotates on every refresh;
    caller must persist the new token via ``on_token_rotated`` callback.
    """

    API_BASE = "https://api.orcaslicer.com"
    AUTH_BASE = "https://auth.orcaslicer.com"
    ANON_KEY = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImttYXVqanhlcXJxdW5nb25jcXp2Iiwic"
        "m9sZSI6ImFub24iLCJpYXQiOjE3NTk3ODk4NzAsImV4cCI6MjA3NTM2NTg3MH0."
        "-ChNHK2t0Fbsi8opS2nFse7zxJKpPtvYWqG15sbE908"
    )
    MAX_PAYLOAD = 1048576  # 1 MB

    def __init__(
        self,
        refresh_token: str,
        on_token_rotated: Callable[[str], None] | None = None,
    ):
        """Store refresh token; callback called when token rotates."""
        self._refresh_token = refresh_token
        self._on_token_rotated = on_token_rotated
        self._access_token: str | None = None

    @classmethod
    def login_with_password(cls, email: str, password: str) -> tuple[str, str]:
        """Log in with email+password, returning (refresh_token, user_id).

        Calls Supabase's password grant endpoint.  No OrcaCloudClient
        instance needed — this is a classmethod that returns the raw
        refresh token for storage.
        """
        resp = requests.post(
            f"{cls.AUTH_BASE}/auth/v1/token?grant_type=password",
            headers={"apikey": cls.ANON_KEY, "Content-Type": "application/json"},
            json={"email": email, "password": password},
            timeout=30,
        )
        if resp.status_code != 200:
            raise OrcaCloudError(
                resp.status_code,
                resp.text,
                f"login failed: {resp.status_code}",
            )
        data = resp.json()
        refresh_token = data["refresh_token"]
        # Decode user_id from the access token JWT payload.
        access_token = data["access_token"]
        parts = access_token.split(".")
        if len(parts) >= 2:
            padding = "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
            user_id = payload.get("sub", "")
        else:
            user_id = ""
        return refresh_token, user_id

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def is_connected(self) -> bool:
        """Check if the client has valid credentials."""
        return bool(self._refresh_token)

    def _refresh(self) -> str:
        """Exchange refresh token for access token.

        Persist new refresh token via callback.  Returns the access token.
        """
        resp = requests.post(
            f"{self.AUTH_BASE}/auth/v1/token?grant_type=refresh_token",
            headers={"apikey": self.ANON_KEY, "Content-Type": "application/json"},
            json={"refresh_token": self._refresh_token},
            timeout=30,
        )
        if resp.status_code != 200:
            raise OrcaCloudError(
                resp.status_code,
                resp.text,
                f"token refresh failed: {resp.status_code}",
            )
        data = resp.json()
        self._access_token = data["access_token"]
        new_refresh = data["refresh_token"]
        if new_refresh and new_refresh != self._refresh_token:
            self._refresh_token = new_refresh
            if self._on_token_rotated:
                self._on_token_rotated(new_refresh)
        return self._access_token

    def _headers(self) -> dict:
        """Return auth headers for API requests."""
        if self._access_token is None:
            self._refresh()
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> requests.Response:
        """Make an authenticated request, refreshing once on 401."""
        resp = requests.request(
            method,
            url,
            headers=self._headers(),
            json=json_body,
            params=params,
            timeout=30,
        )
        if resp.status_code == 401:
            # Token expired — force refresh and retry once.
            self._access_token = None
            resp = requests.request(
                method,
                url,
                headers=self._headers(),
                json=json_body,
                params=params,
                timeout=30,
            )
        return resp

    # ------------------------------------------------------------------
    # Sync API
    # ------------------------------------------------------------------

    # Fields stored as arrays in local format but as flat strings in Orca Cloud.
    _ARRAY_FIELDS = frozenset({
        "filament_type", "filament_vendor", "nozzle_temperature",
        "nozzle_temperature_initial_layer", "pressure_advance",
        "enable_pressure_advance", "filament_flow_ratio",
        "filament_max_volumetric_speed", "filament_retraction_length",
        "filament_retraction_speed", "fan_min_speed", "fan_max_speed",
        "cool_plate_temp", "eng_plate_temp", "hot_plate_temp",
        "nozzle_temperature_range_low", "nozzle_temperature_range_high",
        "default_filament_colour", "filament_settings_id",
    })

    def cloud_id(self, filament_id: str) -> str:
        """Generate the Orca Cloud resource UUID from user_id + filament_id.

        This is the equivalent of OrcaSlicer's ``setting_id`` in ``.info``
        files — the deterministic link between a local preset and its cloud
        resource.
        """
        if self._access_token is None:
            self._refresh()
        parts = self._access_token.split(".")
        if len(parts) >= 2:
            padding = "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
            user_id = payload.get("sub", "")
        else:
            user_id = ""
        return str(uuid.uuid5(uuid.UUID(user_id), filament_id))

    @staticmethod
    def _local_to_cloud(content: dict) -> dict:
        """Convert local preset format (arrays) to Orca Cloud format (flat strings)."""
        cloud: dict = {}
        for key, value in content.items():
            if key in OrcaCloudClient._ARRAY_FIELDS and isinstance(value, list) and len(value) > 0:
                cloud[key] = str(value[0])
            elif isinstance(value, list) and len(value) == 0:
                cloud[key] = ""
            elif isinstance(value, (list, tuple)):
                cloud[key] = str(value[0]) if value else ""
            else:
                cloud[key] = value
        # Ensure required fields for Orca Cloud
        if "type" not in cloud:
            cloud["type"] = "filament"
        if "version" not in cloud:
            cloud["version"] = "2.4.0.3"
        return cloud

    def sync_push(
        self,
        profile_id: str,
        name: str,
        content: dict,
        original_updated_time: str = "",
        cloud_id: str | None = None,
    ) -> dict:
        """Push a profile.

        ``profile_id`` is the filament_id (e.g. ``Pabc123...``).  By default
        the Orca Cloud resource UUID is derived deterministically from
        user_id + filament_id via UUID v5.  Pass ``cloud_id`` to target a
        specific existing resource (e.g. one discovered via sync_pull or
        stored from a prior import) — this prevents creating a same-name
        duplicate when the stored resource UUID differs from the computed
        one.

        Content is converted from local array format to cloud flat-string
        format automatically.

        Returns ``{updated_time, created_time}`` on success.
        Raises :class:`OrcaCloudConflict` on 409.
        """
        resource_id = cloud_id or self.cloud_id(profile_id)
        cloud_content = self._local_to_cloud(content)
        body: dict = {"id": resource_id, "name": name, "content": cloud_content}
        if original_updated_time:
            body["original_updated_time"] = original_updated_time
        resp = self._request(
            "POST",
            f"{self.API_BASE}/api/v1/sync/push",
            json_body=body,
        )
        if resp.status_code == 409:
            server_profile = None
            try:
                server_profile = resp.json().get("server_profile")
            except (ValueError, requests.JSONDecodeError):
                pass
            raise OrcaCloudConflict(resp.text, server_profile)
        if resp.status_code == 401:
            raise OrcaCloudError(401, resp.text, "authentication failed")
        if not resp.ok:
            raise OrcaCloudError(resp.status_code, resp.text)
        return resp.json()

    def sync_pull(self, cursor: int = 0) -> dict:
        """Pull profiles.

        Returns ``{next_cursor, upserts, deletes}``.
        On 410 Gone, retry without cursor (full resync).
        """
        params = {"cursor": cursor} if cursor else None
        resp = self._request(
            "GET",
            f"{self.API_BASE}/api/v1/sync/pull",
            params=params,
        )
        if resp.status_code == 410:
            # Full resync required — retry without cursor.
            resp = self._request(
                "GET",
                f"{self.API_BASE}/api/v1/sync/pull",
            )
        if resp.status_code == 401:
            raise OrcaCloudError(401, resp.text, "authentication failed")
        if not resp.ok:
            raise OrcaCloudError(resp.status_code, resp.text)
        return resp.json()

    def sync_delete(self, ids: list[str]) -> dict:
        """Delete profiles.  Returns response dict."""
        resp = self._request(
            "DELETE",
            f"{self.API_BASE}/api/v1/sync/delete",
            params={"resource": "profiles"},
            json_body={"ids": ids},
        )
        if resp.status_code == 401:
            raise OrcaCloudError(401, resp.text, "authentication failed")
        if not resp.ok:
            raise OrcaCloudError(resp.status_code, resp.text)
        return resp.json()

    # ------------------------------------------------------------------
    # User info
    # ------------------------------------------------------------------

    def get_user_id(self) -> str:
        """Return the authenticated user's UUID (``sub`` from JWT)."""
        if self._access_token is None:
            self._refresh()
        # Decode the JWT payload (middle segment) without verification.
        parts = self._access_token.split(".")
        if len(parts) < 2:
            raise OrcaCloudError(0, "", "malformed access token")
        payload_b64 = parts[1]
        # JWT base64 is URL-safe without padding.
        padding = "=" * (-len(payload_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + padding)
        payload = json.loads(payload_bytes)
        sub = payload.get("sub")
        if not sub:
            raise OrcaCloudError(0, "", "access token has no sub claim")
        return sub
