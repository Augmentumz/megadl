# MegaDL

A self-hosted web GUI for downloading **mega.nz** links straight onto your Unraid server.

- Paste one link or a whole pile of text. MegaDL picks out every `mega.nz` file and folder link, in both the new (`/file/…#key`, `/folder/…#key`) and old (`#!…`, `#F!…`) formats.
- **Choose where downloads go.** A folder browser over your shares lets you pick or create the destination for each batch, and you can set a default folder.
- **Play, pause and remove** each download. Folder links are handled as one item, so pausing a folder pauses every file in it. There's also Retry for failed downloads, plus Pause all, Resume all and Clear finished.
- **Speed limit.** Change it from the menu in the top bar or set an exact value in Settings. You can also set the number of connections per download.
- **Sign in to your MEGA account** (two-factor codes work) so downloads use your account's transfer quota instead of the small anonymous one. The session survives restarts.
- **Survives restarts.** Unfinished downloads resume, or are queued again automatically.
- **Unraid-friendly files.** Downloads are owned by `nobody:users` with 666/777 permissions, so they show up over SMB.
- An optional sign-in page for the web GUI, a light/dark theme that follows your system, and a layout that works on phones.

Under the hood it runs **MEGAcmd**, MEGA's official command-line client, which handles MEGA's encryption, quotas and resuming. MegaDL adds the GUI and the download management on top.

---

## Install on Unraid

There are two ways to do it. **Option A** is recommended: GitHub builds the image for you, and Unraid's Docker tab can then update it like any other app.

### Option A: publish to GitHub (recommended)

1. **Create a GitHub repository** named `megadl` and push this folder to it:

   ```bash
   cd megadl
   sed -i 's/YOUR_GITHUB_USERNAME/your-github-name/g' unraid/megadl.xml   # lowercase!
   git init && git add . && git commit -m "MegaDL"
   git branch -M main
   git remote add origin https://github.com/your-github-name/megadl.git
   git push -u origin main
   ```

2. **Wait for the image to build.** The included workflow (`.github/workflows/docker-publish.yml`) builds and publishes `ghcr.io/your-github-name/megadl:latest`. Watch progress in the repository's **Actions** tab; it takes about five minutes.

3. **Make the image public.** On GitHub, open your profile, go to **Packages**, choose **megadl**, then **Package settings**, and under **Change visibility** select **Public**. This lets Unraid pull the image without logging in.

4. **Add the template to Unraid.** In the Unraid web UI, open **Terminal** and run:

   ```bash
   curl -fsSL -o /boot/config/plugins/dockerMan/templates-user/my-MegaDL.xml \
     https://raw.githubusercontent.com/your-github-name/megadl/main/unraid/megadl.xml
   ```

5. Go to **Docker**, click **Add Container**, and choose **MegaDL** from the **Template** list. Check the paths, then click **Apply**.

6. Click the MegaDL icon and choose **WebUI**.

The workflow also rebuilds every Monday, so the image keeps up with new MEGAcmd releases. Unraid will show "update ready" when there's a new build.

### Option B: build it on the Unraid server (no GitHub)

1. Copy this folder to your server, for example to `/mnt/user/appdata/megadl-src` over SMB.
2. In the Unraid **Terminal**:

   ```bash
   cd /mnt/user/appdata/megadl-src
   docker build -t megadl:latest .
   bash unraid/install-template.sh unraid/megadl.xml
   ```

3. Go to **Docker**, click **Add Container**, choose **MegaDL**, change **Repository** to `megadl:latest`, and click **Apply**.

Unraid can't check a locally built image for updates; it will say "not available", which is expected. To update MEGAcmd later, run `docker build --no-cache -t megadl:latest .` again, then restart the container.

---

## Template settings

| Setting | Container side | Default | What it does |
|---|---|---|---|
| Web GUI port | `8080` | `8080` | Port for the web interface. |
| Downloads root | `/data` | `/mnt/user/` | The part of the server MegaDL can browse and save into. `/mnt/user/` gives access to all shares; point it at one share to restrict it. |
| Default download folder | `DEFAULT_DOWNLOAD_DIR` | `downloads` | Folder inside the downloads root used when you don't pick one. It's created if it's missing, which on Unraid creates a `downloads` share. You can change it in Settings. |
| Web GUI password | `WEBUI_PASSWORD` | empty | When set, MegaDL shows a sign-in page (user name `WEBUI_USER`, default `admin`). The password is sent once, then the browser keeps a sign-in cookie for up to 30 days. Changing the password signs everyone out. |
| Appdata | `/config` | `/mnt/user/appdata/megadl` | The download list, settings, logs and MEGA session. |
| Host path label | `HOST_PATH_LABEL` | `/mnt/user` | Display only. Set it to the same host path as Downloads root so the GUI shows real paths. |
| PUID / PGID | | `99` / `100` | The user and group that own downloaded files (`nobody:users`). |
| UMASK | | `000` | umask for files the app creates. |
| File / folder permissions | `FILE_PERMISSIONS` / `FOLDER_PERMISSIONS` | `666` / `777` | Permissions MEGAcmd gives new files and folders. MEGAcmd's own default is 600/700, which would hide downloads from SMB users. |

Less common options: `POLL_INTERVAL` (seconds between progress updates, default `1.5`), `RECOVERY_GRACE_SECONDS` (how long to wait after a restart before re-queuing unfinished downloads, default `60`), and `LOG_LEVEL` (`INFO` or `DEBUG`).

---

## Using it

**Add downloads.** Paste links into the box (Ctrl/Cmd+Enter also submits), check the **Save to** folder, and click **Add downloads**. Click the folder path to browse your shares or create a new folder. For password-protected links, click **Link has a password?** and enter it; it's used for every link in that batch.

**Download states**

