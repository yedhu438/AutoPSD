"""
ps_bridge.py  —  Varsany Photoshop Bridge
==========================================
Python side of the Photoshop integration.
1. submit_job()  — writes a JSON job file for Photoshop to pick up
2. trigger_and_wait() — tells the open Photoshop to run ps_worker.jsx via COM, waits for result

No pywin32 needed — uses a lightweight VBScript one-liner via cscript.exe.
Photoshop must be open before calling trigger_and_wait().
"""

import json, os, shutil, ssl, subprocess, time, urllib.request, urllib.parse
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass

# ── Paths ──────────────────────────────────────────────────────────────────────
_BRIDGE_ROOT     = Path(r"C:\gimpTest")
JOBS_DIR         = _BRIDGE_ROOT / "jobs"
ASSETS_DIR       = _BRIDGE_ROOT / "Temp" / "OrderImages"   # UXP getImageEntry looks here
DONE_DIR         = _BRIDGE_ROOT / "done"    # UXP writes done files here (bridge root, not jobs/)
ERROR_DIR        = _BRIDGE_ROOT / "error"   # UXP writes error files here
IMAGE_SERVER_URL = os.environ.get("IMAGE_SERVER_URL", "http://www.crssoft.co.uk/CustomOrderImages/")

for _d in (JOBS_DIR, ASSETS_DIR, DONE_DIR, ERROR_DIR):
    _d.mkdir(parents=True, exist_ok=True)

JSX_PATH = str(Path(__file__).parent / "ps_worker.jsx")

# ── Submit job ─────────────────────────────────────────────────────────────────
def _clean_zone(zone_name, zone, canvas_w_px, canvas_h_px):
    """Copy image to ASSETS_DIR and download preview. Returns a cleaned zone dict for the job JSON."""
    img_src = (zone.get("customer_image") or "").strip()
    img_path = None
    if img_src and os.path.isfile(img_src):
        img_filename = Path(img_src).name
        dest = ASSETS_DIR / img_filename
        if not dest.exists():
            shutil.copy2(img_src, dest)
        img_path = img_filename

    emoji_src = (zone.get("emoji_text_image") or "").strip()
    emoji_path = None
    if emoji_src and os.path.isfile(emoji_src):
        emoji_filename = Path(emoji_src).name
        dest_e = ASSETS_DIR / emoji_filename
        if not dest_e.exists():
            shutil.copy2(emoji_src, dest_e)
        emoji_path = emoji_filename

    preview_src = (zone.get("preview_image") or "").strip()
    preview_path = None
    if preview_src:
        if preview_src.startswith("http"):
            url_basename = Path(urllib.parse.urlparse(preview_src).path).name or "preview.jpg"
            dest_p = ASSETS_DIR / url_basename
            if not dest_p.exists():
                try:
                    _ctx = ssl.create_default_context(); _ctx.check_hostname = False; _ctx.verify_mode = ssl.CERT_NONE
                    with urllib.request.urlopen(preview_src, context=_ctx) as _r:
                        dest_p.write_bytes(_r.read())
                    print(f"[PS Bridge] Preview downloaded: {url_basename}")
                except Exception as e:
                    print(f"[PS Bridge] Preview download failed ({zone_name}): {e}")
                    url_basename = None
            preview_path = url_basename
        elif os.path.isfile(preview_src):
            url_basename = Path(preview_src).name
            dest_p = ASSETS_DIR / url_basename
            if not dest_p.exists():
                shutil.copy2(preview_src, dest_p)
            preview_path = url_basename
        else:
            url_basename = Path(preview_src).name
            dest_p = ASSETS_DIR / url_basename
            if not dest_p.exists():
                try:
                    url = IMAGE_SERVER_URL.rstrip("/") + "/" + url_basename
                    _ctx = ssl.create_default_context(); _ctx.check_hostname = False; _ctx.verify_mode = ssl.CERT_NONE
                    with urllib.request.urlopen(url, context=_ctx) as _r:
                        dest_p.write_bytes(_r.read())
                    print(f"[PS Bridge] Preview downloaded from crssoft: {url_basename}")
                except Exception as e:
                    print(f"[PS Bridge] Preview download failed ({zone_name}): {e}")
                    url_basename = None
            preview_path = url_basename

    return {
        "customer_image":   img_path,
        "emoji_text_image": emoji_path,
        "text_lines":       zone.get("text_lines") or [],
        "font_name":        zone.get("font_name") or "Arial Bold",
        "font_ps_name":     zone.get("font_ps_name") or "Arial-BoldMT",
        "font_family":      zone.get("font_family") or "Arial",
        "font_style":       zone.get("font_style") or "Bold",
        "colour_hex":       zone.get("colour_hex") if zone.get("colour_hex") is not None else "#ffffff",
        "label":            zone.get("label") or zone_name.upper(),
        "zone_w_px":        zone.get("zone_w_px") or canvas_w_px,
        "zone_h_px":        zone.get("zone_h_px") or canvas_h_px,
        "preview_image":    preview_path,
    }


