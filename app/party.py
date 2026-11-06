"""Party page and D&D Beyond character import (M4).

D&D Beyond characters in this campaign are private, so the app can't fetch them on its own.
Instead the DM runs a bookmark ("Sync from D&D Beyond") while viewing the campaign page:
it uses the DM's own logged-in D&D Beyond session in their browser to download the
characters and POSTs them here with an import key. The app never stores D&D Beyond
credentials. Uploading exported JSON files is the fallback.
"""
import json
import secrets
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse

from . import characters, db, indexing
from .web import dm_only, page_role, templates

router = APIRouter()

MAX_IMPORT_BYTES = 30 * 1024 * 1024


# --------------------------------------------------------------------------- import key
def import_key(conn, reset: bool = False) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key='import_key'").fetchone()
    if row and not reset:
        return row[0]
    key = secrets.token_urlsafe(24)
    conn.execute("INSERT INTO settings(key, value) VALUES ('import_key', ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key,))
    conn.commit()
    return key


def save_characters(conn, raw_list: list[dict]) -> list[str]:
    """Parse, upsert and index each D&D Beyond character. Returns the names imported."""
    names = []
    for raw in raw_list:
        raw = raw.get("data", raw) if isinstance(raw, dict) else None   # accept {data: ...}
        if not isinstance(raw, dict) or not raw.get("id") or not raw.get("name"):
            continue
        sheet = characters.parse(raw)
        conn.execute("""
            INSERT INTO characters(ddb_id, name, player, summary, sheet_json, raw_json, synced_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(ddb_id) DO UPDATE SET name=excluded.name, player=excluded.player,
              summary=excluded.summary, sheet_json=excluded.sheet_json,
              raw_json=excluded.raw_json, synced_at=excluded.synced_at""",
                     (raw["id"], sheet["name"], sheet["player"], sheet["summary"],
                      json.dumps(sheet), json.dumps(raw), indexing.now()))
        conn.commit()
        cid = conn.execute("SELECT id FROM characters WHERE ddb_id=?", (raw["id"],)).fetchone()[0]
        indexing.index_character(conn, cid, sheet)
        names.append(sheet["name"])
    if names:
        indexing.index_party_overview(conn)
    return names


@router.post("/party/receive")
async def receive_from_ddb(request: Request):
    """Target of the bookmark's form submission from dndbeyond.com.

    Browsers block background requests from internet pages to local apps (localhost),
    but a top-level form navigation is allowed, so the bookmark submits a form here with
    fields `key` and `payload` (JSON list of characters). The session cookie isn't sent on
    a cross-site POST, so the import key is what authorises this."""
    body = await request.body()
    if len(body) > MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="Too much data")
    fields = parse_qs(body.decode("utf-8", "replace"), max_num_fields=10)
    key = (fields.get("key") or [""])[0]
    conn = db.connect()
    if not secrets.compare_digest(key, import_key(conn)):
        return RedirectResponse("/party?sync=badkey#sync", status_code=303)
    try:
        raw = json.loads((fields.get("payload") or ["[]"])[0])
    except ValueError:
        return RedirectResponse("/party?sync=baddata#sync", status_code=303)
    names = save_characters(conn, raw if isinstance(raw, list) else [raw])
    return RedirectResponse(f"/party?sync=ok&n={len(names)}", status_code=303)


# --------------------------------------------------------------------------- pages
BOOKMARKLET = """(async()=>{const app=%(origin)s,key=%(key)s;
const ids=[...new Set([...document.querySelectorAll('a[href*="/characters/"]')].map(a=>(a.href.match(/characters\\/(\\d+)/)||[])[1]).filter(Boolean))];
if(!location.hostname.endsWith('dndbeyond.com')||!ids.length){alert('Open your campaign page on dndbeyond.com first, then click this bookmark.');return}
try{const t=(await(await fetch('https://auth-service.dndbeyond.com/v1/cobalt-token',{method:'POST',credentials:'include'})).json()).token;
const chars=[];for(const id of ids){const r=await fetch('https://character-service.dndbeyond.com/character/v5/character/'+id+'?includeCustomItems=true',{headers:{Authorization:'Bearer '+t}});if(r.ok){const j=await r.json();if(j.data)chars.push(j.data)}}
if(!chars.length){alert('D&D Beyond returned no characters. Are you signed in?');return}
const f=document.createElement('form');f.method='POST';f.action=app+'/party/receive';
for(const [n,v] of [['key',key],['payload',JSON.stringify(chars)]]){const i=document.createElement('textarea');i.name=n;i.value=v;f.appendChild(i)}
f.style.display='none';document.body.appendChild(f);f.submit()}
catch(e){alert('Sync failed: '+e.message)}})()"""


@router.get("/party")
def party(request: Request, role: str = Depends(page_role)):
    conn = db.connect()
    rows = conn.execute("SELECT * FROM characters ORDER BY name COLLATE NOCASE").fetchall()
    chars = [{"id": r["id"], "synced_at": r["synced_at"], **json.loads(r["sheet_json"])} for r in rows]
    ctx = {"role": role, "chars": chars, "skills": characters.SKILLS,
           "ability_names": characters.ABILITY_NAMES, "signed": characters.signed,
           "sync": request.query_params.get("sync"), "sync_n": request.query_params.get("n")}
    if role == "dm":
        origin = str(request.base_url).rstrip("/")
        js = BOOKMARKLET % {"origin": json.dumps(origin), "key": json.dumps(import_key(conn))}
        ctx["bookmarklet"] = "javascript:" + " ".join(js.split("\n"))
    return templates.TemplateResponse(request, "party.html", ctx)


@router.post("/party/upload")
async def upload(files: list[UploadFile] = File(...), role: str = Depends(dm_only)):
    raw = []
    for f in files:
        data = await f.read(MAX_IMPORT_BYTES + 1)
        if len(data) > MAX_IMPORT_BYTES:
            raise HTTPException(status_code=413, detail=f"{f.filename} is too large")
        try:
            obj = json.loads(data)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"{f.filename} is not JSON")
        raw.extend(obj if isinstance(obj, list) else [obj])
    save_characters(db.connect(), raw)
    return RedirectResponse("/party", status_code=303)


@router.post("/party/reset-key")
def reset_key(role: str = Depends(dm_only)):
    import_key(db.connect(), reset=True)
    return RedirectResponse("/party#sync", status_code=303)


@router.post("/party/{char_id}/delete")
def delete_character(char_id: int, role: str = Depends(dm_only)):
    conn = db.connect()
    indexing.remove_character(conn, char_id)
    conn.execute("DELETE FROM characters WHERE id=?", (char_id,))
    conn.commit()
    return RedirectResponse("/party", status_code=303)
