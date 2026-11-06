# Hosting the Strixhaven Companion on Unraid

End result: **https://dnd.gulu.stream** → Cloudflare → SWAG → the Companion container →
an Ollama container running the AI on the Intel Arc A580.

```
Browser ──► Cloudflare (dnd.gulu.stream, proxied) ──► SWAG :443 (host network)
              └─► 192.168.1.230:8765  strixhaven-companion  (/data = appdata)
                    └─► 192.168.1.230:11434  ollama  (/dev/dri → Arc A580)
```

Plan for about an hour. Steps 1–3 get it running on your home network; steps 4–7 put it
on the internet. Want a different address than `dnd.gulu.stream`? Use your name
everywhere this guide says `dnd`.

---

## Step 1 — Copy your data to the server

Your PC has everything: logins, notes, sessions, characters and the processed books.

1. On the PC, make a fresh upload bundle (safe while the app is running):

   ```powershell
   cd C:\Users\lavel\Documents\DND\dnd-companion
   .\.venv\Scripts\python.exe -m ingest.export_data C:\Users\lavel\Documents\DND\server-upload
   ```

2. In **WinSCP**, create `/mnt/user/appdata/strixhaven-companion` and upload the
   *contents* of `server-upload` into it, so you end up with:

   ```
   /mnt/user/appdata/strixhaven-companion/companion.db
   /mnt/user/appdata/strixhaven-companion/extracted/   (SRD.json, Strixhaven.json)
   /mnt/user/appdata/strixhaven-companion/sources/     (srd-5.2.1.pdf, strixhaven.pdf)
   ```

3. Fix ownership (WinSCP uploads as root; the container runs as Unraid's `nobody`).
   Unraid web UI → **>_ Terminal** (top right):

   ```bash
   chown -R nobody:users /mnt/user/appdata/strixhaven-companion
   chmod -R u+rwX,g+rwX /mnt/user/appdata/strixhaven-companion
   ```

> From now on the **server is the real copy**. Stop using the app on your PC, or changes
> made there won't be on the server. (To redo the move later, repeat this step with the
> container stopped.)

---

## Step 2 — Ollama (the AI) on the Arc A580

1. **Apps** tab → search **ollama** → install the official `ollama/ollama` template.
2. Settings:

   | Field | Value |
   |---|---|
   | Network type | Bridge |
   | Port | `11434` → `11434` |
   | Path `/root/.ollama` | `/mnt/user/appdata/ollama` (models, ~7 GB) |
   | **Add another Path/Port/Variable/Device → Device** | `/dev/dri` |
   | **Add → Variable** `OLLAMA_VULKAN` | `1` (lets Ollama use the Intel Arc) |
   | **Add → Variable** `OLLAMA_KEEP_ALIVE` | `30m` |

   `/dev/dri` is the same GPU device Jellyfin uses; they can share it (a long transcode
   will slow answers down while it runs).

3. Start it, then click its icon → **Console** and download the models:

   ```bash
   ollama pull qwen3.5:latest
   ollama pull nomic-embed-text
   ```

4. **Check it's using the GPU:**

   ```bash
   ollama run qwen3.5:latest "Say hi in five words." --verbose
   ollama ps
   ```

   You want `ollama ps` to show **PROCESSOR 100% GPU**, and `--verbose` to report an
   *eval rate* of roughly **25+ tokens/s**. If it says **CPU**, the Arc wasn't picked up —
   see Troubleshooting. (It still works on the CPU, just ~3–5× slower.)

---

## Step 3 — The Companion container

**Easy way — use the template from the repo:**

1. In WinSCP, copy `docs/unraid/my-strixhaven-companion.xml` from the repo to
   `/boot/config/plugins/dockerMan/templates-user/` on the server.
2. **Docker** tab → **Add Container** → **Template** dropdown → `strixhaven-companion`.

**Or by hand:** Docker → Add Container:

| Field | Value |
|---|---|
| Name | `strixhaven-companion` |
| Repository | `ghcr.io/mglavelle/gulu_strixhaven:latest` |
| Network type | Bridge |
| Extra Parameters (Advanced view) | `--user 99:100` |
| Port: container `8000` | host `8765` |
| Path: container `/data` | `/mnt/user/appdata/strixhaven-companion` |
| Variable `DM_PASSWORD` | your DM password |
| Variable `SESSION_SECRET` | a long random string — see below |
| Variable `OLLAMA_URL` | `http://192.168.1.230:11434` |

Make the session secret in the Unraid terminal and paste the result in:

```bash
openssl rand -hex 32
```

Notes:
- `OLLAMA_URL` must use the server's IP, **not** `localhost` (that would mean "inside the
  Companion container").
- `--user 99:100` makes the app run as `nobody:users` so it can write its database.
- Keep `SESSION_SECRET` the same forever; changing it logs everyone out.

**Apply**, then open **http://192.168.1.230:8765** on your PC. Sign in as the DM and ask
a rules question. If you get an answer with citations, the server side works. 🎉

---

## Step 4 — Lock it down *before* it's on the internet

