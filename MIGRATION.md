# Moving the Varsany automation to another PC

Verified against the live PC on **2026-10-13**. For the *current* system this replaces `SETUP_GUIDE.md`
(that guide describes an older VPS layout: `C:\Varsany`, Python 3.11, mapped W:/Z: drives).
Nothing in this file is a secret. Secrets travel only in the git-ignored `migration\bundle\` folder.

## What actually runs today

| Piece | How it is set up | In git? | How it moves |
|---|---|---|---|
| Code | `C:\gimpTest` | yes (commit first, see A1) | git, or copy the folder |
| Secrets + paths | `.env` | no | bundle |
| Python | 3.14.x installed for one Windows user; 49 pinned packages | no | `setup_new_pc.ps1` (from `requirements.lock.txt`) |
| Daemon | Windows service `VarsanyDaemon` (NSSM), runs **as that Windows user**: `-u C:\gimpTest\batch_processor.py --daemon --date-after 2026-07-18`, output to `daemon.log`, restarts if it exits | no | `setup_new_pc.ps1` (settings come from the bundle) |
| Photoshop 2026 (27.x) + UXP plugin | plugin is loaded by hand as an *unpacked developer plugin* | plugin yes | manual, step B6 |
| Fonts | 1,108 custom .ttf/.otf installed in `C:\Windows\Fonts` and the per-user fonts folder | no | `bundle\fonts` + `install_fonts.py` |
| Jersey template | `template\england Football Adult.psd` (276 MB) - the only semi-custom template that exists | no | bundle |
| Chrome | headless Chrome renders the premium SVG colour fonts | - | winget |
| B2 upload | rclone to bucket `varsaniautomation`, SYSTEM scheduled task `VarsanyB2Sync`; the Synology pulls from B2 (nothing to change there) | scripts yes, key no | `setup_new_pc.ps1` + bundle |
| Database | the SQL Server named in `.env`; it only accepts allow-listed IPs | external | ask the DB host (B3) |

**Deliberately not moved:** `Output\` (83 GB, already in B2), `Temp\` (102 GB customer-image cache, re-downloaded on
demand), `done\` `error\` `jobs\`, the logs (about 2 GB each), `uxp_token.txt` (per machine; the plugin falls back to
`C:\gimpTest` by itself), and Claude's memory folder (several entries describe the Claude app on the old PC and would
mislead on another one).

## Three things that bite

1. **Use exactly `C:\gimpTest`.** `ps_bridge.py` hardcodes it and the plugin looks there first.
2. **Never run the daemon on both PCs.** Both would process the same orders. At cutover the old service must be
   stopped *and disabled* (otherwise a reboot brings it back).
3. **The DB host must allow the new PC's public IP** (port 1433). Ask first, it is the slowest step.

## A. On the old PC

1. **Commit the code.** Four files are modified but uncommitted (`batch_processor.py`, `uxp-plugin/index.js`,
   `uxp-plugin/manifest.json`, `sync_to_b2.bat`) and `restart_daemon.bat` plus this `migration\` folder are new.
   A plain `git clone` of the last commit would give the new PC the older plugin manifest and a `sync_to_b2.bat`
   that the SYSTEM account cannot run. Commit and push, or skip git and copy the whole folder.
   The repo is public: never force-add `.env`, `migration\bundle\` or `jobs\` (all git-ignored).
2. **Build or refresh the bundle** from a *normal* Windows PowerShell window (not from inside the Claude app, whose
   view of `%APPDATA%` can be stale; that is why a bundle built there leaves the B2 key file out):
   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\gimpTest\migration\export_bundle.ps1 -IncludeRcloneConf
   ```
   Unchanged files are skipped, so a re-run is quick. It collects `.env`, the service settings, the pinned package list,
   `template\`, `nssm.exe`, `rclone.exe`, every custom font, `rclone.conf`, and writes `SHA256SUMS.txt`.
3. **Copy `C:\gimpTest\migration\bundle`** (about 1 GB) to the new PC by USB or LAN. It holds the DB password and the
   B2 key: delete the copies afterwards.

## B. On the new PC

1. **Install the prerequisites** (elevated PowerShell), then Photoshop 2026 and "UXP Developer Tools" from Creative
   Cloud. Install Python as the Windows user that will run the service.
   ```powershell
   winget install -e --id Python.Python.3.14
   winget install -e --id Google.Chrome
   winget install -e --id Git.Git
   ```
2. **Put the code at `C:\gimpTest`** (`git clone https://github.com/yedhu438/gimpTest.git C:\gimpTest`, or copy the
   folder) and the bundle at `C:\gimpTest\migration\bundle`.
3. **Get the DB host to allow this PC's public IP** on port 1433. Find it with
   `(Invoke-RestMethod https://api.ipify.org)`.
4. **Match the time zone:** `tzutil /s "GMT Standard Time"`. For unattended running also turn off sleep and sign in
   automatically: the service starts at boot, but Photoshop and the plugin only run inside a signed-in session.
