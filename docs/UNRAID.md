# Hosting the Strixhaven Companion on Unraid — step by step

When you're done, your group opens **https://dnd.gulu.stream** and gets the Companion,
running on your Unraid server with the AI on the Intel Arc A580.

```
Phone/PC ──► Cloudflare (dnd.gulu.stream) ──► SWAG (on Unraid, port 443)
                 └─► Companion container  192.168.1.230:8765   (data in appdata)
                       └─► Ollama container  192.168.1.230:11434  (AI on the Arc GPU)
```

**How to use this guide:** do the parts in order. Each part ends with a **✅ Check** — don't
move on until the check passes. If one fails, look at **Troubleshooting** at the bottom.

Tools you'll use: the **Unraid web UI** (http://192.168.1.230), **WinSCP** (connected to
the server the way you already use it for SWAG), **PowerShell** on your PC, and the
**Cloudflare dashboard**.

Time: about an hour, most of it waiting for downloads.

Want a different web address than `dnd.gulu.stream`? Replace `dnd` with your choice
everywhere in this guide (including inside the SWAG file in Part 7).

---

## Part 1 — Make a fresh copy of your data on the PC

Your PC's copy has all the logins, notes, sessions, characters and the processed books.
Make a fresh export so it includes everything you've done up to now.

1. On your PC, open **PowerShell** (Start menu → type `PowerShell` → Enter).
2. Copy and paste these two lines, then press **Enter**:

   ```powershell
   cd C:\Users\lavel\Documents\DND\dnd-companion
   .\.venv\Scripts\python.exe -m ingest.export_data C:\Users\lavel\Documents\DND\server-upload
   ```

3. It prints a list ending with the two PDFs.

✅ **Check:** in File Explorer, `C:\Users\lavel\Documents\DND\server-upload` contains
exactly these three things:

```
companion.db        (about 24 MB)
extracted           (folder)
sources             (folder with the two PDFs)
```

> ⚠️ After this export, **stop using the app on your PC.** Anything you change on the PC
> from now on won't be on the server. (If you do change something, just redo Part 1 and
> Part 2 before Part 5.)

---

## Part 2 — Create the app's folder on the server and upload the data

### 2a. Create the folder

Yes — you create this folder yourself. Either way works; pick one:

**Option A — Unraid terminal (fastest)**
1. Open the Unraid web UI: http://192.168.1.230
2. Click the **>_** icon in the top-right corner. A black terminal window opens.
3. Type this and press **Enter**:

   ```bash
   mkdir -p /mnt/user/appdata/strixhaven-companion
   ```

   (No output means it worked.) Leave the terminal open — you'll need it again in 2c.

**Option B — WinSCP**
1. In WinSCP, go to `/mnt/user/appdata/`.
2. Right-click an empty spot → **New** → **Directory…**
3. Name: `strixhaven-companion` → **OK**.

### 2b. Upload the data

1. In WinSCP, open the new folder `/mnt/user/appdata/strixhaven-companion/` (double-click it).
   The window should be empty.
2. In Windows **File Explorer**, open `C:\Users\lavel\Documents\DND\server-upload`.
3. Select all three items there (**Ctrl+A**): `companion.db`, `extracted`, `sources`.
4. **Drag** them into the WinSCP window.
5. WinSCP asks to confirm the upload → click **OK**. Wait for it to finish (~107 MB).

✅ **Check:** the WinSCP window for `/mnt/user/appdata/strixhaven-companion/` now shows
`companion.db`, `extracted` and `sources` — directly in that folder, **not** inside a
`server-upload` subfolder. Double-click `sources` → you see `srd-5.2.1.pdf` and
`strixhaven.pdf`.

### 2c. Give the files to the right owner

WinSCP uploaded the files as `root`, but the app will run as Unraid's normal app user
(`nobody`). In the Unraid terminal (**>_** icon), paste these two lines and press **Enter**:

```bash
chown -R nobody:users /mnt/user/appdata/strixhaven-companion
chmod -R u+rwX,g+rwX /mnt/user/appdata/strixhaven-companion
```

