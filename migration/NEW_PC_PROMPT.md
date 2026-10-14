# Prompts for Claude Code on the NEW PC

Use these once all four of these are true:

1. The code is cloned to `C:\gimpTest` (`git clone https://github.com/yedhu438/gimpTest.git C:\gimpTest`).
2. The bundle folder is pasted at `C:\gimpTest\migration\bundle`.
3. Python 3.14, Chrome, Git, Photoshop 2026 and UXP Developer Tools are installed (MIGRATION.md, step B1).
4. Claude Code is opened **in the folder `C:\gimpTest`**, so it reads `CLAUDE.md` by itself.

## 1. Setup prompt (paste this first)

```text
This PC is the NEW home of the Varsany print automation. The code is cloned at C:\gimpTest and the copied
bundle is at C:\gimpTest\migration\bundle. The OLD PC is still running production, so do NOT go live.

Read these first, in this order: C:\gimpTest\MIGRATION.md (the runbook), C:\gimpTest\CLAUDE.md (project
knowledge), and the header comment of C:\gimpTest\migration\setup_new_pc.ps1.

Then get this PC ready to take over, following MIGRATION.md section B:
1. Run  powershell -ExecutionPolicy Bypass -File C:\gimpTest\migration\setup_new_pc.ps1 -Check  (read-only)
   and show me the result as a short table.
2. Tell me what is still missing (Python 3.14, Chrome, Git, Photoshop 2026, UXP Developer Tools, the DB host
   allowing this PC's public IP, the time zone) and give me the exact commands for the parts I must run myself.
3. When the check is clean enough, walk me through the real run of setup_new_pc.ps1. It needs an elevated
   PowerShell, so I will run it myself: tell me exactly what to paste, I will paste the output back to you.
   It installs the VarsanyDaemon service STOPPED and creates the B2 sync task DISABLED. Then run -Check again.
4. Walk me through the manual Photoshop steps (MIGRATION.md B6). Do not try to drive Photoshop's UI.
5. Help me run the pilot (MIGRATION.md B7) on one already-finished order, writing to
   C:\gimpTest\Temp\migration_test, and tell me what to look for in the PSD.

Hard rules:
- Never start the VarsanyDaemon service, enable VarsanyB2Sync, or run setup_new_pc.ps1 -GoLive until I tell
  you the OLD PC's daemon is stopped AND disabled. Two daemons on the same DB process the same orders twice.
- Use exactly C:\gimpTest (ps_bridge.py hardcodes it).
- This repo is public. Never print, commit or push .env, rclone.conf, anything under migration\bundle, or any
  password or key. Do not git push without asking me first.
- You may not be able to run elevated commands, or to write outside C:\gimpTest, from the Claude app. If a step
  needs admin, give me the exact command to paste into an elevated PowerShell, then verify the result by
  reading back from C:\gimpTest.
- If a script step fails, show me the error. If it is a bug in the script, fix the script in the repo and tell
  me what you changed. Do not hand-roll a different setup.
- Ask before anything destructive, and before anything that writes to the database.

End every reply with three lines: what is done, what is next, and what I must do by hand.
```

## 2. Cutover prompt (only after the old PC's daemon is stopped AND disabled)

```text
The OLD PC's VarsanyDaemon is now stopped and set to Disabled, its Photoshop is closed, and its
VarsanyB2Sync task is disabled. Take me through MIGRATION.md section C step 3 and 4: I will run
setup_new_pc.ps1 -GoLive in an elevated PowerShell and type YES. Then verify with me: daemon.log shows
"Orders to process", the first PSD appears under C:\gimpTest\Output, and rclone_sync.log shows "Copied (new)".
If anything looks wrong, tell me the rollback (MIGRATION.md section D) before you change anything.
```

## 3. Health check, any time

```text
Run setup_new_pc.ps1 -Check, summarise it as a short table, and tell me what is wrong and what to do about it.
Also check that the Varsany Orders panel is loaded in Photoshop by asking me what it says (see MIGRATION.md
section E: the plugin must be re-loaded after every Photoshop restart).
```
