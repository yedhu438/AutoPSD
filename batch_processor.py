"""
Varsany Batch Processor
========================
Processes all unprocessed orders from the database.
Generates layered PSD files for each order.

Images : W:\\images\\Jan-Image\\ and W:\\images\\Feb-Image\\
Fonts  : C:\\gimpTest\\Fonts\\ + W:\\fonts\\ + system fonts
Output : C:\\gimpTest\\Output\\YYYY-MM-DD\\OrderID.psd

Usage:
    python batch_processor.py                  # all unprocessed orders
    python batch_processor.py --limit 10       # first 10 only (test)
    python batch_processor.py --order 203-xxx  # one specific order
    python batch_processor.py --dry-run        # preview, no files written
    python batch_processor.py --dpi 320        # full print resolution
"""

import os, json, struct, io, argparse, traceback, urllib.request, tempfile
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont, ImageCms
import pyodbc

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass  # python-dotenv not installed — fall back to hardcoded defaults

from db import get_connection as _db_get_connection

try:
    from synology_upload import SynologyUploader
    SYNOLOGY_AVAILABLE = True
except ImportError:
    SYNOLOGY_AVAILABLE = False

try:
    from b2sdk.v2 import InMemoryAccountInfo, B2Api
    B2_AVAILABLE = True
except ImportError:
    B2_AVAILABLE = False


class B2Uploader:
    """Upload PSDs to a Backblaze B2 bucket.

    Reads credentials from .env:
        B2_KEY_ID            — application key ID
        B2_APPLICATION_KEY   — application key
        B2_BUCKET_NAME       — target bucket name
        B2_REMOTE_PREFIX     — optional remote path prefix (default: "Output")
    """

    def __init__(self):
        self.connected = False
        key_id  = os.environ.get("B2_KEY_ID", "").strip()
        app_key = os.environ.get("B2_APPLICATION_KEY", "").strip()
        bucket  = os.environ.get("B2_BUCKET_NAME", "").strip()
        self._prefix = os.environ.get("B2_REMOTE_PREFIX", "Output").strip()
        if not (key_id and app_key and bucket):
            print("[WARN] B2_KEY_ID / B2_APPLICATION_KEY / B2_BUCKET_NAME not set in .env — B2 upload disabled")
            return
        try:
            info = InMemoryAccountInfo()
            self._api = B2Api(info)
            self._api.authorize_account("production", key_id, app_key)
            self._bucket = self._api.get_bucket_by_name(bucket)
            self.connected = True
        except Exception as e:
            print(f"[WARN] B2 connect failed: {e}")

    def upload(self, local_path):
        """Upload local_path to B2, preserving Output sub-folder structure."""
        try:
            rel = os.path.relpath(local_path, OUTPUT_FOLDER)
            remote_path = f"{self._prefix}/{rel.replace(os.sep, '/')}"
            self._bucket.upload_local_file(local_file=local_path, file_name=remote_path)
            print(f"[INFO] B2 uploaded: {remote_path}")
        except Exception as e:
            print(f"[WARN] B2 upload failed for {os.path.basename(local_path)}: {e}")


try:
    from rembg import remove as rembg_remove
    REMBG_AVAILABLE = True
except ImportError:
    REMBG_AVAILABLE = False


# Editable text layer writer (TySh + Txt2 PSD blocks)
try:
    from psd_text_layer import build_editable_text_tagged_blocks, resolve_ps_font_name
    EDITABLE_TEXT_AVAILABLE = True
except ImportError:
    EDITABLE_TEXT_AVAILABLE = False

# ─── PHOTOSHOP BRIDGE (Option 2 — Headless Photoshop) ────────────────────────
# Set USE_PHOTOSHOP_BRIDGE=1 in your .env to route orders through real Photoshop
# instead of the Python PSD writer.  Falls back to Python writer automatically
# if Photoshop is not running or ps_bridge.py is unavailable.
#
# Requires:
#   - ps_worker.jsx running inside Photoshop (see ps_worker.jsx)
#   - PSD templates in PS_TEMPLATES_DIR with layers named:
#       "CustomerImage_front", "CustomerText_front",
#       "CustomerImage_back",  "CustomerText_back", etc.
#   - .env:  USE_PHOTOSHOP_BRIDGE=1
#            PS_BRIDGE_DIR=C:\Varsany\photoshop_bridge
#            PS_TEMPLATES_DIR=W:\VarsaniAutomation\templates
_USE_PS_BRIDGE   = os.environ.get("USE_PHOTOSHOP_BRIDGE", "0").strip() == "1"
TEMPLATES_FOLDER = os.environ.get("PS_TEMPLATES_DIR", r"W:\VarsaniAutomation\templates")

try:
    import ps_bridge
    from ps_bridge import (submit_job as _ps_submit,
                           wait_for_completion as _ps_wait,
                           ps_worker_running as _ps_alive)
    PS_BRIDGE_AVAILABLE = True
except ImportError:
    ps_bridge = None
    PS_BRIDGE_AVAILABLE = False