Then check it:

```bash
ls -la /mnt/user/appdata/strixhaven-companion
```

✅ **Check:** every line shows `nobody users`, like:

```
-rw-rw-r-- 1 nobody users 23900160 ... companion.db
drwxrwxr-x 2 nobody users     ...    extracted
drwxrwxr-x 2 nobody users     ...    sources
```

---

## Part 3 — Install Ollama (the AI engine) using the Arc GPU

### 3a. Confirm Unraid sees the GPU

In the Unraid terminal, run:

```bash
ls /dev/dri
```

✅ **Check:** you see something like `by-path  card0  renderD128`. (Jellyfin already uses
this, so it should be there.)

### 3b. Install the Ollama container

1. Unraid web UI → **Apps** tab (top menu).
2. In the search box type **ollama** and press **Enter**.
3. Several results appear. Pick the one whose **Repository** is **`ollama/ollama`** (hover
   or click the card to see it) → click **Install**. A settings form opens.
4. Fill in / check these fields (scroll down through the form):

   | Field | Set it to |
   |---|---|
   | **Name** | `ollama` |
   | **Network Type** | `Bridge` |
   | **Port** (container 11434) | `11434` |
   | **Path** for `/root/.ollama` (may be labelled AppData / Models) | `/mnt/user/appdata/ollama` |

5. If the template has any **NVIDIA** settings, remove them: click **Advanced View**
   (toggle, top right of the form) and look at **Extra Parameters** — delete
   `--gpus all` or `--runtime=nvidia` if present. Delete any variable named
   `NVIDIA_VISIBLE_DEVICES` (click its **Remove** button).
6. Scroll to the bottom → click **+ Add another Path, Port, Variable, Label or Device**.
   A small popup opens:
   - **Config Type:** `Device`
   - **Name:** `Intel GPU`
   - **Value:** `/dev/dri`
   - click **Add**.
7. Click **+ Add another Path, Port, Variable, Label or Device** again:
   - **Config Type:** `Variable`
   - **Name:** `Use Vulkan (Intel Arc)`
   - **Key:** `OLLAMA_VULKAN`
   - **Value:** `1`
   - click **Add**.
8. Keep the model loaded between questions: the template probably already has an
   **OLLAMA_KEEP_ALIVE** row (value `5m`) — click its **EDIT** and change the value to
   `30m`. Only if there is **no** such row, add one:
   - **Config Type:** `Variable`, **Name:** `Keep model loaded`, **Key:** `OLLAMA_KEEP_ALIVE`,
     **Value:** `30m` → **Add**.

   Don't end up with two OLLAMA_KEEP_ALIVE rows.
9. Click **Apply** at the bottom. Unraid downloads the image; when the log window says
   **Command execution completed successfully**, click **Done**.

✅ **Check:** on the **Docker** tab, `ollama` shows a green ▶ **started**. In your PC's
browser, open http://192.168.1.230:11434 — the page says **Ollama is running**.

### 3c. Download the two AI models

1. **Docker** tab → click the **ollama icon** → **Console**. A terminal opens *inside* the
   Ollama container.
2. Paste this and press **Enter** (≈6.6 GB, a few minutes):

   ```bash
   ollama pull qwen3.5:latest
   ```

   Wait until it prints **success**.
3. Then this (small, seconds):

   ```bash
   ollama pull nomic-embed-text
   ```

   Wait for **success**.

### 3d. Check the AI runs on the GPU

Still in the Ollama console:

```bash
ollama run qwen3.5:latest "Say hello to my D&D group." --think=false --verbose
```

It answers in a sentence or two, then prints stats. Look for the line **`eval rate:`**.

> Leave `--think=false` in. Without it, this model "thinks out loud" first and can ramble
> for minutes (press **Ctrl+C** to stop it). The Companion app already turns thinking off
> except when you tick **Careful**.

Then run:

```bash
ollama ps
```

✅ **Check:** `ollama ps` shows **`100% GPU`** in the PROCESSOR column, and the eval rate
was roughly **20 tokens/s or more**.

