"""Login: the password you enter identifies you (the DM or one player).

The session cookie stores only the user id; every request looks the user up, so deleting
a player (or changing their role) takes effect immediately. The role is what search.py
uses to filter DM-only content, and the user id unlocks that player's own notes.
"""
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request

from . import config, db, users


@dataclass
class User:
    id: int
    name: str
    role: str                    # dm | player
    character_id: int | None

    @property
    def is_dm(self) -> bool:
        return self.role == "dm"

    @property
    def is_admin(self) -> bool:
        """May open the Usage page: only the logins listed in ADMIN_USERS (not the DM)."""
        return self.name.lower() in config.ADMIN_USERS


def any_logins(conn=None) -> bool:
    conn = conn or db.connect()
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0


def check_password(password: str) -> User | None:
    """Return the matching user, or None (slowly, to discourage guessing)."""
    conn = db.connect()
    u = users.find_by_password(conn, password)
    if not u:
        time.sleep(1.0)
        return None
    return User(u["id"], u["name"], u["role"], u["character_id"])


def current_user(request: Request) -> User | None:
    uid = request.session.get("uid")
    u = users.get(db.connect(), uid) if isinstance(uid, int) else None
    return User(u["id"], u["name"], u["role"], u["character_id"]) if u else None


def current_role(request: Request) -> str | None:
    u = current_user(request)
    return u.role if u else None


def require_user(request: Request) -> User:
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=401, detail="Not logged in")
    return u


def require_role(request: Request) -> str:
    return require_user(request).role