5. **Run the setup** (elevated PowerShell). Look first, then apply:
   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\gimpTest\migration\setup_new_pc.ps1 -Check
   powershell -ExecutionPolicy Bypass -File C:\gimpTest\migration\setup_new_pc.ps1
   ```
   It verifies the bundle checksums, creates the folders, restores `.env` and the template, installs the pinned
   packages and the fonts, installs the `VarsanyDaemon` service **without starting it** (it asks for the Windows
   password), installs rclone and creates the B2 task **disabled**, then tests the DB login. Secrets are never printed.
   Re-running is safe; `-DryRun` shows what it would do; `-Check` changes nothing and also works on the old PC.
6. **Photoshop (manual, the script cannot do this):**
   1. Sign in to Creative Cloud and start Photoshop 2026. Restart it after the fonts were installed.
   2. Preferences > Plugins > turn on Developer Mode if offered (needs admin; restart Photoshop).
   3. UXP Developer Tools > Add Plugin > `C:\gimpTest\uxp-plugin\manifest.json` > Load
      (or Plugins > Development > Load Unpacked Plugin, as in the old SETUP_GUIDE).
   4. Open the **Varsany Orders** panel. It should log `Ready: C:\gimpTest`. If not, click *Set Root Folder* and pick
      `C:\gimpTest` (this writes a fresh `uxp_token.txt`).
   5. Leave Photoshop open and not minimised, in the signed-in session.
7. **Pilot, without touching production.** Push one already-finished order through to a throwaway folder:
   ```powershell
   cd C:\gimpTest
   py -3.14 -u batch_processor.py --reprocess --order <a-finished-OrderID> --output C:\gimpTest\Temp\migration_test
   ```
   `--reprocess` skips the status filters and `--output` writes outside `Output\`, so nothing reaches B2 or the NAS
   (it re-marks that already-finished order as processed). Open the PSD and check fonts, colours and, for an order with
   a premium font, the colour font. This command was checked against the code but not run.

## C. Cutover (in this order)

1. **Old PC**, elevated PowerShell. Stop the daemon and disable it so a reboot cannot revive it:
   ```powershell
   net stop VarsanyDaemon
   sc.exe config VarsanyDaemon start= disabled
   ```
2. **Old PC:** let Photoshop finish what it is doing, then close it so it stops writing PSDs. Whatever is left in `jobs\`
   is not needed: the new daemon re-queues every still-pending order from the DB. When `rclone_sync.log` ends with
   `There was nothing to transfer`, turn the old upload off:
   ```powershell
   schtasks /change /tn VarsanyB2Sync /disable
   ```
3. **New PC**, elevated PowerShell:
   ```powershell
   powershell -ExecutionPolicy Bypass -File C:\gimpTest\migration\setup_new_pc.ps1 -GoLive
   ```
   It asks you to type `YES` (old daemon stopped and disabled), then starts `VarsanyDaemon` and enables + starts
   `VarsanyB2Sync`.
4. **Check on the new PC:**
   - `Get-Content C:\gimpTest\daemon.log -Tail 400 | Select-String -NotMatch 'Fonts +:' | Select-Object -Last 15`
     should show `Orders to process: N` and jobs being submitted (the very long `Fonts :` line is skipped).
   - The first `.psd` appears under `C:\gimpTest\Output\<ship-by-date>\...` (the folder is the order's ship-by date,
     not today's date).
   - `Get-Content C:\gimpTest\rclone_sync.log -Tail 10` shows `Copied (new)`, and the file reaches the Synology about a
     minute later.

## D. Rollback

The DB flags are the only shared state, so rolling back means swapping the switches. New PC: `net stop VarsanyDaemon`
and `schtasks /change /tn VarsanyB2Sync /disable`. Old PC: `sc.exe config VarsanyDaemon start= auto`,
`net start VarsanyDaemon` and `schtasks /change /tn VarsanyB2Sync /enable`.

## E. Known issues you will carry over (as of 2026-10-13)

- **The plugin must be re-loaded after a Photoshop restart.** On the old PC no PSD has been written since
  2026-10-12 around 14:33, about 30 jobs sit in `jobs\`, and every order logs `UXP timed out`; Photoshop was restarted
  at 14:26 today. That fits an unpacked plugin that was not re-loaded (not confirmed: the panel state cannot be read
  from outside). On whichever PC is live, after any Photoshop restart re-load the plugin and check the panel says
  `Ready`. Packaging it as a .ccx (UXP Developer Tools > Package) might make it survive restarts; untested.
- The daemon logs `FAILED ... UXP timed out` after 10 minutes even when Photoshop finishes later, so those orders are
  re-submitted. Look for the PSD in `Output\` before concluding an order failed.
- `daemon.log` and `batch_log.txt` grow without limit (about 2 GB each today).
- `start_daemon.bat` in the repo is stale (a previous user's Python path and `--date-after 2026-07-10`). Do not use it:
  the `VarsanyDaemon` service settings are the source of truth.
- The font `Archivo Black` is not installed on the old PC (the daemon warns and falls back to Arial).