Right now player passwords are their character names, which anyone at the table (or anyone
who sees the Party page) can guess — and the site holds a copyrighted book. Pick one:

**A. Cloudflare Access (recommended).** Cloudflare asks for an email code before anyone
even reaches the app, so only your group gets in. Free for up to 50 people.

1. Cloudflare dashboard → **Zero Trust** (first time: pick a team name, Free plan).
2. **Access → Applications → Add an application → Self-hosted**:
   - Domain: `dnd.gulu.stream`
   - Policy: **Allow**, rule *Emails* → list everyone's email address.
   - Login method: **One-time PIN** (codes by email; no accounts needed).
3. Add a **second** self-hosted application for the D&D Beyond sync:
   - Domain `dnd.gulu.stream`, **path** `party/receive`
   - Policy action **Bypass**, rule *Everyone*.

   Why: the DM's "Sync to Companion" bookmark submits from dndbeyond.com, and browsers
   don't send the Cloudflare login cookie with that kind of cross-site form. That one path
   is still protected by the import key built into the bookmark.

**B. Real passwords.** Each player signs in and changes theirs under **Account** (click
your name, top right). The DM can also set new ones on the **Players** page.

Doing both is best.

---

## Step 5 — Cloudflare DNS

Cloudflare → gulu.stream → **DNS → Records → Add record**, matching your `watch` record:

| Type | Name | Content | Proxy |
|---|---|---|---|
| A | `dnd` | your home IP — copy it from the `watch` record | **Proxied** (orange cloud) |

Your SWAG certificate is a wildcard (`*.gulu.stream`), so the new name is already covered.

---

## Step 6 — SWAG

1. In WinSCP, copy `docs/unraid/dnd.subdomain.conf` from the repo to
   `/mnt/user/appdata/swag/nginx/proxy-confs/` (next to `watch.subdomain.conf`).
2. Restart the **swag** container (Docker tab → swag → Restart).
3. Check the SWAG log for errors (swag icon → Logs). `nginx: configuration file … test is
   successful` or no `[emerg]` lines = good.

This config is the same as your Jellyfin one plus three things the Companion needs:
streaming answers (`proxy_buffering off`), long "Careful" answers (`proxy_read_timeout`)
and the ~3 MB D&D Beyond sync (`client_max_body_size`).

---

## Step 7 — Test from outside, then tell the group

1. On your phone **with Wi-Fi off**, open **https://dnd.gulu.stream**.
   (With Cloudflare Access you'll get the email-code page first.)
2. Sign in, ask a question, watch the answer stream in.
3. **DM:** open **Party** on the *hosted* site and drag a fresh **Sync to Companion**
   bookmark to the bookmarks bar. The old one points at your PC (`localhost`) and won't
   work any more. Then run it once on the D&D Beyond campaign page.

---

## Updating later

1. Code changes are pushed to GitHub → GitHub Actions builds a new image (~3 min,
   repo → **Actions**).
2. Unraid → **Docker** → **Check for Updates** → **apply update** on
   strixhaven-companion. Your data in appdata is untouched.

Back up `/mnt/user/appdata/strixhaven-companion/companion.db` (it's everything you've
written) — e.g. include `appdata` in the Appdata Backup plugin.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Container stops right away; log says `unable to open database file` / `readonly database` | Step 1.3 ownership fix; check Extra Parameters has `--user 99:100`. |
| "Ollama not reachable" / answers never start | `OLLAMA_URL` must be `http://192.168.1.230:11434`; is the ollama container running? Test `http://192.168.1.230:11434` in a browser — it should say "Ollama is running". |
| `model "qwen3.5:latest" not found` | Run the `ollama pull` commands (Step 2.3). |
| Ollama runs on **CPU** | Check Unraid sees the Arc (`ls /dev/dri` shows `renderD128`), that the device is added to the ollama container, and `OLLAMA_VULKAN=1` is set; update the ollama image. If Vulkan won't take, Intel's IPEX-LLM build of Ollama is the fallback (ask Claude to set it up). |
| Answers slow or stall mid-way | `ollama ps` should say 100% GPU. If a Jellyfin transcode is running, they share the GPU. Don't raise `CHAT_CTX` above 6144 unless it stays at 100% GPU. |
| `502 Bad Gateway` from SWAG | The Companion isn't on `192.168.1.230:8765` — check the port mapping and that it's running. |
| `413 Request Entity Too Large` on D&D Beyond sync | `client_max_body_size` missing from the SWAG conf; restart SWAG after editing. |
| Answer appears all at once at the end | Buffering: confirm `proxy_buffering off;` is in the SWAG conf. |
| D&D Beyond sync lands on a Cloudflare login page | Add the `party/receive` **Bypass** application (Step 4A.3). |
| Everyone got logged out after an update | `SESSION_SECRET` changed or is empty — set it once and leave it. |
| Search finds nothing / errors about vectors | `EMBED_MODEL` must be `nomic-embed-text` (what the index was built with), and that model must be pulled in Ollama. |