| State | Meaning |
|---|---|
| Queued | Waiting to be handed to MEGAcmd. Links are handed over one at a time. |
| Starting / Reading folder | MEGAcmd is opening the link. Large folders can take a moment. |
| Waiting for a free slot | MEGAcmd runs a few transfers at once; this one starts when a slot frees up. |
| Downloading | In progress, with speed and time left. |
| Retrying | MEGA paused the transfer, usually because the transfer quota ran out or the connection dropped. It continues on its own. |
| Paused | Paused by you. Press play to continue. |
| Done | Finished. |
| Failed | Something went wrong. The reason is shown; press Retry to try again. |

**Remove a download** with the trash button. You can also tick **Also delete the downloaded files from the server**. MegaDL only offers this for files or folders it created itself, never for something that was already in the folder before the download started.

**Speed limit.** Pick a preset from **Limit** in the top bar, or choose **Custom…** to type an exact value in Settings. The limit applies to all downloads combined.

**MEGA account.** Sign in on the **Account** tab. Public links download fine without an account, but MEGA limits how much anonymous users can transfer. If your account uses two-factor authentication, enter the current code with your password. Signing out stops downloads that are in progress.

---

## Troubleshooting

**Where are the logs?**
- Container log: on the Docker tab, click the MegaDL icon and choose **Logs**.
- MEGAcmd's own log: `appdata/megadl/.megaCmd/megacmdserver.log` (plus `.log.out` and `.log.err`).

**The banner says "MEGAcmd didn't start".** MEGAcmd needs a unix socket and file locks in `/config/.megaCmd`. If your appdata share goes through Unraid's FUSE layer, change the Appdata path to the pool directly (for example `/mnt/cache/appdata/megadl`) or make the appdata share exclusive.

**Downloads sit on "Retrying".** This is almost always MEGA's transfer quota. Sign in with an account that has quota left, or wait. MEGAcmd resumes on its own when quota frees up.

**I can't see the files over SMB.** Check that PUID/PGID are `99`/`100` and the file and folder permissions are `666`/`777`. Files downloaded before you changed these keep their old permissions; on Unraid, run **Tools > New Permissions** on the share to fix them.

**Sign-in fails although the password is right.** MEGAcmd can't handle passwords that start with a quote, or that contain a `"` together with spaces. Change the password on mega.nz, or sign in with a password without those characters.

**MegaDL keeps losing its connection over a VPN (for example UniFi WiFiman/Teleport).** Your router's security scanner (UniFi CyberSecure/IPS) may be blocking the connection. Older MegaDL versions sent the password with every request, which those scanners flag. Update MegaDL, then remove the block in the router's security settings or move MegaDL to another port.

**I want to save to Unassigned Devices.** Set Downloads root to `/mnt/` with access mode **RW/Slave**, and set Host path label to `/mnt`.

---

## How it works

```
Browser ──HTTP──▶ MegaDL (Python/Flask) ──runs──▶ mega-get, mega-transfers, mega-speedlimit, …
                     │                                   │ unix socket
                     │ SQLite: jobs + settings           ▼
                     └──────────────────────────▶ mega-cmd-server (MEGAcmd) ──▶ MEGA
```

- MEGAcmd's commands start `mega-cmd-server` automatically when it isn't running. MegaDL notices when that server restarts and picks up from there. MEGAcmd's session lives in `/config/.megaCmd`, so you stay signed in.
- Each link is queued with `mega-get -q`. MEGAcmd reports one transfer per file, so MegaDL groups them back into one download per link: it remembers the highest transfer number before queuing a link, and the first new transfer inside the chosen folder reveals the file or folder name.
- Progress comes from `mega-transfers` every 1.5 seconds. Pause, resume and remove map to `mega-transfers -p/-r/-c` on every file of the download. If a folder download is paused while MEGAcmd is still adding its files, newly added files are paused too.
- After a restart, MegaDL gives MEGAcmd a minute to resume its own cached transfers (it does this when signed in), then re-queues anything still missing.

## Development

You can run the app on any machine with Python 3.9+ and Flask, using the included **fake MEGAcmd** (`dev/mock-megacmd`). It imitates MEGAcmd's output, so you can work on the GUI without a MEGA connection:

```bash
mkdir -p /tmp/megadl/config /tmp/megadl/data/downloads
cd app
DOWNLOAD_ROOT=/tmp/megadl/data CONFIG_DIR=/tmp/megadl/config \
MEGACMD_BIN_DIR="$PWD/../dev/mock-megacmd" DEFAULT_DOWNLOAD_DIR=downloads \
python3 -m megadl
# open http://localhost:8080
```

Words in a link control the fake: `bad` makes the link fail, `fail` makes the transfer fail partway, `quota` holds it in Retrying for 20 seconds, `tiny` finishes instantly, and `/folder/` produces a folder with three files. For example: `https://mega.nz/folder/test#key`.

Project layout:

```
Dockerfile, docker/entrypoint.sh   image: Debian 12 + MEGAcmd + the app, runs as PUID/PGID
app/megadl/megacmd.py              wrapper around the mega-* commands and their output
app/megadl/manager.py              queue, progress tracking, pause/resume/remove, restart recovery
app/megadl/web.py                  JSON API + static GUI
app/megadl/static/                 the GUI (plain HTML/CSS/JS, no build step)
unraid/megadl.xml                  Unraid template
.github/workflows/                 builds and publishes the image to GHCR
```

## Notes

- MegaDL is not affiliated with MEGA. Only download content you have the right to download, and follow MEGA's terms of service.
- MEGAcmd is © Mega Limited and is installed from MEGA's official package repository when the image is built. It is distributed under its own BSD-style license.
- The bundled Fira Sans font is licensed under the SIL Open Font License (`app/megadl/static/fonts/OFL.txt`).
