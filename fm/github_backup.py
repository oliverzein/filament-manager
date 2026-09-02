# fm/github_backup.py
"""GitHub backup client — atomic commits via the Git Data REST API.

No ``git`` binary required.  All operations are HTTP requests using
``requests`` (already a project dependency).

The ``backup`` method creates a single commit containing all provided
files, following the Git Data API flow:
ref → base commit → base tree → blobs → new tree → new commit → ref update.
"""
from __future__ import annotations

import base64

import requests


class GitHubBackupError(Exception):
    """GitHub API error with status code and response body."""

    def __init__(self, status: int, body: str, message: str = ""):
        self.status = status
        self.body = body
        self.message = message or body
        super().__init__(self.message)


class GitHubBackupClient:
    """HTTP-only GitHub client for atomic multi-file commits."""

    API_BASE = "https://api.github.com"
    TIMEOUT = 30

    def __init__(self, token: str, repo: str, branch: str = "main"):
        self._token = token
        self._repo = repo  # "owner/repo"
        self._branch = branch

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict | None = None,
    ) -> requests.Response:
        return requests.request(
            method,
            url,
            headers=self._headers(),
            json=json_body,
            timeout=self.TIMEOUT,
        )

    def _url(self, path: str) -> str:
        return f"{self.API_BASE}/repos/{self._repo}/{path}"

    def backup(self, files: dict[str, str], message: str) -> dict:
        """Create an atomic commit with all *files*.

        ``files`` maps repo-relative path → file content (string).
        Returns ``{"commit_sha": str, "files_written": int}``.
        Raises :class:`GitHubBackupError` on any API failure.
        """
        # 1. Get current ref SHA (409 = empty repo, needs initialization)
        resp = self._request(
            "GET", self._url(f"git/ref/heads/{self._branch}"),
        )
        is_empty_repo = resp.status_code == 409
        if not resp.ok and not is_empty_repo:
            raise GitHubBackupError(
                resp.status_code, resp.text,
                f"branch '{self._branch}' not found" if resp.status_code == 404
                else f"ref lookup failed: {resp.status_code}",
            )

        if is_empty_repo:
            # GitHub's Git Data API returns 409 for ALL operations on an
            # empty repo.  Use the Contents API to create an initial commit,
            # which initializes the branch.  Then re-fetch the ref.
            resp = self._request(
                "PUT", self._url("contents/.gitkeep"),
                json_body={
                    "message": "Initialize repository",
                    "content": base64.b64encode(b"").decode("ascii"),
                    "branch": self._branch,
                },
            )
            if not resp.ok:
                raise GitHubBackupError(
                    resp.status_code, resp.text,
                    f"failed to initialize empty repo: {resp.status_code}",
                )
            # Re-fetch ref — repo now has a commit
            resp = self._request(
                "GET", self._url(f"git/ref/heads/{self._branch}"),
            )
            if not resp.ok:
                raise GitHubBackupError(resp.status_code, resp.text)

        ref_sha = resp.json()["object"]["sha"]

        # 2. Get base tree SHA from the commit
        resp = self._request(
            "GET", self._url(f"git/commits/{ref_sha}"),
        )
        if not resp.ok:
            raise GitHubBackupError(resp.status_code, resp.text)
        base_tree_sha = resp.json()["tree"]["sha"]

        # 3. Create blobs for each file
        tree_entries = []
        for path, content in files.items():
            resp = self._request(
                "POST", self._url("git/blobs"),
                json_body={
                    "content": base64.b64encode(
                        content.encode("utf-8")
                    ).decode("ascii"),
                    "encoding": "base64",
                },
            )
            if not resp.ok:
                raise GitHubBackupError(resp.status_code, resp.text)
            blob_sha = resp.json()["sha"]
            tree_entries.append({
                "path": path,
                "mode": "100644",
                "type": "blob",
                "sha": blob_sha,
            })

        # 4. Create new tree (no base_tree for empty repo)
        tree_body: dict = {"tree": tree_entries}
        if base_tree_sha:
            tree_body["base_tree"] = base_tree_sha
        resp = self._request(
            "POST", self._url("git/trees"),
            json_body=tree_body,
        )
        if not resp.ok:
            raise GitHubBackupError(resp.status_code, resp.text)
        tree_sha = resp.json()["sha"]

        # 5. Create commit (no parents for empty repo)
        commit_body: dict = {"message": message, "tree": tree_sha}
        if ref_sha:
            commit_body["parents"] = [ref_sha]
        resp = self._request(
            "POST", self._url("git/commits"),
            json_body=commit_body,
        )
        if not resp.ok:
            raise GitHubBackupError(resp.status_code, resp.text)
        commit_sha = resp.json()["sha"]

        # 6. Update ref (repo is always initialized by this point)
        resp = self._request(
            "PATCH", self._url(f"git/refs/heads/{self._branch}"),
            json_body={"sha": commit_sha},
        )
        if not resp.ok:
            raise GitHubBackupError(resp.status_code, resp.text)

        return {"commit_sha": commit_sha, "files_written": len(files)}

    def test_connection(self) -> dict:
        """Verify token + repo access. Returns repo metadata."""
        resp = self._request("GET", f"{self.API_BASE}/repos/{self._repo}")
        if not resp.ok:
            raise GitHubBackupError(
                resp.status_code, resp.text,
                "token invalid" if resp.status_code == 401
                else f"repo not found: {resp.status_code}",
            )
        data = resp.json()
        return {
            "repo": data.get("full_name", self._repo),
            "branch": self._branch,
            "default_branch": data.get("default_branch", "main"),
        }