**On the Intel Arc it will most likely show `100% CPU` or `95%/5% CPU/GPU` at ~5 tokens/s.**
That's expected: Ollama's "will the model fit?" check misreads the Arc's free memory over
Vulkan and puts 0 layers on the GPU (its log says `offloaded 0/33 layers to GPU` even with
7 GB free). Force all layers onto the GPU to confirm the card itself works:

1. In the Ollama console run `ollama run qwen3.5:latest --think=false --verbose`
2. At the `>>>` prompt type `/set parameter num_gpu 99` and press Enter.
3. Type `Say hello to my D&D group.` and press Enter — note the **eval rate**.
4. Type `/bye`, then run `ollama ps`.

✅ **Check:** now **`100% GPU`** and roughly **20–40 tokens/s**. The Companion does the same
thing automatically through its **GPU layers = 99** setting (Part 4).

If it's still on the CPU, Vulkan isn't on — see Troubleshooting ("`ollama ps` says CPU").

---

## Part 4 — Install the Companion container

### 4a. Make a session secret

This is a random string that keeps everyone logged in. In the **Unraid terminal**
(the **>_** icon, not the Ollama console), run:

```bash
openssl rand -hex 32
```

It prints a 64-character string like `3f9a…c21e`. Select it with the mouse and copy it
(select = copy in the Unraid terminal; or right-click → Copy). Paste it somewhere safe
(Notepad / password manager) — you need it in 4c and must keep it the same forever.

### 4b. Put the Companion template on the server

1. In WinSCP, go to **`/boot/config/plugins/dockerMan/templates-user/`**.
   (`/boot` is the Unraid USB stick. You'll see other `my-….xml` files there — your
   existing containers.)