def submit_job(order_id, template_path, zones, output_path, canvas_w_px=3780, canvas_h_px=3780, combined=True,
               job_type="standard", text_replacements=None, colour_hex=None, quantity=1, items=None,
               multi_items=None, extra=None):
    """
    Write a job JSON for Photoshop UXP plugin to pick up.
    zones = {
        "front": {
            "customer_image": "C:/path/to/image.jpg",   # full path on disk, or None
            "text_lines":     ["Line 1", "Line 2"],      # list of strings, or []
            "font_name":      "Arial Bold",              # display name
            "font_ps_name":   "Arial-BoldMT",            # PostScript name for batchPlay
            "font_family":    "Arial",
            "font_style":     "Bold",
            "colour_hex":     "#ffffff",
            "label":          "FRONT",
            "zone_w_px":      9600,
            "zone_h_px":      9600,
        },
        ...
    }
    combined=True    → UXP stacks zones vertically (needed for blank templates)
    combined=False   → UXP uses per-layer CustomerText_ slots (for layered templates)
    multi_items      → list of {zones, sku} dicts for orders with different designs per item
    """
    clean_zones = {zn: _clean_zone(zn, z, canvas_w_px, canvas_h_px) for zn, z in zones.items()}

    clean_multi_items = []
    for item in (multi_items or []):
        item_zones = {zn: _clean_zone(zn, z, canvas_w_px, canvas_h_px)
                      for zn, z in (item.get("zones") or {}).items()}
        clean_multi_items.append({"zones": item_zones, "sku": item.get("sku", "")})

    job = {
        "order_id":          order_id,
        "template":          str(template_path),
        "zones":             clean_zones,
        "output_path":       str(output_path),
        "combined":          combined,
        "canvas_w_px":       canvas_w_px,
        "canvas_h_px":       canvas_h_px,
        "dpi":               320,
        "type":              job_type,
        "text_replacements": text_replacements or {},
        "colour_hex":        colour_hex or "#ffffff",
        "quantity":          max(1, int(quantity or 1)),
        "items":             items or [],
        "multi_items":       clean_multi_items,
        "submitted_at":      datetime.now().isoformat(),
    }
    job.update(extra or {})     # job-specific settings for the plugin (e.g. hoodie crop / fit)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    # Clear any stale done/error files from previous runs for this order
    for stale in [DONE_DIR / f"{order_id}.json", ERROR_DIR / f"{order_id}.json"]:
        try:
            stale.unlink(missing_ok=True)
        except Exception:
            pass
    job_file = JOBS_DIR / f"{order_id}.json"
    job_file.write_text(json.dumps(job, indent=2), encoding="utf-8")
    print(f"[PS Bridge] Job submitted: {order_id}")
    return Path(output_path)


# ── Trigger Photoshop via VBScript COM ─────────────────────────────────────────
def _trigger_photoshop():
    """
    Tell the open Photoshop to run ps_worker.jsx using a VBScript one-liner.
    No pywin32 needed — cscript.exe is built into Windows.
    """
    vbs_file = _BRIDGE_ROOT / "trigger_ps.vbs"
    vbs_content = (
        'Dim ps : Set ps = CreateObject("Photoshop.Application") : '
        'ps.DoJavaScriptFile "' + JSX_PATH.replace("\\", "\\\\") + '"'
    )
    vbs_file.write_text(vbs_content, encoding="utf-8")
    try:
        subprocess.Popen(
            [r"C:\Windows\System32\cscript.exe", "//Nologo", str(vbs_file)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as e:
        print(f"[PS Bridge] VBScript trigger failed: {e}")
        return False


# ── Wait for result ────────────────────────────────────────────────────────────
def wait_for_completion(order_id, timeout_sec=300, use_uxp=True):
    """
    Poll until UXP plugin marks job done or errored.
    use_uxp=True  → skip VBScript trigger; UXP plugin polls jobs/ every 3 s automatically
    use_uxp=False → legacy mode: fire VBScript to run ps_worker.jsx via COM first
    """
    done_file  = DONE_DIR  / f"{order_id}.json"
    error_file = ERROR_DIR / f"{order_id}.json"

    if not use_uxp:
        _trigger_photoshop()   # legacy JSX mode only

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if done_file.exists():
            print(f"[PS Bridge] Done: {order_id}")
            return True
        if error_file.exists():
            try:
                data = json.loads(error_file.read_text(encoding="utf-8"))
                print(f"[PS Bridge] Error: {data.get('error','unknown')}")
            except Exception:
                pass
            return False
        time.sleep(2)

    print(f"[PS Bridge] Timeout: {order_id}")
    return False


def ps_worker_running():
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Photoshop.exe", "/NH"],
            capture_output=True, text=True, timeout=5
        )
        return "Photoshop.exe" in result.stdout
    except Exception:
        return False
