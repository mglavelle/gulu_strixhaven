"""User accounts: one DM, one login per player.

Login is a single password box: the password alone identifies the user, so passwords
must be unique. Passwords are compared case-insensitively (typing "aldric" or "Aldric"
both work) and stored as salted PBKDF2 hashes.

The DM account mirrors DM_PASSWORD from .env, so editing .env is always a way back in.
"""
import hashlib
import hmac
import secrets

from . import config, indexing

ITERATIONS = 120_000


def _norm(password: str) -> bytes:
    return password.strip().lower().encode()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", _norm(password), bytes.fromhex(salt), ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt}${digest.hex()}"


def verify(password: str, stored: str) -> bool:
    try:
        _, iters, salt, digest = stored.split("$")
        check = hashlib.pbkdf2_hmac("sha256", _norm(password), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(check.hex(), digest)
    except (ValueError, TypeError):
        return False


def find_by_password(conn, password: str):
    if not password.strip():
        return None
    for u in conn.execute("SELECT * FROM users ORDER BY id"):
        if verify(password, u["password_hash"]):
            return u
    return None


def password_taken(conn, password: str, except_id: int | None = None) -> bool:
    u = find_by_password(conn, password)
    return bool(u and u["id"] != except_id)


def get(conn, user_id: int | None):
    if not user_id:
        return None
    return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def ensure_dm(conn) -> None:
    """Create/refresh the DM account from DM_PASSWORD in .env."""
    pw = config.DM_PASSWORD
    if not pw or pw in config.DEFAULT_PASSWORDS:
        return
    dm = conn.execute("SELECT * FROM users WHERE role='dm' ORDER BY id LIMIT 1").fetchone()
    if not dm:
        conn.execute("INSERT INTO users(name, role, password_hash, created_at) VALUES ('DM','dm',?,?)",
                     (hash_password(pw), indexing.now()))
    elif not verify(pw, dm["password_hash"]):
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(pw), dm["id"]))
    conn.commit()


def visibilities(role: str, user_id: int | None) -> tuple[str, ...]:
    """Which chunk visibilities this person may search."""
    base = ("public", "dm") if role == "dm" else ("public", "player")
    return base + ((f"user:{user_id}",) if user_id and role != "dm" else ())