2. In File Explorer, open `C:\Users\lavel\Documents\DND\dnd-companion\docs\unraid\`.
3. Drag **`my-strixhaven-companion.xml`** into the WinSCP window → **OK**.

### 4c. Create the container from the template

1. Unraid web UI → **Docker** tab → scroll to the bottom → click **Add Container**.
2. At the top of the form, open the **Template** dropdown → under **User templates**,
   choose **strixhaven-companion**. The form fills itself in.
3. Fill in the two empty fields:
   - **DM password:** the same DM password you use on the PC.
   - **Session secret:** paste the string from 4a.
4. Check these were pre-filled correctly:

   | Field | Should be |
   |---|---|
   | Repository | `ghcr.io/mglavelle/gulu_strixhaven:latest` |
   | Network Type | `Bridge` |
   | Web UI port | `8765` |
   | Data | `/mnt/user/appdata/strixhaven-companion` |
   | Ollama URL | `http://192.168.1.230:11434` |
   | GPU layers | `99` (puts the whole model on the Arc — see Part 3d) |
   | Admin logins | `Aldric` (who can open the Usage page; the DM isn't admin unless listed) |

5. Toggle **Advanced View** (top right) and check **Extra Parameters** is
   **`--user 99:100`**. (This makes the app run as `nobody`, matching Part 2c.)
6. Click **Apply** → wait for **Command execution completed successfully** → **Done**.

✅ **Check (container):** on the **Docker** tab, `strixhaven-companion` is green/started.
Click its icon → **Logs**. The last lines include:

```
Application startup complete.
Uvicorn running on http://0.0.0.0:8000
```

✅ **Check (app, on your home network):** in your PC's browser open
**http://192.168.1.230:8765**
1. The **Strixhaven Companion** login page appears.
2. Sign in with the **DM password**.
3. **Party** tab shows your 7 characters; **Sessions** shows your session(s). → your data
   moved over.
4. **Ask** tab → ask *"How does grappling work?"*. The first answer can take ~20–30 s
   (the model is loading into the GPU); after that ~5–15 s, with numbered citations.

🎉 The server side works. Parts 5–8 put it on the internet.

---

## Part 5 — Protect it before it goes on the internet

Players' passwords are their character names — anyone who sees the Party page can guess
them, and the site contains a copyrighted book. Set up **Cloudflare Access** so only your
group can reach the site at all. It's free for up to 50 people, and players just enter a
code that's emailed to them.

You'll need everyone's email address.

### 5a. Turn on Cloudflare Zero Trust (first time only)

1. Go to https://dash.cloudflare.com and sign in.
2. In the left sidebar click **Zero Trust**.
3. First time only: choose a **team name** (anything, e.g. `gulu`) → choose the **Free**
   plan → finish the setup. (Cloudflare may ask for a payment method even for the free
   plan; you won't be charged on Free.)

### 5b. Protect dnd.gulu.stream

1. In Zero Trust: left sidebar **Access** → **Applications** → **Add an application**.
2. Choose **Self-hosted**.
3. **Application name:** `D&D Companion`.
   **Session duration:** `1 month` (so people don't re-enter codes every day).
4. **Add public hostname:** Subdomain `dnd`, Domain `gulu.stream` (leave Path empty).
5. Click **Next** (or go to the **Policies** section) → **Add a policy**:
   - **Policy name:** `Our group`
   - **Action:** `Allow`
   - **Include** → Selector **Emails** → type each person's email (press Enter after each).
6. **Login methods:** make sure **One-time PIN** is enabled.
7. Click through **Next** and **Save** / **Add application**.

### 5c. Let the D&D Beyond sync through

The DM's "Sync to Companion" bookmark sends data from dndbeyond.com, and browsers don't
send the Cloudflare login with that kind of request — so one path must skip Access. (It's
still protected by the import key inside the bookmark.)

1. **Access** → **Applications** → **Add an application** → **Self-hosted**.
2. **Application name:** `D&D Companion – D&D Beyond sync`.
3. **Public hostname:** Subdomain `dnd`, Domain `gulu.stream`, **Path** `party/receive`.
4. **Add a policy:** name `Sync`, **Action:** `Bypass`, **Include** → Selector
   **Everyone**.
5. **Save**.

✅ **Check:** **Access → Applications** lists both applications.

(Also recommended: once the site is live, each player signs in and sets a real password —
click their name at the top right → **Account** → **Change password**.)

---

## Part 6 — Add the DNS record in Cloudflare

1. https://dash.cloudflare.com → click **gulu.stream** → left sidebar **DNS** → **Records**.
2. Find the **`watch`** row → click **Edit** → copy the **IPv4 address** → **Cancel**.
   (That's your home's public IP.)
3. Click **+ Add record**:
   - **Type:** `A`
   - **Name:** `dnd`
   - **IPv4 address:** paste the address from step 2
   - **Proxy status:** **on** (orange cloud — *Proxied*)
   - **TTL:** `Auto`
4. **Save**.

✅ **Check:** the record list now has `dnd.gulu.stream  A  …  Proxied`, just like `watch`.

Your SWAG certificate is a wildcard (`*.gulu.stream`), so nothing changes for HTTPS.

---

## Part 7 — Tell SWAG about the new site

1. In WinSCP, go to **`/mnt/user/appdata/swag/nginx/proxy-confs/`**
   (the same folder as your `watch.subdomain.conf`).
2. In File Explorer, open `C:\Users\lavel\Documents\DND\dnd-companion\docs\unraid\`.
3. Drag **`dnd.subdomain.conf`** into WinSCP → **OK**.
   (Using a different name than `dnd`? Open the file in WinSCP and change
   `server_name dnd.gulu.stream;` to your name.)
4. Unraid **Docker** tab → click the **swag** icon → **Restart**.
5. In the **Unraid terminal**, check the config is valid:

   ```bash
   docker exec swag nginx -t
   ```

   (If your SWAG container has a different name on the Docker tab, use that instead of
   `swag`.)

✅ **Check:** the output ends with **`test is successful`**.

What this file adds compared with your Jellyfin one: answers stream word by word
(`proxy_buffering off`), slow "Careful" answers don't time out (`proxy_read_timeout`), and
the ~3 MB D&D Beyond sync is allowed (`client_max_body_size`).

---

## Part 8 — Test from outside and hand it over

1. On your phone, **turn Wi-Fi off** (so you're coming from the internet).
2. Open **https://dnd.gulu.stream**.
3. You get the Cloudflare page → enter your email → enter the code from your inbox.
4. The Companion login page appears → sign in → ask a question → the answer streams in.

✅ **Check:** all of that works on mobile data.

Then:
- **Players:** send them the link, tell them to use their email for the Cloudflare code
  and their character's first name as the password — and to change it under **Account**.
- **DM:** sign in at https://dnd.gulu.stream → **Party** → scroll to **Sync from D&D
  Beyond** → drag the **⟳ Sync to Companion** button to the bookmarks bar (delete the old
  one; it points at your PC). Open the campaign on dndbeyond.com, click the bookmark —
  it should land on the Party page with "Synced 7 characters".

---

## Updating later

1. New code is pushed to GitHub → GitHub builds a new image automatically (~3 min;
   repo → **Actions** tab shows a green ✓).
2. Unraid → **Docker** tab → **Check for Updates** (bottom) → `strixhaven-companion` shows
   **apply update** → click it.

Your data in `/mnt/user/appdata/strixhaven-companion` is never touched by updates.

**Back up** `/mnt/user/appdata/strixhaven-companion/companion.db` — it's everything your
group has written. If you use the *Appdata Backup* plugin, it's included automatically.

---

## Troubleshooting

| What you see | What to do |
|---|---|
| Companion container stops right after starting; log says `unable to open database file` or `readonly database` | Redo Part 2c (ownership), and check **Extra Parameters** is `--user 99:100` (Part 4c step 5). |
| Login page says no logins exist | `companion.db` isn't in `/mnt/user/appdata/strixhaven-companion/` directly (maybe inside a `server-upload` subfolder). Move it up one level, redo 2c, restart the container. |
| Answers never start / "Ollama not reachable" | **Ollama URL** must be `http://192.168.1.230:11434` (not `localhost`). Check http://192.168.1.230:11434 says "Ollama is running". |
| Error mentions `model "qwen3.5:latest" not found` | Redo Part 3c. |
| `ollama ps` says CPU | 1) In the Unraid terminal run `docker exec ollama env \| grep -i vulkan` — it must print `OLLAMA_VULKAN=1` (retype the key/value in the container's settings if not). 2) `docker logs ollama 2>&1 \| grep -i "inference compute"` must mention the Arc A580; if it says `library=cpu`, Vulkan is off. 3) If the log shows the Arc but `offloaded 0/33 layers`, that's the fit-check problem: use **GPU layers = 99** (Part 3d / Part 4). Still CPU? Ask Claude to set up Intel's IPEX-LLM build of Ollama instead. |
| Answers stop with an out-of-memory error after setting GPU layers 99 | Lower **Context size** to `4096`, or set **GPU layers** to `30`. |
| Answers are slow or pause mid-answer | Run `ollama ps` while it answers — it should say 100% GPU. A Jellyfin transcode running at the same time shares the GPU. |
| Phone shows Cloudflare error **521/522** | SWAG isn't reachable from the internet — but `watch` works, so most likely SWAG didn't restart cleanly: redo Part 7 steps 4–5. |
| **502 Bad Gateway** (nginx page) | The Companion isn't answering on `192.168.1.230:8765`: is it started? Does http://192.168.1.230:8765 work at home? |
| D&D Beyond sync says **413 Request Entity Too Large** | The SWAG file is missing `client_max_body_size`; re-copy it (Part 7) and restart SWAG. |
| D&D Beyond sync lands on a Cloudflare login page | Part 5c (the Bypass application for `party/receive`) is missing. |
| Answer appears all at once at the end instead of word by word | `proxy_buffering off;` missing from the SWAG file — re-copy it and restart SWAG. |
| Everyone got logged out after an update | The **Session secret** changed or is empty. Set it back to the value from 4a and leave it. |
| Search finds nothing / errors about vectors | **Embedding model** (Advanced View) must be `nomic-embed-text`, and Part 3c's second `ollama pull` must have succeeded. |
