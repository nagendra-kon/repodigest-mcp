"""Shared fixtures: a temporary, synthetic multi-file Python project."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from repodigest_mcp import index as index_module

FILES: dict[str, str] = {
    "app/__init__.py": "",
    "app/hashing.py": '''
        import hashlib


        def hash_password(password: str, salt: str = "") -> str:
            """Return the hex SHA-256 digest of a salted password."""
            data = (salt + password).encode("utf-8")
            digest = hashlib.sha256(data).hexdigest()
            return digest
    ''',
    "app/store.py": '''
        class UserStore:
            """In-memory user database."""

            def __init__(self) -> None:
                self._users: dict[str, str] = {}

            def add_user(self, username: str, password_hash: str) -> None:
                """Register a user with an already hashed password."""
                self._users[username] = password_hash

            def lookup_hash(self, username: str) -> str | None:
                """Return the stored password hash for a user, if any."""
                return self._users.get(username)
    ''',
    "app/auth.py": '''
        from app.hashing import hash_password
        from app.store import UserStore


        class AuthService:
            """Authenticates users against a store."""

            def __init__(self, store: UserStore) -> None:
                self.store = store

            def register(self, username: str, password: str) -> None:
                """Create a new account."""
                self.store.add_user(username, hash_password(password))

            def login(self, username: str, password: str) -> bool:
                """Check a username and password against the store; return True on success."""
                stored = self.store.lookup_hash(username)
                if stored is None:
                    return False
                return stored == hash_password(password)
    ''',
    "app/api.py": '''
        from app.auth import AuthService


        def handle_login_request(service: AuthService, payload: dict) -> dict:
            """HTTP handler for user login requests."""
            ok = service.login(payload["username"], payload["password"])
            return {"ok": ok}
    ''',
    "app/text.py": '''
        def slugify(title: str) -> str:
            """Convert a title into a URL slug."""
            return "-".join(title.lower().split())
    ''',
    # Two unrelated `run` methods, to exercise ambiguous-name handling.
    "app/jobs.py": '''
        class Worker:
            def run(self) -> None:
                """Process queued jobs."""


        class Scheduler:
            def run(self) -> None:
                """Fire due timers."""
    ''',
    # Things the indexer must ignore or survive.
    "app/broken.py": "def oops(:\n",
    "venv/pyvenv.cfg": "home = /usr/bin\n",
    "venv/lib/vendored.py": "def vendored_helper():\n    return 1\n",
    ".cache/junk.py": "def cached_junk():\n    return 2\n",
    "node_modules/pkg/tool.py": "def node_tool():\n    return 3\n",
}


def write_project(root: Path, files: dict[str, str] = FILES) -> Path:
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dedent(content).lstrip("\n"))
    return root


@pytest.fixture(autouse=True)
def _fresh_index_cache() -> None:
    index_module._CACHE.clear()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return write_project(tmp_path / "proj")
