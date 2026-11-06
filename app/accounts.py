"""Players page (DM manages logins) and Account page (change your own password)."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import db, indexing, users
from .auth import User
from .web import dm_only, page_user, templates

router = APIRouter()


def _players_page(request, conn, user, error=None, ok=None, status=200):
    rows = conn.execute("""SELECT u.*, c.name AS character_name,
                                  (SELECT COUNT(*) FROM entries e WHERE e.owner_id = u.id) AS notes
                           FROM users u LEFT JOIN characters c ON c.id = u.character_id
                           ORDER BY u.role = 'player', u.name COLLATE NOCASE""").fetchall()
    chars = conn.execute("SELECT id, name, player FROM characters ORDER BY name COLLATE NOCASE").fetchall()
    taken = {r["character_id"] for r in rows if r["character_id"]}
    return templates.TemplateResponse(request, "players.html", {
        "role": user.role, "users": rows, "chars": chars, "taken": taken,
        "error": error, "ok": ok}, status_code=status)


@router.get("/players")
def players(request: Request, role: str = Depends(dm_only)):
    return _players_page(request, db.connect(), request.state.user, ok=request.query_params.get("ok"))


@router.post("/players/add")
def add_player(request: Request, name: str = Form(...), password: str = Form(""),
               character_id: str = Form(""), role: str = Depends(dm_only)):
    conn = db.connect()
    name = name.strip()[:40]
    password = password.strip() or name
    if not name:
        return _players_page(request, conn, request.state.user, error="Enter a name.", status=400)
    if conn.execute("SELECT 1 FROM users WHERE name = ? COLLATE NOCASE", (name,)).fetchone():
        return _players_page(request, conn, request.state.user, error=f"There's already a login called {name}.", status=400)
    if users.password_taken(conn, password):
        return _players_page(request, conn, request.state.user,
                             error="Someone already uses that password. Passwords must be different, "
                                   "because the password alone decides who logs in.", status=400)
    conn.execute("INSERT INTO users(name, role, password_hash, character_id, created_at) VALUES (?,?,?,?,?)",
                 (name, "player", users.hash_password(password), int(character_id) if character_id else None,
                  indexing.now()))
    conn.commit()
    return RedirectResponse(f"/players?ok=Added+{name}", status_code=303)


@router.post("/players/{user_id}")
def update_player(request: Request, user_id: int, action: str = Form(...), password: str = Form(""),
                  character_id: str = Form(""), role: str = Depends(dm_only)):
    conn = db.connect()
    u = users.get(conn, user_id)
    if not u or u["role"] != "player":
        raise HTTPException(status_code=404, detail="Not found")
    if action == "character":
        conn.execute("UPDATE users SET character_id=? WHERE id=?",
                     (int(character_id) if character_id else None, user_id))
        msg = f"Updated {u['name']}'s character"
    elif action == "password":
        password = password.strip()
        if not password:
            return _players_page(request, conn, request.state.user, error="Enter a new password.", status=400)
        if users.password_taken(conn, password, except_id=user_id):
            return _players_page(request, conn, request.state.user, error="Someone else already uses that password.", status=400)
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (users.hash_password(password), user_id))
        msg = f"Set a new password for {u['name']}"
    elif action == "delete":
        for (eid,) in conn.execute("SELECT id FROM entries WHERE owner_id=?", (user_id,)).fetchall():
            indexing.remove_entry(conn, eid)
        conn.execute("DELETE FROM entries WHERE owner_id=?", (user_id,))
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        msg = f"Removed {u['name']} and their notes"
    else:
        raise HTTPException(status_code=400, detail="Unknown action")
    conn.commit()
    return RedirectResponse(f"/players?ok={msg.replace(' ', '+')}", status_code=303)


@router.get("/account")
def account(request: Request, user: User = Depends(page_user)):
    return templates.TemplateResponse(request, "account.html", {
        "role": user.role, "error": None, "ok": request.query_params.get("ok")})


@router.post("/account/password")
def change_password(request: Request, current: str = Form(...), new: str = Form(...),
                    confirm: str = Form(...), user: User = Depends(page_user)):
    conn = db.connect()
    u = users.get(conn, user.id)

    def fail(msg):
        return templates.TemplateResponse(request, "account.html",
                                          {"role": user.role, "error": msg, "ok": None}, status_code=400)
    if user.is_dm:
        return fail("The DM password is set by DM_PASSWORD in the app's .env file.")
    if not users.verify(current, u["password_hash"]):
        return fail("Your current password isn't right.")
    if new.strip() != confirm.strip() or len(new.strip()) < 4:
        return fail("The new passwords must match and be at least 4 characters.")
    if users.password_taken(conn, new, except_id=user.id):
        return fail("Pick a different password.")      # don't say whose it is
    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (users.hash_password(new), user.id))
    conn.commit()
    return RedirectResponse("/account?ok=1", status_code=303)
