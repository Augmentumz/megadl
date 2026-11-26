# MegaDL

Download **mega.nz** links straight onto your Unraid server from a web page.

Paste a MEGA link, pick a folder on your shares, and MegaDL downloads it in the background. Under the hood it uses **MEGAcmd**, MEGA's official command-line client, which handles MEGA's encryption, transfer quotas and resuming.

## What it does

- **Downloads file and folder links.** Paste one link or a whole block of text. MegaDL finds every `mega.nz` link in it, old or new format, including password-protected links.
- **Saves where you choose.** Browse your shares, create folders, and set a default download folder.
- **Lets you pick files inside folders.** Open a folder download to see its files and subfolders, and untick anything you don't want. Add a folder with **Start paused** to choose before anything downloads.
- **Play, pause, retry and remove** each download, or use **Pause all**, **Resume all** and **Clear finished**. Removing can also delete the downloaded files.
- **Limits download speed**, from the top bar or in Settings.
- **Signs in to your MEGA account**, with two-factor codes supported, so downloads use your account's transfer quota instead of the small anonymous one.
- **Keeps going after restarts.** Unfinished downloads pick up again on their own.
- **Saves files your shares can read.** Downloads belong to `nobody:users`, so they show up over SMB like any other file.
- **Optional sign-in page** to protect the web GUI with a password.

## Install on Unraid

1. **Add the template.** In the Unraid web UI, open **Terminal** (the `>_` icon, top right) and run:

   ```bash
   curl -fsSL -o /boot/config/plugins/dockerMan/templates-user/my-MegaDL.xml \
     https://raw.githubusercontent.com/augmentumz/megadl/main/unraid/megadl.xml
   ```

2. **Create the container.** Go to **Docker**, click **Add Container**, and choose **MegaDL** from the **Template** list.

3. **Check the settings** (the defaults work for most setups) and click **Apply**:

   | Setting | Default | What it's for |
   |---|---|---|
   | Web GUI port | `8095` | Port for the web page. Pick another if something else uses it. |
   | Downloads root | `/mnt/user/` | The part of your server MegaDL can browse and save into. |
   | Default download folder | `downloads` | Where downloads go unless you pick another folder. Created if missing. |
   | Web GUI password | empty | Set one to show a sign-in page (user name `admin`, changeable under **Show more settings**). |
   | Host path label | `/mnt/user` | Display only. Set it to the same path as Downloads root so the GUI shows real paths. |
   | Appdata | `/mnt/user/appdata/megadl` | Download list, settings and your MEGA session. |

   More settings (PUID/PGID, UMASK, file permissions) are under **Show more settings**. The defaults suit Unraid.

4. **Open it.** Click the MegaDL icon on the Docker tab and choose **WebUI**, or go to `http://YOUR-SERVER-IP:8095`.

### Updating

GitHub rebuilds the MegaDL image whenever the code changes, and every Monday to pick up new MEGAcmd releases. When Unraid shows **update ready** next to MegaDL, click it to apply. If nothing shows up, turn on **Advanced View** on the Docker tab and click **force update**.

## Using it

1. **Add downloads:** paste links into the box, check the **Save to** folder (click it to choose another), and click **Add downloads**. For a password-protected link, click **Link has a password?** first.
2. **Choose files in a folder:** click the arrow next to a folder download. Unticked files are held back, and dropped once the ticked files have finished. You can re-tick them until then.
3. **Control downloads:** use the play/pause and trash buttons on each row.
4. **Limit speed:** pick a value from **Limit** in the top bar, or choose **Custom…** to type an exact one.
5. **Use your MEGA account:** sign in on the **Account** tab. Public links work without an account, but MEGA limits anonymous transfers.

A download showing **Retrying** usually means MEGA's transfer quota ran out. It continues on its own once quota is available.

## Troubleshooting

- **Logs:** container log on the Docker tab (MegaDL icon, then **Logs**). MEGAcmd's own log is `appdata/megadl/.megaCmd/megacmdserver.log`.
- **"MEGAcmd didn't start":** change the Appdata path to your pool directly, for example `/mnt/cache/appdata/megadl`.
- **Files not visible over SMB:** keep PUID/PGID at `99`/`100` and file/folder permissions at `666`/`777`.
- **Connection keeps dropping over a VPN** (for example UniFi WiFiman): your router's security scanner may be blocking it. Remove the block in the router's security settings, or move MegaDL to another port.
- **MEGA sign-in fails with the right password:** MEGAcmd can't handle passwords that start with a quote, or that contain `"` together with spaces.

## How it's built

- `app/` is the web app (Python/Flask plus plain HTML/CSS/JS). It runs MEGAcmd's commands and keeps the download list in `/config`.
- `Dockerfile` and `docker/entrypoint.sh` build the image: Debian 12, MEGAcmd from MEGA's package repository, and the app, running as PUID/PGID.
- `unraid/megadl.xml` is the Unraid template.
- `.github/workflows/docker-publish.yml` builds the image and publishes it to `ghcr.io/augmentumz/megadl`. The package must be set to **Public** on GitHub so Unraid can download it.
- `dev/mock-megacmd/` is a fake MEGAcmd for testing the GUI without a MEGA connection.

## Notes

MegaDL is not affiliated with MEGA. Only download content you have the right to download, and follow MEGA's terms of service. MEGAcmd is © Mega Limited and is distributed under its own BSD-style license. The bundled Fira Sans font is licensed under the SIL Open Font License.