def _build_psd_via_photoshop(order_id, row, out_path, force_bg_remove=False, quantity_override=None):
    """Route one order through Headless Photoshop (ps_worker.jsx).
    Returns (success: bool, message: str) — same contract as build_psd_for_order.
    """
    if not PS_BRIDGE_AVAILABLE:
        return False, "ps_bridge.py not found"
    if not _ps_alive():
        return False, "Photoshop not running"

    sku     = row.get("SKU") or ""
    product = detect_product(sku)

    # UXP creates the canvas from scratch (blank CMYK/transparent, exact zone dims) —
    # no template file needed, so no "No template found" failure mode for standard orders.
    template_path = ""

    # Build zones dict from the order row, with full font + dimension info for UXP
    # use_topaz: True when IsTopazImageProcess=1 (Topaz done).
    # IsTopazImageProcess=None means either text-only OR image not yet Topaz'd —
    # caller already resolved which image filename to use before reaching here.
    _topaz_flag = row.get("IsTopazImageProcess")
    use_topaz   = (_topaz_flag == 1 or _topaz_flag is True)
    zone_map = {
        "front":  ("FrontText",  "FrontFonts",  "FrontColours",  "FrontImage",  "FrontImageJSON",  "FrontPreviewImage",  "FrontTopazImage"),
        "back":   ("BackText",   "BackFonts",   "BackColours",   "BackImage",   "BackImageJSON",   "BackPreviewImage",   "BackTopazImage"),
        "pocket": ("PocketText", "PocketFonts", "PocketColours", "PocketImage", "PocketImageJSON", "PocketPreviewImage", "PocketTopazImage"),
        "sleeve": ("SleeveText", "SleeveFonts", "SleeveColours", "SleeveImage", "SleeveImageJSON", "SleevePreviewImage", "SleeveTopazImage"),
    }
    product_canvas = get_product_canvas(sku, product)
    garment_rgb    = get_garment_rgb(sku)
    bg_flag_cols   = {
        "front":  "IsFrontBgRemove",
        "back":   "IsBackBgRemove",
        "pocket": "IsPocketBgRemove",
        "sleeve": "IsSleeveBgRemove",
    }
    zones = {}
    for zone_name, (txt_col, font_col, col_col, img_col, json_col, prev_col, topaz_col) in zone_map.items():
        text_raw  = (row.get(txt_col) or "").strip()
        img_files = resolve_zone_images(row, use_topaz, topaz_col, json_col, img_col)
        if not text_raw and not img_files:
            continue

        # Pocket "Left & Right" combo carries 2 images — a dict can't hold two
        # images under one "pocket" key, so build pocket_left/pocket_right as
        # separate zone entries, each with its own image/font/colour/label.
        if zone_name == "pocket" and len(img_files) >= 2:
            sides = [("pocket_left", "Left", img_files[0]), ("pocket_right", "Right", img_files[1])]
        else:
            sides = [(zone_name, None, img_files[0] if img_files else "")]

        for zone_key, side, img_raw in sides:
            font_name = parse_font(row.get(font_col) or "", side)
            zone_w, zone_h = product_canvas.get(zone_name, product_canvas.get("front", (9600, 9600)))

            # Background removal: apply before handing the image to Photoshop.
            # Triggered by force_bg_remove (CLI flag), designer DB flag, or auto-detection.
            img_path = find_image(img_raw) if img_raw else None
            # If Topaz image download failed, fall back to original image
            if not img_path and use_topaz and img_raw:
                _orig_files = resolve_zone_images(row, False, topaz_col, json_col, img_col)
                _side_idx   = 1 if side == "Right" else 0
                _orig_raw   = _orig_files[_side_idx] if len(_orig_files) > _side_idx else (_orig_files[0] if _orig_files else "")
                if _orig_raw:
                    log(f"  Topaz image unavailable [{zone_key}], falling back to original", "WARN")
                    img_path = find_image(_orig_raw)
            if img_path and garment_rgb:
                db_flag = bool(row.get(bg_flag_cols.get(zone_name, ""), False))
                should_remove = force_bg_remove or db_flag
                try:
                    from PIL import Image as _PILImage
                    _src = _PILImage.open(img_path).convert("RGBA")
                    if not should_remove:
                        should_remove = image_bg_matches_garment(_src, garment_rgb)
                    if should_remove:
                        _cleaned  = remove_background_colourkey(_src, garment_rgb)
                        _out_path = img_path.rsplit(".", 1)[0] + "_bgremoved.png"
                        _cleaned.save(_out_path, "PNG")
                        img_path  = _out_path
                        log(f"  bg-remove ({'forced' if (force_bg_remove or db_flag) else 'auto'}) [{zone_key}]: {garment_rgb}", "INFO")
                except Exception as _e:
                    log(f"  bg-remove failed [{zone_key}]: {_e}", "WARN")

            # Derive Photoshop font fields — UXP batchPlay uses these to set the font
            _ps_name, _ps_family, _ps_style = get_ps_font(font_name)
            _text_lines  = parse_texts(text_raw) if text_raw and side is None else []
            _colour_hex  = parse_colour(row.get(col_col) or "", side)

            # Premium/SVG colour fonts: use black as base colour so Photoshop has
            # something to render. The font's own SVG texture overrides the colour.
            # Passing "" (empty) leaves no colour → text is invisible.
            if _text_lines and _is_svg_only_font(font_name):
                _colour_hex = "#000000"

            _emoji_text_path = None

            zones[zone_key] = {
                "customer_image":   img_path,
                "emoji_text_image": _emoji_text_path,
                "text_lines":       _text_lines,
                "font_name":      font_name,
                "font_ps_name":   _ps_name,
                "font_family":    _ps_family,
                "font_style":     _ps_style,
                "colour_hex":     _colour_hex,
                "label":          make_zone_label(zone_key.replace("_", " "), sku),
                "zone_w_px":      zone_w,
                "zone_h_px":      zone_h,
                "preview_image":  (row.get(prev_col) or None),
            }

    if not zones:
        return False, "No zones — no image or text data"

    # canvas_w = widest zone width (used as template canvas width)
    canvas_w = max(z["zone_w_px"] for z in zones.values())
    canvas_h = max(z["zone_h_px"] for z in zones.values())

    # Quantity: UXP plugin stacks N identical copies vertically with a 1cm gap between each.
    # quantity_override is set when multiple order rows share the same design (different sizes/colours)
    # — the caller sums their quantities so all copies land on one canvas.
    if quantity_override is not None:
        quantity = max(1, quantity_override)
    else:
        quantity = max(1, int(row.get("Quantity") or 1))
    if quantity > 1:
        log(f"  Quantity={quantity} — UXP will stack {quantity} copies vertically", "INFO")

    # Socks: always print twice regardless of order quantity (one pair = two socks).
    # Each sock canvas is 6×6cm; doubling produces two designs stacked on one DTF sheet.
    if product == "socks":
        quantity = quantity * 2
        log(f"  Socks pair: printing {quantity} copies (2 per ordered pair)", "INFO")

    # ── Split into multiple PSDs if the stacked canvas would exceed the ~30,000px
    # PSD format height ceiling — same fix and reasoning as _build_psd_multi_via_photoshop
    # (regular .psd files cannot exceed 30000px per side; UXP's save can silently fail
    # past that while still reporting "done"). Every copy here is identical, so the
    # per-copy height is one fixed value rather than something computed per item.
    _LABEL_PX, _GAP_PX, _QTY_GAP_PX = 126, 126, 126
    _SAFE_MAX_H = 28000  # margin below the hard 30000px ceiling

    per_copy_h    = sum(_LABEL_PX + z["zone_h_px"] + _GAP_PX for z in zones.values())
    max_per_chunk = max(1, int((_SAFE_MAX_H + _QTY_GAP_PX) // (per_copy_h + _QTY_GAP_PX)))

    remaining, chunk_sizes = quantity, []
    while remaining > 0:
        k = min(max_per_chunk, remaining)
        chunk_sizes.append(k)
        remaining -= k

    if len(chunk_sizes) > 1:
        log(f"  Quantity={quantity} needs {len(chunk_sizes)} PSDs — combined stack would exceed the 30,000px PSD height limit", "INFO")

    output_paths = []
    for idx, k in enumerate(chunk_sizes):
        if idx == 0:
            chunk_out_path = out_path
        else:
            _root = out_path[:-4] if out_path.lower().endswith(".psd") else out_path
            chunk_out_path = f"{_root}-{idx}.psd"

        _ps_submit(order_id, template_path, zones, chunk_out_path,
                   canvas_w_px=canvas_w, canvas_h_px=canvas_h, combined=True,
                   quantity=k)
        # Increase timeout for multi-copy chunks (each copy takes ~same time)
        timeout = 600 * max(1, k // 2) if k > 2 else 600
        success = _ps_wait(order_id, timeout_sec=timeout, use_uxp=True)
        if not success:
            return False, f"Photoshop UXP timed out or errored (batch {idx + 1}/{len(chunk_sizes)})"
        if not os.path.isfile(chunk_out_path):
            return False, f"UXP reported done but output file missing: {chunk_out_path} (batch {idx + 1}/{len(chunk_sizes)})"
        output_paths.append(chunk_out_path)

    if len(output_paths) == 1:
        return True, output_paths[0]
    return True, f"{len(output_paths)} PSD(s): " + ", ".join(os.path.basename(p) for p in output_paths)


def _build_psd_multi_via_photoshop(order_id, group_rows, out_path, force_bg_remove=False):
    """Build ONE PSD with multiple differently-customised items stacked vertically on one canvas."""
    if not PS_BRIDGE_AVAILABLE:
        return False, "ps_bridge.py not found"
    if not _ps_alive():
        return False, "Photoshop not running"

    first_row    = group_rows[0]
    first_sku    = first_row.get("SKU") or ""
    first_product = detect_product(first_sku)

    # UXP creates the canvas from scratch — no template file needed (see _build_psd_via_photoshop).
    template_path = ""

    _zone_map = {
        "front":  ("FrontText",  "FrontFonts",  "FrontColours",  "FrontImage",  "FrontImageJSON",  "FrontPreviewImage",  "FrontTopazImage"),
        "back":   ("BackText",   "BackFonts",   "BackColours",   "BackImage",   "BackImageJSON",   "BackPreviewImage",   "BackTopazImage"),
        "pocket": ("PocketText", "PocketFonts", "PocketColours", "PocketImage", "PocketImageJSON", "PocketPreviewImage", "PocketTopazImage"),
        "sleeve": ("SleeveText", "SleeveFonts", "SleeveColours", "SleeveImage", "SleeveImageJSON", "SleevePreviewImage", "SleeveTopazImage"),
    }
    _bg_cols = {"front":"IsFrontBgRemove","back":"IsBackBgRemove","pocket":"IsPocketBgRemove","sleeve":"IsSleeveBgRemove"}

    multi_items = []
    canvas_w = 0
    canvas_h = 0

    for row in group_rows:
        row_sku      = row.get("SKU") or ""
        row_product  = detect_product(row_sku)
        prod_canvas  = get_product_canvas(row_sku, row_product)
        garment_rgb  = get_garment_rgb(row_sku)
        use_topaz    = (row.get("IsTopazImageProcess") in (1, True))

        zones = {}
        for zone_name, (txt_col, font_col, col_col, img_col, json_col, prev_col, topaz_col) in _zone_map.items():
            text_raw  = (row.get(txt_col) or "").strip()
            img_files = resolve_zone_images(row, use_topaz, topaz_col, json_col, img_col)
            if not text_raw and not img_files:
                continue

            # Pocket "Left & Right" combo carries 2 images — split into
            # pocket_left/pocket_right zone entries (see _build_psd_via_photoshop).
            if zone_name == "pocket" and len(img_files) >= 2:
                sides = [("pocket_left", "Left", img_files[0]), ("pocket_right", "Right", img_files[1])]
            else:
                sides = [(zone_name, None, img_files[0] if img_files else "")]

            for zone_key, side, img_raw in sides:
                font_name        = parse_font(row.get(font_col) or "", side)
                zone_w, zone_h   = prod_canvas.get(zone_name, prod_canvas.get("front", (9600, 9600)))
                canvas_w         = max(canvas_w, zone_w)
                canvas_h         = max(canvas_h, zone_h)

                img_path = find_image(img_raw) if img_raw else None
                if not img_path and use_topaz and img_raw:
                    _orig_files = resolve_zone_images(row, False, topaz_col, json_col, img_col)
                    _side_idx   = 1 if side == "Right" else 0
                    _orig = _orig_files[_side_idx] if len(_orig_files) > _side_idx else (_orig_files[0] if _orig_files else "")
                    if _orig:
                        log(f"  Topaz unavailable [{zone_key}], falling back to original", "WARN")
                        img_path = find_image(_orig)
                if img_path and garment_rgb:
                    db_flag = bool(row.get(_bg_cols.get(zone_name, ""), False))
                    should_remove = force_bg_remove or db_flag
                    try:
                        from PIL import Image as _PILImage
                        _src = _PILImage.open(img_path).convert("RGBA")
                        if not should_remove:
                            should_remove = image_bg_matches_garment(_src, garment_rgb)
                        if should_remove:
                            _cl  = remove_background_colourkey(_src, garment_rgb)
                            _op  = img_path.rsplit(".", 1)[0] + "_bgremoved.png"
                            _cl.save(_op, "PNG")
                            img_path = _op
                    except Exception as _e:
                        log(f"  bg-remove failed [{zone_key}]: {_e}", "WARN")

                _ps_name, _ps_family, _ps_style = get_ps_font(font_name)
                _text_lines = parse_texts(text_raw) if text_raw and side is None else []
                _colour_hex = parse_colour(row.get(col_col) or "", side)
                if _text_lines and _is_svg_only_font(font_name):
                    if not _colour_hex:
                        _colour_hex = "#000000"

                _emoji_text_path = None

                zones[zone_key] = {
                    "customer_image":   img_path,
                    "emoji_text_image": _emoji_text_path,
                    "text_lines":       _text_lines,
                    "font_name":      font_name,
                    "font_ps_name":   _ps_name,
                    "font_family":    _ps_family,
                    "font_style":     _ps_style,
                    "colour_hex":     _colour_hex,
                    "label":          make_zone_label(zone_key.replace("_", " "), row_sku),
                    "zone_w_px":      zone_w,
                    "zone_h_px":      zone_h,
                    "preview_image":  (row.get(prev_col) or None),
                }

        if zones:
            qty = max(1, int(row.get("Quantity") or 1))
            for _ in range(qty):
                multi_items.append({"zones": zones, "sku": row_sku})
            log(f"  Multi-item built: {row_sku} qty={qty} zones={list(zones.keys())}", "INFO")

    if not multi_items:
        return False, "No zones found across all items"

    # ── Split into multiple PSDs if the stacked canvas would exceed the ~30,000px
    # PSD format height ceiling — regular .psd files cannot exceed 30000px per side
    # (a hard format limit, not a performance issue; index.js's resizeAllTemplates
    # already treats TARGET_H=30000 as "Max PSD height" for the same reason). Past
    # that size, UXP's save can silently fail while still reporting "done", which is
    # exactly the "reported done but file missing" failure this fixes.
    #
    # Chunk boundaries mirror the exact per-item height formula the UXP plugin uses
    # for job.multi_items (LABEL_PX + zone_h_px + GAP_PX per zone, plus QTY_GAP_PX
    # between items — see index.js's multi-item totalH calculation), so this lands
    # chunks correctly regardless of how many zones an item has or how tall the
    # product is, instead of assuming a fixed item count per file.
    _LABEL_PX, _GAP_PX, _QTY_GAP_PX = 126, 126, 126
    _SAFE_MAX_H = 28000  # margin below the hard 30000px ceiling

    def _item_height(item):
        return sum(_LABEL_PX + (z.get("zone_h_px") or 9600) + _GAP_PX for z in item["zones"].values())

    chunks, _cur, _cur_h = [], [], 0
    for item in multi_items:
        ih = _item_height(item)
        added = ih + (_QTY_GAP_PX if _cur else 0)
        if _cur and _cur_h + added > _SAFE_MAX_H:
            chunks.append(_cur)
            _cur, _cur_h = [item], ih
        else:
            _cur_h += added
            _cur.append(item)
    if _cur:
        chunks.append(_cur)

    if len(chunks) > 1:
        log(f"  Multi-item order needs {len(chunks)} PSDs — combined stack would exceed the 30,000px PSD height limit", "INFO")

    output_paths = []
    for idx, chunk in enumerate(chunks):
        if idx == 0:
            chunk_out_path = out_path
        else:
            _root = out_path[:-4] if out_path.lower().endswith(".psd") else out_path
            chunk_out_path = f"{_root}-{idx}.psd"

        _ps_submit(order_id, template_path,
                   chunk[0]["zones"],   # primary zones (first item) for template compat
                   chunk_out_path,
                   canvas_w_px=canvas_w or 9600,
                   canvas_h_px=canvas_h or 9600,
                   combined=True,
                   quantity=1,
                   multi_items=chunk)

        timeout = 600 * len(chunk)
        success = _ps_wait(order_id, timeout_sec=timeout, use_uxp=True)
        if not success:
            return False, f"Photoshop UXP timed out or errored (batch {idx + 1}/{len(chunks)})"
        if not os.path.isfile(chunk_out_path):
            return False, f"UXP reported done but output file missing: {chunk_out_path} (batch {idx + 1}/{len(chunks)})"
        output_paths.append(chunk_out_path)

    if len(output_paths) == 1:
        return True, output_paths[0]
    return True, f"{len(output_paths)} PSD(s): " + ", ".join(os.path.basename(p) for p in output_paths)


# ─── CONFIG ───────────────────────────────────────────────────────────────────
# Database connection is centralised in db.py (reads from .env).

# Paths — override any of these in your .env file
_base         = os.environ.get("VARSANY_BASE",    r"C:\gimpTest")
FONT_FOLDERS  = [os.path.join(_base, "Fonts")] + \
                [p.strip() for p in os.environ.get("VARSANY_FONTS_EXTRA", r"W:\fonts").split(",") if p.strip()] + \
                [r"C:\Windows\Fonts",  # system-wide fonts
                 os.path.join(os.environ.get("LOCALAPPDATA",""), r"Microsoft\Windows\Fonts")]  # per-user installed fonts
OUTPUT_FOLDER = os.environ.get("VARSANY_OUTPUT", os.path.join(_base, "Output"))
LOG_FILE      = os.environ.get("VARSANY_LOG",    os.path.join(_base, "batch_log.txt"))
TEMP_FOLDER   = os.environ.get("VARSANY_TEMP",   os.path.join(_base, "Temp"))

IMAGE_SERVER_URL = os.environ.get("IMAGE_SERVER_URL", "http://www.crssoft.co.uk/CustomOrderImages/")

DPI       = 320              # 320 DPI (pixels/inch) — production DTF resolution
PX_PER_CM = DPI / 2.54      # ~125.98 px/cm
K_GAMMA   = 1.0             # K channel gamma — 1.0 = no adjustment (linear ICC conversion)

os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# ── Semi-customised: template PSDs with named text layers ─────────────────────
# These orders open a pre-designed template and only edit specific text layers.
# No image placement, no blank canvas — just text substitution.
_JERSEY_TEMPLATE = "england Football Adult.psd"

SEMI_CUSTOM_TEMPLATE_MAP = {
    # ── England Football ──────────────────────────────────────────────────────
    "FootballAdultTee":         _JERSEY_TEMPLATE,
    "FootballKidsTee":          _JERSEY_TEMPLATE,
    "FootballEngAdultTee":      _JERSEY_TEMPLATE,
    "FootballEngKidsTee":       _JERSEY_TEMPLATE,
    "EngFootball_Tee":          _JERSEY_TEMPLATE,
    "EngFootballKids_Tee":      _JERSEY_TEMPLATE,
    "EngFootballKidsTee":       _JERSEY_TEMPLATE,
    "EngFBallset01Kids":        _JERSEY_TEMPLATE,
    "KidsEngSet":               _JERSEY_TEMPLATE,
    "PPFBall01_Tee":            _JERSEY_TEMPLATE,
    "PPFBall01KidsTee":         _JERSEY_TEMPLATE,
    "PIFBall01_Tee":            _JERSEY_TEMPLATE,   # Ireland Football
    "PIFBall01KidsTee":         _JERSEY_TEMPLATE,
    "PNFBall01_Tee":            _JERSEY_TEMPLATE,   # Netherlands Football
    "PNFBall01KidsTee":         _JERSEY_TEMPLATE,
    "PWFBall01_Tee":            _JERSEY_TEMPLATE,   # Wales Football
    "UKOlympicKids_Tee":        _JERSEY_TEMPLATE,
    "FootballPortKidsTee":      _JERSEY_TEMPLATE,
    "FootballWalAdultTee":      _JERSEY_TEMPLATE,
    "FootballGerKidsTee":       _JERSEY_TEMPLATE,
    # ── Scotland Football ─────────────────────────────────────────────────────
    "Scotland_Football":        _JERSEY_TEMPLATE,
    "Scotland_FootballKids":    _JERSEY_TEMPLATE,
    "FootballScot3KidsTee":     _JERSEY_TEMPLATE,
    "FootballScoKidsTee":       _JERSEY_TEMPLATE,
    "ScotFBallset01Kids":       _JERSEY_TEMPLATE,
    "FootballbabyVscot":        _JERSEY_TEMPLATE,
    # ── Other Countries ───────────────────────────────────────────────────────
    "PerFrance01":              _JERSEY_TEMPLATE,
    "PerBrazil01":              _JERSEY_TEMPLATE,
    "PerSpain01":               _JERSEY_TEMPLATE,
    "PerNetherlands01":         _JERSEY_TEMPLATE,
    "PerArgentina01":           _JERSEY_TEMPLATE,
    "PerPortugal01":            _JERSEY_TEMPLATE,
    "PerGermany01":             _JERSEY_TEMPLATE,
    "PerItalia01":              _JERSEY_TEMPLATE,
    "PinkCymru01KidsTee":       _JERSEY_TEMPLATE,
    # ── Club / Champions ──────────────────────────────────────────────────────
    "PerChampARSENAL01":        _JERSEY_TEMPLATE,
    "Lvrpool201_Tee":           _JERSEY_TEMPLATE,
    "VillaperChamps2601":       _JERSEY_TEMPLATE,
    "PalacePer01_Tee":          _JERSEY_TEMPLATE,
    "LBalls01Tee":              _JERSEY_TEMPLATE,
    "LBalls02Tee":              _JERSEY_TEMPLATE,
    # ── Rugby ─────────────────────────────────────────────────────────────────
    "ScotlandRugby01":          _JERSEY_TEMPLATE,
    "WalesRugby01":             _JERSEY_TEMPLATE,
    "IrelandRugby01":           _JERSEY_TEMPLATE,
    "EngRugby01":               _JERSEY_TEMPLATE,
    "ERugby01":                 _JERSEY_TEMPLATE,
    "EngFBallset01":            _JERSEY_TEMPLATE,
    # ── Polo Jerseys ──────────────────────────────────────────────────────────
    "PEngFB01PoloJersy":        _JERSEY_TEMPLATE,
    "PEngR01PoloJersy":         _JERSEY_TEMPLATE,
    "PSct01PoloJersy":          _JERSEY_TEMPLATE,
    "PWals01PoloJersy":         _JERSEY_TEMPLATE,
    "PWFBall01PoloJersy":       _JERSEY_TEMPLATE,
}

# SEMI_CUSTOM_OUTPUT_FOLDER is built at runtime inside run_batch (uses today's date folder)

# ── Slot config per template ───────────────────────────────────────────────────
# slot_h_px  : height of one jersey/item slot in the template (pixels)
# max_slots  : how many slots the template has (user set this up manually)
# player_layer / number_layer : base Photoshop layer names for the text fields
# Photoshop auto-naming when duplicating: "Player", "Player copy", "Player copy 2" ...
SEMI_CUSTOM_SLOT_CONFIG = {
    "england Football Adult.psd":  {"slot_h_px": 3780, "max_slots": 7, "player_layer": "Player", "number_layer": "08"},
    "england Football Kids.psd":   {"slot_h_px": 3780, "max_slots": 7, "player_layer": "Player", "number_layer": "08"},
    "scotland Football Adult.psd": {"slot_h_px": 3780, "max_slots": 7, "player_layer": "Player", "number_layer": "08"},
    "scotland Football Kids.psd":  {"slot_h_px": 3780, "max_slots": 7, "player_layer": "Player", "number_layer": "08"},
}


def _ps_copy_layer_name(base, slot_idx):
    """Return the Photoshop auto-copy layer name for a given 0-based slot index.
    Slot 0 → 'Player'
    Slot 1 → 'Player copy'
    Slot 2 → 'Player copy 2'
    Slot N → 'Player copy N'
    """
    if slot_idx == 0:
        return base
    if slot_idx == 1:
        return f"{base} copy"
    return f"{base} copy {slot_idx}"


def format_jersey_number(raw):
    """Return jersey number exactly as customer entered it (trimmed whitespace only)."""
    return str(raw).strip()


def get_semi_custom_template(sku):
    """Return template filename for a semi-custom SKU, or None if not recognised."""
    for prefix, template in SEMI_CUSTOM_TEMPLATE_MAP.items():
        if sku.startswith(prefix):
            return template
    return None

# Images are downloaded on-demand from IMAGE_SERVER_URL — no local index needed.

# ─── BUILD FONT INDEX ─────────────────────────────────────────────────────────
# Maps normalised font name -> full file path
# e.g. "bebasneuepro" -> "C:\\Varsany\\Fonts\\BebasNeue-Regular.ttf"

FONT_INDEX = {}
for _folder in FONT_FOLDERS:
    if os.path.exists(_folder):
        for _f in os.listdir(_folder):
            if _f.lower().endswith(('.ttf', '.otf')):
                _norm = os.path.splitext(_f)[0].lower()
                _norm = _norm.replace(' ','').replace('-','').replace('_','')
                FONT_INDEX[_norm] = os.path.join(_folder, _f)

# Explicit aliases so database font names map to actual filenames
FONT_ALIASES = {
    # Standard fonts
    "abel":             "abelregular",
    "arial":            "arial",
    "arialbold":        "arial",
    "bebasneuefree":    "bebasneueregular",
    "bebasneuepro":     "bebasneueregular",
    "bebasneue":        "bebasneueregular",
    "chewy":            "chewyregular",
    "fondamento":       "fondamentoregular",
    # "helvetica" — install Helvetica font manually, no alias needed
    "lato":             "latoregular",
    "latobold":         "latobold",
    "permanentmarker":  "permanentmarkerregular",
    "roboto":           "robotoregular",
    "robotoregular":    "robotoregular",
    "russoone":         "russooneregular",
    "ultra":            "ultraregular",
    "verdana":          "verdana",
    # Premium texture fonts — DB/Amazon display name → FONT_INDEX key.
    # Left side is the normalised form (lowercase, no spaces/hyphens/underscores).
    # Amazon display name  →  what designers call it  →  key
    "texturefont":            "smartkids",    # "Texture font"   → Smart Kids
    "texture":                "smartkids",
    "smartkids":              "smartkids",
    "smartkidsregular":       "smartkids",
    "blockfont":              "colorfulblocks",  # "Block font"  → Colorful Block
    "colorfulblock":          "colorfulblocks",
    "colorfulblocks":         "colorfulblocks",
    "colorfulblocksregular":  "colorfulblocks",
    "paintfont":              "paintsplashesrainbow",  # "Paint font" → Paint Splashes
    "paintsplashes":          "paintsplashesrainbow",
    "paintsplashesrainbow":   "paintsplashesrainbow",
    "paintsplashesrainbowregular": "paintsplashesrainbow",
    "mermaidfont":            "wavemermaid",   # "Mermaid font" → Mermaid Regular (Wavemermaid)
    "mermaid":                "wavemermaid",
    "mermaidregular":         "wavemermaid",
    "wavemermaid":            "wavemermaid",
    "wavemermaidregular":     "wavemermaid",
    "reflectionfont":         "refractionray", # "Reflection font" → RefractionRay Regular
    "reflection":             "refractionray",
    "refractionray":          "refractionray",
    "refractionrayregular":   "refractionray",
    "camofont":               "camoblock",     # "Camo font"    → Camo Block Regular
    "camo":                   "camoblock",
    "camoblock":              "camoblock",
    "camoblockregular":       "camoblock",
    "spideyfont":             "spiderweb",     # "Spidey font"  → Spider Web Regular
    "spidey":                 "spiderweb",
    "spiderweb":              "spiderweb",
    "spiderwebregular":       "spiderweb",
    "cozyfont":               "cozywinter",    # "Cozy font"    → Cozy Winter Regular
    "cozy":                   "cozywinter",
    "cozywinter":             "cozywinter",
    "cozywinterregular":      "cozywinter",
    "footballfont":           "football",      # "Football font" → Football Regular (FOOTBALL.OTF)
    "football":               "football",
    "footballregular":        "football",
    "flowerfont":             "tropicalflower",  # "Flower font" → Tropical Flower Regular
    "flower":                 "tropicalflower",
    "tropicalflower":         "tropicalflower",
    "tropicalflowerregular":  "tropicalflower",
    # Vinyl — TTF is installed
    "vinyl":            "vinylfont",
    "vinylFont":        "vinylfont",
    "vinyl font":       "vinylfont",
    "vinylfont":        "vinylfont",
    # Non-print methods (flag to designer — no TTF, fall through to Arial placeholder)
    "rhinestone":                   None,
    "rhinestonefont":               None,
    "embroidery":                   None,
    "embroideryfont":               None,
    "emroideryfont":                None,
    "crystalfont":                  None,
    "varsanycrystal":               None,
    "varsanycrystalfont":           None,
    "varsanyrhinestonefont":        None,
    "25mmcapsrhinestonefont":       None,
    # Custom fonts — files not yet installed (fall through to Arial)
    "bsl":                          None,
    "dtftext":                      None,
    "glovesfont":                   None,
    "shortsfont":                   None,
    "supervibes":                   None,
    "varsany":                      None,
    "welliesfont":                  None,
    "wellisfont":                   None,
}

print(f"  Fonts indexed: {list(FONT_INDEX.keys())}")

# ─── PHOTOSHOP POSTSCRIPT FONT NAME MAP ──────────────────────────────────────
# Maps normalised DB font name → (ps_name, family, style) for Photoshop UXP.
# PS name is the exact PostScript name embedded in the font file (nameID 6).
# Photoshop matches fonts by PostScript name — wrong name = Myriad Pro fallback.
PS_FONT_MAP = {
    # Standard web/system fonts
    "arial":                ("Arial-BoldMT",           "Arial",            "Bold"),
    "arialbold":            ("Arial-BoldMT",           "Arial",            "Bold"),
    "helvetica":            ("Helvetica",              "Helvetica",        "Regular"),
    "helveticabold":        ("Helvetica-Bold",         "Helvetica",        "Bold"),
    "verdana":              ("Verdana",                "Verdana",          "Regular"),
    "timesnewroman":        ("TimesNewRomanPSMT",      "Times New Roman",  "Regular"),
    "georgia":              ("Georgia",                "Georgia",          "Regular"),
    "impact":               ("Impact",                 "Impact",           "Normal"),
    "tahoma":               ("Tahoma",                 "Tahoma",           "Regular"),
    # Custom fonts — PostScript names read directly from font files
    "abel":                 ("Abel-Regular",           "Abel",             "Regular"),
    "abelregular":          ("Abel-Regular",           "Abel",             "Regular"),
    "bebasneue":            ("BebasNeue-Regular",      "Bebas Neue",       "Regular"),
    "bebasneueregular":     ("BebasNeue-Regular",      "Bebas Neue",       "Regular"),
    "bebasneuefree":        ("BebasNeue-Regular",      "Bebas Neue",       "Regular"),
    "bebasneuepro":         ("BebasNeue-Regular",      "Bebas Neue",       "Regular"),
    "chewy":                ("Chewy-Regular",          "Chewy",            "Regular"),
    "chewyregular":         ("Chewy-Regular",          "Chewy",            "Regular"),
    "fondamento":           ("Fondamento-Regular",     "Fondamento",       "Regular"),
    "fondamentoregular":    ("Fondamento-Regular",     "Fondamento",       "Regular"),
    "lato":                 ("Lato-Regular",           "Lato",             "Regular"),
    "latoregular":          ("Lato-Regular",           "Lato",             "Regular"),
    "latobold":             ("Lato-Regular",           "Lato",             "Regular"),  # Bold file missing, use Regular
    "permanentmarker":      ("PermanentMarker-Regular","Permanent Marker", "Regular"),
    "permanentmarkerregular":("PermanentMarker-Regular","Permanent Marker","Regular"),
    "roboto":               ("Roboto-Regular",         "Roboto",           "Regular"),
    "robotoregular":        ("Roboto-Regular",         "Roboto",           "Regular"),
    "russoone":             ("RussoOne-Regular",       "Russo One",        "Regular"),
    "russooneregular":      ("RussoOne-Regular",       "Russo One",        "Regular"),
    "ultra":                ("Ultra-Regular",          "Ultra",            "Regular"),
    "ultraregular":         ("Ultra-Regular",          "Ultra",            "Regular"),
    "vinylfont":            ("VinylFont-Regular",      "VinylFont",        "Regular"),
    "vinyl":                ("VinylFont-Regular",      "VinylFont",        "Regular"),
    "vinylFont":            ("VinylFont-Regular",      "VinylFont",        "Regular"),
    # Premium texture / specialty fonts (PS names read from font files in C:\Varsany\Fonts\)
    "smartkids":            ("SmartKidsRegular",        "Smart Kids",            "Regular"),
    "colorfulblocks":       ("ColorfulBlocksRegular",   "Colorful Blocks",       "Regular"),
    "paintsplashesrainbow": ("PaintSplashesRainbow",    "Paint Splashes Rainbow","Regular"),
    "wavemermaid":          ("WavemermaidRegular",       "Wavemermaid",           "Regular"),
    "refractionray":        ("RefractionRayRegular",    "RefractionRay",         "Regular"),
    "camoblock":            ("CamoBlockRegular",        "Camo Block",            "Regular"),
    "spiderweb":            ("SpiderWebRegular",        "Spider Web",            "Regular"),
    "cozywinter":           ("CozyWinterRegular",       "Cozy Winter",           "Regular"),
    "football":             ("FootBallRegular",          "FootBall",              "Regular"),
    "tropicalflower":       ("TropicalFlowerRegular",   "Tropical Flower",       "Regular"),
}

def get_ps_font(db_font_name):
    """Return (ps_name, family, style) for use in Photoshop UXP batchPlay.
    Falls back to Arial if the font isn't in PS_FONT_MAP.
    Also applies FONT_ALIASES so aliased names resolve correctly."""
    norm = (db_font_name or "").lower().replace(" ", "").replace("-", "").replace("_", "")
    # Apply FONT_ALIASES first (e.g. "helvetica" → "arial")
    resolved = FONT_ALIASES.get(norm, norm)
    if resolved and resolved in PS_FONT_MAP:
        return PS_FONT_MAP[resolved]
    if norm in PS_FONT_MAP:
        return PS_FONT_MAP[norm]
    # Try partial match against PS_FONT_MAP keys
    for key in PS_FONT_MAP:
        if norm.startswith(key) or key.startswith(norm):
            return PS_FONT_MAP[key]
    log(f"  get_ps_font: no PS name for '{db_font_name}' (norm='{norm}') — using Arial", "WARN")
    return ("Arial-BoldMT", "Arial", "Bold")

# Keys in FONT_INDEX that belong to premium texture/specialty fonts
PREMIUM_FONT_KEYS = {
    "smartkids", "colorfulblocks", "paintsplashesrainbow",
    "wavemermaid", "refractionray", "camoblock", "spiderweb",
    "cozywinter", "football", "tropicalflower",
}

# Per-font tracking multiplier applied to every hmtx advance width.
# Full-width glyph images preserve transparent sidebearings, so T < 1.0 is safe when
# the font has genuine LSB/RSB (the overlap region is transparent, not visible artwork).
# Fonts whose artwork fills the full advance width must stay at 1.0 to avoid pixel overlap.
FONT_TRACKING = {
    # Reflection Font (Refraction Ray.otf): PNG bitmaps in SVG fill only ~65.5% of advance.
    # Chrome pixel measurement A-Z: mean fill=0.655, min-safe=0.680 (S→T pair).
    # T=0.68 → letters nearly touching; T=1.0 → ~200px visible gap per letter.
    "refractionray": 0.68,
    # Smart Kids font: SVG artwork fills ~67.9% of advance, min-safe=0.712 (Chrome measurement A-Z/a-z).
    "smartkids": 0.72,
    # Cozy Winter font: SVG artwork fills ~57.8% of advance, min-safe=0.599 (Chrome measurement A-Z).
    "cozywinter": 0.61,
    # camoblock, colorfulblocks, football, paintsplashesrainbow, wavemermaid,
    # spiderweb: artwork fills full advance → 1.0 (no entry = default).
}

# Per-font character-category scale corrections.
# Generated by measure_premium_fonts.py — do not edit by hand.
# Each entry: font_key → {category: ratio_relative_to_cap_height}
# "upper"=A-Z, "lower"=a-z, "digit"=0-9, "special"=punctuation/symbols.
# 1.0 means the category renders at the same height as uppercase (no correction).
# Omitting a font or category means no correction for that font/category.
FONT_CHAR_METRICS = {
    # Generated by measure_premium_fonts.py on 2026-05-15.
    # Ratios are relative to uppercase cap height (upper=1.0 is always reference).
    # Values within 2% of 1.0 are ignored at render time (no resize applied).
    # None means no SVG glyphs in that category — no correction applied.
    "smartkids": {
        "upper":   1.0,
        "lower":   0.668,
        "digit":   0.994,
        "special": None,
    },
    "colorfulblocks": {
        "upper":   1.0,
        "lower":   1.0,
        "digit":   1.013,   # within 2% — skipped
        "special": None,
    },
    "paintsplashesrainbow": {
        "upper":   1.0,
        "lower":   1.0,
        "digit":   1.0,
        "special": 1.0,
    },
    "wavemermaid": {
        "upper":   1.0,
        "lower":   1.0,
        "digit":   1.0,
        "special": 1.0,
    },
    "refractionray": {
        "upper":   1.0,
        "lower":   0.758,
        "digit":   0.758,
        "special": None,
    },
    "camoblock": {
        "upper":   1.0,
        "lower":   0.893,
        "digit":   0.892,
        "special": None,
    },
    "spiderweb": {
        "upper":   1.0,
        "lower":   1.0,
        "digit":   1.0,
        "special": 1.0,
    },
    "cozywinter": {
        "upper":   1.0,
        "lower":   1.0,
        "digit":   1.0,
        "special": 1.0,
    },
    "football": {
        "upper":   1.0,
        "lower":   1.0,
        "digit":   1.013,   # within 2% — skipped
        "special": None,
    },
    "tropicalflower": {
        "upper":   1.0,
        "lower":   1.0,
        "digit":   1.017,   # within 2% — skipped
        "special": None,    # no SVG special chars in this font
    },
}

def _char_category(ch):
    """Return 'upper', 'lower', 'digit', 'special', or 'other' for a character."""
    if ch.isupper():   return "upper"
    if ch.islower():   return "lower"
    if ch.isdigit():   return "digit"
    if ch.isspace():   return "other"
    return "special"

def _resolve_font_key(font_name):
    """Return the FONT_INDEX key for font_name, or None if unmappable."""
    if not font_name:
        return None
    norm = font_name.lower().replace(" ", "").replace("-", "").replace("_", "")
    alias = FONT_ALIASES.get(norm)
    if alias is not None:
        return alias
    return norm if norm in FONT_INDEX else None

def has_premium_font(row):
    """Return True if any zone in this order row uses a premium font.
    Checks: (1) JSON PremiumFont field in FrontFonts/BackFonts (post-Feb-2026 format),
            (2) FrontPremiumFont='Yes' DB column (live DB),
            (3) font name resolves to a PREMIUM_FONT_KEY (pre-Feb-2026 fallback).
    """
    # Post-Feb-2026: font stored as JSON {"NormalFont":"...","PremiumFont":"..."}
    for col in ("FrontFonts", "BackFonts", "PocketFonts", "SleeveFonts"):
        if parse_is_premium_font(row.get(col) or ""):
            return True
    # Live DB has dedicated FrontPremiumFont column
    for col in ("FrontPremiumFont", "BackPremiumFont", "PocketPremiumFont", "SleevePremiumFont"):
        val = row.get(col)
        if val and str(val).strip().lower() in ("yes", "1", "true"):
            return True
    # Pre-Feb-2026 fallback: font name matches a known premium key
    for col in ("FrontFonts", "BackFonts", "PocketFonts", "SleeveFonts"):
        key = _resolve_font_key(row.get(col) or "")
        if key and key in PREMIUM_FONT_KEYS:
            return True
    return False


def cm_to_px(cm): return int(round(cm * PX_PER_CM))

# ─── CANVAS SIZES — from owner Canvases.xlsx ──────────────────────────────────
PRODUCT_CANVAS = {
    # T-shirts
    "adulttshirt":    {"front": (cm_to_px(30), cm_to_px(30)), "back": (cm_to_px(30), cm_to_px(30)), "pocket": (cm_to_px(9),  cm_to_px(9))},
    "kidstshirt":     {"front": (cm_to_px(23), cm_to_px(30)), "back": (cm_to_px(23), cm_to_px(30)), "pocket": (cm_to_px(9),  cm_to_px(9))},
    # Hoodies
    "adulthoodie":    {"front": (cm_to_px(25), cm_to_px(25)), "back": (cm_to_px(25), cm_to_px(25)), "pocket": (cm_to_px(9),  cm_to_px(9)), "sleeve": (cm_to_px(9), cm_to_px(7))},
    "kidshoodie":     {"front": (cm_to_px(23), cm_to_px(20)), "back": (cm_to_px(23), cm_to_px(20)), "pocket": (cm_to_px(9),  cm_to_px(9))},
    # Bags
    "totebag":        {"front": (cm_to_px(28), cm_to_px(28)), "back": (cm_to_px(28), cm_to_px(28))},
    "backpack":       {"front": (cm_to_px(18), cm_to_px(12))},
    "makeupbag":      {"front": (cm_to_px(23), cm_to_px(14))},
    "shoebag":        {"front": (cm_to_px(23), cm_to_px(14))},
    "shoebag2":       {"front": (cm_to_px(14), cm_to_px(14))},
    "stringbag":      {"front": (cm_to_px(22), cm_to_px(24))},
    "pebag":          {"front": (cm_to_px(22), cm_to_px(24))},   # PE bag, every design (AnyTxtPEBag_, PEBagCustom_, FBall0xPEBag_ ...)
    "knittingbag":    {"front": (cm_to_px(25), cm_to_px(21))},
    # Accessories
    "buckethat":      {"front": (cm_to_px(11), cm_to_px(4))},
    "beanie":         {"front": (cm_to_px(9.5),cm_to_px(4.5))},
    "socks":          {"front": (cm_to_px(6),  cm_to_px(6))},   # one sock = 6×6cm; doubled per-pair below
    "seatbelt":       {"front": (cm_to_px(18), cm_to_px(4))},
    "footballshorts": {"front": (756,          1134)},
    # Baby / Kids
    "babyvest":       {"front": (cm_to_px(15), cm_to_px(17))},
    "sleepsuit":      {"front": (cm_to_px(13), cm_to_px(18))},
    "hodieblanket":   {"front": (cm_to_px(17), cm_to_px(5))},
    # Home / Other
    "cushion":        {"front": (cm_to_px(30), cm_to_px(30))},
    "memorialplaque": {"front": (cm_to_px(13), cm_to_px(8))},
    "golftowel":      {"front": (cm_to_px(17), cm_to_px(17))},
    "golfcase":       {"front": (cm_to_px(15), cm_to_px(6))},
    "slipper":        {"front": (cm_to_px(6),  cm_to_px(6))},
    # Default fallback
    "default":        {"front": (cm_to_px(30), cm_to_px(30)), "back": (cm_to_px(30), cm_to_px(30)), "pocket": (cm_to_px(9), cm_to_px(9))},
}

# Kids aged 1-4 get a smaller print area: 16 x 16 cm for front and back (owner request, 2026-10-14).
# The age comes from the size in the SKU (KidsTee_Blk34 -> "3-4", Birthday1KidsTee_Wht12 -> "1-2").
# Pocket and sleeve zones keep their own sizes. Applies to the products whose SKU sizes are age RANGES
# (1-2, 3-4, 5-6 ...): kids T-shirt, kids hoodie and sleepsuit, plus rainsuits (no product key of their own, so
# matched by SKU prefix). Football shorts are NOT included: their sizes are single ages (6, 8, 10, 12), so a code
# like "12" means age 12, not 1-2, and no shorts for ages 1-4 exist.
# A size such as 4-5 is not "up to 4", so it keeps the normal size.
SMALL_KIDS_PRODUCTS     = ("kidstshirt", "kidshoodie", "sleepsuit")
SMALL_KIDS_SKU_PREFIXES = ("Rainsuit",)
SMALL_KIDS_MAX_AGE      = 4
SMALL_KIDS_CM           = 16

def kids_upper_age(sku):
    """Upper age of the SKU's age size ('3-4' -> 4, '12-13' -> 13), or None when the size is not an age range."""
    import re as _re
    # Read the digits at the end of the SKU ("..._Pnk12", "..._SBlue23", "..._Wht1213") rather than relying on
    # the colour-code table, which mis-splits colour names it does not know (SBlue23 -> size "e23").
    m = _re.search(r"(\d+)$", (sku or "").split("_")[-1])
    if not m or len(m.group(1)) < 2:
        return None
    digits = m.group(1)
    mid = len(digits) // 2
    lo, hi = int(digits[:mid]), int(digits[mid:])
    return hi if hi > lo else None     # "1-0" / "1-1" are really single ages (10, 11), not a range

def get_product_canvas(sku, product):
    """PRODUCT_CANVAS entry for this SKU: the product's sheet size, except kids aged 1-4 (16 x 16 cm front/back)."""
    canvas = PRODUCT_CANVAS.get(product, PRODUCT_CANVAS["default"])
    if product in SMALL_KIDS_PRODUCTS or (sku or "").startswith(SMALL_KIDS_SKU_PREFIXES):
        age = kids_upper_age(sku)
        if age is not None and age <= SMALL_KIDS_MAX_AGE:
            side = cm_to_px(SMALL_KIDS_CM)
            canvas = dict(canvas)
            for z in ("front", "back"):
                if z in canvas:
                    canvas[z] = (side, side)
    return canvas

# ─── SKU PREFIX → PRODUCT KEY — from owner Canvases.xlsx ─────────────────────
SKU_MAP = [
    # Adult T-shirt
    ("MenTee_",                       "adulttshirt"),
    ("AnyTxtOverSizeTee_",            "adulttshirt"),
    ("WmnTee_",                       "adulttshirt"),
    ("PoloTee_",                      "adulttshirt"),
    ("AdultPoloTee_",                 "adulttshirt"),
    ("SignLan01_Tee_",                "adulttshirt"),
    ("Custom04_Tee_",                 "adulttshirt"),
    ("LegendSince",                   "adulttshirt"),
    # Kids T-shirt
    ("KidsTee_",                      "kidstshirt"),
    ("SLan01KidsTee_",                "kidstshirt"),
    ("PerSingleLetter01KidsTee_",     "kidstshirt"),
    ("FootballKids",                  "kidstshirt"),
    ("67BdayT02Kid",                  "kidstshirt"),
    # Adult Hoodie
    ("AnyTxtAdultHood_",              "adulthoodie"),
    ("MenHood_",                      "adulthoodie"),
    ("HandStand",                     "adulthoodie"),
    ("SplitGirl",                     "adulthoodie"),
    ("FballN",                        "adulthoodie"),
    ("NewFball",                      "adulthoodie"),
    # Kids Hoodie
    ("AnyTxtKidsHood_",               "kidshoodie"),
    ("KidsHood_",                     "kidshoodie"),
    # Tote Bag
    ("AnyTxtTote_",                   "totebag"),
    ("Tote",                          "totebag"),
    # Backpack
    ("AnyTxtBckpck_",                 "backpack"),
    ("BckPack",                       "backpack"),
    ("Name01",                        "backpack"),
    # Baby Vest
    ("AnyTxtBabyVest_",               "babyvest"),
    ("BabyVest",                      "babyvest"),
    # Bucket Hat
    ("AnyTextHat_",                   "buckethat"),
    # Beanie
    ("AnytxtBeanie_",                 "beanie"),
    # Make Up Bag
    ("AnyTxtMakUp_",                  "makeupbag"),
    # Hoodie Blanket
    ("AnyTxtBlanketHood_",            "hodieblanket"),
    # Shoe Bag Sports
    ("AnyTxtShoeB_",                  "shoebag"),
    # Slipper
    ("AnyTxtSlip",                    "slipper"),
    # Socks
    ("AnyTxtSocks",                   "socks"),
    # Women's vest (adult 30×30cm — same canvas as adult tee)
    ("AnytxtWmn01_",                  "adulttshirt"),
    # Golf Towel
    ("AnyTxtTwl_",                    "golftowel"),
    # Generic AnyTxt catch-all — MUST stay after all specific AnyTxt* entries above
    ("AnyTxt",                        "adulttshirt"),
    # Cushion
    ("PCushion",                      "cushion"),
    # Custom Tee variants (same canvas as standard tees)
    ("CustomKidsTee_",                "kidstshirt"),   # e.g. CustomKidsTee_Blk78
    ("Custom_Tee_",                   "adulttshirt"),  # e.g. Custom_Tee_BlkM
    # Gym / Swim
    ("GymLeo",                        "default"),
    ("SwimSuit",                      "default"),
    # Seatbelt / Belt / Luggage belt variants
    ("SeatbeltCover",                 "seatbelt"),
    ("SingleBelt_",                   "seatbelt"),
    ("NSingleBelt_",                  "seatbelt"),
    # Sweatshirt (same canvas as hoodie)
    ("MenSwetShirt_",                 "adulthoodie"),
    ("WmnSwetShirt_",                 "adulthoodie"),
    ("AdultSwetShirt",                "adulttshirt"),   # 30×30 — same canvas as adult tee
    # Knitting bag variant
    ("Knitting",                      "knittingbag"),
    # Pyjama / sleepsuit
    ("IWakeup_",                      "sleepsuit"),
    # Football shorts
    ("FootballShorts_",               "footballshorts"),
    # PE Bag (22×24 — same canvas as string bag)
    ("FBall01PEBag",                  "pebag"),
    # Hand Towel (17×17 — same canvas as golf towel)
    ("HandAnyTxtTwl",                 "golftowel"),
    # Legsuit (15×17 — same canvas as baby vest)
    ("AnytxtLegsuit",                 "babyvest"),
]

def _validate_sku_map():
    """Warn if a short catch-all prefix appears BEFORE a longer specific prefix with a different key.
    If triggered, adding the new specific prefix before the catch-all will silence the warning."""
    shadowed = []
    for i, (short, short_key) in enumerate(SKU_MAP):
        for j, (long_prefix, long_key) in enumerate(SKU_MAP):
            if (long_prefix != short and long_prefix.startswith(short)
                    and short_key != long_key and i < j):
                shadowed.append(
                    f"  '{short}' ({short_key}) at position {i} is before "
                    f"'{long_prefix}' ({long_key}) at position {j} — move '{short}' after it"
                )
    if shadowed:
        print("ERROR: SKU_MAP order wrong — catch-all prefix appears before specific one:")
        for s in shadowed:
            print(s)
        print("  Fix: move the catch-all entry to AFTER all its specific variants in SKU_MAP.")

_validate_sku_map()

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def log(msg, level="INFO"):
    ts   = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except:
        pass

def find_image(filename):
    """Download image from crssoft image server and return local temp path, or None on failure."""
    if not filename or not filename.strip():
        return None
    if not IMAGE_SERVER_URL:
        return None
    url  = IMAGE_SERVER_URL.rstrip("/") + "/" + filename.strip()
    dest = os.path.join(TEMP_FOLDER, filename.strip())
    # Return cached temp file if already downloaded this session
    if os.path.isfile(dest):
        return dest
    try:
        os.makedirs(TEMP_FOLDER, exist_ok=True)
        import urllib.request, ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(url, context=ctx) as resp:
            with open(dest, "wb") as f:
                f.write(resp.read())
        return dest
    except Exception as e:
        log(f"  Image download failed ({filename}): {e}", "WARN")
        return None

def download_preview(url):
    """Load a preview image from a URL or a local filename, return PIL Image or None."""
    if not url or not url.strip():
        return None
    src = url.strip()
    # Full URL → download
    if src.startswith("http://") or src.startswith("https://"):
        tmp = tempfile.mktemp(suffix=".jpg")
        try:
            import ssl as _ssl
            ctx = _ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
            with urllib.request.urlopen(src, context=ctx) as resp:
                with open(tmp, "wb") as f:
                    f.write(resp.read())
            img = Image.open(tmp).convert("RGBA")
            return img
        except Exception as e:
            log(f"    Preview download failed: {e}", "WARN")
            return None
        finally:
            try: os.remove(tmp)
            except: pass
    # Filename → look up in image index (same as customer images on local DB)
    path = find_image(src)
    if path:
        try:
            return Image.open(path).convert("RGBA")
        except Exception as e:
            log(f"    Preview load failed: {e}", "WARN")
    return None

def parse_image_json(json_str):
    if not json_str or not json_str.strip():
        return []
    try:
        d = json.loads(json_str.strip())
        return [d[f"Image{i}"].strip() for i in range(1, 6) if d.get(f"Image{i}", "").strip()]
    except:
        return []

def resolve_zone_images(row, use_topaz, topaz_col, json_col, plain_col):
    """Return list of image filenames for a zone (front/back/pocket/sleeve), Topaz-first.
    Falls back to the multi-image *ImageJSON column (e.g. PocketImageJSON — holds
    Image1/Image2 for the "pocket left + right" combo), then the single plain column."""
    if use_topaz:
        v = (row.get(topaz_col) or "").strip()
        if v:
            if v.startswith("{"):
                imgs = parse_image_json(v)
                if imgs:
                    return imgs
            else:
                return [v]
    imgs = parse_image_json(row.get(json_col) or "")
    if imgs:
        return imgs
    plain = (row.get(plain_col) or "").strip()
    return [plain] if plain else []

def _parse_font_json(fonts_raw, side=None):
    """Parse FrontFonts/BackFonts value. Returns (font_name, is_premium).
    Handles both valid JSON (double quotes) and Python dict repr (single quotes).
    side: "Left" or "Right" — for PocketFonts, which uses {"Left Font":..,"Right Font":..}
    instead of {"NormalFont":..}.
    """
    if not fonts_raw:
        return "Arial Bold", False
    s = fonts_raw.strip()
    if s.startswith("{"):
        d = None
        # Try JSON first (double quotes)
        try:
            d = json.loads(s)
        except Exception:
            pass
        # Fallback: Python dict repr (single quotes from DB)
        if d is None:
            try:
                import ast
                d = ast.literal_eval(s)
            except Exception:
                pass
        if d is not None:
            premium = (d.get("PremiumFont") or "").strip()
            normal  = (d.get(f"{side} Font") or "").strip() if side else (d.get("NormalFont") or "").strip()
            if premium and premium.lower() not in ("no", "none", ""):
                # Only use premium font if it resolves to a known font.
                # Apply FONT_ALIASES first so Amazon display names ("Football Regular",
                # "Mermaid Regular", etc.) resolve to their canonical key ("football",
                # "wavemermaid", etc.) before checking PS_FONT_MAP / PREMIUM_FONT_KEYS.
                _pkey = premium.lower().replace(" ", "").replace("-", "").replace("_", "")
                _resolved = FONT_ALIASES.get(_pkey, _pkey)
                if _resolved in PS_FONT_MAP or _resolved in PREMIUM_FONT_KEYS:
                    return premium, True
            return normal or "Arial", False
    return s or "Arial", False

def parse_font(fonts_raw, side=None):
    """Return font name to use (premium takes priority over normal).
    side: "Left" or "Right" — for PocketFonts' per-side font keys."""
    return _parse_font_json(fonts_raw, side)[0]

def parse_is_premium_font(fonts_raw, side=None):
    """Return True if this zone uses a premium font."""
    return _parse_font_json(fonts_raw, side)[1]

def parse_colour(colours_raw, side=None):
    """side: "Left" or "Right" — for PocketColours' per-side colour keys."""
    if not colours_raw:
        return "#ffffff"
    s = colours_raw.strip()
    if s.startswith("{"):
        try:
            d = json.loads(s)
            if side and d.get(f"{side} Colour"):
                return d[f"{side} Colour"]
            # Try known keys first, then fall back to scanning all values for any
            # hex colour — handles any future Amazon JSON schema changes automatically.
            for key in ("Colour1", "colour1", "Left Colour", "left colour",
                        "Right Colour", "right colour"):
                if d.get(key):
                    return d[key]
            for val in d.values():
                if isinstance(val, str) and val.startswith("#") and len(val) in (4, 7):
                    return val
        except:
            pass
    if s.startswith("#"):
        return s
    return "#ffffff"

def parse_texts(raw):
    """Parse customer text, preserving blank lines as spacers (capped at 1 consecutive blank).
    Empty string "" in the returned list = one blank spacer line."""
    if not raw or not raw.strip():
        return []
    raw = raw.strip()
    if "|" in raw and "\n" not in raw:
        return [t.strip() for t in raw.split("|") if t.strip()]
    lines = raw.split("\n")
    result = []
    prev_blank = False
    for line in lines:
        s = line.strip()
        if s:
            result.append(s)
            prev_blank = False
        else:
            # Only add one blank spacer between real text lines (not at start)
            if result and not prev_blank:
                result.append("")
            prev_blank = True
    return result

def hex_to_rgb(hex_col):
    h = hex_col.lstrip("#")
    if len(h) == 3:
        h = "".join(c*2 for c in h)
    try:
        return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
    except:
        return (255, 255, 255)

def get_font(font_name, size_px):
    norm = font_name.lower().replace(' ','').replace('-','').replace('_','')
    # Check aliases first
    resolved = FONT_ALIASES.get(norm)
    if resolved is None:
        pass  # known non-renderable font (rhinestone etc) - fall through to arial
    elif resolved and resolved in FONT_INDEX:
        try:
            return ImageFont.truetype(FONT_INDEX[resolved], size_px)
        except:
            pass
    # Direct match in index
    if norm in FONT_INDEX:
        try:
            return ImageFont.truetype(FONT_INDEX[norm], size_px)
        except:
            pass
    # Partial match - find any font whose key contains the norm
    for key, path in FONT_INDEX.items():
        if norm in key or key in norm:
            try:
                return ImageFont.truetype(path, size_px)
            except:
                pass
    # System fonts
    system_map = {
        "arial": "arialbd.ttf", "arialbold": "arialbd.ttf",
        "timesnewroman": "times.ttf", "couriernew": "cour.ttf",
        "verdana": "verdana.ttf", "impact": "impact.ttf",
        "helvetica": "arialbd.ttf", "helveticabold": "arialbd.ttf", "georgia": "georgia.ttf",
        "tahoma": "tahoma.ttf",
    }
    if norm in system_map:
        try:
            return ImageFont.truetype(system_map[norm], size_px)
        except:
            pass
    # Final fallback
    try:
        return ImageFont.truetype("arialbd.ttf", size_px)
    except:
        return ImageFont.load_default()

def detect_category(sku):
    if not sku:
        return "Other"
    s = sku.lower()
    if "polo" in s:                                    return "Polo"
    if "kidstee" in s or "kidstshirt" in s:            return "Kids T-Shirt"
    if "hood" in s:                                    return "Hoodie"
    if "tote" in s:                                    return "Tote Bag"
    if "slipper" in s:                                 return "Slipper"
    if "baby" in s or "vest" in s:                     return "Baby Vest"
    if "backpack" in s or "bckpck" in s:               return "Backpack"
    if "mentee" in s or "_tee_" in s or "wmntee" in s: return "T-Shirt"
    if "hat" in s or "cap" in s or "beanie" in s:      return "Hat"
    if "gym" in s or "leo" in s or "legsui" in s:      return "Gym & Leotard"
    if "dart" in s:                                    return "Dart Case"
    if "towel" in s or "twl" in s:                     return "Towel"
    if "rainsuit" in s or "wellis" in s or "socks" in s or "keychain" in s: return "Accessories"
    if "lan" in s:                                     return "Sign Language"
    return "Other"

def sku_colour_folder(sku):
    """Return 'black', 'white', or '' (save directly, no sub-folder) from SKU colour code."""
    if not sku:
        return ""
    s = sku.lower()
    if "blk" in s:  return "black"
    if "wht" in s:  return "white"
    return ""

def is_multizone_row(row):
    """True if the row has content in more than one print zone (front+back, +sleeve, etc.)."""
    active = 0
    if row.get("IsFrontLocation") or (row.get("FrontImage") or "").strip() or (row.get("FrontText") or "").strip():
        active += 1
    if row.get("IsBackLocation") or (row.get("BackImage") or "").strip() or (row.get("BackText") or "").strip():
        active += 1
    if row.get("IsPocketLocation") or (row.get("PocketImage") or "").strip() or (row.get("PocketText") or "").strip():
        active += 1
    if row.get("IsSleeveLocation") or (row.get("SleeveImage") or "").strip() or (row.get("SleeveText") or "").strip():
        active += 1
    return active > 1

EMBROIDERY_SKU_PREFIXES = (
    "HandTwlLwnBowl",           # lawn bowls hand towel — embroidery, not DTF
    "Wellis_Pink_Purple",       # not processed by automation
    "NumPlateRectangle_keychain", # not processed by automation
    "VARBathPer1002",           # not processed by automation
)

def is_emb_rhine_row(row):
    """True if any zone uses an embroidery or rhinestone font, or SKU is an embroidery-only product."""
    sku = (row.get("SKU") or "")
    if any(sku.startswith(p) for p in EMBROIDERY_SKU_PREFIXES):
        return True
    for field in ("FrontFonts", "BackFonts", "PocketFonts", "SleeveFonts"):
        val = (row.get(field) or "").lower()
        if "emb" in val or "rhine" in val:
            return True
    return False

def detect_product(sku):
    """Map SKU to product key using SKU_MAP from owner canvas file.
    Tries each prefix in order — first match wins.
    Falls back to keyword matching, then default.
    """
    if not sku:
        return "default"
    # PE bags: any design prefix (AnyTxtPEBag_, PEBagCustom_, FBall02PEBag_, ...) is a 22 x 24 cm PE bag.
    # Checked first so the generic "AnyTxt" catch-all (adult T-shirt) can never claim them.
    if "pebag" in sku.lower():
        return "pebag"
    # Longest-prefix match — most specific entry wins over generic catch-alls like "AnyTxt"
    for prefix, product_key in sorted(SKU_MAP, key=lambda x: -len(x[0])):
        if sku.startswith(prefix):
            return product_key
    # Keyword fallback for edge cases
    s = sku.lower()
    if "kidstee" in s:          return "kidstshirt"
    if "kidshoo" in s:          return "kidshoodie"
    if "hood" in s:             return "adulthoodie"
    if "tote" in s:             return "totebag"
    if "slipper" in s:          return "slipper"
    if "baby" in s:             return "babyvest"
    if "vest" in s:             return "babyvest"
    if "backpack" in s or "bckpck" in s: return "backpack"
    if "beanie" in s:           return "beanie"
    if "hat" in s:              return "buckethat"
    if "tee" in s or "polo" in s: return "adulttshirt"
    return "default"

def get_dims(product, zone):
    spec = PRODUCT_CANVAS.get(product, PRODUCT_CANVAS["default"])
    return spec.get(zone, spec.get("front", (cm_to_px(30), cm_to_px(30))))

# ─── PSD WRITER ───────────────────────────────────────────────────────────────

def _pack_layer_name(s):
    b = s.encode("latin-1", errors="replace")[:255]
    data = bytes([len(b)]) + b
    pad = (4 - len(data) % 4) % 4
    return data + b'\x00' * pad

def _find_swop_profile():
    # Project-bundled copy takes priority — works on any machine without Photoshop
    _here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(_here, "icc", "USWebCoatedSWOP.icc"),
        r"C:\Windows\System32\spool\drivers\color\USWebCoatedSWOP.icc",
        r"C:\Program Files\Common Files\Adobe\Color\Profiles\Recommended\USWebCoatedSWOP.icc",
        r"C:\Program Files (x86)\Common Files\Adobe\Color\Profiles\Recommended\USWebCoatedSWOP.icc",
        r"C:\ProgramData\Adobe\Color\Profiles\Recommended\USWebCoatedSWOP.icc",
        os.path.expanduser(r"~\AppData\Roaming\Adobe\Color\Profiles\USWebCoatedSWOP.icc"),
    ]
    # Dynamically find any installed Photoshop version (2020, 2021, … 2026+)
    adobe_root = r"C:\Program Files\Adobe"
    if os.path.isdir(adobe_root):
        for folder in sorted(os.listdir(adobe_root), reverse=True):  # newest first
            if "Photoshop" in folder:
                for sub in ("Required\\ICC Profiles", "Presets\\Color Profiles"):
                    candidates.append(
                        os.path.join(adobe_root, folder, sub, "USWebCoatedSWOP.icc")
                    )
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None

_ICC_PATH = _find_swop_profile()
if not _ICC_PATH:
    import warnings
    warnings.warn("USWebCoatedSWOP.icc not found — copy it to v4/icc/ for accurate CMYK output")

def _load_icc_bytes():
    if _ICC_PATH and os.path.isfile(_ICC_PATH):
        with open(_ICC_PATH, 'rb') as f:
            return f.read()
    return None

_cmyk_transform = None
_k_scale        = 1.0  # stretch factor so pure black K reaches 255 after ICC conversion

def _get_cmyk_transform():
    """Build a cached sRGB → SWOP v2 ImageCms transform for accurate colour conversion."""
    global _cmyk_transform, _k_scale
    if _cmyk_transform is None:
        srgb = ImageCms.createProfile("sRGB")
        if _ICC_PATH:
            swop = ImageCms.getOpenProfile(_ICC_PATH)
            _cmyk_transform = ImageCms.buildTransform(
                srgb, swop, "RGB", "CMYK",
                renderingIntent=ImageCms.Intent.PERCEPTUAL,
                flags=0x2000,  # black point compensation
            )
            # Calibrate K stretch: find what K value pure black (0,0,0) produces
            _black  = Image.new("RGB", (1, 1), (0, 0, 0))
            _max_k  = ImageCms.applyTransform(_black, _cmyk_transform).getpixel((0, 0))[3]
            _k_scale = 255 / _max_k if _max_k > 0 else 1.0
        else:
            _cmyk_transform = None
    return _cmyk_transform

def _rgb_to_cmyk(rgba_img):
    """Max-K GCR conversion for DTF: maximises K so blacks are rich and
    saturated colours stay vivid. No press ICC profile — SWOP v2 adds dot-gain
    that over-darkens DTF film output.
    Returns (cmyk PIL image, alpha PIL band) — alpha stored separately in PSD."""
    import numpy as np
    rgba    = np.asarray(rgba_img.convert("RGBA"), dtype=np.float32)
    r, g, b = rgba[:,:,0]/255.0, rgba[:,:,1]/255.0, rgba[:,:,2]/255.0
    alpha   = rgba[:,:,3]

    k      = 1.0 - np.maximum.reduce([r, g, b])          # max-K GCR
    denom  = np.where(k < 1.0, 1.0 - k, 1.0)
    c      = np.clip((1.0 - r - k) / denom, 0.0, 1.0)
    m      = np.clip((1.0 - g - k) / denom, 0.0, 1.0)
    y      = np.clip((1.0 - b - k) / denom, 0.0, 1.0)

    # Collapse CMY floating-point noise on near-pure-black pixels to clean K-only
    pure_k = k >= (247.0 / 255.0)
    c[pure_k] = 0.0
    m[pure_k] = 0.0
    y[pure_k] = 0.0

    cmyk_arr  = (np.stack([c, m, y, k], axis=2) * 255.0).round().astype(np.uint8)
    alpha_arr = alpha.round().astype(np.uint8)
    return Image.fromarray(cmyk_arr, "CMYK"), Image.fromarray(alpha_arr, "L")

def _to_channels_rgb(img):
    """Convert RGBA PIL image → RGB + alpha channel dict for PSD.
    Channel IDs: -1=alpha, 0=R, 1=G, 2=B. Values are straight (not inverted)."""
    rgba = img.convert("RGBA")
    r, g, b, a = rgba.split()
    return {
        -1: a.tobytes(),
         0: r.tobytes(),
         1: g.tobytes(),
         2: b.tobytes(),
    }

def _to_channels_cmyk(img):
    """Convert RGBA PIL image → CMYK + alpha channel dict for a CMYK PSD.

    CRITICAL: PSD CMYK stores values inverted (0 = full ink, 255 = no ink),
    opposite to PIL (255 = full ink).  Without this inversion, K=255 in PIL
    (pure black) is written as K=255 in the PSD, which Photoshop reads as
    *zero* black ink — making blacks appear grey and all colours washed out.

    Channel IDs: -1=alpha (not inverted), 0=C, 1=M, 2=Y, 3=K (all inverted).
    """
    import numpy as np
    cmyk_img, alpha = _rgb_to_cmyk(img)
    c, m, y, k = cmyk_img.split()
    def inv(band):
        return (255 - np.asarray(band, dtype=np.uint8)).tobytes()
    return {
        -1: np.asarray(alpha, dtype=np.uint8).tobytes(),
         0: inv(c),
         1: inv(m),
         2: inv(y),
         3: inv(k),
    }

def _packbits_row(data: bytes) -> bytes:
    """PackBits-encode one scanline."""
    result = bytearray()
    i, n = 0, len(data)
    while i < n:
        val = data[i]
        j = i + 1
        while j < n and j - i < 128 and data[j] == val:
            j += 1
        rlen = j - i
        if rlen >= 2:
            result.append((257 - rlen) & 0xFF)
            result.append(val)
            i = j
        else:
            lstart = i
            i += 1
            while i < n and i - lstart < 128:
                if i + 1 < n and data[i] == data[i + 1]:
                    break
                i += 1
            result.append(i - lstart - 1)
            result.extend(data[lstart:i])
    return bytes(result)

def _rle_encode_channel(raw: bytes, width: int, height: int):
    """PackBits-encode a full channel. Returns (row_counts_bytes, compressed_bytes)."""
    rows = [_packbits_row(raw[r * width:(r + 1) * width]) for r in range(height)]
    return struct.pack(f'>{height}H', *[len(r) for r in rows]), b''.join(rows)

def _get_srgb_icc_bytes():
    """Return raw sRGB ICC profile bytes to embed in the PSD (resource 1039).
    Embedding sRGB stops Photoshop applying SWOP dot-gain interpretation,
    so colours display accurately without any soft-proof mode active."""
    try:
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
        return profile.tobytes()
    except Exception:
        return None

_SRGB_ICC = _get_srgb_icc_bytes()

def write_psd(out_path, canvas_w, canvas_h, layers):
    """Write a CMYK layered PSD (8-bit) with embedded SWOP v2 ICC profile.

    CMYK is written directly — all pixel data is converted from RGBA source
    using the max-K GCR formula in _rgb_to_cmyk before being stored.
    PSD CMYK channel values are inverted (0=full ink, 255=no ink) as required
    by the PSD spec — _to_channels_cmyk handles this inversion.
    The embedded SWOP v2 ICC profile tells Photoshop exactly how to interpret
    the ink values, ensuring accurate soft-proof and final print output."""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    buf = io.BytesIO()
    p   = buf.write

    # ── Header ──────────────────────────────────────────────────────────────
    p(b'8BPS')
    p(struct.pack('>H', 1))     # version
    p(b'\x00' * 6)              # reserved
    p(struct.pack('>H', 5))     # 5 channels: alpha + C M Y K
    p(struct.pack('>I', canvas_h))
    p(struct.pack('>I', canvas_w))
    p(struct.pack('>H', 8))     # 8 bits/channel
    p(struct.pack('>H', 4))     # CMYK colour mode

    p(struct.pack('>I', 0))     # colour mode data (empty)

    # ── Image Resources ──────────────────────────────────────────────────────
    res_blocks = b''

    # Resource 1005 — resolution (DPI stored as 16.16 fixed-point)
    dpi_fixed = DPI << 16
    res_data  = struct.pack('>IHHIHH', dpi_fixed, 1, 1, dpi_fixed, 1, 1)
    res_blocks += (b'8BIM' + struct.pack('>H', 1005) +
                   b'\x00\x00' + struct.pack('>I', len(res_data)) + res_data)

    # Resource 1039 — ICC profile (SWOP v2): tells Photoshop how to interpret CMYK ink values
    _swop_icc = _load_icc_bytes()
    if _swop_icc:
        icc_padded = _swop_icc + (b'\x00' if len(_swop_icc) % 2 else b'')
        res_blocks += (b'8BIM' + struct.pack('>H', 1039) +
                       b'\x00\x00' + struct.pack('>I', len(_swop_icc)) + icc_padded)

    p(struct.pack('>I', len(res_blocks)))
    p(res_blocks)

    # ── Layer & Mask Info ────────────────────────────────────────────────────
    lr_buf   = io.BytesIO()
    ld_buf   = io.BytesIO()
    ch_order = [-1, 0, 1, 2, 3]   # alpha, C, M, Y, K

    for lyr in layers:
        img    = lyr['image']
        top    = lyr['top']
        left   = lyr['left']
        bottom = top  + img.height
        right  = left + img.width
        flags  = 0 if lyr.get('visible', True) else 2

        ch_raw = _to_channels_cmyk(img)
        ch_rle = {cid: _rle_encode_channel(ch_raw[cid], img.width, img.height)
                  for cid in ch_order}

        lr = io.BytesIO()
        lr.write(struct.pack('>iiii', top, left, bottom, right))
        lr.write(struct.pack('>H', 5))  # 5 channels: alpha + C M Y K
        for cid in ch_order:
            rc, cd = ch_rle[cid]
            lr.write(struct.pack('>hI', cid, 2 + len(rc) + len(cd)))
        lr.write(b'8BIM')
        lr.write(b'norm')
        lr.write(struct.pack('>B', lyr.get('opacity', 255)))
        lr.write(struct.pack('>B', 0))
        lr.write(struct.pack('>B', flags))
        lr.write(b'\x00')
        name_bytes = _pack_layer_name(lyr['name'])
        extra = struct.pack('>I', 0) + struct.pack('>I', 0) + name_bytes
        lr.write(struct.pack('>I', len(extra)))
        lr.write(extra)
        lr_buf.write(lr.getvalue())

        for cid in ch_order:
            rc, cd = ch_rle[cid]
            ld_buf.write(struct.pack('>H', 1))  # RLE (PackBits)
            ld_buf.write(rc)
            ld_buf.write(cd)

    layer_info = struct.pack('>h', len(layers)) + lr_buf.getvalue() + ld_buf.getvalue()
    if len(layer_info) % 4:
        layer_info += b'\x00' * (4 - len(layer_info) % 4)

    lmi = struct.pack('>I', len(layer_info)) + layer_info + struct.pack('>I', 0)
    p(struct.pack('>I', len(lmi)))
    p(lmi)

    # ── Merged composite (CMYK) ──────────────────────────────────────────────
    # Composite is white background in CMYK (0,0,0,0 = no ink = white)
    composite = Image.new("RGBA", (canvas_w, canvas_h), (255, 255, 255, 255))
    for lyr in layers:
        if lyr.get('visible', True):
            src = lyr['image'].convert("RGBA")
            composite.paste(src, (lyr['left'], lyr['top']), src)
    # Convert composite to CMYK with alpha, then encode 5 channels
    comp_cmyk, comp_alpha = _rgb_to_cmyk(composite)
    c_ch, m_ch, y_ch, k_ch = comp_cmyk.split()
    import numpy as _np2
    def _inv_band(band):
        return Image.fromarray((255 - _np2.asarray(band, dtype=_np2.uint8)))
    comp_bands = [
        comp_alpha,           # alpha — not inverted
        _inv_band(c_ch),      # C inverted (PSD CMYK convention)
        _inv_band(m_ch),      # M inverted
        _inv_band(y_ch),      # Y inverted
        _inv_band(k_ch),      # K inverted
    ]
    comp_rle = [_rle_encode_channel(band.tobytes(), canvas_w, canvas_h)
                for band in comp_bands]
    p(struct.pack('>H', 1))     # RLE encoding
    for rc, _  in comp_rle: p(rc)
    for _,  cd in comp_rle: p(cd)

    with open(out_path, 'wb') as f:
        f.write(buf.getvalue())
    return out_path

# ─── LAYER BUILDERS ───────────────────────────────────────────────────────────

def build_image_layer(img_path, w, h, sku=None, no_bg_remove=False, force_bg_remove=False):
    if not img_path or not os.path.isfile(img_path):
        return Image.new("RGBA", (1, 1), (0, 0, 0, 0)), 0, 0
    src = Image.open(img_path).convert("RGBA")

    # Auto background removal: if image background matches garment colour, remove it.
    # force_bg_remove=True (designer set IsFrontBgRemove=1) bypasses the edge-detection
    # gate — we trust the designer's explicit flag over the auto-detection heuristic.
    garment_rgb = get_garment_rgb(sku) if sku else None
    if not no_bg_remove and garment_rgb:
        if force_bg_remove or image_bg_matches_garment(src, garment_rgb):
            log(f"  bg-remove ({'forced' if force_bg_remove else 'auto'}): garment colour {garment_rgb}", "INFO")
            src = remove_background(src, garment_rgb=garment_rgb)

    # Alpha threshold — remove near-transparent noise pixels
    r, g, b, a = src.split()
    a = a.point(lambda x: 0 if x < 128 else x)
    src = Image.merge("RGBA", (r, g, b, a))

    # Crop to content bounding box so transparent borders (left by bg-removal)
    # don't shrink the design when it is scaled to fill the canvas
    bbox = src.getbbox()
    if bbox:
        src = src.crop(bbox)

    # Contain scaling: fit within zone (w × h) keeping aspect ratio.
    ratio = min(w / src.width, h / src.height)
    nw    = max(1, int(src.width  * ratio))
    nh    = max(1, int(src.height * ratio))
    src = src.resize((nw, nh), Image.LANCZOS)
    # Centre within the zone so landscape images don't pin to the top-left corner.
    top  = (h - nh) // 2
    left = (w - nw) // 2
    return src, top, left

try:
    from pilmoji import Pilmoji
    from pilmoji.source import GoogleEmojiSource
    PILMOJI_AVAILABLE = True
except ImportError:
    PILMOJI_AVAILABLE = False

# ─── CHROME SVG COLOUR FONT RENDERER ─────────────────────────────────────────
# Fonts like RefractionRay, Smart Kids, Cozy Winter store glyphs as SVG only.
# PIL/FreeType cannot render them. We fall back to headless Chrome which
# supports all OpenType SVG colour fonts natively.

CHROME_EXE = None
for _p in [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]:
    if os.path.exists(_p):
        CHROME_EXE = _p
        break

# Fonts that need Chrome to render correctly:
#   (a) PIL renders zero pixels (SVG/colour-only, no outline fallback), OR
#   (b) font has an SVG/COLR/sbix colour table — PIL renders outline fallback only,
#       which loses all the colour/texture effect (e.g. Wavemermaid grey outlines).
# Only checked for PREMIUM_FONT_KEYS so standard/system fonts are not affected.
SVG_ONLY_FONT_KEYS = set()

def _test_font_renders(path):
    """Return True if this font file produces visible pixels when rendering 'A'."""
    try:
        from PIL import ImageFont as _IF, Image as _Im, ImageDraw as _ID
        import numpy as _np
        _f   = _IF.truetype(path, 200)
        _img = _Im.new("RGBA", (300, 300), (0, 0, 0, 0))
        _ID.Draw(_img).text((10, 10), "A", font=_f, fill=(0, 0, 0, 255))
        return _np.array(_img)[:, :, 3].max() > 0
    except Exception:
        return False  # can't load at all → also needs Chrome

def _has_colour_table(path):
    """Return True if the font has SVG/COLR/sbix colour glyph tables."""
    try:
        from fontTools.ttLib import TTFont as _TT
        _tt = _TT(path, lazy=True)
        return any(t in _tt for t in ('SVG ', 'COLR', 'sbix'))
    except Exception:
        return False

def _auto_tracking_from_font(path):
    """Compute tracking multiplier from font glyph fill ratios for A-Z / a-z.
    fill_ratio = mean(glyph_bbox_width / advance_width).
    tracking   = 1 − (1 − fill_ratio) × 0.5  (removes half the built-in sidebearing).
    Returns 1.0 on any error."""
    try:
        from fontTools.ttLib import TTFont as _TT
        _ft   = _TT(path, lazy=True)
        _hmtx = _ft['hmtx'].metrics
        _cmap = _ft.getBestCmap()
        _glyf = _ft.get('glyf')
        _ratios = []
        for _cp in list(range(ord('A'), ord('Z') + 1)) + list(range(ord('a'), ord('z') + 1)):
            _gname = _cmap.get(_cp) if _cmap else None
            if not _gname:
                continue
            _adv, _lsb = _hmtx.get(_gname, (0, 0))
            if _adv <= 0:
                continue
            _w = None
            if _glyf:
                try:
                    _g = _glyf[_gname]
                    if hasattr(_g, 'xMax') and _g.xMax is not None:
                        _w = _g.xMax - _g.xMin
                except Exception:
                    pass
            if _w is None or _w <= 0:
                _w = _adv - 2 * max(0, _lsb)   # proxy: assume symmetric sidebearings
            if _w > 0:
                _ratios.append(min(1.0, _w / _adv))
        if not _ratios:
            return 1.0
        _fill = max(0.50, min(1.0, sum(_ratios) / len(_ratios)))
        return round(1.0 - (1.0 - _fill) * 0.5, 3)
    except Exception:
        return 1.0

for _key, _path in FONT_INDEX.items():
    if _key in PREMIUM_FONT_KEYS:
        if not _test_font_renders(_path) or _has_colour_table(_path):
            SVG_ONLY_FONT_KEYS.add(_key)

if SVG_ONLY_FONT_KEYS:
    print(f"  SVG colour fonts (Chrome renderer): {sorted(SVG_ONLY_FONT_KEYS)}")

if SVG_ONLY_FONT_KEYS:
    for _k in sorted(SVG_ONLY_FONT_KEYS):
        _t = FONT_TRACKING.get(_k, 1.0)
        print(f"  Font tracking: {_t}  ({_k})")

def _is_premium_font_name(font_name):
    """Return True if this font is a premium colour font (all go through Chrome)."""
    key = _resolve_font_key(font_name)
    return key in PREMIUM_FONT_KEYS

def _is_svg_only_font(font_name):
    """Return True if this font requires Chrome to render (PIL fails)."""
    key = _resolve_font_key(font_name)
    return key in SVG_ONLY_FONT_KEYS

def build_text_layer_chrome(text_lines, font_name, colour_hex, canvas_w):
    """Render premium SVG colour fonts using Chrome.

    Strategy A (primary): one HTML page per text block with @font-face pointing
    directly at the font file via --allow-file-access-from-files.  Single Chrome
    call per text block — no profile-lock issues, handles kerning automatically.

    Strategy B (fallback): glyph-by-glyph SVG render — one Chrome call per
    character, composited in PIL.  Avoids CSS font-rendering limitations.

    Returns RGBA PIL image, or None on failure.
    """
    if not CHROME_EXE:
        return None
    real_lines = [l for l in text_lines if l.strip()]
    if not real_lines:
        return None

    key       = _resolve_font_key(font_name)
    font_path = FONT_INDEX.get(key, "")
    if not font_path:
        return None

    # Prefer OTF — it carries the full SVG colour glyph data
    otf_path = font_path.rsplit(".", 1)[0] + ".otf"
    if os.path.exists(otf_path):
        font_path = otf_path

    import subprocess, numpy as np, re, html as _html

    os.makedirs(TEMP_FOLDER, exist_ok=True)

    # ── STRATEGY A: glyph-collage SVG (single Chrome call, correct font colours) ─
    # Each unique character is rendered as an inline SVG <img> data-URL element.
    # SVG rendering preserves the font's built-in colour data (unlike CSS @font-face).
    # All glyphs are placed in one HTML page → one Chrome call, no profile-lock issues.
    def _try_collage_render():
        import base64 as _b64

        try:
            from fontTools.ttLib import TTFont
        except ImportError:
            return None

        try:
            ft_font     = TTFont(font_path)
            svg_table   = ft_font.get('SVG ')
            cmap        = ft_font.getBestCmap()
            glyph_order = ft_font.getGlyphOrder()
            upem        = ft_font['head'].unitsPerEm
            ascender    = ft_font['hhea'].ascent
            descender   = ft_font['hhea'].descent   # negative
            vb_h        = ascender - descender
            hmtx        = ft_font['hmtx'].metrics
        except Exception:
            return None

        if not svg_table:
            return None

        svg_map = {}
        for doc in svg_table.docList:
            for gid in range(doc.startGlyphID, doc.endGlyphID + 1):
                svg_map[gid] = (doc.data,
                                doc.startGlyphID != doc.endGlyphID)  # shared doc?

        # Render glyphs at the actual output size so Chrome produces
        # sharp detail (football patterns, textures etc.) without any
        # upscaling step.  Target: each glyph fills its share of canvas width.
        # We cap at 2400px so the collage HTML doesn't exceed Chrome's limits.
        max_chars = max(len(l) for l in real_lines if l)
        target_w  = int(canvas_w * 0.85 / max(1, max_chars))   # px per char
        # Use ×4 multiplier so glyphs render at high resolution before any
        # scale-to-canvas step — prevents blurring on upscale.
        glyph_h   = max(800, min(2400, target_w * 4))

        # Square viewBox: "0 -850 1000 1000" — same width and height (1000 units).
        # vb_top=-850 captures the full cap height (700u) + 150u buffer above.
        # The viewBox bottom is at y=+150, covering descenders.
        # Square viewBox + square element = x_scale == y_scale == glyph_h/1000,
        # so glyph proportions are preserved exactly (no distortion).
        # scale = adv_px factor; sf = composition advance factor — both glyph_h/1000.
        vb_top      = -850
        vb_h_render = 1000  # square viewBox (== upem)
        scale       = glyph_h / upem            # glyph_h / 1000

        # ── Collect unique characters and prepare their SVG ──────────────────
        all_chars = sorted(set(ch for l in text_lines for ch in l if ch.strip()))
        char_svg  = {}   # ch → (adv_px, fixed_svg_string)
        char_gid  = {}   # ch → gid

        for ch in all_chars:
            cp         = ord(ch)
            glyph_name = cmap.get(cp)
            if not glyph_name:
                continue
            try:
                gid = glyph_order.index(glyph_name)
            except ValueError:
                continue
            entry = svg_map.get(gid)
            if not entry:
                continue
            svg_raw, shared = entry
            adv_units = hmtx.get(glyph_name, (upem // 3, 0))[0]
            adv_px    = max(1, int(adv_units * scale))
            h_px      = glyph_h   # viewport height = glyph_h (= vb_h_render * scale)

            svg = svg_raw
            # Force a UNIFORM viewBox for every glyph so all letters are
            # rendered at the same visual scale regardless of how the font
            # designer laid out the SVG coordinate space per glyph.
            # The viewBox spans the full advance width horizontally and the
            # standard cap-height range vertically.
            vb_attr = f'viewBox="0 {vb_top} {upem} {vb_h_render}"'
            if 'viewBox' in svg:
                svg = re.sub(r'viewBox="[^"]*"', vb_attr, svg)
            else:
                svg = svg.replace('<svg', f'<svg {vb_attr}', 1)
            # Pixel dimensions: width scales with advance, height = glyph_h.
            # Both are derived from the SAME scale factor so all glyphs have
            # identical height and proportional widths.
            # Square element (h_px × h_px) + square viewBox (1000 × 1000):
            # x_scale = y_scale = h_px/1000, so glyph proportions are exact.
            # Default "xMidYMid meet" has no letterboxing effect on a
            # square-on-square layout — content fills the full element.
            # The advance-width crop (x=0..adv_px) is taken from the left
            # side of the square element in the crop step below.
            svg = re.sub(r'\s+preserveAspectRatio="[^"]*"', '', svg)
            svg = re.sub(r'\s+width="[^"]*"',  '', svg)
            svg = re.sub(r'\s+height="[^"]*"', '', svg)
            svg = svg.replace('<svg', f'<svg width="{h_px}px" height="{h_px}px"', 1)
            # Shared document: inject CSS to show only this glyph's <g> element
            if shared:
                hide = (f'<style>g{{display:none}}'
                        f'#glyph{gid}{{display:inline}}'
                        f'#glyph\\.{gid}{{display:inline}}</style>')
                svg = re.sub(r'(<svg[^>]*>)', r'\1' + hide, svg, count=1)

            char_svg[ch] = (adv_px, h_px, svg)
            char_gid[ch] = gid

        if not char_svg:
            return None

        # ── Collect fallback (emoji / missing-glyph) characters ──────────────
        # These are rendered in a separate Chrome pass AFTER cap_h is known
        # so they can be sized to match the premium font letter height exactly.
        _VARIATION_SELECTORS = set(range(0xFE00, 0xFE10)) | {0xFE0F}
        emoji_fallback = set()   # chars not in cmap at all → Chrome emoji pass
        outline_only   = set()   # chars in cmap but no SVG colour entry → PIL + colour
        for _line in text_lines:
            for _ch in _line:
                _cp = ord(_ch)
                if not _ch.strip() or _cp in _VARIATION_SELECTORS:
                    continue
                if _ch not in char_svg:
                    if cmap.get(_cp):
                        outline_only.add(_ch)   # outline glyph exists, just no colour table
                    else:
                        emoji_fallback.add(_ch)

        # ── Build one-line collage HTML (premium font glyphs only) ───────────
        # Each glyph element is h_px × h_px (square) so Chrome renders it
        # without distortion.  x_pos tracks the LEFT edge of each element;
        # the crop step below takes only the advance-width slice (0..adv_px)
        # from the left side of the square element.
        collage_w = sum(v[1] for v in char_svg.values()) + 10  # v[1]=h_px (square width)
        collage_h = max((v[1] for v in char_svg.values()), default=glyph_h) + 10

        items_html = ""
        x_pos = {}
        cx = 0
        for ch, (adv_px, h_px, svg_str) in char_svg.items():
            svg_b64 = _b64.b64encode(svg_str.encode("utf-8")).decode("ascii")
            items_html += (
                f'<img style="position:absolute;left:{cx}px;top:0;'
                f'width:{h_px}px;height:{h_px}px;display:block" '  # square element
                f'src="data:image/svg+xml;base64,{svg_b64}">\n'
            )
            x_pos[ch] = cx
            cx += h_px  # advance by element width (= glyph_h), not adv_px

        html_src = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
*{{margin:0;padding:0}}html,body{{background:#ffffff;overflow:hidden}}
.c{{position:relative;width:{collage_w}px;height:{collage_h}px}}
</style></head><body>
<div class="c">{items_html}</div>
</body></html>"""

        _pid      = os.getpid()
        html_path = os.path.join(TEMP_FOLDER, f"glyph_collage_{_pid}.html")
        png_path  = os.path.join(TEMP_FOLDER, f"glyph_collage_{_pid}.png")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_src)
        try:
            if os.path.exists(png_path):
                os.remove(png_path)
        except Exception:
            pass

        cmd = [
            CHROME_EXE, "--headless", "--no-sandbox",
            "--disable-gpu", "--disable-extensions",
            "--no-first-run", "--disable-sync",
            f"--screenshot={png_path}",
            f"--window-size={collage_w},{collage_h}",
            "file:///" + html_path.replace("\\", "/"),
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=40)
        except Exception:
            return None

        if not os.path.exists(png_path):
            return None
        try:
            collage_img = Image.open(png_path)
            collage_img.load()
            collage_img = collage_img.convert("RGBA")
        except Exception:
            return None
        for _f in (html_path, png_path):
            try: os.remove(_f)
            except Exception: pass

        # Handle HiDPI: Chrome may return 2× pixels
        dpr = collage_img.width / collage_w if collage_w > 0 else 1.0

        # ── Baseline position in the rendered collage ────────────────────────
        # vb_top = -850, vb_h_render = 1000: baseline (y=0) sits 850 units from top
        # → baseline_y_px = (850/1000) * glyph_h = 0.85·h
        _vb_top_abs   = 850
        baseline_y_px = int(_vb_top_abs / vb_h_render * glyph_h)
        # sf strictly from the 1000-unit Em Square (same as scale)
        sf = glyph_h / upem

        # ── Crop individual glyphs from collage, record per-glyph baseline offsets ─
        # Horizontal: keep the FULL advance-width slice — do NOT tight-crop sides.
        # The transparent sidebearing space in the image IS the inter-letter gap;
        # removing it would eliminate the font's designed spacing and cause letters
        # to touch or overlap when composed.
        # Vertical: tight-crop to the visible rows only (removes blank top/bottom).
        glyph_imgs_raw = {}
        glyph_above_bl = {}
        glyph_below_bl = {}
        for ch, (adv_px, h_px, _) in char_svg.items():
            x0 = int(x_pos[ch] * dpr)
            x1 = int((x_pos[ch] + adv_px) * dpr)
            y1 = int(h_px * dpr)
            crop = collage_img.crop((x0, 0, min(x1, collage_img.width),
                                     min(y1, collage_img.height)))
            if dpr != 1.0:
                crop = crop.resize((adv_px, h_px), Image.LANCZOS)
            arr = np.array(crop)
            white = (arr[:, :, 0] > 240) & (arr[:, :, 1] > 240) & (arr[:, :, 2] > 240)
            arr[white, 3] = 0
            img_rgba = Image.fromarray(arr)
            if arr[:, :, 3].max() > 0:
                # Vertical tight-crop: find first and last rows with visible pixels
                _row_alpha = arr[:, :, 3].max(axis=1)
                _vis = np.where(_row_alpha > 0)[0]
                if len(_vis) > 0:
                    _yt = int(_vis[0]);  _yb = int(_vis[-1]) + 1
                    glyph_imgs_raw[ch] = img_rgba.crop((0, _yt, img_rgba.width, _yb))
                    glyph_above_bl[ch] = max(0, min(baseline_y_px, _yb) - _yt)
                    glyph_below_bl[ch] = max(0, _yb - baseline_y_px)
                else:
                    glyph_imgs_raw[ch] = None
                    glyph_above_bl[ch] = 0
                    glyph_below_bl[ch] = 0
            else:
                glyph_imgs_raw[ch] = None
                glyph_above_bl[ch] = 0
                glyph_below_bl[ch] = 0

        if not any(v is not None for v in glyph_imgs_raw.values()):
            log(f"  Collage render: all glyphs transparent for {font_name}", "WARN")
            return None

        # ── Per-character-category scaling (FONT_CHAR_METRICS) ───────────────
        # Some premium SVG fonts encode all glyphs at the same visual scale
        # (lowercase 'a' as tall as uppercase 'A').  FONT_CHAR_METRICS stores
        # measured ratios (generated by measure_premium_fonts.py) so each
        # category is scaled to its correct proportion relative to cap height.
        _cat_metrics = FONT_CHAR_METRICS.get(key)
        if _cat_metrics:
            _upper_vals = [glyph_above_bl[c] for c in glyph_above_bl
                           if c.isupper() and glyph_above_bl[c] > 0
                           and glyph_imgs_raw.get(c) is not None]
            _cap_h_ref = int(sum(_upper_vals) / len(_upper_vals)) if _upper_vals else 0
            if _cap_h_ref > 0:
                for _ch in list(glyph_imgs_raw.keys()):
                    _gimg = glyph_imgs_raw[_ch]
                    if _gimg is None or glyph_above_bl.get(_ch, 0) <= 0:
                        continue
                    _cat   = _char_category(_ch)
                    _ratio = _cat_metrics.get(_cat)
                    if _ratio is None:
                        continue
                    _target_above = max(1, int(_cap_h_ref * _ratio))
                    _cur_above    = glyph_above_bl[_ch]
                    if _cur_above <= 0:
                        continue
                    _sf2 = _target_above / _cur_above
                    if abs(_sf2 - 1.0) < 0.02:
                        continue
                    _new_w = max(1, int(_gimg.width  * _sf2))
                    _new_h = max(1, int(_gimg.height * _sf2))
                    glyph_imgs_raw[_ch]  = _gimg.resize((_new_w, _new_h), Image.LANCZOS)
                    glyph_above_bl[_ch]  = _target_above
                    glyph_below_bl[_ch]  = max(0, int(glyph_below_bl.get(_ch, 0) * _sf2))
                    # Scale stored advance proportionally so spacing matches glyph size
                    if _ch in char_svg:
                        _adv0, _hp0, _sv0 = char_svg[_ch]
                        char_svg[_ch] = (max(1, int(_adv0 * _sf2)), max(1, int(_hp0 * _sf2)), _sv0)

        # Line metrics driven purely by the rendered glyphs.
        max_above = max((v for v in glyph_above_bl.values() if v > 0),
                        default=int(glyph_h * 0.625))
        max_below = max((v for v in glyph_below_bl.values() if v > 0),
                        default=int(glyph_h * 0.1))
        line_h    = max_above + max_below
        _tracking = FONT_TRACKING.get(key, 1.0)

        # ── Render outline-only glyphs via PIL with customer colour ───────────
        # Characters like " " ' that are in the cmap (have outline glyphs) but
        # have no SVG colour entry.  Render them at cap-height size in the
        # customer's chosen colour so they match the overall text scheme.
        if outline_only:
            try:
                from PIL import ImageFont as _PIF, Image as _PImg, ImageDraw as _PID
                _cx = colour_hex or "#ffffff"
                _r  = int(_cx[1:3], 16) if len(_cx) >= 7 else 255
                _g  = int(_cx[3:5], 16) if len(_cx) >= 7 else 255
                _bv = int(_cx[5:7], 16) if len(_cx) >= 7 else 255
                _pil_sz = max(50, max_above)
                _pil_f  = None
                for _fp in [font_path, None]:
                    try:
                        _pil_f = _PIF.truetype(_fp, _pil_sz) if _fp else _PIF.load_default()
                        break
                    except Exception:
                        continue
                if _pil_f:
                    for _ch in outline_only:
                        _gn = cmap.get(ord(_ch))
                        _adv_u = hmtx.get(_gn, (upem // 3, 0))[0] if _gn else upem // 3
                        _adv_px = max(1, int(_adv_u * sf * _tracking))
                        _tmp = _PImg.new("RGBA", (max(100, _adv_px * 3), max(100, _pil_sz * 2)), (0, 0, 0, 0))
                        _PID.Draw(_tmp).text((0, 0), _ch, font=_pil_f, fill=(_r, _g, _bv, 255))
                        _bb = _tmp.getbbox()
                        if _bb:
                            _crop = _tmp.crop(_bb)
                            glyph_imgs_raw[_ch] = _crop
                            glyph_above_bl[_ch] = _crop.height
                            glyph_below_bl[_ch] = 0
                            char_svg[_ch] = (_adv_px, _pil_sz, None)
            except Exception:
                pass

        # Special-char vertical alignment after FONT_CHAR_METRICS scaling.
        # gy = max_above - above_bl determines paste Y position.
        #   APOSTROPHE/QUOTE  TOP:      above_bl = max_above
        #   PERIOD/COMMA      BASELINE: above_bl = glyph_h
        #   HYPHEN/DASH       CENTRE:   above_bl = glyph_h//2 + max_above//2
        _TOP_PUNCT      = frozenset(["'", '‘', '’', '"', '“', '”', '`', '´', 'ʼ', 'ʻ', '＇'])
        _BASELINE_PUNCT = frozenset(['.', ','])
        _CENTRE_PUNCT   = frozenset(['-', '–', '—'])
        for _ch in list(glyph_above_bl):
            _gimg = glyph_imgs_raw.get(_ch)
            if _gimg is None:
                continue
            if _ch in _TOP_PUNCT:
                glyph_above_bl[_ch] = max_above
            elif _ch in _BASELINE_PUNCT:
                glyph_above_bl[_ch] = _gimg.height
            elif _ch in _CENTRE_PUNCT:
                glyph_above_bl[_ch] = _gimg.height // 2 + max_above // 2

        # ── Render emoji at cap-height size (separate Chrome pass) ────────────
        if emoji_fallback:
            em_font_px = max(20, int(max_above / 0.80))
            em_div_px  = int(em_font_px * 2.0)
            em_cols    = {}
            em_items   = ""
            em_cx      = 0
            for _ch in sorted(emoji_fallback):
                ch_html = ''.join(f'&#x{ord(c):X};' for c in _ch) + '&#xFE0F;'
                em_items += (
                    f'<div style="position:absolute;left:{em_cx}px;top:0;'
                    f'width:{em_div_px}px;height:{em_div_px}px;display:flex;'
                    f'align-items:center;justify-content:center;'
                    f'font-family:\'Segoe UI Emoji\',\'Apple Color Emoji\',sans-serif;'
                    f'font-size:{em_font_px}px;line-height:1">{ch_html}</div>\n'
                )
                em_cols[_ch] = em_cx
                em_cx += em_div_px
            em_w = em_cx + 5
            em_h = em_div_px + 5
            em_html = (
                f'<!DOCTYPE html><html><head><meta charset="utf-8"><style>'
                f'*{{margin:0;padding:0}}html,body{{background:#ffffff;overflow:hidden}}'
                f'.c{{position:relative;width:{em_w}px;height:{em_h}px}}'
                f'</style></head><body><div class="c">{em_items}</div></body></html>'
            )
            em_html_path = os.path.join(TEMP_FOLDER, f"emoji_col_{_pid}.html")
            em_png_path  = os.path.join(TEMP_FOLDER, f"emoji_col_{_pid}.png")
            with open(em_html_path, "w", encoding="utf-8") as _f:
                _f.write(em_html)
            try:
                if os.path.exists(em_png_path):
                    os.remove(em_png_path)
            except Exception:
                pass
            cmd2 = [
                CHROME_EXE, "--headless", "--no-sandbox",
                "--disable-gpu", "--disable-extensions",
                "--no-first-run", "--disable-sync",
                f"--screenshot={em_png_path}",
                f"--window-size={em_w},{em_h}",
                "file:///" + em_html_path.replace("\\", "/"),
            ]
            try:
                subprocess.run(cmd2, capture_output=True, timeout=40)
            except Exception:
                pass
            if os.path.exists(em_png_path):
                try:
                    em_img = Image.open(em_png_path)
                    em_img.load()
                    em_img = em_img.convert("RGBA")
                    em_dpr = em_img.width / em_w if em_w > 0 else 1.0
                    for _ch in emoji_fallback:
                        x0 = int(em_cols[_ch] * em_dpr)
                        x1 = int((em_cols[_ch] + em_div_px) * em_dpr)
                        y1 = int(em_div_px * em_dpr)
                        _crop = em_img.crop((x0, 0, min(x1, em_img.width), min(y1, em_img.height)))
                        if em_dpr != 1.0:
                            _crop = _crop.resize((em_div_px, em_div_px), Image.LANCZOS)
                        _arr = np.array(_crop)
                        _white = (_arr[:, :, 0] > 248) & (_arr[:, :, 1] > 248) & (_arr[:, :, 2] > 248)
                        _arr[_white, 3] = 0
                        _rgba = Image.fromarray(_arr)
                        _bbox = _rgba.getbbox()
                        if _bbox and _arr[:, :, 3].max() > 0:
                            _cropped = _rgba.crop(_bbox)
                            _em_w = max(1, int(_cropped.width * max_above / _cropped.height))
                            glyph_imgs_raw[_ch] = _cropped.resize((_em_w, max_above), Image.LANCZOS)
                            glyph_above_bl[_ch] = max_above
                            glyph_below_bl[_ch] = 0
                        else:
                            glyph_imgs_raw[_ch] = None
                            glyph_above_bl[_ch] = 0
                            glyph_below_bl[_ch] = 0
                except Exception:
                    pass
            for _f in (em_html_path, em_png_path):
                try: os.remove(_f)
                except Exception: pass

        # ── Compose text lines — uniform sf, hmtx advance, baseline-anchored ─
        # X advance = hmtx_advance × sf × tracking  (single scale, per-font tracking)
        # Y paste   = max_above - above_bl[ch]  (aligns all baselines to one row)
        # Period uses identical sf as every other glyph — no independent shrinking.
        _tracking = FONT_TRACKING.get(key, 1.0)
        space_em  = hmtx.get('space', hmtx.get('uni0020', (upem // 3, 0)))[0]
        space_w   = max(4, int(space_em * sf * _tracking))
        line_imgs = []
        for line_text in text_lines:
            if not line_text.strip():
                line_imgs.append(None)
                continue
            x     = 0
            parts = []
            for ch in line_text:
                gimg = glyph_imgs_raw.get(ch)
                if ch in char_svg:
                    gname = cmap.get(ord(ch))
                    # Use char_svg advance (already in px, already scaled by FONT_CHAR_METRICS)
                    adv   = max(1, int(char_svg[ch][0] * _tracking))
                    parts.append((gimg, x, glyph_above_bl.get(ch, max_above)))
                    x += adv
                elif ch in emoji_fallback:
                    adv = max(1, int((gimg.width + 4) * _tracking)) if gimg is not None else space_w
                    parts.append((gimg, x, glyph_above_bl.get(ch, max_above)))
                    x += adv
                else:
                    x += space_w
            if x <= 0:
                line_imgs.append(None)
                continue
            _min_w = max(x, max((gx + g.width for g, gx, _ in parts if g is not None), default=x))
            line_img = Image.new("RGBA", (_min_w, line_h), (0, 0, 0, 0))
            for gimg, gx, above in parts:
                if gimg is not None:
                    gy = max_above - above   # baseline-anchor: align to common baseline row
                    line_img.paste(gimg, (gx, max(0, gy)), gimg)
            line_imgs.append(line_img)

        valid = [li for li in line_imgs if li is not None]
        if not valid:
            return None

        # Stack lines — use actual max line width to prevent right-edge clipping
        line_gap   = max(line_h // 3, 40)  # ~33% of cap height between lines
        n_lines    = len(line_imgs)
        total_h    = line_h * n_lines + line_gap * max(0, n_lines - 1)
        max_line_w = max((limg.width for limg in line_imgs if limg is not None), default=canvas_w)
        result     = Image.new("RGBA", (max_line_w, max(1, total_h)), (0, 0, 0, 0))
        y = 0
        for limg in line_imgs:
            if limg is not None:
                cx2 = max(0, (max_line_w - limg.width) // 2)
                result.paste(limg, (cx2, y), limg)
            y += line_h + line_gap

        bbox = result.getbbox()
        if not bbox:
            return None
        result = result.crop(bbox)

        # Scale to fill ~85% of canvas width — same as the PIL renderer does.
        avail_w = int(canvas_w * 0.85)
        if result.width > 0 and result.width != avail_w:
            ratio   = avail_w / result.width
            new_h   = max(1, int(result.height * ratio))
            result  = result.resize((avail_w, new_h), Image.LANCZOS)

        log(f"  Collage render OK for {font_name}: {result.size}", "INFO")
        return result

    result = _try_collage_render()
    if result is not None:
        return result

    log(f"  Collage render failed for {font_name} — falling back to CSS font render", "WARN")

    # ── STRATEGY B: CSS @font-face (single Chrome call — shape only, no SVG colour) ─
    def _try_html_render():
        max_chars  = max(len(l) for l in real_lines if l)
        font_px    = max(80, min(500, int(canvas_w * 0.85 / max(1, max_chars))))
        line_height = int(font_px * 1.25)
        total_h     = line_height * len(text_lines) + font_px

        bg = "#00FE00"   # lime-green background for clean keying

        lines_html = "\n".join(
            f'<div class="tl">{_html.escape(l) if l.strip() else "&nbsp;"}</div>'
            for l in text_lines
        )

        import base64 as _b64
        with open(font_path, "rb") as _fh:
            _font_b64 = _b64.b64encode(_fh.read()).decode("ascii")
        fmt      = "opentype" if font_path.lower().endswith(".otf") else "truetype"
        font_url = f"data:font/{fmt};base64,{_font_b64}"

        html_src = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{background:{bg};width:{canvas_w}px}}
@font-face{{font-family:'PF';src:url('{font_url}') format('{fmt}')}}
.tl{{font-family:'PF',sans-serif;font-size:{font_px}px;
     line-height:{line_height}px;text-align:center;
     width:{canvas_w}px;white-space:pre;overflow:hidden}}
</style></head><body>
{lines_html}
</body></html>"""

        _pid      = os.getpid()
        html_path = os.path.join(TEMP_FOLDER, f"premium_font_{_pid}.html")
        png_path  = os.path.join(TEMP_FOLDER, f"premium_font_{_pid}.png")

        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_src)
        try:
            if os.path.exists(png_path):
                os.remove(png_path)
        except Exception:
            pass

        cmd = [
            CHROME_EXE, "--headless", "--no-sandbox",
            "--disable-gpu", "--disable-extensions",
            "--no-first-run", "--disable-sync",
            f"--screenshot={png_path}",
            f"--window-size={canvas_w},{max(200, total_h)}",
            "file:///" + html_path.replace("\\", "/"),
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=30)
        except Exception:
            return None

        if not os.path.exists(png_path):
            return None
        try:
            img = Image.open(png_path)
            img.load()
            img = img.convert("RGBA")
        except Exception:
            return None
        for _f in (html_path, png_path):
            try: os.remove(_f)
            except Exception: pass

        if img.width > canvas_w:
            scale_f = canvas_w / img.width
            img = img.resize(
                (canvas_w, max(1, int(img.height * scale_f))), Image.LANCZOS)

        arr = np.array(img)
        lime = (arr[:, :, 0] < 30) & (arr[:, :, 1] > 240) & (arr[:, :, 2] < 30)
        arr[lime, 3] = 0

        if arr[:, :, 3].max() == 0:
            return None

        result = Image.fromarray(arr)
        bbox = result.getbbox()
        if not bbox:
            return None
        log(f"  CSS render OK for {font_name}: {result.size} -> bbox {bbox}", "INFO")
        return result.crop(bbox)

    result = _try_html_render()
    if result is not None:
        return result

    log(f"  CSS render also failed for {font_name} — falling back to glyph-by-glyph SVG", "WARN")

    # ── STRATEGY B: glyph-by-glyph SVG render ──────────────────────────────────
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        log("fontTools not available — cannot render premium font", "WARN")
        return None

    try:
        ft_font     = TTFont(font_path)
        svg_table   = ft_font.get('SVG ')
        cmap        = ft_font.getBestCmap()
        glyph_order = ft_font.getGlyphOrder()
        upem        = ft_font['head'].unitsPerEm
        ascender    = ft_font['hhea'].ascent
        descender   = ft_font['hhea'].descent   # negative number
        vb_h        = ascender - descender
        hmtx        = ft_font['hmtx'].metrics
    except Exception as e:
        log(f"fontTools load error for {font_name}: {e}", "WARN")
        return None

    if not svg_table:
        log(f"No SVG table in {font_name}", "WARN")
        return None

    # glyph_id → SVG document
    svg_map = {}
    for doc in svg_table.docList:
        for gid in range(doc.startGlyphID, doc.endGlyphID + 1):
            svg_map[gid] = doc.data

    # Target glyph size
    max_chars  = max(len(l) for l in real_lines if l)
    glyph_px   = max(150, min(400, canvas_w // max(1, max_chars)))
    scale      = glyph_px / vb_h
    line_h     = max(10, int(vb_h * scale * 1.1))
    profile_dir = os.path.join(TEMP_FOLDER, "chrome_profile")  # shared — calls are sequential

    def render_glyph_svg(gid):
        """Render one glyph → RGBA image, or (None, adv_px) on failure."""
        glyph_name = glyph_order[gid] if gid < len(glyph_order) else None
        adv_units  = hmtx.get(glyph_name, (upem // 3, 0))[0] if glyph_name else upem // 3
        adv_px     = max(1, int(adv_units * scale))
        h_px       = max(1, int(vb_h * scale))

        svg_raw = svg_map.get(gid)
        if not svg_raw:
            return None, adv_px

        svg = svg_raw
        # Fix / add viewBox so Chrome maps the glyph coordinate space correctly
        vb_attr = f'viewBox="0 {descender} {upem} {vb_h}"'
        if 'viewBox' in svg:
            svg = re.sub(r'viewBox="[^"]*"', vb_attr, svg)
        else:
            svg = svg.replace('<svg', f'<svg {vb_attr}', 1)

        # Force explicit pixel size — remove any existing width/height first
        svg = re.sub(r'\s+width="[^"]*"',  '', svg)
        svg = re.sub(r'\s+height="[^"]*"', '', svg)
        svg = svg.replace('<svg', f'<svg width="{adv_px}" height="{h_px}"', 1)

        # For shared SVG documents (multiple glyphs), hide all except this one
        # by injecting a CSS rule that shows only #glyph{gid}
        if svg_map.get(gid) and svg_table.docList:
            for doc in svg_table.docList:
                if doc.startGlyphID <= gid <= doc.endGlyphID and \
                        doc.startGlyphID != doc.endGlyphID:
                    # Shared document — CSS-hide all sibling glyphs
                    hide_css = (
                        f'<style>g{{display:none}} '
                        f'#glyph{gid},#glyph.{gid}{{display:inline}}</style>'
                    )
                    svg = svg.replace('<svg', '<svg', 1)  # no-op anchor
                    svg = re.sub(r'(<svg[^>]*>)', r'\1' + hide_css, svg, count=1)
                    break

        svg_path = os.path.join(TEMP_FOLDER, f"glyph_{gid}.svg")
        png_path = os.path.join(TEMP_FOLDER, f"glyph_{gid}.png")

        try:
            if os.path.exists(png_path):
                os.remove(png_path)
        except Exception:
            pass

        with open(svg_path, "w", encoding="utf-8") as f:
            f.write(svg)

        cmd = [
            CHROME_EXE, "--headless", "--no-sandbox",
            "--disable-gpu", "--disable-extensions",
            "--no-first-run", "--disable-sync",
            f"--user-data-dir={profile_dir}",
            f"--screenshot={png_path}",
            f"--window-size={adv_px},{h_px}",
            "file:///" + svg_path.replace("\\", "/"),
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=20)
        except Exception as e:
            log(f"  Chrome error glyph {gid}: {e}", "WARN")
            return None, adv_px

        if not os.path.exists(png_path):
            return None, adv_px

        try:
            img = Image.open(png_path)
            img.load()
            img = img.convert("RGBA")
        except Exception:
            return None, adv_px
        for _f in (svg_path, png_path):
            try: os.remove(_f)
            except Exception: pass

        # Handle HiDPI — resize to expected dimensions
        if img.width != adv_px or img.height != h_px:
            img = img.resize((adv_px, h_px), Image.LANCZOS)

        # Remove white background
        arr = np.array(img)
        white = (arr[:, :, 0] > 240) & (arr[:, :, 1] > 240) & (arr[:, :, 2] > 240)
        arr[white, 3] = 0

        if arr[:, :, 3].max() == 0:
            return None, adv_px  # Glyph rendered no visible pixels

        return Image.fromarray(arr), adv_px

    # Build one image per line
    line_images = []
    for line_text in text_lines:
        if not line_text.strip():
            line_images.append(None)
            continue

        x     = 0
        parts = []
        for ch in line_text:
            cp         = ord(ch)
            glyph_name = cmap.get(cp)
            if not glyph_name:
                adv_px = int((upem // 3) * scale)
                parts.append((None, x, adv_px))
                x += adv_px
                continue
            try:
                gid = glyph_order.index(glyph_name)
            except ValueError:
                adv_px = max(1, int(hmtx.get(glyph_name, (upem // 3, 0))[0] * scale))
                parts.append((None, x, adv_px))
                x += adv_px
                continue
            glyph_img, adv_px = render_glyph_svg(gid)
            parts.append((glyph_img, x, adv_px))
            x += adv_px

        if x <= 0:
            line_images.append(None)
            continue

        line_img = Image.new("RGBA", (x, line_h), (0, 0, 0, 0))
        for glyph_img, gx, adv_px in parts:
            if glyph_img is None:
                continue
            gh      = min(glyph_img.height, line_h)
            paste_y = max(0, (line_h - gh) // 2)
            clip_w  = min(glyph_img.width, line_img.width - gx)
            if clip_w > 0:
                clip = glyph_img.crop((0, 0, clip_w, gh))
                line_img.paste(clip, (gx, paste_y), clip)

        line_images.append(line_img)

    valid = [li for li in line_images if li is not None]
    if not valid:
        log(f"  Glyph-SVG renderer: no visible glyphs for {font_name}", "WARN")
        return None

    # Assemble lines on canvas
    line_gap = max(line_h // 3, 40)  # ~33% of cap height between lines
    n_lines  = len(line_images)
    total_h  = line_h * n_lines + line_gap * max(0, n_lines - 1)
    result   = Image.new("RGBA", (canvas_w, max(1, total_h)), (0, 0, 0, 0))
    y        = 0
    for limg in line_images:
        if limg is not None:
            cx = max(0, (canvas_w - limg.width) // 2)
            result.paste(limg, (cx, y), limg)
        y += line_h + line_gap

    bbox = result.getbbox()
    if not bbox:
        log(f"  Glyph-SVG renderer: final result transparent for {font_name}", "WARN")
        return None

    return result.crop(bbox)

def build_text_layer(text_lines, font_name, colour_hex, w, h):
    if not text_lines:
        return Image.new("RGBA", (1, 1), (0, 0, 0, 0)), 0, 0

    # Premium fonts: try Chrome first (Strategy A renders SVG glyph patterns —
    # mermaid scales, camo, football texture etc.).  If Chrome fails (font has no
    # SVG table) we fall through to PIL with black fill.
    # SVG-only fonts (PIL renders blank) also route through Chrome, passing None
    # for colour so the font's own embedded colours are used.
    if _is_svg_only_font(font_name) or _is_premium_font_name(font_name):
        chrome_col = None if _is_svg_only_font(font_name) else colour_hex
        chrome_img = build_text_layer_chrome(text_lines, font_name, chrome_col, w)
        if chrome_img is not None:
            # Scale to fill canvas width (85 % margin) — same target the PIL
            # binary-search path uses.  Chrome renders at glyph_h resolution so
            # upscaling here stays sharp.
            avail_w = int(w * 0.85)
            if chrome_img.width > 0 and chrome_img.width != avail_w:
                _s = avail_w / chrome_img.width
                chrome_img = chrome_img.resize(
                    (avail_w, max(1, int(chrome_img.height * _s))),
                    Image.LANCZOS)
            left = max(0, (w - chrome_img.width) // 2)
            return chrome_img, 0, left
        # Chrome failed (no SVG table) → fall through to PIL

    is_premium = _is_premium_font_name(font_name)
    r, g, b    = (0, 0, 0) if is_premium else hex_to_rgb(colour_hex)
    avail_w    = int(w * 0.90)
    real_lines = [l for l in text_lines if l.strip()]
    if not real_lines:
        return Image.new("RGBA", (1, 1), (0, 0, 0, 0)), 0, 0

    # Per-font tracking multiplier (same map used by Chrome SVG renderer)
    _font_key = _resolve_font_key(font_name) or ""
    _tracking  = FONT_TRACKING.get(_font_key, 1.0)

    # ── Load fontTools metrics (head + hmtx) ─────────────────────────────────
    font_path = FONT_INDEX.get(_font_key)
    ft_hmtx   = None
    ft_cmap   = None
    ft_upem   = None
    if font_path:
        try:
            from fontTools.ttLib import TTFont as _TTFont
            _ft    = _TTFont(font_path)
            ft_upem = _ft['head'].unitsPerEm          # unitsPerEm from head table
            ft_hmtx = _ft['hmtx'].metrics             # glyph_name → (advance, lsb)
            ft_cmap = _ft.getBestCmap()               # codepoint → glyph_name
        except Exception:
            ft_hmtx = ft_cmap = ft_upem = None

    def _line_width_px(line, font_size):
        """Width = Σ advance_width × (font_size / unitsPerEm) × tracking.
        Never uses textbbox() or getmask() — purely metric-based."""
        if ft_hmtx and ft_cmap and ft_upem:
            sf    = font_size / ft_upem * _tracking
            total = 0
            for ch in line:
                gname = ft_cmap.get(ord(ch))
                adv   = ft_hmtx.get(gname, (ft_upem // 3, 0))[0] if gname else ft_upem // 3
                total += int(adv * sf)
            return total
        # Fallback only when fontTools is unavailable (textbbox for sizing, not X-advance)
        scratch = Image.new("RGBA", (1, 1))
        bb = ImageDraw.Draw(scratch).textbbox((0, 0), line, font=get_font(font_name, font_size))
        return bb[2] - bb[0]

    # ── Binary-search for the largest font size that fits avail_w ────────────
    lo, hi, best = 20, min(int(h * 0.25), h // max(1, len(real_lines))), 60
    while lo <= hi:
        mid    = (lo + hi) // 2
        widest = max(_line_width_px(l, mid) for l in real_lines)
        if widest <= avail_w:
            best = mid
            lo   = mid + 1
        else:
            hi   = mid - 1

    font         = get_font(font_name, best)
    # scale_factor = font_size / unitsPerEm  (per user spec)
    scale_factor = (best / ft_upem) if ft_upem else None

    # Line height from textbbox — height measurement only, not used for X positioning
    scratch = Image.new("RGBA", (1, 1))
    bb0     = ImageDraw.Draw(scratch).textbbox((0, 0), real_lines[0], font=font)
    line_h  = int((bb0[3] - bb0[1]) * 1.4)
    pad     = line_h

    tmp_w = w + pad * 2
    tmp_h = line_h * len(text_lines) + pad * 2
    img   = Image.new("RGBA", (tmp_w, tmp_h), (0, 0, 0, 0))
    d2    = ImageDraw.Draw(img)
    baseline_y = pad + int(line_h * 0.75)   # anchor='ls' baseline row

    has_emoji = any(ord(c) > 127 for line in text_lines for c in line)

    for line in text_lines:
        if line.strip():
            if ft_hmtx and ft_cmap and ft_upem and scale_factor and not has_emoji:
                # ── Metric-based cursor positioning ───────────────────────────
                # advance_width × scale_factor × tracking — no textbbox/getmask
                # for X advance. Period stays its natural size; 'W' stays wide.
                line_px = sum(
                    int(ft_hmtx.get(ft_cmap.get(ord(ch)), (ft_upem // 3, 0))[0]
                        * scale_factor * _tracking)
                    for ch in line
                )
                current_x = max(pad, pad + (w - line_px) // 2)
                for ch in line:
                    gname = ft_cmap.get(ord(ch))
                    adv_u = ft_hmtx.get(gname, (ft_upem // 3, 0))[0] if gname else ft_upem // 3
                    # anchor='ls': current_x = left of advance cell, baseline_y = baseline
                    d2.text((current_x, baseline_y), ch, font=font,
                            fill=(r, g, b, 255), anchor='ls')
                    current_x += int(adv_u * scale_factor * _tracking)
            elif has_emoji and PILMOJI_AVAILABLE:
                # ── Pilmoji path: Google Emoji for any emoji characters ────────
                bb = ImageDraw.Draw(Image.new("RGBA", (1, 1))).textbbox(
                    (0, 0), line, font=font)
                x = max(pad, pad + (w - (bb[2] - bb[0])) // 2)
                with Pilmoji(img, source=GoogleEmojiSource) as pil:
                    pil.text((x, baseline_y - int(line_h * 0.75)),
                             line, fill=(r, g, b, 255), font=font)
            else:
                # ── Plain PIL fallback (no emoji, no fontTools) ────────────────
                bb = d2.textbbox((0, 0), line, font=font)
                x  = max(pad, pad + (w - (bb[2] - bb[0])) // 2)
                d2.text((x, baseline_y), line, font=font,
                        fill=(r, g, b, 255), anchor='ls')
        baseline_y += line_h

    bbox = img.getbbox()
    if bbox:
        margin = max(10, line_h // 6)
        img = img.crop((max(0, bbox[0] - margin), max(0, bbox[1] - margin),
                        min(tmp_w, bbox[2] + margin), min(tmp_h, bbox[3] + margin)))

    left = max(0, (w - img.width) // 2)
    return img, 0, left

# ─── SKU COLOUR / SIZE PARSING ───────────────────────────────────────────────
# Maps SKU colour codes → readable colour names (matches owner's label format)
COLOUR_MAP = {
    "Blk": "Black",  "Wht": "White",  "Nvy": "Navy",   "Red": "Red",
    "Pnk": "Pink",   "Gry": "Grey",   "Blu": "Blue",   "Grn": "Green",
    "Ylw": "Yellow", "Fus": "Fuchsia","Pur": "Purple",  "Org": "Orange",
    "Bur": "Burgundy","Nat": "Natural","Lav": "Lavender","RBlu":"Royal Blue",
    "SBlu":"Sky Blue","Camo":"Camo",   "TD": "Tie Dye",  "GryM":"Grey Marl",
    "Ivry":"Ivory",   "BPnk":"Baby Pink",
}

def parse_sku_colour_size(sku):
    """
    Extract colour and size from SKU for use in layer labels.
    e.g. MenTee_WhtXL   -> ("White", "XL")
         KidsTee_Blk911 -> ("Black", "9-11")
         AdultPoloTee_RBluM -> ("Royal Blue", "M")
    Returns (colour_str, size_str) — either may be empty string.
    """
    if not sku:
        return "", ""
    # Split on underscore — last segment has colour+size
    parts = sku.split("_")
    if len(parts) < 2:
        return "", ""
    last = parts[-1]

    # Try to match colour codes (longest match first)
    colour_str = ""
    remainder  = last
    for code in sorted(COLOUR_MAP.keys(), key=len, reverse=True):
        if last.startswith(code):
            colour_str = COLOUR_MAP[code]
            remainder  = last[len(code):]
            break

    # Size: whatever remains after the colour code
    # Normalise age sizes: 911 → 9-11, 78 → 7-8, 1213 → 12-13 etc.
    size_raw = remainder.strip()
    size_str = size_raw
    if size_raw.isdigit() and len(size_raw) >= 2:
        mid = len(size_raw) // 2
        size_str = size_raw[:mid] + "-" + size_raw[mid:]

    return colour_str, size_str


def make_zone_label(zone_key, sku, use_sku_detail=True):
    """
    Build the layer label string.
    - use_sku_detail=True  → "Front - White XL"   (different designs per size)
    - use_sku_detail=False → "front"               (identical designs)
    """
    zone_display = zone_key.title()   # "front" → "Front"
    if not use_sku_detail:
        return zone_display
    colour, size = parse_sku_colour_size(sku)
    parts = [zone_display]
    if colour:
        parts.append(colour)
    if size:
        parts.append(size)
    return " - ".join(parts)   # "Front - White XL"



# Maps SKU colour codes -> approximate RGB of the garment
# Used to detect if the image background matches the garment colour
GARMENT_RGB = {
    "Blk":  (20,  20,  20),
    "Wht":  (255, 255, 255),
    "Nvy":  (31,  40,  80),
    "Red":  (200, 30,  30),
    "Pnk":  (255, 150, 180),
    "BPnk": (255, 182, 193),
    "Gry":  (150, 150, 150),
    "GryM": (160, 160, 160),
    "Blu":  (30,  100, 200),
    "RBlu": (65,  105, 225),
    "SBlu": (135, 206, 235),
    "Grn":  (34,  139, 34),
    "Ylw":  (255, 220, 0),
    "Fus":  (255, 0,   144),
    "Pur":  (128, 0,   128),
    "Org":  (255, 140, 0),
    "Bur":  (128, 0,   32),
    "Nat":  (245, 222, 179),
    "Lav":  (230, 190, 255),
    "Ivry": (255, 255, 240),
}

def get_garment_rgb(sku):
    if not sku:
        return None
    # Search the full SKU string for a colour code — longest match wins.
    # Handles both "MenTee_BlkS" (starts-with) and "VestBlkS" (embedded).
    sku_upper = sku  # codes are mixed-case, match case-sensitively
    for code in sorted(GARMENT_RGB.keys(), key=len, reverse=True):
        if code in sku_upper:
            return GARMENT_RGB[code]
    return None

def _is_light_colour(rgb):
    """True if the garment is a light colour (white, ivory, yellow, light pink etc.)"""
    r, g, b = rgb
    brightness = (r * 299 + g * 587 + b * 114) / 1000
    return brightness > 160

def image_bg_matches_garment(img_rgba, garment_rgb, tolerance=40):
    """
    Returns True when edges are solid-background AND interior check passes.

    Interior check only applies for LIGHT garments (white, ivory, yellow etc.):
      - Light garment: if 25%+ of interior also matches garment colour, it's
        complex artwork (e.g. poster with white smoke/mist) — skip removal.
      - Dark garment (black, navy etc.): skip interior check entirely.
        Dark backgrounds naturally fill the whole canvas; rembg handles them cleanly.
    """
    w, h = img_rgba.size
    if w < 20 or h < 20:
        return False
    strip = 5
    arr = img_rgba.load()
    gr, gg, gb = garment_rgb

    def matches(px):
        return abs(px[0]-gr) <= tolerance and abs(px[1]-gg) <= tolerance and abs(px[2]-gb) <= tolerance

    # --- Edge sample (95% must match garment colour) ---
    edge_px = []
    for x in range(0, w, max(1, w // 100)):
        for y in range(strip):
            edge_px.append(arr[x, y])
            edge_px.append(arr[x, h - 1 - y])
    for y in range(0, h, max(1, h // 100)):
        for x in range(strip):
            edge_px.append(arr[x, y])
            edge_px.append(arr[w - 1 - x, y])
    if not edge_px:
        return False
    edge_match = sum(1 for px in edge_px if matches(px)) / len(edge_px)
    if edge_match < 0.95:
        return False

    # --- Interior check — only for light garments ---
    if _is_light_colour(garment_rgb):
        bx0, by0 = int(w * 0.1), int(h * 0.1)
        bx1, by1 = int(w * 0.9), int(h * 0.9)
        interior_px = []
        step_x = max(1, (bx1 - bx0) // 50)
        step_y = max(1, (by1 - by0) // 50)
        for x in range(bx0, bx1, step_x):
            for y in range(by0, by1, step_y):
                interior_px.append(arr[x, y])
        if interior_px:
            interior_match = sum(1 for px in interior_px if matches(px)) / len(interior_px)
            # 95%+ interior matches → almost entirely background colour, no real design
            # visible (e.g. completely blank canvas). Wide logos on white backgrounds
            # score 80-90% — still remove those (threshold raised from 80→95).
            if interior_match >= 0.95:
                return False

    return True

def remove_background_colourkey(img_rgba, garment_rgb, tolerance=40):
    """
    Fast colour-key removal: replace every pixel close to garment_rgb with transparent.
    Works perfectly for flat graphic designs / logos on solid-colour backgrounds.
    Much more accurate than rembg for non-photographic images.
    """
    import numpy as np
    arr = np.array(img_rgba, dtype=np.int32)
    gr, gg, gb = garment_rgb
    # Distance from each pixel to garment colour
    diff = (np.abs(arr[:,:,0] - gr) <= tolerance) & \
           (np.abs(arr[:,:,1] - gg) <= tolerance) & \
           (np.abs(arr[:,:,2] - gb) <= tolerance)
    result = arr.copy().astype(np.uint8)
    result[diff, 3] = 0   # make matching pixels transparent
    return Image.fromarray(result, 'RGBA')

def remove_background(img_rgba, garment_rgb=None):
    """
    For graphic designs on solid backgrounds → fast colour-key removal.
    For photos (light garments) → rembg AI removal.
    """
    if garment_rgb and not _is_light_colour(garment_rgb):
        # Dark garment (black, navy etc.) — always use colour-key, not rembg
        # rembg destroys dark graphic designs; colour-key is precise
        return remove_background_colourkey(img_rgba, garment_rgb)

    # Light garment — use rembg for photo subjects
    if not REMBG_AVAILABLE:
        log("rembg not installed - falling back to colour-key removal", "WARN")
        if garment_rgb:
            return remove_background_colourkey(img_rgba, garment_rgb)
        return img_rgba
    try:
        result = rembg_remove(img_rgba)
        # Validate rembg output: if fewer than 15% of pixels are visible it
        # destroyed a flat graphic/logo — fall back to colour-key removal instead
        import numpy as _np
        alpha = _np.array(result)[:, :, 3]
        visible_pct = (alpha > 10).sum() / max(1, alpha.size)
        if visible_pct < 0.15:
            log(f"  rembg left only {visible_pct*100:.1f}% visible — flat graphic detected, switching to colour-key", "WARN")
            if garment_rgb:
                return remove_background_colourkey(img_rgba, garment_rgb)
        return result
    except Exception as e:
        log(f"rembg failed: {e} — falling back to colour-key", "WARN")
        if garment_rgb:
            return remove_background_colourkey(img_rgba, garment_rgb)
        return img_rgba

def build_label_layer(label_text):
    """Small black label overlay for top-left corner of zone."""
    font_size = max(20, cm_to_px(0.5))
    try:
        f = ImageFont.truetype("arialbd.ttf", font_size)
    except:
        f = ImageFont.load_default()
    # Measure actual bounding box — bb[0/1] may be non-zero (font descenders)
    tmp = Image.new("RGBA", (1, 1))
    bb  = ImageDraw.Draw(tmp).textbbox((0, 0), label_text.upper(), font=f)
    pad = 6
    tw  = bb[2] - bb[0] + pad * 2
    th  = bb[3] - bb[1] + pad * 2
    img = Image.new("RGBA", (max(1, tw), max(1, th)), (0, 0, 0, 0))
    # Offset by bb[0/1] so text is never clipped
    ImageDraw.Draw(img).text((pad - bb[0], pad - bb[1]), label_text.upper(), font=f, fill=(0, 0, 0, 255))
    return img

# ─── SEMI-CUSTOM PSD BUILDER ──────────────────────────────────────────────────

def _build_psd_semi_custom(order_id, rows, out_path):
    """
    Process semi-customised rows (e.g. football jerseys with player name + number).

    Uses a pre-built multi-slot template (default: 7 slots stacked vertically).
    Each slot's text layers follow Photoshop copy-naming convention:
        Slot 0: 'Player' / '08'
        Slot 1: 'Player copy' / '08 copy'
        Slot 2: 'Player copy 2' / '08 copy 2'  ... etc.

    All slots for a batch are edited in ONE UXP job via a flat text_replacements
    dict — no canvas manipulation, no cross-document operations.  The UXP plugin
    crops the canvas to exactly K active slots at the end.

    For orders larger than max_slots (7), multiple PSDs are created automatically:
        OrderID.psd   → items 1-7
        OrderID_b.psd → items 8-12  (etc.)

    Returns (success: bool, message: str).
    """
    if not PS_BRIDGE_AVAILABLE:
        return False, "ps_bridge.py not found — cannot process semi-custom order"
    if not _ps_alive():
        return False, "Photoshop not running — cannot process semi-custom order"
    if not rows:
        return False, "No rows supplied to _build_psd_semi_custom"

    # ── Resolve template ──────────────────────────────────────────────────────
    first_row     = rows[0]
    sku           = (first_row.get("SKU") or "").strip()
    template_name = get_semi_custom_template(sku)
    if not template_name:
        return False, f"No semi-custom template mapped for SKU: {sku}"
    template_path = os.path.join(TEMPLATES_FOLDER, template_name)
    if not os.path.isfile(template_path):
        return False, f"Template file not found: {template_path}"

    # ── Slot config ───────────────────────────────────────────────────────────
    slot_cfg   = SEMI_CUSTOM_SLOT_CONFIG.get(
        template_name,
        {"slot_h_px": 3780, "max_slots": 7, "player_layer": "Player", "number_layer": "08"}
    )
    max_slots  = slot_cfg["max_slots"]
    slot_h_px  = slot_cfg["slot_h_px"]
    player_lyr = slot_cfg["player_layer"]
    number_lyr = slot_cfg["number_layer"]

    # ── Parse all rows into items ─────────────────────────────────────────────
    items = []
    for row in rows:
        player_name       = (row.get("FrontText") or "").strip()
        jersey_number_raw = ""

        front_text_json_raw = (row.get("FrontTextJSON") or "").strip()
        if front_text_json_raw:
            try:
                import json as _json
                ftj = _json.loads(front_text_json_raw)
                if ftj.get("Text1"):
                    player_name = str(ftj["Text1"]).strip()
                if ftj.get("Text2"):
                    jersey_number_raw = str(ftj["Text2"]).strip()
            except Exception:
                pass

        if not player_name:
            log(f"  [semi-custom] Skipping row — player name empty (SKU: {row.get('SKU')})", "WARN")
            continue

        jersey_number = format_jersey_number(jersey_number_raw) if jersey_number_raw else "00"

        colour_hex = "#ffffff"
        front_colours_raw = (row.get("FrontColours") or "").strip()
        if front_colours_raw:
            try:
                import json as _json
                fc = _json.loads(front_colours_raw)
                colour_hex = fc.get("Colour1", "#ffffff")
            except Exception:
                pass

        log(f"  [semi-custom] item: name='{player_name}'  number='{jersey_number}'  colour={colour_hex}", "INFO")
        items.append({"player": player_name, "number": jersey_number, "colour_hex": colour_hex})

    if not items:
        return False, "No valid items extracted — all rows had empty player names"

    log(f"  [semi-custom] {len(items)} item(s) | template: {template_name}", "INFO")

    # ── Split into batches of max_slots; each batch → one PSD ─────────────────
    batches  = [items[i:i + max_slots] for i in range(0, len(items), max_slots)]
    suffixes = [""] + [f"_{chr(ord('b') + i)}" for i in range(len(batches) - 1)]  # "", "_b", "_c"…

    try:
        from ps_bridge import submit_job, wait_for_completion
    except Exception as e:
        return False, f"ps_bridge import error: {e}"

    for batch_idx, (batch, suffix) in enumerate(zip(batches, suffixes)):
        # Build output path — first batch uses out_path, extras get _b/_c suffix
        batch_path = out_path.replace(".psd", f"{suffix}.psd") if suffix else out_path
        if batch_idx > 0:
            from pathlib import Path as _Path
            _Path(batch_path).parent.mkdir(parents=True, exist_ok=True)

        # Build flat text_replacements using Photoshop copy-naming for each slot
        text_replacements = {}
        for slot_idx, item in enumerate(batch):
            text_replacements[_ps_copy_layer_name(player_lyr, slot_idx)] = item["player"]
            text_replacements[_ps_copy_layer_name(number_lyr, slot_idx)] = item["number"]

        k = len(batch)
        log(f"  [semi-custom] batch {batch_idx+1}/{len(batches)}: {k} slot(s) -> {os.path.basename(batch_path)}", "INFO")

        submit_job(
            order_id          = order_id,
            template_path     = template_path,
            zones             = {},
            output_path       = batch_path,
            combined          = False,
            job_type          = "semi_custom",
            text_replacements = text_replacements,  # all slots in one flat dict
            colour_hex        = batch[0]["colour_hex"],
            quantity          = k,        # UXP crops canvas to k × slot_h_px + (k-1) × 126px
            canvas_h_px       = slot_h_px, # height of one slot (UXP uses for crop calculation)
            items             = [],        # flat text_replacements handles everything
        )

        # Semi-custom templates are large (built up to 30,000px tall) — opening,
        # rendering, and saving one routinely takes several minutes regardless of
        # item count. 120s/item was too short and made Photoshop's real, successful
        # save get falsely marked as a timeout before it even finished, causing the
        # order to retry and produce a duplicate output file next pass.
        timeout = max(600, 150 * k)
        success = wait_for_completion(order_id, timeout_sec=timeout)
        if not success:
            return False, f"Photoshop job timed out or errored (batch {batch_idx + 1})"

    return True, f"-> {os.path.basename(out_path)}  ({len(items)} item(s), {len(batches)} PSD(s))"


# ─── ZONE BUILDER ─────────────────────────────────────────────────────────────

def build_zones(row, product):
    preview_map = {
        "front":  row.get("FrontPreviewImage")  or "",
        "back":   row.get("BackPreviewImage")   or "",
        "sleeve": row.get("SleevePreviewImage") or "",
        "pocket": row.get("PocketPreviewImage") or "",
    }

    sku = row.get("SKU") or ""

    bg_remove_flags = {
        "front":  bool(row.get("IsFrontBgRemove")),
        "back":   bool(row.get("IsBackBgRemove")),
        "pocket": bool(row.get("IsPocketBgRemove")),
        "sleeve": bool(row.get("IsSleeveBgRemove")),
    }

    def make_zone(label, zone_key, img_filename=None, text_lines=None, font=None, colour=None):
        w, h = get_dims(product, zone_key)
        return {
            "label":            label,
            "zone_key":         zone_key,
            "w":                w,
            "h":                h,
            "img_path":         find_image(img_filename) if img_filename else None,
            "img_filename":     img_filename or "",
            "text_lines":       text_lines or [],
            "font":             font   or "",
            "colour":           colour or "#ffffff",
            "preview_url":      preview_map.get(zone_key, ""),
            "sku":              sku,
            "force_bg_remove":  bg_remove_flags.get(zone_key, False),
        }

    zones = []

    # Per-zone fonts and colours
    # Premium fonts have built-in colour/texture — customer colour does not apply
    front_font   = parse_font(row.get("FrontFonts")   or "")
    front_colour = parse_colour(row.get("FrontColours") or "")
    back_font    = parse_font(row.get("BackFonts")    or "") or front_font
    back_colour  = parse_colour(row.get("BackColours") or "") or front_colour
    pocket_font  = parse_font(row.get("PocketFonts")  or "") or front_font
    pocket_colour= parse_colour(row.get("PocketColours") or "") or front_colour
    sleeve_font  = parse_font(row.get("SleeveFonts")  or "") or front_font
    sleeve_colour= parse_colour(row.get("SleeveColours") or "") or front_colour

    # Use Topaz-enhanced images when IsTopazImageProcess=1 (Topaz done).
    # _topaz_override is set by run_batch when it re-checked DB after 30-min wait
    # and still got NULL — in that case we fall back to original FrontImage.
    _topaz_flag = row.get("IsTopazImageProcess")
    use_topaz   = (_topaz_flag == 1 or _topaz_flag is True)

    def _resolve_imgs(topaz_col, json_col, plain_col):
        """Return list of image filenames for a zone, Topaz-first."""
        if use_topaz:
            v = (row.get(topaz_col) or "").strip()
            if v:
                # Topaz field may be JSON {"Image1":"..."} or a plain filename
                if v.startswith("{"):
                    imgs = parse_image_json(v)
                    if imgs:
                        return imgs
                else:
                    return [v]
        # Fall back to FrontImageJSON / plain FrontImage
        imgs = parse_image_json(row.get(json_col) or "")
        if imgs:
            return imgs
        plain = (row.get(plain_col) or "").strip()
        return [plain] if plain else []

    # FRONT — up to 5 images (front first so it appears at top of canvas)
    front_imgs = _resolve_imgs("FrontTopazImage", "FrontImageJSON", "FrontImage")
    front_text = parse_texts(row.get("FrontText") or "")
    if front_imgs:
        for i, fname in enumerate(front_imgs):
            label = "front" if len(front_imgs) == 1 else f"front {i+1}"
            zones.append(make_zone(label, "front", fname, front_text if i == 0 else [], front_font, front_colour))
    elif front_text:
        zones.append(make_zone("front", "front", text_lines=front_text, font=front_font, colour=front_colour))

    # BACK
    back_imgs = _resolve_imgs("BackTopazImage", "BackImageJSON", "BackImage")
    back_text = parse_texts(row.get("BackText") or "")
    if back_imgs:
        zones.append(make_zone("back", "back", back_imgs[0], back_text, back_font, back_colour))
    elif back_text:
        zones.append(make_zone("back", "back", text_lines=back_text, font=back_font, colour=back_colour))

    # POCKET — pocket left + right if 2 images
    pocket_imgs = _resolve_imgs("PocketTopazImage", "PocketImageJSON", "PocketImage")
    pocket_text = parse_texts(row.get("PocketText") or "")
    if len(pocket_imgs) >= 2:
        zones.append(make_zone("pocket left",  "pocket", pocket_imgs[0], font=pocket_font, colour=pocket_colour))
        zones.append(make_zone("pocket right", "pocket", pocket_imgs[1], font=pocket_font, colour=pocket_colour))
    elif len(pocket_imgs) == 1:
        zones.append(make_zone("pocket", "pocket", pocket_imgs[0], pocket_text, pocket_font, pocket_colour))
    elif pocket_text:
        zones.append(make_zone("pocket", "pocket", text_lines=pocket_text, font=pocket_font, colour=pocket_colour))

    # SLEEVE
    sleeve_imgs = _resolve_imgs("SleeveTopazImage", "SleeveImageJSON", "SleeveImage")
    sleeve_text = parse_texts(row.get("SleeveText") or "")
    if sleeve_imgs:
        zones.append(make_zone("sleeve", "sleeve", sleeve_imgs[0], sleeve_text, sleeve_font, sleeve_colour))
    elif sleeve_text:
        zones.append(make_zone("sleeve", "sleeve", text_lines=sleeve_text, font=sleeve_font, colour=sleeve_colour))

    return zones

# ─── PSD BUILDER ──────────────────────────────────────────────────────────────

def build_psd_for_order(order_id, row, out_path, no_bg_remove=False, force_bg_remove=False):
    sku      = row.get("SKU") or ""
    product  = detect_product(sku)
    zones    = build_zones(row, product)
    quantity = max(1, int(row.get("Quantity") or 1))

    if not zones:
        return False, "No zones found — no image or text data"

    PADDING  = cm_to_px(1)    # 1 cm border around whole canvas
    GAP      = cm_to_px(0.5)  # gap between different zones (front/back/sleeve)
    QTY_GAP  = cm_to_px(1.0)  # 1 cm gap between quantity copies (for cutting)

    # Label sits in its own small strip above each zone (not on the image)
    lbl_sample = build_label_layer("front")
    LABEL_H    = lbl_sample.height + cm_to_px(2.0)   # label text height + 2cm gap below it

    TEXT_GAP = cm_to_px(0.3)
    max_zw   = max(z["w"] for z in zones)
    canvas_w = PADDING + max_zw + PADDING

    # First pass: pre-build all layers so we know actual content heights
    for zone in zones:
        zw, zh = zone["w"], zone["h"]
        zone["_img"] = zone["_it"] = zone["_il"] = None
        if zone["img_path"]:
            zone["_img"], zone["_it"], zone["_il"] = build_image_layer(zone["img_path"], zw, zh, sku=sku, no_bg_remove=no_bg_remove, force_bg_remove=force_bg_remove or zone.get("force_bg_remove", False))
        elif zone["img_filename"]:
            log(f"    WARNING image not found: {zone['img_filename']}", "WARN")

        zone["_txt"] = zone["_tt"] = zone["_tl"] = None
        if zone["text_lines"]:
            zone["_txt"], zone["_tt"], zone["_tl"] = build_text_layer(
                zone["text_lines"], zone["font"], zone["colour"], zw, zh)

        zone["_prev"] = zone["_pnw"] = zone["_pnh"] = None
        if zone.get("preview_url"):
            pi = download_preview(zone["preview_url"])
            if pi:
                ratio = min(zw / pi.width, zh / pi.height)
                pnw = max(1, int(pi.width  * ratio))
                pnh = max(1, int(pi.height * ratio))
                zone["_prev"] = pi.resize((pnw, pnh), Image.LANCZOS)
                zone["_pnw"]  = pnw
                zone["_pnh"]  = pnh

        img_h    = zone["_img"].height if zone["_img"] else 0
        txt_h    = zone["_txt"].height if zone["_txt"] else 0
        raw_h    = txt_h + (TEXT_GAP + img_h if txt_h and img_h else img_h)
        # Always use actual content size — no spec_h padding.
        # Canvas adapts to content; printing team cuts by zone label.
        content_h = raw_h if raw_h > 0 else cm_to_px(1)
        # Per-copy spacing: use raw_h so qty repeats don't have huge gaps
        repeat_h  = raw_h if raw_h > 0 else content_h
        zone["_txt_v_offset"] = 0   # no extra centring — 1cm gap from label is enough
        zone["_txt_h"]    = txt_h
        zone["_img_h"]    = img_h
        zone["_content_h"] = content_h
        zone["_repeat_h"]  = repeat_h

    # ── Estimate bytes per copy and batch copies into ≤2 GB files ────────────
    MAX_FILE_BYTES  = 2 * 1024 ** 3
    content_block_h = (sum(LABEL_H + z["_content_h"] for z in zones)
                       + GAP * (len(zones) - 1))
    canvas_h        = PADDING + content_block_h + PADDING  # single-copy canvas height

    bytes_per_copy  = canvas_w * content_block_h * 5       # composite (5 CMYK channels)
    for zone in zones:
        for pil in (zone["_img"], zone["_txt"], zone["_prev"]):
            if pil:
                bytes_per_copy += pil.width * pil.height * 5

    batches:   list = []
    cur_batch: list = []
    cur_bytes       = 0
    for copy_idx in range(quantity):
        if cur_batch and cur_bytes + bytes_per_copy > MAX_FILE_BYTES:
            batches.append(cur_batch)
            cur_batch = [copy_idx]
            cur_bytes = bytes_per_copy
        else:
            cur_batch.append(copy_idx)
            cur_bytes += bytes_per_copy
    if cur_batch:
        batches.append(cur_batch)

    total_files   = len(batches)
    base_path     = out_path[:-4] if out_path.lower().endswith('.psd') else out_path
    written_paths = []

    for file_idx, batch_copies in enumerate(batches):
        n         = len(batch_copies)
        batch_h   = PADDING + n * content_block_h + (n - 1) * QTY_GAP + PADDING
        file_path = (f"{base_path}_{file_idx + 1}of{total_files}.psd"
                     if total_files > 1 else out_path)

        all_layers = []
        y_base     = PADDING

        for copy_idx in batch_copies:
            y_cursor = y_base

            for zone_idx, zone in enumerate(zones):
                zw            = zone["w"]
                x_left        = PADDING + (max_zw - zw) // 2
                display_label = zone.get("display_label") or zone["label"]

                lbl = build_label_layer(display_label)
                all_layers.append({
                    "name":    f"{display_label} label",
                    "image":   lbl,
                    "top":     y_cursor,
                    "left":    x_left,
                    "opacity": 255,
                    "visible": True,
                })

                content_start = y_cursor + LABEL_H
                img_pil   = zone["_img"];  it = zone["_it"];  il = zone["_il"]
                txt_pil   = zone["_txt"];  tt = zone["_tt"];  tl = zone["_tl"]
                prev_img  = zone["_prev"]; pnw = zone["_pnw"]; pnh = zone["_pnh"]
                txt_h     = zone["_txt_h"]
                content_h = zone["_content_h"]
                v_off     = zone.get("_txt_v_offset", 0)

                if txt_pil:
                    _tl_dict = {
                        "name":    f"{display_label} CustomerText",
                        "image":   txt_pil,
                        "top":     content_start + v_off + tt,
                        "left":    x_left + tl,
                        "opacity": 255,
                        "visible": True,
                    }
                    if EDITABLE_TEXT_AVAILABLE and zone.get("text_lines"):
                        _ps_font = resolve_ps_font_name(zone.get("font", "arial"))
                        _r, _g, _b = hex_to_rgb(zone.get("colour", "#ffffff"))
                        _tl_dict["_text_blocks"] = build_editable_text_tagged_blocks(
                            text="\n".join(zone["text_lines"]),
                            font_name=_ps_font,
                            font_size_px=txt_pil.height,
                            r=_r, g=_g, b=_b,
                            px_per_cm=PX_PER_CM,
                            layer_left=x_left + tl,
                            layer_top=content_start + v_off + tt,
                            layer_w=txt_pil.width,
                            layer_h=txt_pil.height,
                        )
                    all_layers.append(_tl_dict)

                if img_pil:
                    img_top = content_start + txt_h + (TEXT_GAP if txt_pil else 0) + it
                    all_layers.append({
                        "name":    f"{display_label} CustomerImage",
                        "image":   img_pil,
                        "top":     img_top,
                        "left":    x_left + il,
                        "opacity": 255,
                        "visible": True,
                    })

                if prev_img:
                    all_layers.append({
                        "name":    f"{display_label} Preview Reference",
                        "image":   prev_img,
                        "top":     content_start + (content_h - pnh) // 2,
                        "left":    x_left + (zw - pnw) // 2,
                        "opacity": 255,
                        "visible": False,
                    })

                y_cursor += LABEL_H + content_h
                if zone_idx < len(zones) - 1:
                    y_cursor += GAP

            y_base += content_block_h + QTY_GAP

        actual = write_psd(file_path, canvas_w, batch_h, all_layers)
        if actual and os.path.isfile(actual):
            written_paths.append(actual)

    if not written_paths:
        return False, "PSD file not written"

    total_mb   = sum(os.path.getsize(p) for p in written_paths) / (1024 * 1024)
    zone_names = [z["label"] for z in zones]
    return True, f"{total_mb:.1f} MB total | {len(written_paths)} file(s) | zones: {zone_names}"


def rows_have_same_design(rows):
    """True when all rows share the same customisation content (text/fonts/colours).
    Image filenames are intentionally excluded — Amazon stores the same customer
    image under different filenames for each size variant in a multi-size order."""
    if len(rows) <= 1:
        return True
    def sig(row):
        return (
            (row.get("FrontText") or "").strip(),
            (row.get("FrontFonts") or "").strip(),
            (row.get("FrontColours") or "").strip(),
            (row.get("BackText") or "").strip(),
            (row.get("BackFonts") or "").strip(),
            (row.get("BackColours") or "").strip(),
            (row.get("PocketText") or "").strip(),
            (row.get("PocketFonts") or "").strip(),
            (row.get("PocketColours") or "").strip(),
            (row.get("SleeveText") or "").strip(),
            (row.get("SleeveFonts") or "").strip(),
            (row.get("SleeveColours") or "").strip(),
        )
    first = sig(rows[0])
    return all(sig(r) == first for r in rows)


def build_merged_psd_for_order_group(order_id, rows, out_path, no_bg_remove=False, force_bg_remove=False):
    """
    Builds one merged PSD for an order that has multiple items (rows).

    Owner's rules:
      - All items identical design → stack vertically, label = "front" (no SKU detail)
      - Items have different designs → stack vertically, label = "Front - White XL" etc.
      - 1cm gap between copies (for cutting)
    """
    if not rows:
        return False, "No rows"

    same_design = rows_have_same_design(rows)
    log(f"  Order group: {len(rows)} items, same_design={same_design}", "INFO")

    PADDING  = cm_to_px(1)
    QTY_GAP  = cm_to_px(1.0)
    TEXT_GAP = cm_to_px(0.3)
    lbl_h    = build_label_layer("front").height + cm_to_px(2.0)

    # Build zones for every row, attaching display_label and pre-built layers
    # First pass: collect zones and compute canvas width
    all_row_zones = []
    for row in rows:
        sku     = row.get("SKU") or ""
        product = detect_product(sku)
        zones   = build_zones(row, product)
        for z in zones:
            z["display_label"] = (z["label"].title() if same_design
                                  else make_zone_label(z["label"], sku, use_sku_detail=True))
        all_row_zones.append(zones)

    all_zones_flat = [z for zones in all_row_zones for z in zones]
    if not all_zones_flat:
        return False, "No zones in any row"

    # Canvas width — determined by the widest zone (e.g. adult XXL tshirt)
    max_zw   = max(z["w"] for z in all_zones_flat)

    # Second pass: pre-build layers using each zone's own spec width so smaller
    # garments (e.g. kids tshirt 23cm) render proportionally smaller than adult XXL (30cm)
    for zones in all_row_zones:
        for z in zones:
            draw_w = z["w"]   # use this zone's own spec width, not the widest
            zh     = z["h"]

            z["_img"] = z["_it"] = z["_il"] = None
            if z["img_path"]:
                z["_img"], z["_it"], z["_il"] = build_image_layer(z["img_path"], draw_w, zh, sku=z.get("sku"), no_bg_remove=no_bg_remove, force_bg_remove=force_bg_remove or z.get("force_bg_remove", False))
            elif z["img_filename"]:
                log(f"    WARNING image not found: {z['img_filename']}", "WARN")

            z["_txt"] = z["_tt"] = z["_tl"] = None
            if z["text_lines"]:
                z["_txt"], z["_tt"], z["_tl"] = build_text_layer(
                    z["text_lines"], z["font"], z["colour"], draw_w, zh)

            z["_prev"] = z["_pnw"] = z["_pnh"] = None
            if z.get("preview_url"):
                pi = download_preview(z["preview_url"])
                if pi:
                    ratio = min(draw_w / pi.width, zh / pi.height)
                    pnw = max(1, int(pi.width  * ratio))
                    pnh = max(1, int(pi.height * ratio))
                    z["_prev"] = pi.resize((pnw, pnh), Image.LANCZOS)
                    z["_pnw"]  = pnw
                    z["_pnh"]  = pnh

            img_h    = z["_img"].height if z["_img"] else 0
            txt_h    = z["_txt"].height if z["_txt"] else 0
            raw_h    = txt_h + (TEXT_GAP + img_h if txt_h and img_h else img_h)
            content_h = raw_h if raw_h > 0 else cm_to_px(1)
            z["_txt_v_offset"] = 0
            z["_txt_h"]    = txt_h
            z["_img_h"]    = img_h
            z["_content_h"] = content_h

    canvas_w = PADDING + max_zw + PADDING

    GAP = cm_to_px(0.5)  # gap between zones within the same item (front/back/sleeve)

    def _row_canvas_h(zones):
        if not zones:
            return 0
        return (PADDING
                + sum(lbl_h + z["_content_h"] for z in zones)
                + GAP * (len(zones) - 1)
                + PADDING)

    written_paths = []

    for row_idx, (row, zones) in enumerate(zip(rows, all_row_zones)):
        if not zones:
            continue

        row_h      = _row_canvas_h(zones)
        row_layers = []
        y_cursor   = PADDING

        for zone_idx, zone in enumerate(zones):
            display_label = zone["display_label"]
            x_left    = PADDING + (max_zw - zone["w"]) // 2
            img_start = y_cursor + lbl_h

            lbl = build_label_layer(display_label)
            row_layers.append({
                "name": f"{display_label} label",
                "image": lbl,
                "top": y_cursor,
                "left": x_left,
                "opacity": 255, "visible": True,
            })

            v_off = zone.get("_txt_v_offset", 0)
            if zone["_txt"]:
                _tl_dict2 = {
                    "name": f"{display_label} CustomerText",
                    "image": zone["_txt"],
                    "top": img_start + v_off + zone["_tt"],
                    "left": x_left + zone["_tl"],
                    "opacity": 255, "visible": True,
                }
                if EDITABLE_TEXT_AVAILABLE and zone.get("text_lines"):
                    _ps_font2 = resolve_ps_font_name(zone.get("font", "arial"))
                    _r2, _g2, _b2 = hex_to_rgb(zone.get("colour", "#ffffff"))
                    _tl_dict2["_text_blocks"] = build_editable_text_tagged_blocks(
                        text="\n".join(zone["text_lines"]),
                        font_name=_ps_font2,
                        font_size_px=zone["_txt"].height,
                        r=_r2, g=_g2, b=_b2,
                        px_per_cm=PX_PER_CM,
                        layer_left=x_left + zone["_tl"],
                        layer_top=img_start + v_off + zone["_tt"],
                        layer_w=zone["_txt"].width,
                        layer_h=zone["_txt"].height,
                    )
                row_layers.append(_tl_dict2)

            if zone["_img"]:
                img_top = img_start + zone["_txt_h"] + (TEXT_GAP if zone["_txt"] else 0) + zone["_it"]
                row_layers.append({
                    "name": f"{display_label} CustomerImage",
                    "image": zone["_img"],
                    "top": img_top,
                    "left": x_left + zone["_il"],
                    "opacity": 255, "visible": True,
                })

            if zone["_prev"]:
                pnh       = zone["_pnh"]
                pnw       = zone["_pnw"]
                content_h = zone["_content_h"]
                row_layers.append({
                    "name":    f"{display_label} Preview Reference",
                    "image":   zone["_prev"],
                    "top":     img_start + (content_h - pnh) // 2,
                    "left":    x_left + (max_zw - pnw) // 2,
                    "opacity": 255,
                    "visible": False,
                })

            y_cursor += lbl_h + zone["_content_h"]
            if zone_idx < len(zones) - 1:
                y_cursor += GAP

        if row_idx == 0:
            row_path = out_path
        else:
            base = out_path[:-4] if out_path.lower().endswith('.psd') else out_path
            row_path = f"{base}-{row_idx}.psd"

        actual = write_psd(row_path, canvas_w, row_h, row_layers)
        if actual and os.path.isfile(actual):
            written_paths.append(actual)

    if not written_paths:
        return False, "PSD file not written"

    total_mb = sum(os.path.getsize(p) for p in written_paths) / (1024 * 1024)
    labels   = [z.get("display_label") for zones in all_row_zones for z in zones]
    return True, f"{total_mb:.1f} MB total | {len(written_paths)} file(s) | labels: {labels}"


# ─── DATABASE ─────────────────────────────────────────────────────────────────

def get_db():
    return _db_get_connection()

def fetch_orders(limit=None, order_id_filter=None, sku_filter=None, multizone=False, reprocess=False, date_filter=None, date_after=None, font_filter=None, hours=None, with_images=False):
    conn  = get_db()
    cur   = conn.cursor()
    if reprocess:
        where = "1=1"   # skip all filters when reprocessing
    else:
        # Already-shipped orders don't need a PSD anymore — they went out through
        # some other route. Excluding them stops the daemon wasting cycles on
        # stale backlog (e.g. an entire day's worth of orders that shipped
        # without ever needing automation output) instead of newer, still-needed ones.
        where = ("(d.Topaz_Processed = 0 OR d.Topaz_Processed IS NULL)"
                 " AND (o.IsShipped = 0 OR o.IsShipped IS NULL)")
    if order_id_filter:
        if isinstance(order_id_filter, list):
            # Flatten any comma-separated values in the list
            flat = []
            for item in order_id_filter:
                flat.extend(i.strip() for i in item.split(",") if i.strip())
            ids = "','".join(flat)
            where += f" AND o.OrderID IN ('{ids}')"
        else:
            # Also handle comma-separated string
            parts = [i.strip() for i in order_id_filter.split(",") if i.strip()]
            if len(parts) > 1:
                ids = "','".join(parts)
                where += f" AND o.OrderID IN ('{ids}')"
            else:
                where += f" AND o.OrderID = '{order_id_filter}'"
    if sku_filter:
        like_clauses = " OR ".join(f"o.SKU LIKE '%{s}%'" for s in sku_filter.split(","))
        where += f" AND ({like_clauses})"
    if multizone:
        where += " AND d.PrintLocation LIKE '%+%'"
    if date_filter:
        where += f" AND CAST(o.DateAdd AS DATE) = '{date_filter}'"
    if date_after:
        where += f" AND o.DateAdd >= '{date_after}'"
    if hours:
        where += f" AND o.DateAdd >= DATEADD(HOUR, -{hours}, GETUTCDATE())"
    if with_images:
        where += (" AND (ISNULL(d.FrontImage,'') <> '' OR ISNULL(d.BackImage,'') <> ''"
                  " OR ISNULL(d.PocketImage,'') <> '' OR ISNULL(d.SleeveImage,'') <> '')")
    if font_filter:
        fonts = [f.strip() for f in font_filter.split(",") if f.strip()]
        like_parts = []
        for f in fonts:
            like_parts.append(f"d.FrontFonts LIKE '%{f}%'")
            like_parts.append(f"d.BackFonts LIKE '%{f}%'")
        where += f" AND ({' OR '.join(like_parts)})"
    top = f"TOP {limit}" if limit else ""
    # Check whether the premium font columns exist (live DB has them, local may not)
    try:
        cur.execute("SELECT TOP 0 FrontPremiumFont FROM tblCustomOrderDetails")
        premium_cols = ", d.FrontPremiumFont, d.BackPremiumFont, d.PocketPremiumFont, d.SleevePremiumFont"
    except Exception:
        premium_cols = ""
        conn.close(); conn = get_db(); cur = conn.cursor()

    # Check whether the Topaz columns exist (added by backend team)
    try:
        cur.execute("SELECT TOP 0 FrontTopazImage FROM tblCustomOrderDetails")
        topaz_cols = (", d.FrontTopazImage, d.BackTopazImage, d.PocketTopazImage, d.SleeveTopazImage"
                      ", d.IsTopazImageProcess")
    except Exception:
        topaz_cols = ""
        conn.close(); conn = get_db(); cur = conn.cursor()

    # Check whether the background-removal flag columns exist
    try:
        cur.execute("SELECT TOP 0 IsFrontBgRemove FROM tblCustomOrderDetails")
        bg_remove_cols = ", d.IsFrontBgRemove, d.IsBackBgRemove, d.IsPocketBgRemove, d.IsSleeveBgRemove"
    except Exception:
        bg_remove_cols = ""
        conn.close(); conn = get_db(); cur = conn.cursor()

    cur.execute(f"""
        SELECT {top}
            o.OrderID, o.SKU, o.ItemType, o.Quantity, o.DateAdd,
            o.ConvertedShipByDate,
            d.idCustomOrderDetails, d.PrintLocation,
            d.FrontText, d.FrontTextJSON, d.FrontFonts, d.FrontColours,
            d.FrontImage, d.FrontImageJSON, d.FrontPreviewImage,
            d.BackText, d.BackFonts, d.BackColours,
            d.BackImage, d.BackImageJSON, d.BackPreviewImage,
            d.PocketText, d.PocketFonts, d.PocketColours,
            d.PocketImage, d.PocketImageJSON, d.PocketPreviewImage,
            d.SleeveText, d.SleeveFonts, d.SleeveColours,
            d.SleeveImage, d.SleeveImageJSON, d.SleevePreviewImage,
            d.CustomizationCategory
            {premium_cols}
            {topaz_cols}
            {bg_remove_cols}
        FROM tblCustomOrder o
        JOIN tblCustomOrderDetails d ON o.idCustomOrder = d.idCustomOrder
        WHERE {where}
        ORDER BY o.DateAdd ASC
    """)
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    return rows

NO_MARK = False   # set by --no-mark: test exports must not touch the live Topaz_Processed flags

def mark_processed(detail_id):
    """Mark a single order detail row as processed by the automation."""
    if NO_MARK:
        return
    conn = get_db()
    conn.cursor().execute("""
        UPDATE tblCustomOrderDetails
        SET Topaz_Processed = 1
        WHERE idCustomOrderDetails = ?
    """, detail_id)
    conn.commit()
    conn.close()

# ─── GOOGLE DRIVE UPLOAD ──────────────────────────────────────────────────────

GDRIVE_CREDENTIALS  = os.environ.get("GDRIVE_CREDENTIALS", os.path.join(_base, "credentials.json"))
GDRIVE_TOKEN        = os.environ.get("GDRIVE_TOKEN",       os.path.join(_base, "gdrive_token.json"))
GDRIVE_ROOT_FOLDER  = "1ZObOngMUAQo519ThI0vEckR4waKp7bsj"   # shared Drive folder
GDRIVE_SCOPES       = ["https://www.googleapis.com/auth/drive.file"]

_gdrive_service = None   # module-level cache

def get_gdrive_service():
    global _gdrive_service
    if _gdrive_service:
        return _gdrive_service
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        creds = None
        if os.path.exists(GDRIVE_TOKEN):
            creds = Credentials.from_authorized_user_file(GDRIVE_TOKEN, GDRIVE_SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(GDRIVE_CREDENTIALS, GDRIVE_SCOPES)
                creds = flow.run_local_server(port=0)
            with open(GDRIVE_TOKEN, "w") as f:
                f.write(creds.to_json())

        _gdrive_service = build("drive", "v3", credentials=creds)
        return _gdrive_service
    except Exception as e:
        log(f"Google Drive auth failed: {e}", "WARN")
        return None


def gdrive_get_or_create_folder(service, name, parent_id):
    """Return the Drive folder ID for `name` inside `parent_id`, creating it if needed."""
    q = (f"name='{name}' and mimeType='application/vnd.google-apps.folder' "
         f"and '{parent_id}' in parents and trashed=false")
    res = service.files().list(q=q, fields="files(id,name)", pageSize=1).execute()
    items = res.get("files", [])
    if items:
        return items[0]["id"]
    meta = {
        "name":     name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents":  [parent_id],
    }
    folder = service.files().create(body=meta, fields="id").execute()
    return folder["id"]


def gdrive_upload_file(service, local_path, parent_id):
    """Upload a file to Drive, replacing any existing file with the same name."""
    from googleapiclient.http import MediaFileUpload
    fname = os.path.basename(local_path)

    # Delete existing file with same name to avoid duplicates
    q = f"name='{fname}' and '{parent_id}' in parents and trashed=false"
    existing = service.files().list(q=q, fields="files(id)", pageSize=1).execute().get("files", [])
    for f in existing:
        service.files().delete(fileId=f["id"]).execute()

    mime = "application/octet-stream"
    media = MediaFileUpload(local_path, mimetype=mime, resumable=True, chunksize=10 * 1024 * 1024)
    meta  = {"name": fname, "parents": [parent_id]}
    uploaded = service.files().create(body=meta, media_body=media, fields="id,name,size").execute()
    size_mb = int(uploaded.get("size", 0)) / 1_048_576
    return uploaded["id"], size_mb


def gdrive_upload_psd(local_path, date_str, folder_type, colour_sub):
    """Mirror the local folder structure on Drive and upload the PSD."""
    try:
        service = get_gdrive_service()
        if not service:
            return False

        # Build path: ROOT / date / folder_type / [colour_sub]
        date_id   = gdrive_get_or_create_folder(service, date_str,    GDRIVE_ROOT_FOLDER)
        type_id   = gdrive_get_or_create_folder(service, folder_type, date_id)
        parent_id = gdrive_get_or_create_folder(service, colour_sub, type_id) if colour_sub else type_id

        file_id, size_mb = gdrive_upload_file(service, local_path, parent_id)
        log(f"  Drive: uploaded {os.path.basename(local_path)}  ({size_mb:.1f} MB)", "OK")
        return True
    except Exception as e:
        log(f"  Drive upload failed: {e}", "WARN")
        return False


def _get_psd_split_paths(base_path):
    """Return base_path plus any -1, -2, ... split files that exist alongside it."""
    paths = [base_path] if os.path.isfile(base_path) else []
    root = base_path[:-4] if base_path.lower().endswith('.psd') else base_path
    i = 1
    while True:
        p = f"{root}-{i}.psd"
        if os.path.isfile(p):
            paths.append(p)
            i += 1
        else:
            break
    return paths


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def run_batch(limit=None, order_id_filter=None, dry_run=False, sku_filter=None, multizone=False, reprocess=False, date_filter=None, date_after=None, upload_gdrive=False, upload_nas=False, upload_b2=False, no_bg_remove=False, force_bg_remove=False, output_folder=None, font_filter=None, hours=None, with_images=False):
    log("=" * 60)
    log(f"Varsany Batch Processor  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"Resolution : {DPI} DPI  ({PX_PER_CM:.2f} px/cm)")
    log(f"Images     : downloaded on-demand from {IMAGE_SERVER_URL}")
    log(f"Fonts      : {list(FONT_INDEX.keys())}")
    if dry_run:
        log("MODE       : DRY RUN — no files written")
    log("=" * 60)

    orders = fetch_orders(limit=limit, order_id_filter=order_id_filter,
                          sku_filter=sku_filter, multizone=multizone, reprocess=reprocess,
                          date_filter=date_filter, date_after=date_after, font_filter=font_filter,
                          hours=hours, with_images=with_images)
    total  = len(orders)
    log(f"Orders to process: {total}")

    if not orders:
        log("Nothing to process.")
        return

    ok_count      = 0
    fail_count    = 0
    skip_count    = 0
    sweep_failures = []  # collected this sweep: [{order_id, sku, reason}]

    nas_uploader = None
    if upload_nas:
        if SYNOLOGY_AVAILABLE:
            nas_uploader = SynologyUploader()
            if not nas_uploader.connected:
                log("NAS connection failed — uploads will be skipped", "WARN")
                nas_uploader = None
        else:
            log("synology_upload.py not found — --nas flag ignored", "WARN")

    b2_uploader = None
    if upload_b2:
        if B2_AVAILABLE:
            b2_uploader = B2Uploader()
            if not b2_uploader.connected:
                log("B2 connection failed — uploads will be skipped", "WARN")
                b2_uploader = None
        else:
            log("b2sdk not installed — --b2 flag ignored. Run: pip install b2sdk", "WARN")

    # Group rows by OrderID — one PSD per order (may contain multiple SKUs)
    from collections import OrderedDict
    order_groups = OrderedDict()
    for row in orders:
        oid = row["OrderID"]
        if oid not in order_groups:
            order_groups[oid] = []
        order_groups[oid].append(row)

    total_orders = len(order_groups)
    log(f"Unique orders: {total_orders}  (from {total} rows)")

    for i, (order_id, group_rows) in enumerate(order_groups.items(), 1):
        first_row   = group_rows[0]

        # Date folder: use ConvertedShipByDate, fall back to today if missing
        _ship = first_row.get("ConvertedShipByDate")
        _date_str = str(_ship)[:10] if _ship else datetime.now().strftime("%Y-%m-%d")
        if output_folder:
            out_dir = output_folder
        else:
            out_dir = os.path.join(OUTPUT_FOLDER, _date_str)
        os.makedirs(out_dir, exist_ok=True)

        safe_id     = order_id.replace("/", "-")
        sku_raw     = first_row.get("SKU") or ""
        sku         = sku_raw.replace("/", "-").replace("\\", "-")
        is_emb_rhine = any(is_emb_rhine_row(r) for r in group_rows)
        is_multi     = any(is_multizone_row(r) for r in group_rows)
        is_kids_hood = any("kidshoo" in (r.get("SKU") or "").lower() or
                           "gymhoodie" in (r.get("SKU") or "").lower() or
                           "kidshood" in (r.get("SKU") or "").lower()
                           for r in group_rows)
        if is_emb_rhine:
            log(f"  Skipping {order_id} — Emb & Rhine (manual process)", "INFO")
            skip_count += 1
            continue

        # ── Plain / no-personalisation orders ────────────────────────────────
        _print_loc = (first_row.get("PrintLocation") or "").lower()
        if "plain" in _print_loc or "no personalisation" in _print_loc:
            log(f"  Plain order {order_id} — no PSD needed, marking complete", "INFO")
            for row in group_rows:
                mark_processed(row["idCustomOrderDetails"])
            ok_count += 1
            continue

        # ── Topaz / image-readiness check ────────────────────────────────────
        # Each row/item can have its own Topaz state, so check per-row rather than
        # assuming first_row speaks for the whole group — a multi-item order can have
        # one item ready and another still pending. A row is "pending" when Topaz
        # hasn't confirmed done (flag isn't 1/True) AND a Topaz filename is already
        # on record for it — that's what tells us Topaz was actually asked to produce
        # one. A flag of 0 with no Topaz filename at all means Topaz was never
        # invoked for that image, so there's nothing to wait for.
        def _topaz_pending(r):
            if r.get("IsTopazImageProcess") in (1, True):
                return False
            return any((r.get(c) or "").strip() for c in
                       ("FrontTopazImage", "BackTopazImage", "PocketTopazImage", "SleeveTopazImage"))

        _has_image = any(
            (r.get("FrontImage") or "").strip() or
            (r.get("BackImage")  or "").strip() or
            (r.get("PocketImage") or "").strip() or
            (r.get("SleeveImage") or "").strip()
            for r in group_rows
        )
        _pending_rows = [r for r in group_rows if _topaz_pending(r)]

        if _has_image and _pending_rows:
            # Image order but Topaz hasn't confirmed done for at least one item.
            # Option C: wait 30 mins, then re-check DB.
            #   → If Topaz flipped to 1  → use FrontTopazImage (best quality)
            #   → If still not ready after 30m → fall back to original FrontImage
            date_add = first_row.get("DateAdd")
            age_mins = None
            if date_add:
                try:
                    # DateAdd is written in the DB server's local time (UK), not UTC —
                    # confirmed by comparing GETDATE()/GETUTCDATE() against this machine's
                    # own clock, which runs the same local timezone. datetime.utcnow() here
                    # was comparing a local timestamp against true UTC, silently shifting
                    # every age by the local UTC offset (+1h during BST).
                    age_mins = (datetime.now() - date_add).total_seconds() / 60
                except Exception:
                    age_mins = None

            if age_mins is not None and age_mins < 30:
                log(f"  Deferring {order_id} — image order, Topaz pending, "
                    f"only {age_mins:.0f} min old (need 30)", "INFO")
                skip_count += 1
                continue

            # Order is ≥ 30 mins old — re-query DB for each pending row's latest IsTopazImageProcess
            for r in _pending_rows:
                try:
                    _recheck_conn = get_db()
                    _rc = _recheck_conn.cursor()
                    _rc.execute(
                        "SELECT IsTopazImageProcess, FrontTopazImage, BackTopazImage, "
                        "PocketTopazImage, SleeveTopazImage "
                        "FROM tblCustomOrderDetails "
                        "WHERE idCustomOrderDetails = ?",
                        r.get("idCustomOrderDetails")
                    )
                    _fresh = _rc.fetchone()
                    _recheck_conn.close()
                    if _fresh:
                        _fresh_topaz_flag = _fresh[0]
                        if _fresh_topaz_flag == 1 or _fresh_topaz_flag is True:
                            # Topaz is now done for this item — update just this row's Topaz data
                            log(f"  Topaz now ready for {order_id} [{r.get('idCustomOrderDetails')}] — using Topaz images", "INFO")
                            r["IsTopazImageProcess"] = 1
                            r["FrontTopazImage"]  = _fresh[1] or r.get("FrontTopazImage")
                            r["BackTopazImage"]   = _fresh[2] or r.get("BackTopazImage")
                            r["PocketTopazImage"] = _fresh[3] or r.get("PocketTopazImage")
                            r["SleeveTopazImage"] = _fresh[4] or r.get("SleeveTopazImage")
                        # else: still not ready after 30 mins — process with original image (use_topaz stays False)
                except Exception:
                    pass  # DB re-check failed — process with original image silently
        # Text-only orders (no image) and rows with Topaz already ready/not-expected fall through to normal processing

        if is_multi:
            folder_type = "Automated"
        elif is_kids_hood:
            folder_type = "DTF Kids Hoodie"
        else:
            folder_type = "DTF Front"
        # Automated folder has no colour sub-folder; DTF Front and DTF Kids Hoodie do
        colour_sub = "" if is_multi else sku_colour_folder(sku_raw)
        if colour_sub:
            cat_dir = os.path.join(out_dir, folder_type, colour_sub)
        else:
            cat_dir = os.path.join(out_dir, folder_type)
        os.makedirs(cat_dir, exist_ok=True)

        # Filename: OrderID.psd
        if len(group_rows) == 1:
            base_name = f"{safe_id}.psd"
        else:
            base_name = f"{safe_id}_{len(group_rows)}items.psd"
        base_path = os.path.join(cat_dir, base_name)
        out_path  = base_path
        counter   = 2
        while os.path.exists(out_path):
            out_path = base_path.replace(".psd", f"_{counter}.psd")
            counter += 1

        skus_str = " | ".join(r.get("SKU", "") for r in group_rows)
        log(f"[{i}/{total_orders}] {order_id}  ({len(group_rows)} items)  |  {skus_str}")

        # ── Semi-customised detection ─────────────────────────────────────────
        # Only treat as semi-custom if the SKU has a known jersey template.
        # Orders tagged SemiCustomized but with no template go through normal processing.
        _cat = (first_row.get("CustomizationCategory") or "").strip().lower()
        is_semi_custom = _cat == "semicustomized" and get_semi_custom_template(sku_raw) is not None

        if is_semi_custom:
            # Override output path → Output\<date>\semicustomized\
            semi_custom_dir = os.path.join(out_dir, "semicustomized")
            os.makedirs(semi_custom_dir, exist_ok=True)
            safe_id   = order_id.replace("/", "-")
            semi_path = os.path.join(semi_custom_dir, f"{safe_id}.psd")
            _batch_suffixes = list("bcdefghijklmnopqrstuvwxyz")
            _bi = 0
            while os.path.exists(semi_path):
                suffix = f"_{_batch_suffixes[_bi]}" if _bi < len(_batch_suffixes) else f"_{_bi + 2}"
                semi_path = os.path.join(semi_custom_dir, f"{safe_id}{suffix}.psd")
                _bi += 1
            out_path = semi_path

        if dry_run:
            if is_semi_custom:
                import json as _json
                ftj_raw = (first_row.get("FrontTextJSON") or "").strip()
                ftj = {}
                try: ftj = _json.loads(ftj_raw)
                except Exception: pass
                log(f"  [semi-custom]  name='{ftj.get('Text1', first_row.get('FrontText',''))}'  "
                    f"number='{format_jersey_number(ftj.get('Text2',''))}'"
                    f"  template={get_semi_custom_template(sku_raw) or 'NONE'}", "DRY")
            else:
                for row in group_rows:
                    product = detect_product(row.get("SKU") or "")
                    zones   = build_zones(row, product)
                    for z in zones:
                        status = "FOUND" if z["img_path"] else ("MISSING" if z["img_filename"] else "text-only")
                        log(f"  [{z['label']}]  img={z['img_filename'] or 'none'} ({status})  text={z['text_lines']}", "DRY")
                    if not zones:
                        log("  SKIP — no zones", "DRY")
            continue

        try:
            ok = False
            msg = ""

            # ── Semi-custom: template text replacement ────────────────────────
            if is_semi_custom:
                # Pass ALL rows so multi-name orders (e.g. 4 football jerseys)
                # are stacked onto one canvas instead of only processing row 1.
                ok, msg = _build_psd_semi_custom(order_id, group_rows, out_path)
                if not ok:
                    log(f"  Semi-custom FAILED: {msg}", "FAIL")

            else:
                # ── Engine selection ──────────────────────────────────────────
                # USE_PHOTOSHOP_BRIDGE=1 in .env → strictly use UXP plugin only.
                # USE_PHOTOSHOP_BRIDGE not set → use Python PSD writer only.
                # No cross-fallback between engines.
                if _USE_PS_BRIDGE:
                    if len(group_rows) == 1:
                        # Single item → one PSD with qty copies stacked
                        qty = max(1, int(first_row.get("Quantity") or 1))
                        ok, msg = _build_psd_via_photoshop(order_id, first_row, out_path,
                                                           force_bg_remove=force_bg_remove,
                                                           quantity_override=qty if qty > 1 else None)
                        if not ok:
                            log(f"  UXP Bridge FAILED: {msg}", "FAIL")
                    else:
                        # Multiple items (same or different design) → always one combined PSD,
                        # each item labeled with its own SKU/size and stacked by its own qty
                        ok, msg = _build_psd_multi_via_photoshop(order_id, group_rows, out_path,
                                                                  force_bg_remove=force_bg_remove)
                        if not ok:
                            log(f"  UXP Bridge FAILED: {msg}", "FAIL")
                else:
                    ok, msg = build_merged_psd_for_order_group(order_id, group_rows, out_path, no_bg_remove=no_bg_remove, force_bg_remove=force_bg_remove)

            if ok:
                log(f"  OK  {msg}", "OK")
                for row in group_rows:
                    mark_processed(row["idCustomOrderDetails"])
                ok_count += 1
                if upload_gdrive and not dry_run:
                    for _split in _get_psd_split_paths(out_path):
                        gdrive_upload_psd(_split, _date_str, folder_type, colour_sub)
                if nas_uploader and not dry_run:
                    for _split in _get_psd_split_paths(out_path):
                        nas_uploader.upload(_split, sub_folder=_date_str)
                if b2_uploader and not dry_run:
                    for _split in _get_psd_split_paths(out_path):
                        b2_uploader.upload(_split)
            else:
                log(f"  FAIL  {msg}", "FAIL")
                fail_count += 1
                sweep_failures.append({"order_id": order_id, "sku": skus_str, "reason": msg})
        except Exception as e:
            log(f"  ERROR  {e}", "ERROR")
            log(traceback.format_exc()[-400:], "ERROR")
            fail_count += 1
            sweep_failures.append({"order_id": order_id, "sku": skus_str, "reason": f"Exception: {e}"})

        if i % 50 == 0:
            log(f"--- Progress {i}/{total_orders}  ok={ok_count}  fail={fail_count} ---")

    log("=" * 60)
    log(f"DONE  {ok_count} OK  |  {fail_count} FAILED  |  {skip_count} SKIPPED (Emb & Rhine)  |  Output: {out_dir}")
    log("=" * 60)
    return sweep_failures


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Varsany Batch PSD Processor")
    parser.add_argument("--limit",      type=int, default=None, help="Max orders to process")
    parser.add_argument("--order",      type=str, default=None, action="append", help="Process specific OrderID(s) — can be repeated")
    parser.add_argument("--dry-run",    action="store_true",    help="Preview only, no files written")
    parser.add_argument("--dpi",        type=int, default=320,  help="Output resolution in DPI (pixels/inch)")
    parser.add_argument("--sku-filter", type=str, default=None, help="Comma-separated SKU substrings e.g. MenTee,WmnTee")
    parser.add_argument("--multizone",   action="store_true",    help="Only orders with multiple print zones")
    parser.add_argument("--reprocess",   action="store_true",    help="Re-export already-completed orders")
    parser.add_argument("--date",        type=str, default=None, help="Export orders from a specific date e.g. 2026-02-28")
    parser.add_argument("--date-after",  type=str, default=None, help="Export orders placed after a date e.g. 2026-04-10")
    parser.add_argument("--gdrive",        action="store_true",    help="Upload finished PSDs to Google Drive after export")
    parser.add_argument("--nas",           action="store_true",    help="Upload finished PSDs to Synology NAS after export")
    parser.add_argument("--b2",            action="store_true",    help="Upload finished PSDs to Backblaze B2 after export")
    parser.add_argument("--no-bg-remove",    action="store_true",    help="Skip background removal for all zones")
    parser.add_argument("--force-bg-remove", action="store_true",    help="Force background removal for all zones, bypassing auto-detection")
    parser.add_argument("--output",        type=str, default=None, help="Override output folder e.g. C:\\gimpTest\\Output\\test1")
    parser.add_argument("--font-filter",   type=str, default=None, help="Comma-separated font name substrings e.g. 'Mermaid Font,Block Font'")
    parser.add_argument("--hours",         type=int, default=None, help="Only process orders added in the last N hours e.g. --hours 2")
    parser.add_argument("--with-images",   action="store_true",    help="Only process orders that have a customer image upload")
    parser.add_argument("--no-mark",       action="store_true",    help="Test mode: build the PSDs but never write Topaz_Processed to the database (use with --output)")
    parser.add_argument("--daemon",        action="store_true",    help="Run continuously, polling for new orders every 30 seconds")
    parser.add_argument("--interval",      type=int, default=30,   help="Poll interval in seconds when running as daemon (default: 30)")
    args = parser.parse_args()

    DPI       = args.dpi
    PX_PER_CM = DPI / 2.54
    NO_MARK   = args.no_mark

    batch_kwargs = dict(
        limit          = args.limit,
        order_id_filter= args.order,
        dry_run        = args.dry_run,
        sku_filter     = args.sku_filter,
        multizone      = args.multizone,
        reprocess      = args.reprocess,
        date_filter    = args.date,
        date_after     = args.date_after,
        upload_gdrive  = args.gdrive,
        upload_nas     = args.nas,
        upload_b2      = args.b2,
        no_bg_remove   = args.no_bg_remove,
        force_bg_remove= args.force_bg_remove,
        output_folder  = args.output,
        font_filter    = args.font_filter,
        hours          = args.hours,
        with_images    = args.with_images,
    )

    if args.daemon:
        import time, json as _json
        log(f"Daemon started — polling every {args.interval}s.  Press Ctrl+C to stop.")
        _all_failures     = {}         # order_id → failure record (persists across sweeps all day)
        _last_report_time = 0          # force first write immediately after first sweep
        _REPORT_INTERVAL  = 600        # 10 minutes

        while True:
            try:
                sweep_fails = run_batch(**batch_kwargs) or []
                _now_str = datetime.now().isoformat(timespec="seconds")
                for f in sweep_fails:
                    oid = f["order_id"]
                    if oid in _all_failures:
                        _all_failures[oid]["last_failed_at"] = _now_str
                        _all_failures[oid]["fail_count"] += 1
                        _all_failures[oid]["reason"] = f["reason"]  # keep latest reason
                    else:
                        _all_failures[oid] = {
                            "order_id":      oid,
                            "sku":           f["sku"],
                            "reason":        f["reason"],
                            "first_failed_at": _now_str,
                            "last_failed_at":  _now_str,
                            "fail_count":    1,
                        }
            except Exception as _e:
                log(f"Daemon run error: {_e}", "ERROR")

            # Write failure report every 10 minutes
            if time.time() - _last_report_time >= _REPORT_INTERVAL:
                _last_report_time = time.time()
                _date_str  = datetime.now().strftime("%Y-%m-%d")
                _report_dir = os.path.join(OUTPUT_FOLDER, _date_str)
                os.makedirs(_report_dir, exist_ok=True)
                _report_path = os.path.join(_report_dir, "failed_orders.json")
                _report = {
                    "generated_at":   datetime.now().isoformat(timespec="seconds"),
                    "date":           _date_str,
                    "total_failures": len(_all_failures),
                    "failed_orders":  sorted(_all_failures.values(), key=lambda x: x["first_failed_at"]),
                }
                with open(_report_path, "w", encoding="utf-8") as _fh:
                    _json.dump(_report, _fh, indent=2, ensure_ascii=False)
                if _all_failures:
                    log(f"Failure report updated: {_report_path}  ({len(_all_failures)} orders)")

            log(f"Sleeping {args.interval}s...")
            time.sleep(args.interval)
    else:
        run_batch(**batch_kwargs)
