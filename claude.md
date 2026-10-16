# Varsany Print Automation — Project Knowledge Base

> Business: Varsany / Fullymerched — Amazon custom print-on-demand
> Last updated: 2026-10-16 (incidents, dog garments, stockings gang sheets, document quirk / plugin v3.18c — sections 25-28; hoodies, birthday tops, vinyl names, cushions, pairs — section 23; layout rework in section 22; earlier changes in section 21)

---

## 1. Business Overview

**Company:** Varsany (sold on Amazon UK as CrystalsRus)
**Store:** amazon.co.uk/stores/Varsany
**Business model:** Customer customises garment on Amazon → order placed →
design team creates print file → printing department prints and ships

**Products sold:**
- Hoodies, sweatshirts, zip sweatshirts
- T-shirts (adult, kids, polo)
- Baby vests, slippers, swimsuits, legsuits
- Gymnastics towel ponchos, tote bags, track pants, leggings
- Bags (tote, backpack, makeup, shoe, string, knitting), golf towels/cases,
  memorial plaques, cushions, beanies, bucket hats, socks, seatbelt covers

**Print type:** DTF (Direct to Film) — all products personalised per order.
Some orders are rhinestone/embroidery instead of DTF — these are detected
and routed to manual processing (see section 10).

---

## 2. Current Manual Process (Before Automation)

1. Customer places order on Amazon with customisation (image, text, font, colour)
2. Order data saved to cloud database (Azure/AWS SQL Server)
3. Designer opens order page at `crssoft.co.uk/Order/CustomOrder`
4. Designer manually upscales image in Topaz Labs website
5. Designer opens Photoshop, creates canvas for correct product/zone
6. Designer types customer text, applies font and colour manually
7. Designer positions elements matching the Amazon preview image
8. Designer saves as [OrderID].psd to Synology Drive
9. File syncs from India to UK via Synology + SpeedFusion/Peplink router
10. Printing department opens PSD, flattens, and prints

**Time per order:** 10-15 minutes manually
**Target with automation:** 30-60 seconds automatically (actual current rate:
~5-7 minutes/order on this VM — see section 19 for why, and section 21 for
the fixes applied so far)

---

## 3. Database Structure

**Database name:** dbAmazonCustomOrders
**Type:** Microsoft SQL Server
**Hosted:** Azure / AWS cloud server
**Local backup:** .bak file restored to localhost\SQLEXPRESS for testing
**Connection (local):**
```
Server=localhost\SQLEXPRESS; Database=dbAmazonCustomOrders;
Trusted_Connection=yes; TrustServerCertificate=yes;
```
**Connection (live):**
```
Server=your-server.database.windows.net;
Database=dbAmazonCustomOrders;
UID=automation_user; PWD=your_password;
TrustServerCertificate=yes;
```

### tblCustomOrder — Key Columns
| Column | Type | Description |
|---|---|---|
| idCustomOrder | uniqueidentifier PK | Primary key |
| OrderID | nvarchar | Amazon order ID e.g. 205-6487629-5805162 |
| SKU | nvarchar | Encodes colour+size e.g. MenTee_BlkM |
| Quantity | int | Number of copies ordered |
| ItemType | nvarchar | Product type (DTF etc.) |
| IsShipped | bit | Whether shipped — **the automation queue now excludes any order where this is 1, regardless of processing status** (see section 21) |
| ConvertedShipByDate | datetime | Ship-by deadline — **the Output folder is dated by this, not by processing date** (see section 6) |
| Notes | nvarchar | Used for automation error messages |

### tblCustomOrderDetails — Key Columns
| Column | Type | Description |
|---|---|---|
| idCustomOrderDetails | uniqueidentifier PK | Primary key |
| idCustomOrder | uniqueidentifier FK | Links to tblCustomOrder |
| PrintLocation | nvarchar | e.g. "Front + Back + Sleeve" |
| IsFrontLocation | bit | Front zone active |
| IsBackLocation | bit | Back zone active |
| IsPocketLocation | bit | Pocket zone active |
| IsSleeveLocation | bit | Sleeve zone active |
| FrontImage | nvarchar(500) | URL/filename of customer uploaded image |
| FrontImageJSON | nvarchar | `{"Image1":"...","Image2":"..."}` — multi-image zones (e.g. pocket left+right) use this, not FrontImage |
| FrontText | nvarchar(1000) | Customer text (newline separated) |
| FrontTextJSON | nvarchar | `{"Text1":"...","Text2":"..."}` — used for semi-custom jersey name/number |
| FrontFonts | nvarchar(200) | JSON `{"NormalFont":"...","PremiumFont":"..."}` (pocket uses `{"Left Font":..,"Right Font":..}` instead) |
| FrontColours | nvarchar(200) | JSON `{"Colour1":"#hex"}` (pocket uses `{"Left Colour":..,"Right Colour":..}`) |
| FrontPreviewImage | nvarchar(500) | Amazon preview image URL — **confirmed populated** on real orders checked this session (resolves the "Questions Still Open" item from the old doc) |
| FrontTopazImage | nvarchar | JSON `{"Image1":"...-Topaz.jpg"}` — Topaz-upscaled image, produced by an **external** process, not by this script (see section 10) |
| IsTopazImageProcess | bit | Whether Topaz upscaling has completed for this row |
| CustomizationCategory | nvarchar | `Fullycustomized` (standard pipeline) or `Semicustomized` (jersey template pipeline) |
| Topaz_Processed | bit | **This is the actual "is this order done" flag the daemon checks** — not IsDesignComplete |
| ProcessBy | nvarchar | Name of whoever last touched the order on the order page (human designer name, e.g. "Ajay") **or** `AutomationScript`. A human name here does NOT mean the automation output exists — it just means someone opened the order page. |
| (same Text/Image/Fonts/Colours columns for Back, Pocket, Sleeve) | | |

### New Columns — still NOT added on this local DB (verified this session)
```sql
USE dbAmazonCustomOrders;
ALTER TABLE tblCustomOrderDetails ADD
    IsFrontBgRemove  bit NULL DEFAULT 0,
    IsBackBgRemove   bit NULL DEFAULT 0,
    IsPocketBgRemove bit NULL DEFAULT 0,
    IsSleeveBgRemove bit NULL DEFAULT 0,
    QCStatus         nvarchar(20)  NULL DEFAULT 'pending',
    QCNotes          nvarchar(500) NULL,
    IsComplexOrder   bit           NULL DEFAULT 0,
    OutputFilePath   nvarchar(500) NULL;
```
None of these 8 columns currently exist on the local DB, and **none of the code that would use them (QC scoring, complexity flags) is actually implemented yet either** — see sections 11 and 14. This is still a real gap, not just a missing column.

---

## 4. Print Specifications

**Resolution: 320 DPI (dots per inch) = ~125.98 pixels/centimetre.**
The formula is **cm × 125.98 = px** (i.e. `px = cm * (320 / 2.54)`), confirmed
directly from the running code (`PX_PER_CM = DPI / 2.54`, `DPI = 320`) and
matches every real template file's actual pixel dimensions on disk.

> ⚠️ Earlier versions of this doc said "320 pixels/centimetre = 812.8 DPI" and
> listed canvas sizes computed as `cm × 320`. That was wrong — it silently
> describes a canvas **2.54× larger** than what the code actually produces.
> The cm figures below are correct (they match designer/print intent); only
> the px column and the conversion formula were fixed.

**Colour mode:** CMYK
**ICC Profile:** U.S. Web Coated (SWOP) v2
**Background:** Always transparent
**Format:** PSD with layers (printing team flattens before printing)
**Large files:** Auto-save as PSB if file exceeds 2GB

### Canvas Sizes — verified directly from `PRODUCT_CANVAS` in `batch_processor.py`
| Product key | Zone | Width cm | Height cm | Width px | Height px |
|---|---|---|---|---|---|
| adulttshirt | front | 30 | 30 | 3780 | 3780 |
| adulttshirt | back | 30 | 30 | 3780 | 3780 |
| adulttshirt | pocket | 9 | 9 | 1134 | 1134 |
| kidstshirt | front | 23 | 30 | 2898 | 3780 |
| kidstshirt | back | 23 | 30 | 2898 | 3780 |
| kidstshirt | pocket | 9 | 9 | 1134 | 1134 |
| adulthoodie | front | 25 | 25 | 3150 | 3150 |
| adulthoodie | back | 25 | 25 | 3150 | 3150 |
| adulthoodie | pocket | 9 | 9 | 1134 | 1134 |
| adulthoodie | sleeve | 9 | 7 | 1134 | 882 |
| kidshoodie | front | 23 | 20 | 2898 | 2520 |
| kidshoodie | back | 23 | 20 | 2898 | 2520 |
| kidshoodie | pocket | 9 | 9 | 1134 | 1134 |
| totebag | front | 28 | 28 | 3528 | 3528 |
| totebag | back | 28 | 28 | 3528 | 3528 |
| backpack | front | 18 | 12 | 2268 | 1512 |
| makeupbag | front | 23 | 14 | 2898 | 1764 |
| shoebag | front | 23 | 14 | 2898 | 1764 |
| shoebag2 | front | 14 | 14 | 1764 | 1764 |
| stringbag | front | 22 | 24 | 2772 | 3024 |
| pebag | front | 22 | 24 | 2772 | 3024 |
| knittingbag | front | 25 | 21 | 3150 | 2646 |
| buckethat | front | 11 | 4 | 1386 | 504 |
| beanie | front | 9.5 | 4.5 | 1197 | 567 |
| beanietext | front | 12 | 4 | 1512 | 504 |
| socks | front | 6 | 6 | 756 | 756 |
| footballshorts | front | 6 | 9 | 756 | 1134 |
| seatbelt | front | 18 | 4 | 2268 | 504 |
| babyvest | front | 15 | 17 | 1890 | 2142 |
| sleepsuit | front | 13 | 18 | 1638 | 2268 |
| hodieblanket | front | 17 | 5 | 2142 | 630 |
| cushion | front | 30 | 30 | 3780 | 3780 |
| memorialplaque | front | 13 | 8 | 1638 | 1008 |
| golftowel | front | 17 | 17 | 2142 | 2142 |
| golfcase | front | 15 | 6 | 1890 | 756 |
| slipper | front | 6 | 6 | 756 | 756 |
| default | front | 30 | 30 | 3780 | 3780 |
| default | back | 30 | 30 | 3780 | 3780 |
| default | pocket | 9 | 9 | 1134 | 1134 |

Note vs. old doc: pocket zones are now **9×9cm square** on every product (was
documented as 9×7cm), and socks is **6×6cm** (was documented as 6×12cm).
These are the values the live code actually uses today.

Any SKU whose product can't be detected falls back to `default` — it is
**never** a hard failure (see section 8).

### PE bags: 22 × 24 cm for every design (owner request, 2026-10-14)
`detect_product()` maps **any SKU containing "PEBag"** (`AnyTxtPEBag_`, `PEBagCustom_`, `PEBagBundle_`,
`FBall01/02/03PEBag_`, `Swim02PEBag_` …) to the `pebag` product (22 × 24 cm = 2772 × 3024 px), checked
before the SKU prefix table so the generic `AnyTxt` catch-all (adult T-shirt, 30 × 30 cm) can no longer
claim them. Before this only `FBall01PEBag_` had the size; `AnyTxtPEBag_` (1,339 rows a year) came out
30 × 30 cm. Embroidery PE bags (`PEBag_…EMB`) get it too but are skipped by the pipeline anyway.

### Cushions / pillows: 30 × 30 cm (owner request, 2026-10-15)
Any SKU containing `pilow`, `pillow` or `cushion` maps to the `cushion` product (30 × 30 cm = 3780 × 3780 px,
text layout like any other text canvas). The check runs early in `detect_product()`, before the SKU prefix table,
so the generic `AnyTxt` catch-all can no longer claim them.

### Pairs: socks and slippers always print twice
`PAIR_PRODUCTS = ("socks", "slipper")` in `batch_processor.py`: for these products the number of copies is
**doubled** (one ordered pair = two items to print), in the single-item and the multi-item builders alike.
Quantity 1 → 2 copies, quantity 2 → 4 copies.

### Kids aged 1-4: 16 × 16 cm (owner request, 2026-10-14)
For kids aged 1-4 the **front and back** canvas is **16 × 16 cm = 2016 × 2016 px**
instead of the product's normal size. `get_product_canvas(sku, product)` in
`batch_processor.py` applies it (single-item and multi-item builders). It reads the age
from the **trailing digits of the SKU** (`..._Pnk12` → 1-2, `..._SBlue23` → 2-3,
`..._Blk34` → 3-4) and applies when the upper age is ≤ 4:

| Product | Sizes that change | Rows in the year to Oct 2026 |
|---|---|---|
| Kids T-shirt (`kidstshirt`) | 1-2, 3-4 | 639 + 1,041 |
| Sleepsuit (`sleepsuit`, `IWakeup_PJ…`) | 1-2, 2-3, 3-4 | 142 + 82 + 69 |
| Rainsuit (SKU prefix `Rainsuit`, product `default`) | 2-3, 3-4 | 56 + 52 |

- **Pocket and sleeve zones keep their own sizes** (pocket stays 9 × 9 cm).
- A size such as 4-5 has upper age 5, so it keeps the normal size.
- **Football shorts are deliberately excluded:** their size codes are single ages
  (6, 8, 10, 12), so `..._12` means age 12 — not 1-2 — and none exist for ages 1-4.
  Codes like `10` / `11` parse as "1-0" / "1-1" and are ignored (not a valid range).
- The older label parser still shows odd sizes for colour names it does not know
  (`SBlue23` → "E23"); cosmetic only, the canvas rule above does not use it.

---

## 5. Amazon Print Location Combos

| Combo | Zones | Price |
|---|---|---|
| Front Pocket ONLY | pocket | base |
| Front ONLY | front | +INR 245.67 |
| Back ONLY | back | +INR 245.67 |
| Front + Back | front, back | +INR 491.34 |
| Front Pocket + Back | pocket, back | +INR 491.34 |
| Front + Sleeve | front, sleeve | +INR 368.51 |
| Front + Back + Sleeve | front, back, sleeve | +INR 614.18 |
| Front Pocket Left + Right (+ Back) | pocket_left, pocket_right, back | — |

The last row is a real, common combo (two separate images side by side in
the pocket zone). It's built as two independent zone entries
(`pocket_left`/`pocket_right`), not one — see section 7.

---

## 6. File Storage & Output Folders

**Production sync target (as originally documented — not verified this
session):** Z:\Drive DTF Orders\1. Amazon DTF\, synced India ↔ UK Synology
via SpeedFusion/Peplink.

**Local/actual base path used by the script:** `C:\gimpTest\Output\`
(`VARSANY_OUTPUT` env var, defaults to `{VARSANY_BASE}\Output`).

### The date folder is the order's SHIP-BY date, not the processing date
```python
_ship = first_row.get("ConvertedShipByDate")
_date_str = str(_ship)[:10] if _ship else datetime.now().strftime("%Y-%m-%d")
out_dir = os.path.join(OUTPUT_FOLDER, _date_str)
```
This surprised us mid-session: an order processed today can land in
*yesterday's* (or several days ago's) output folder if that's when it was
supposed to ship. It's correct/intentional, not a bug — but it means
"today's output folder" is not the same thing as "orders processed today."

### Actual folder structure under each date
```
Output\<ship-by-date>\
├── Automated\                    Multi-zone orders (front+back, etc.)
│   ├── black\                    black / white sub-folders since 2026-10-16 (owner: "like the other
│   └── white\                    black and white folders"); other colours stay in Automated\ itself
├── DTF Front\                    Single-zone orders
│   ├── black\                    SKU colour code contains "blk"
│   └── white\                    SKU colour code contains "wht"
├── DTF Kids Hoodie\               Kids hoodie SKUs (kidshoo/gymhoodie/kidshood)
│   ├── black\
│   └── white\
└── semicustomized\               Jersey/semi-custom orders
```
Only "black"/"white" get a colour sub-folder; every other colour saves
directly into the category folder. Filename is `{OrderID}.psd`, or
`{OrderID}_{N}items.psd` for multi-item orders.

**`Automated\` has black / white sub-folders since 2026-10-16 15:13** (daemon restart; the first live
one was `2026-10-16\Automated\white\206-0598511-5565925_2items.psd`). The colour is the garment colour of the
order's FIRST row (`sku_colour_folder`); the 80 files built earlier were sorted the same way: 44 move
(31 black, 13 white), 36 other colours stay at the top level. Done for `2026-10-14` and `2026-10-15` at
15:20 (29 files); the 15 files of `2026-10-16` (live print day) are scheduled for 17:30 the same day.
**How (and why in this order):** `sync_to_b2.bat` is `rclone copy` in a loop (never deletes), so a plain local
move would upload every file again at the new path and leave the old copy in the bucket and on the NAS as a
duplicate. So the bucket object is renamed FIRST (`rclone moveto`, server-side, nothing is uploaded) and the local
file is moved right after (`os.replace`); the sync loop then finds nothing to do (one harmless
"failed to open source" retry was logged when it listed in the gap). Afterwards the bucket listing is
reconciled (old key gone, new key present). Manifest `Temp\automated_move_manifest_20261016.csv`, log
`Temp\automated_move_log_20261016.txt`; the script is `Temp\automated_move_run_20261016.py DATE...` (`--revert` undoes). Two multi-row orders have rows of different garment colours (first row decides):
`202-7585094-1850747_2items` stays at the top level, `202-2098507-7677936_2items` went to `black`.

**Every order with a pocket print goes to `Automated\` (owner, 2026-10-16 15:25) - also when the pocket is its only
zone** (before, only orders with more than one zone went there; "Front Pocket ONLY" / "Pocket Left + Right"
orders sat in `DTF Front`). `has_pocket_row()` decides by CONTENT: pocket text, pocket image or `PocketImageJSON`
(the `IsPocketLocation` flag is never set on this database, and "pocket" in `PrintLocation` is sometimes chosen with
no pocket design at all: 349 rows in 12 months). Colour sub-folders as above. Orders without a pocket print are
unchanged. In the last 12 months 10,033 rows had a pocket print: 5,541 were already multi-zone, 4,492 were pocket-only
(MenTee, polo tees, kids tees, fleece sweatshirts). Live after the next daemon restart. Rebuilds with `--reprocess`
follow the same rule. **Open question:** phone cases (`Anytext301_Dcase`, `Anytext602_Dcase`, ~10 a month) keep their
design in the pocket field, so the rule sends them to `Automated\` too - owner to confirm or exclude. Already built
pocket-order files outside `Automated\` (20, manifest `Temp\pocket_move_manifest_20261016.csv`, script
`Temp\pocket_move_run_20261016.py DATE...`, `--revert` undoes): the 8 of `2026-10-14` moved at 15:32. The 10 of
`2026-10-16` and the black / white sorting of that day's 15 Automated files were queued for 17:30 as a background job of
the Claude session, which the PC restart of ~15:55 cancelled: **run `Temp\automated_move_run_20261016.py 2026-10-16` and then
`Temp\pocket_move_run_20261016.py 2026-10-16` by hand after 17:30** (python from `%LOCALAPPDATA%\Programs\Python\Python314`);
the 2 phone-case files (hold=1) stay in `DTF Front` until the owner decides.

---

## 7. Design Rules (from designer)

### Background Removal Rule
Remove background when background colour matches garment colour — see the
actual hybrid decision logic in section 13 (this section previously said
"rembg" was the mechanism; it's more nuanced than that).

### Font Sizing
- Text **fills the zone width** (side margin 38 px = 0.3 cm): the largest size at which the
  widest line spans the width (plugin v3.10; it used to be 80% of the width)
- If the block is then taller than the room it has — the whole usable zone height for
  text-only orders, 30% of it when there is also an image — it is shrunk uniformly until it fits
- So short text ("Tom") becomes very large and long text smaller; see section 12 for the height rule

### Text Line Breaks
- Customer types line breaks on Amazon → stored as \n in database
- Parse: `text.split("\n")`

### Zone Labels
Small black text above each zone on every PSD:
- "front", "back", "sleeve", "pocket", "pocket left", "pocket right"
- Front zone sometimes uses SKU code e.g. "Ylw34" (Yellow size 3-4)
- Format: `{Zone} - {Colour} {Size}` e.g. "Pocket Left - Royal Blue XL"

### Quantity > 1
Stack copies vertically on one canvas, with a 1cm gap between copies (for
cutting). Socks always print 2 copies per ordered pair, regardless of the
DB quantity value (one pair = two socks).

### Pocket "Left + Right" combo (fixed this session — see section 21)
When a pocket zone has 2 images (`PocketImageJSON` has `Image1` and
`Image2`), it is built as **two separate zones** (`pocket_left`,
`pocket_right`), each with its own image, font, and colour — pulled from
the `Left`/`Right`-prefixed keys in `PocketFonts`/`PocketColours`. A single
dict key can't hold two images, which is why this needs two zone entries
instead of one.

### Photo Collage (multiple images, front zone)
- 1 image → full canvas, 2 → side by side, 4 → 2×2, 6 → 2×3 grid
- Text (if any) goes below collage

### Screenshot Border Removal
Some customers upload screenshots with black letterbox borders.
Detect: scan edges, if 85%+ pixels are dark → auto-crop borders.

### Amazon Position Data
Amazon does NOT share X/Y coordinates of where customer dragged elements.
Only the preview image (FrontPreviewImage) shows the correct position —
**confirmed populated** on real orders (resolves an old open question).

---

## 8. PSD Templates — MAJOR CHANGE this session

**Standard ("Fullycustomized") orders no longer use template files at all.**
The UXP plugin creates a blank CMYK/transparent canvas from scratch at the
exact product/zone size (`app.documents.add(...)`), sized from the same
`PRODUCT_CANVAS` dimensions in section 4. This was a deliberate fix (see
section 21) — it eliminates the entire class of "No template found for
X/default" failures that used to happen for any unmapped SKU or product.

**Only semi-custom (jersey) orders still open a real template file.** These
templates contain pre-built named layers (crest artwork, `Player`/`08` text
placeholders) that can't be generated from a blank canvas.

### Semi-custom slot config (`SEMI_CUSTOM_SLOT_CONFIG` in batch_processor.py)
| Template file | Slots | Slot height | Text layers |
|---|---|---|---|
| england Football Adult.psd | 7 | 3780px | `Player`, `08` (+ Photoshop auto-copy names for slots 2-7) |
| england Football Kids.psd | 7 | 3780px | same |
| scotland Football Adult.psd | 7 | 3780px | same |
| scotland Football Kids.psd | 7 | 3780px | same |

⚠️ **Only `england Football Adult.psd` currently exists in `C:\gimpTest\template\`.**
The other 3 configured templates are missing on this machine — any
Scotland or Kids jersey order will fail until those files are restored.

### Football / gymnastic kids hoodies (`HOODIE_DESIGNS`, plugin ≥ v3.13c, owner request 2026-10-15)
Templates live in `template\Football Hoodie\` (`FballN01…06KidsHood`, `NewFballN201/202KidsHood`) and
`template\Gymnastic\` (`Gymnastichoodie01…11KidsHood`) — one PSD per design, 2898 × 3780 px, one graphic plus
**one text layer holding a placeholder name**. The design is the SKU prefix before the first `_`. `Birthday Top`
is not automated yet.

- **What the customer's text is:** the *name printed on the sleeve* (`FrontTextJSON` `Text1`, else `FrontText`).
  The graphic is the front print; the name is placed under it in the same file. Source of truth for sizes: the
  owner's `Automation.xlsx` (canvas 23 × 30 cm, sleeve 18 × 5.50 cm; `FCustom` designs have a 9 × 25 cm sleeve).
- **Case:** football designs → ALL CAPS; gymnastic designs → first letter capital, rest lowercase
  (`format_hoodie_name`).
- **Size:** the name keeps the template's own size, but is shrunk (never enlarged) to fit the sleeve box
  **18 × 5.5 cm** (`HOODIE_NAME_FIT_W_PX/H_PX`), centred horizontally on the canvas, vertical centre kept.
- **Canvas:** **23 × 30 cm** (2898 × 3780 px) = the template's own size, **no crop** (`HOODIE_OUT_W_PX`). (An
  earlier version cropped to 20 cm from a chat answer; the Excel says 23 × 30 and overrides it.)
- **Everything stays editable (owner request):** all template layers go into ONE layer **group** per hoodie —
  the name is a **live text layer** (pattern layers still clipped to it) and the label a live text layer inside
  the same group. Nothing is merged or rasterised in the output file.
- **Fit + label:** the group must lie inside the canvas margins (38 px)
  and below the standard label strip — it is shrunk only if it does not already fit (never enlarged) and shifted
  into the free area (in practice the template art already fits; a few templates move down ≤ 72 px). Every
  hoodie gets the **standard top-left label** (`make_zone_label("front", sku)`, e.g. `Front - Red - 11-13`).
- **Quantity / several rows:** copies of the same design are stacked vertically with a 1 cm (126 px) gap, up to
  7 per PSD (`HOODIE_MAX_COPIES_PER_PSD`); more continue in `OrderID_b.psd`; a different design = its own file.
  A stacked file is 2898 × (3780 × n + 126 × (n-1)) px; each copy is one group `Hoodie_<n>_<name>` (design layers +
  live name + live `Label_…`). A single-hoodie file has one group `Hoodie_<name>`.
- **Mixed orders** (hoodie rows + other products, e.g. a kids tee): split in the main loop into a hoodie PSD
  (→ `semicustomized\`) and a normal PSD for the other rows. **Exception:** an order with any embroidery /
  rhinestone row is not split — it is skipped whole for manual handling, as before.
- Output goes to `Output\<ship-by-date>\semicustomized\`.
- **Plugin implementation notes (why the code looks the way it does):**
  - Replacing the text of a template layer makes Photoshop drop the layer's scale factor (size 203 → 52 base).
    `applyHoodieName` measures the drop and scales the layer back by the same factor, then fits it.
  - Re-opening the same template file does **not** give an independent second document — copies are made with
    `duplicate()` from a pristine spare, and documents are always selected **by id**.
  - `bp()` does not report batchPlay errors; use `bpc()` where a silent failure would produce a wrong PSD
    (a failed paste once made the next move/rename hit the wrong layer).
  - The clipboard is not used; a copy's group is sent to the main document with `duplicate … to document` and
    placed by the **name text layer's** bounds (`hoodieRef`) — a layer group's own bounds are NOT usable
    (Photoshop reports the whole canvas for them).
  - The visible extent of a design (needed for the fit) is measured on a throw-away merged copy of the
    document (`measureVisible`), because the group's bounds also count pixels hanging off the canvas.
  - The templates' pattern layers are clipped onto the name: they must stay in one group with it, and nothing may
    be inserted into that chain (an earlier version inserted a layer there and the clipped gradient leaked out as a
    rectangle). Merging was the earlier workaround; the group approach replaced it so the text stays editable.
  - If a stacked job fails, the copy / spare documents are closed again (`closeQuietly`).
- **Known gaps:** kids sweatshirt designs (`FballN0xKidsSwetShirt`) and `FCustom` hoodies (9 × 25 cm sleeve)
  have no templates; the Liverpool semi-custom kids tee (`Lvrpool201KidsTee`, print location "CHAMPIONS 20")
  has no text/image data and no template, so it fails with "No zones".

### Birthday Top kids tees (`BIRTHDAY_DESIGNS`, plugin ≥ v3.14c, owner request 2026-10-15)
Templates in `template\Birthday Top\`: `Birthday01KidsTee.psd` (boy, 2898 × 7285 px) and `Birthday2KidsTee.psd`
(girl, 2898 × 5651 px). Each file holds **two zones stacked in one tall file**: `front` (fixed artwork, one pixel
layer) and `back` (the customer's **name** + **age number**, font MagicalSnow, one colour per letter), each with a
big grey template label (`front` / `back`) that the automation replaces.

- **Which design:** SKU prefix — `Birthday01KidsTee` and `Birthday1KidsTee` = **boy** (template `Birthday01KidsTee`),
  `Birthday02KidsTee` and `Birthday2KidsTee` = **girl** (template `Birthday2KidsTee`). The `1` / `2` spellings are the
  ages 1-2 / 3-4 variants. (The DB names confirm it: 02 rows are girls' names.)
- **Customer data:** `FrontTextJSON` `{"Text1":"<name>","Text2":"<number>"}`, print location `Name:`, no image.
- **Name:** printed in CAPITALS. **Number:** leading zeros dropped (`06` → `6`).
- **Colours (owner's lists)** — name letters take the next colour in order, repeating from 1 after the 6th
  letter; **spaces do not use up a colour**; the number has its own single colour:
  - boy: `266a90 db322f d8792a 292557 266a90 db322f`, number `292557`
  - girl: `664c9f e18e29 df2d3a d92884 00a575 d94e86`, number `d74f85`
- **Size:** the name and number keep the template's own size (shrunk only if wider than the zone, with 38 px
  margins); centred horizontally, the template's vertical position kept.
- **Canvas per zone (owner's Excel):** ages 5+ → 23 × 30 cm: the **template's own layout and size are kept**
  (the file stays 2898 px wide). Ages **1-2 and 3-4 → 16 × 16 cm** per zone: each zone is scaled to fit its
  16 × 16 cm block below the label strip and the file is cut to 2016 px wide (`birthday_zone_px`).
- **Labels:** the template's `front` / `back` labels are removed and the standard small labels added:
  `Front - White - 5-6` / `Back - White - 5-6` (`make_zone_label`).
- **Editable:** one layer group per tee (`Birthday_<n>_<name>`) with live name / number / label text layers.
- **Copies / several rows:** stacked 1 cm apart in the same way as the hoodies; rows with a different design or
  zone size go to their own file (`OrderID_b.psd` ...); at most ~3 tall tees (28,000 px limit) per file.
- **Mixed orders:** `batch_processor` splits an order into one entry per kind (hoodie / birthday / other); an
  order with any embroidery / rhinestone row is still skipped whole.
- **Plugin notes:** a Photoshop style range REPLACES the whole style — every per-letter range must carry the
  font (`MagicalSnow`) and size again (`replaceTemplateText`), otherwise Photoshop falls back to Myriad Pro at a
  tiny size; **every character needs a range, spaces included**, or the space collapses. Replacing the text also
  drops the layer's scale factor, which is restored from the glyph size / height.
- Output goes to `Output\<ship-by-date>\semicustomized\`.

### Vinyl name templates (`template\vynil template.psd`, plugin ≥ v3.15b, owner request 2026-10-15)
Names, initials and numbers cut in vinyl. The template is one tall canvas (**3755 × 10079 px = 29.81 × 80 cm**) with
one **hidden layer group per product** (Socks, Wellies, Sensory Baord, Lunch Box, Luggage Strap, Book Bag, Hooded Robe,
Hoodie, Football Shorts, Football SET Socks, Gloves, Football T-shirt, Rainsuit). Each group holds live text layers —
`VinylFont` names with Bevel & Emboss / Stroke / Inner Glow, `JerseyM54` for the football sets (both fonts are caps-only).
The layer names carry instructions ("Max 20cm Width", "Max 15CM Height", "2x Name", "Left/Right Sock / Hand").
The hidden `Colours HEX Codes` group is **not used**.

- **Flow per order row** (`_build_psd_vinyl`): copy the product's whole group into a NEW canvas, edit **only the text**
  (name / initials = `Text1`, number = `Text2`; the template's own colours stay), keep the template's text size
  (shrunk only to the "Max .. cm" in the layer name, and never wider than the canvas), keep each layer's alignment,
  stack `Quantity` copies 1 cm apart, then **trim the canvas to the artwork** (effects included): no margin, no label.
  Saved as live text in `Output\<ship-by-date>\Vinyl\<OrderID>.psd` (`_b`, `_c` above ~27,000 px).
- **What is vinyl** (`row_vinyl_group`): the database flag **`tblCustomOrder.ItemType`** - `Vinyl`, `Vinyl|Red`, `Vinyl|Blue` ...
  (about 3,700 order items in the last 12 months, ~10 a day). `DTF`, `Rhinestone`, `Embroidery`, `UV Sticker` rows are
  **never** vinyl, whatever the SKU or the font label says (a hoodie with a "VinylFont" label but ItemType DTF is a normal
  DTF order). Only when `ItemType` is empty does the SKU decide (colour code ending in **V**). A vinyl row goes into the
  hourly files only if its product has a template group (below); other vinyl products stay in the normal sweep for now.
- **Routing to template groups** (`get_vinyl_group`): `SprtSocks` → Socks; `BlueWellies`, `Wellies_SuitPnk`, `Wellies_SuitBlu`
  → Wellies; `BookBagCustom_…V` → Book Bag; `BabyHoodedTowl_…V` → Hooded Robe; `Rainsuit_…V` → Rainsuit;
  `FootballGloves` → Gloves; `FootballShorts` → Football Shorts; `FootballShortSocks` → Football SET Socks;
  `LunchBag_…V` → Lunch Box; **`SingleBelt` (luggage straps for suitcases) → Luggage Strap**. **R = rhinestone** stays manual
  (`is_emb_rhine_row` skips it, as before). **No listing exists yet** for Sensory Board, Hoodie and Football T-shirt
  (the `FootBallT0x` SKUs are keychains).
- **Luggage straps = `SingleBelt`** (Amazon title "Personalised Luggage Straps For Suitcases"; owner: "these are vinyl"; plugin v3.17a, no plugin change): `SingleBelt_Blu / _Pnk / _Ylw / _Rainbow`
  are vinyl; `SingleBelt_Red / _Grn`, `NSingleBelt_Orng` and `SeatbeltCover0x_…` (the real seat-belt covers) are DTF (~700 a year, decided by `ItemType`).
  They are the biggest vinyl product (2,274 items in 12 months = 61% of all vinyl, ~6 a day), one line of text up to 14
  characters. They use the template group **Luggage Strap** (a Black Name and a White Name, "Max 25cm Width"); the customer's
  text colour decides which name is kept (`_vinyl_keep_layer`: `#070707` → Black Name, `#ffffff` → White Name; 73% of
  customers pick white). ⚠ Open question: the owner's size sheet gives the belt print area as 18 × 4 cm (`seatbelt` in
  `PRODUCT_CANVAS`) but the template limit is 25 cm - a name of roughly 7-9 letters or more comes out wider than 18 cm.
- **All vinyl products, last 12 months (ItemType Vinyl):** on a template group - SingleBelt 2,274, Rainsuit 280, FootballGloves 133,
  FootballShorts 93, Wellies 94 (BlueWellies + Wellies_Suit…), SprtSocks 55, BookBagCustom 48, BabyHoodedTowl 24. **No template group
  yet (stay in the normal sweep, ~20% of vinyl):** ChristSntHat1000 345 (seasonal, last Jan 2026), MenTee 206 (stopped Oct 2025),
  SantaHat 90 (seasonal), PEBagCustom 42, SplatterApron 15, HandTwlGolfHol 8, BabyCup / BoysDinoHoodTwl / BabyBib 4 each,
  KidsEngSet / GlitterName / BabyBearHat 1 each.
- **Text rules:** a numeric placeholder = number layer, anything else = name layer; an empty value removes that layer.
  "2x Name" (Wellies) repeats the name once per boot (limit 2 × 12 cm + 0.6 cm); vertical sock text is one letter per
  line (limit 15 cm high). **Hooded Robe:** the group has two ALTERNATIVE names side by side, so only the matching one is
  kept: pink robe → "Purple Name - Pink Robe", blue / white robe → "Dark Blue Name - Blue Robe" (what customers chose:
  PnkV `#8521de`, BluV `#1e63e4`, WhtV `#3244dd`). Lunch Box / Gloves / Football Shorts keep both their layers
  (black + white, left + right).
- **HOURLY FILES on 30 × 80 cm sheets, 24 × 7 (owner request, plugin ≥ v3.17a):** vinyl orders are no longer built one
  per file. The orders placed in one **UK clock hour** (`DateAdd`, which on this database is UK local time) become **one
  PSD named after that hour** — `Output\<date>\Vinyl\2026-10-15_1500-1600.psd` — plus a text file of the **same name**
  (`2026-10-15_1500-1600.txt`) that holds **only the order ids, one per line**. The date folder is the window's own
  date (not the ship-by date); `sync_to_b2.bat` copies the whole `Output` tree, so these files travel like any other. The file is exported when its hour ends
  (the 15:00-16:00 file at 16:00). Windows are whole clock hours, so none ever crosses the **4 pm UK order cutoff**
  (a 3:30-4:30 file would miss that day's printing). Runs **around the clock** (nights and after 4 pm included); an hour
  without vinyl orders produces no file. The daily sweep **holds** vinyl orders (they stay pending, one log line per
  order); the daemon runs `run_vinyl_batch()` **once per UK clock hour** — checked after every sweep **and between the
  orders of a sweep** (`_vinyl_tick`), so a long queue of 5-7-minute orders delays a vinyl file by at most the order in
  progress (state `Output\vinyl_batch_state.json`; a failed build is retried every 10 minutes; after downtime it catches
  up, one file per missed hour). Only *finished* hours are built — the current hour waits. On demand: `python batch_processor.py --vinyl-batch` (tests: `--reprocess --order <ids>
  --no-mark --output <folder> --vinyl-one-window` puts every selected order into one file named after the last
  finished hour; `--date YYYY-MM-DD` / `--dry-run` list the hourly groups). `VARSANY_VINYL_BATCH=0` in `.env` switches
  holding off (vinyl orders are built one by one again); `VARSANY_DB_TIME=utc` if the live database stores UTC.
  Explicit `--order` / `--reprocess` runs without `--vinyl-batch` build vinyl orders as single files, unless
  **`--hold-vinyl`** is added (then the test sweep holds them out like the daemon). **Only vinyl is hourly — every other
  product stays one PSD per order.** A whole-day test that mirrors production = `--vinyl-batch --reprocess --no-mark
  --date D --output X` (hourly vinyl files) + the normal sweep with `--reprocess --no-mark --hold-vinyl --date D --output X`.
  - **One plugin job per hour** (`vinyl_sheets`): phase A measures every order line in a work canvas, phase B packs the
    pieces (MaxRects best-short-side-fit, four orderings tried, fewest sheets then least height wins; **no rotation** —
    text must read upright; 1 cm between pieces, 40 px room for stroke / glow), phase C builds each sheet and **trims it
    to the artwork** (`Image > Trim` by transparency — no empty space, no outer margin). A piece = one order line with all
    its copies (split only if taller than a sheet).
  - **Size:** at most 30 cm wide (3780 px) × 80 cm (10079 px) *before* trimming. If an hour's orders need more than 80 cm
    the rest goes into a **second PSD** (`..._1500-1600_2.psd` + `..._2.txt`, then `_3` …); each is trimmed on its own.
  - **Names:** a file that already exists for that hour (a late order inserted after the export) is never overwritten —
    the new one gets a `_run<HHMM>` suffix. Orders with no name / number are warned about once and stay pending.
  - **Marking:** only after the files are saved are the rows marked `Topaz_Processed = 1` (never with `--no-mark`); a
    failed build leaves that hour's orders pending.
  - **Tested (2026-10-15, plugin v3.17a, `--no-mark`):** 16 real vinyl orders pooled into one hour → `…_1000-1100.psd`
    (13 orders, trimmed to 29.3 × 77.3 cm) + `…_1000-1100_2.psd` (2 orders, 24.6 × 5.3 cm), each with its same-name
    `.txt`; the 16th (`FootballShortSocks`, no text) correctly stayed pending. All 7 vinyl orders of 14 Oct (incl. the 2 luggage straps) → 7 files, one per
    hour that had a vinyl order. Ink touches all four edges of every file, no pieces overlap, real ink-to-ink gap between
    pieces ≥ 1.0 cm (up to ~1.7 cm where a piece has no glow / stroke). The window logic (clock-hour buckets, nothing
    built before its hour ends, overnight, UTC mode, once-per-hour state, failure retry) is covered by fake-clock tests.
- **Known gaps:** `FootballShortSocks` / `FootballShortsSet4` orders carry **no text** (the build fails with "No vinyl row
  had a name or number to print" and the order stays pending); `Lunch Box` rows currently use an Embroidery font label,
  so they are skipped as embroidery.
- **Plugin notes:** replacing the text of these layers keeps their scale (effective size unchanged), but a **centred
  name grows to BOTH sides** of its old centre and used to run past the left canvas edge and be clipped — the whole group is
  now moved inside the canvas (and the canvas widened) before the trim. `Image > Trim` (`trim` by transparency) is what
  removes the empty space.

The `template\` folder also still contains ~25 other `{product}.psd` /
`{product}_combined.psd` files (adulttshirt.psd, babyvest.psd, etc.) —
**these are now orphaned/unused** by the standard pipeline since the
canvas-creation change above. They can be archived or deleted; nothing
reads them anymore for non-jersey orders.

---

## 9. Fonts

- Database fonts field is JSON: `{"NormalFont":"...","PremiumFont":"..."}` for
  front/back, `{"Left Font":..,"Right Font":..,"PremiumFont":..}` for pocket.
- When `PremiumFont` is set and resolves to a known font: colour is ignored,
  font has built-in texture (camo, glitter, SVG colour fonts etc.)
- **Fonts are scanned from 4 locations** (`FONT_FOLDERS` in batch_processor.py),
  not just one folder:
  1. `{VARSANY_BASE}\Fonts` (project-local, travels with the repo folder)
  2. `VARSANY_FONTS_EXTRA` — comma-separated extra folders (`.env`-configured;
     on this machine: `C:\gimpTest\Fonts\Premium Fonts`, `C:\gimpTest\Fonts\Fonts`)
  3. `C:\Windows\Fonts` (OS-wide, hardcoded path)
  4. `%LOCALAPPDATA%\Microsoft\Windows\Fonts` (per-user, hardcoded path) —
     **this one is the migration risk**: it's outside the project folder,
     outside git, and tied to this specific Windows user profile. Many key
     premium/SVG-colour fonts (Camoblock, Wavemermaid, Soccer Army, etc.)
     live only here. A consolidated copy of all 4 sources (688 unique fonts,
     verified complete) exists at `C:\Users\VARHeist\Desktop\fonts` for
     migration to a new server.
- **Not every font name in the DB is a real font.** `FONT_ALIASES` deliberately
  maps some normalized names to `None` — these are category/decoration labels
  customers pick from a dropdown (`Rhinestone`, `Embroidery Font`, `DTF Text`,
  `Varsany`, etc.), not typefaces. They're intentionally never resolved to a
  file; they're used instead to detect and skip non-DTF orders (see section 10).
- If a real font name has no file and no alias → font lookup silently fails.
  14 such gaps were found this session (some genuine missing fonts like
  `Abril Fatface`/`Cookie`/`Great Vibes`, some likely mislabeled non-fonts
  like `Football Shorts`/`T-Shirt Font` that just need a `None` alias added).

---

## 10. Automation Pipeline

**Script:** `batch_processor.py`
**Language:** Python 3.14
**Runs as:** a Windows Service (`VarsanyDaemon`, via NSSM at `C:\nssm\nssm.exe`)
— **not** just a manually-run script. This means:
- It survives closing any terminal/RDP session, and restarts automatically
  if it crashes (`nssm set VarsanyDaemon AppParameters ...` to change its
  launch args — editing `start_daemon.bat` alone does **not** change what
  the service actually runs, since the service has its own stored config).
- Queue "resuming" after a restart is automatic and safe — the queue is a
  live DB query (`Topaz_Processed = 0/NULL AND IsShipped = 0/NULL`), not an
  in-memory position, so nothing gets skipped or duplicated on restart.
**Poll interval:** every 30 seconds

### Processing engine: Photoshop UXP plugin, not GIMP
Earlier versions of this doc described a GIMP-headless pipeline. **That was
never actually built** — there is zero GIMP code anywhere in this repo. The
real pipeline is: Python (`batch_processor.py`) → `ps_bridge.py` writes a
job JSON to `jobs\` → the UXP plugin (`uxp-plugin\index.html`, running
inside a real, open Photoshop) picks it up, builds the PSD via
`batchPlay`/DOM API calls, and writes a done/error marker back. See
section 12 for the actual mechanism.

### Processing Steps Per Order (as actually implemented)
1. `fetch_orders()` reads pending rows: `Topaz_Processed = 0/NULL AND IsShipped = 0/NULL`, ordered by `DateAdd ASC`
2. Detect embroidery/rhinestone SKUs or font labels → skip (never marked processed — see section 19 for why this matters for queue counts)
3. Detect semi-custom (jersey) vs standard via `CustomizationCategory` + a matching template
4. For each active zone: resolve image (Topaz-upscaled version preferred — **Topaz upscaling itself happens in a separate, external process/service, not in this script**; this script just waits for `IsTopazImageProcess=1` and reads the resulting URL), resolve font/colour, apply background removal if needed
5. Submit a job to the UXP plugin (dynamic blank canvas for standard orders, real template for semi-custom)
6. UXP builds layers, saves the PSD, reports done/error
7. On success: mark `Topaz_Processed = 1` on every detail row for that order
8. On failure: log and leave the order pending for the next poll cycle (no DB flag change)

**Not implemented (still just documented intent, no code exists for these):**
- Complexity flagging (section 11)
- QC thumbnail/confidence scoring (section 14)
- Slack/email alerts of any kind

---

## 11. Complexity Flags — ⚠️ NOT IMPLEMENTED

This table describes a **planned** system. No code anywhere in
`batch_processor.py` currently writes any of these flag values or an
`IsComplexOrder`/`QCStatus` column (those columns don't exist yet either —
section 3). Treat this as a design doc for future work, not current behavior.

| Code | Reason |
|---|---|
| screenshot_border | Black letterbox borders detected |
| low_resolution | Image < 500px even after upscaling |
| bg_removal_uncertain | Confidence < 80% |
| too_many_photos | More than 6 photos in collage |
| text_overflow | Text too long for canvas |
| unknown_product | Product not in PRODUCT_CANVAS (in practice: falls back to `default`, doesn't fail — see section 8) |
| processing_error | Any script error |
| premium_font | Font .ttf file not installed |

---

## 12. Layered PSD Generation (actual mechanism)

**Engine:** Adobe Photoshop, driven via the **UXP plugin API**
(`uxp-plugin/index.js`, loaded by `index.html`), not GIMP.

> ⚠️ **All plugin JS lives in `index.js`, loaded via `<script src="index.js">`
> — it must NOT be inlined back into `index.html`.** UXP has a documented,
> version-inconsistent restriction that refuses to execute JS embedded
> directly in a `<script>` block in the HTML ("Refusing to load inline
> script tag as executable code. Code generation is disabled in plugins.").
> This surfaced during migration to a new server running an older Photoshop
> build (v27.4.0) — the same inline-script setup happened to work on this
> server's v27.8.0, but was never actually safe. The manifest's
> `allowCodeGenerationFromStrings` permission does **not** cover this — that
> only governs inline HTML event-handler attributes (`onclick="..."`), a
> different restriction entirely.

- Python (`ps_bridge.py`) writes a job JSON to `C:\gimpTest\jobs\{OrderID}.json`
  containing zone data (image paths, text, font, colour, canvas size) and
  polls `done\`/`error\` for a matching marker file.
- The UXP plugin polls `jobs\` every few seconds, and processes each job
  inside `core.executeAsModal(...)`, using `action.batchPlay([...])`
  descriptors for every layer operation (make layer, place image, set text,
  align, rasterize, resize canvas, save).
- **Standard orders:** canvas is created fresh via `app.documents.add({width, height, resolution:320, mode:CMYK, fill:TRANSPARENT, profile:"U.S. Web Coated (SWOP) v2"})`. No template file opened.
- **Semi-custom orders:** the real template `.psd` is opened, and named text
  layers (`Player`, `08`, and their Photoshop auto-copy names for extra
  slots) are edited in place via `batchPlay`'s `set textLayer` descriptor.

**Layer naming actually produced** (verified by opening real output files
with `psd-tools` this session):
- `Label_{Zone} - {Colour} {Size}` — the black zone-identifier text
- `Preview_{zone}_{index}` — hidden Amazon preview image, kept for reference
- `{raw image filename}` (e.g. `65236222312682-1-pocket-Topaz`) — the customer image, placed and rasterized
- `Text_{zone}_{timestamp}` — rendered customer text
- Semi-custom: `Player`/`08` and their `... copy N` variants (text), plus
  supporting graphic/Smart Object layers per slot (not name-distinguished
  per slot — same name can repeat across slots)

**Layout rule (plugin v3.10b, `processZone` in `uxp-plugin/index.js`, 2026-10-14):**
- Every zone is a **block**: a 126 px label strip at the top (the label is *inside* the file) plus a
  design area. Blocks are stacked with a 126 px gap (quantity / multi-zone / multi-item orders), as before.
- **Width:** content fills the zone width (38 px side margins), text included.
- **Height:** each block is only as tall as its content (+ 38 px above and below) — **no empty space** —
  but never taller than the given zone height. A kids tee (2898 × 3780) whose content fills the
  height is exactly 2898 × 3780; a one-line name is a short block.
- **Image + text:** image on top, text underneath (designer rule); the text is capped at 30% of the
  usable height and the image is scaled into the rest. Text-only: the text may use all of it.
- Text is placed by its **visible letters** (Photoshop's "align to canvas" centres the text frame and
  leaves letters a few px off). Pocket zones are left-aligned.
- The plugin writes its layout numbers (`trace`) and its version into each job's marker in
  `done\{OrderID}.json` / `error\…` — read these to debug a build without screenshots.
- The customer's spacing is still stripped (`parse_texts` strips every line), as before.
- Why text goes under the image: the database holds **no position data** for text or image (only
  text, font, colour, file names and Amazon's 400 × 400 preview picture), so the position the
  customer chose cannot be reproduced.

**Known performance issue (not yet fixed, root-caused this session):** this
machine has no real GPU (`Microsoft Hyper-V Video` / `Microsoft Remote
Display Adapter` only), so large-canvas compositing is fully CPU-bound. The
UXP code also currently uses many single-descriptor `batchPlay` calls,
`synchronousExecution:true` (Adobe's own docs say to avoid this), and no
`suspendHistory`/`resumeHistory` wrapping — meaning Photoshop likely
snapshots the whole canvas for undo after nearly every micro-operation.
This is believed to be the biggest lever for speeding up processing; see the
session notes for the specific code changes identified but not yet applied.

**No PNG-per-layer fallback exists** — if UXP/Photoshop is unavailable, the
order simply fails and stays in the queue; there's no degraded-mode output.

---

## 13. Background Removal (actual hybrid logic — not just rembg)

**Decision logic (in order, first match wins):**
1. Manual flag / `--force-bg-remove` CLI flag — highest priority
2. Auto-detection: does the image's background colour match the garment's
   colour (from a SKU→RGB colour map)?
3. If removal is triggered:
   - **Dark garments (black, navy, etc.) → always colour-key removal.**
     rembg (AI background removal) is deliberately **not** used here — it
     destroys dark graphic designs. Colour-key is precise for flat/graphic
     images.
   - **Light garments → rembg first**, with a validation check: if rembg's
     output leaves fewer than 15% of pixels visible (a sign it mis-detected
     a flat graphic as "background"), it automatically falls back to
     colour-key removal instead.

**SKU colour map** (approximate garment RGB, used for the colour-match check):
```python
colour_map = {
    "blk": (20, 20, 20),
    "wht": (255, 255, 255),
    "nvy": (31, 40, 80),
    "red": (200, 30, 30),
    "ylw": (255, 220, 0),
    "pnk": (255, 150, 180),
    "gry": (150, 150, 150),
}
```

---

## 14. QC System — ⚠️ NOT IMPLEMENTED

Same status as section 11 — this is a design doc, not current behavior. No
thumbnail generation, confidence scoring, or alerting code exists yet.

1. *(planned)* Script generates PNG thumbnail of output
2. *(planned)* Compares to FrontPreviewImage (Amazon reference)
3. *(planned)* Confidence score 0-100%
4. *(planned)* ≥85% → auto-move to print folder
5. *(planned)* <85% → alert design team

---

## 15. Prototype Web App

**File:** C:\gimpTest\prototype_app.py
**Framework:** Flask — runs at http://localhost:5000
**Database:** Local SQLEXPRESS (dbAmazonCustomOrders)

**Run:**
```bash
python prototype_app.py
```

**Features:**
- Customisation form (product, zone, image upload, text, font, colour)
- Live automation progress log (right panel)
- Order history dashboard (bottom)

---

## 16. Folder Structure

```
C:\gimpTest\
├── batch_processor.py       Production automation script (runs as VarsanyDaemon service)
├── ps_bridge.py             Photoshop UXP job bridge (writes jobs\, reads done\/error\)
├── uxp-plugin\
│   ├── index.html           Plugin HTML shell — panel UI only, no logic
│   ├── index.js             All plugin logic — loaded via <script src="index.js">,
│   │                        NOT inline in the HTML (see section 12 for why)
│   └── manifest.json        Plugin manifest (host app, permissions, entrypoints)
├── daemon.log               Runtime log (grows large — has hit 250MB+; consider rotating)
├── .env                     Secrets and paths config (VARSANY_BASE, VARSANY_FONTS_EXTRA, DB connection, etc.)
├── Output\                  Finished PSD files, dated by ship-by date (see section 6)
│   └── <ship-by-date>\
│       ├── Automated\, DTF Front\, DTF Kids Hoodie\, semicustomized\
├── Fonts\                   Project-local font files (one of 4 scanned locations — section 9)
├── template\                PSD template files — now only meaningfully used for the 4 semi-custom jersey templates (section 8); everything else here is orphaned
├── Temp\                    Temp files during processing
│   └── OrderImages\         Customer images (UXP reads from here)
├── jobs\                    Job JSON files (batch_processor → UXP plugin) — a job sitting here survives a Python daemon restart; UXP works through it independently and asynchronously
├── done\                    Done markers (UXP plugin → batch_processor)
└── error\                   Error markers (UXP plugin → batch_processor)
```

**Not in this repo (`.gitignore` excludes them):** `Output\`, `Temp\`,
`done\`, `error\`, `Fonts\`, `template\`, `.env`, all `.psd`/`.psb`/`.log`
files. A `git clone` alone will **not** produce a working setup — see the
migration notes in section 19.

---

## 17. Developer Tasks Pending

1. Add 8 new columns to tblCustomOrderDetails (SQL in section 3) — confirmed still not done on local DB
2. Add "Remove Background: Yes/No" dropdown on order page for each zone
3. Create automation_user SQL login (SELECT + UPDATE only)
4. Open firewall port 1433 for automation PC IP
5. ~~Confirm whether FrontPreviewImage is populated on live database~~ — **RESOLVED: yes, confirmed populated on real orders checked this session**
6. Video call with Dhruv to reorganise Synology folder structure
7. *(new)* Restore the 3 missing semi-custom jersey templates (Kids/Scotland variants — section 8)
8. *(new)* Investigate the "UXP reported done but output file missing" failure mode — hit at least twice this session on standard multi-item orders, distinct from the (already-fixed) semi-custom timeout issue
9. *(new)* Implement the UXP performance fixes identified this session (suspendHistory, batched batchPlay calls) if queue growth continues to outpace throughput

---

## 18. Key People

| Person | Role |
|---|---|
| Yedhu | Project owner — Yedhu@fullymerched.com |
| Dhruv | IT / Synology / systems |
| Nimesh | Designer (Photoshop) |
| India team | Design + processing |
| UK team | Printing department |

---

## 19. Important Technical Notes

- **72 PPI display issue:** Photoshop shows 72ppi when opening output but
  pixel dimensions are correct — this is display metadata only
- **File naming:** `{OrderID}.psd`, or `{OrderID}_{N}items.psd` for multi-item orders
- **PSB:** Auto-triggered when estimated size >2GB
- **The daemon and Photoshop/UXP are decoupled, asynchronous processes.**
  Restarting the Python daemon does **not** cancel a job UXP is already
  working on — it keeps rendering in the background at its own pace. A
  large multi-item job can finish minutes or hours after Python gave up
  waiting on it and moved on. This caused confusion this session when an
  already-shipped order's PSD kept getting saved well after the order was
  supposedly excluded from the queue — the job had been submitted before
  the exclusion filter existed, and UXP was still grinding through it.
- **"Skipped" ≠ "processed."** Orders detected as embroidery/rhinestone are
  logged as skipped every single poll cycle **forever** — skipping never
  sets `Topaz_Processed`, so they permanently inflate the "pending" count
  unless someone processes them manually and updates the DB.
- **Wrong template:** was a daily problem for the printing team under the
  old manual process — no longer applicable now that standard orders don't
  use template files at all (section 8).
- **Server migration:** the font-scanning mechanism itself needs no code
  changes to move servers (it's `.env`-driven) — but 2 of its 4 source
  folders (`C:\Windows\Fonts`, the per-user `%LOCALAPPDATA%` folder) are
  machine-specific and won't come along with a `git clone`. A consolidated,
  verified-complete copy of all currently-used fonts exists at
  `C:\Users\VARHeist\Desktop\fonts` for this purpose.
- **Test exports without touching production:** `python batch_processor.py --reprocess --no-mark --order <id,id> --output C:\gimpTest\Temp\<folder>`
  builds the PSDs but `--no-mark` skips the only database write (`Topaz_Processed = 1`), so the
  orders stay in the queue and nothing reaches `Output\`, B2 or the NAS. Use `--date YYYY-MM-DD`
  instead of `--order` for a whole day.
- **Nobody works by hand in the Photoshop session the plugin uses.** During a test run, clicking in
  Photoshop stalled the plugin for ~14 minutes ("Failed to create new document"); the same run
  went through cleanly with Photoshop left alone. Reload the plugin after every Photoshop restart.
- **The database in `.env` is the remote LIVE server, not a local copy** (`DB_SERVER` host - the address is in `.env`, deliberately not in this public repo - shared with the old PC; it receives
  real orders all day). Section 3's `localhost\SQLEXPRESS` text describes the old test setup. Every test / export run therefore uses
  `--reprocess --no-mark`; a bare sweep or the daemon would mark real orders as processed for the old PC.
- **Claude desktop app pitfall - AppData writes are virtualised.** The app is a packaged (MSIX) app: files its shell (or a program it starts,
  e.g. Notepad) writes under `%APPDATA%` / `%LOCALAPPDATA%` go to `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\...` and every
  other process (elevated PowerShell, `setup_new_pc.ps1`, SYSTEM tasks) still sees an empty or old file. Seen with `rclone.conf` on
  2026-10-15. Files under `C:\gimpTest` are not affected. Write such files from your own PowerShell / Notepad, or from a process outside the
  app (WMI `Win32_Process.Create`).
- **Antigravity:** Uses Google account login only —
  Yedhu@fullymerched.com (Microsoft) won't work — use personal Gmail

---

## 20. Questions Still Open

1. ~~Exact 28 PSD template filenames~~ — largely moot now; standard orders don't use template files at all (section 8). Still open: recovering the 3 missing semi-custom jersey templates.
2. ~~Is FrontPreviewImage populated on live database?~~ — **RESOLVED, yes.**
3. What layer names are inside the semi-custom jersey templates beyond `Player`/`08` (the decorative/crest Smart Object layers aren't uniquely named per slot, which matters if we ever want to programmatically delete unused-slot layers)?
4. Video call with Dhruv re: Synology folder structure
5. Font files list — **RESOLVED for this machine**, see section 9; still need the equivalent audit on the designer's own PC if fonts differ there
6. Does printing team prefer all-in-one file or separate per zone for quantity orders? (Current behavior: all-in-one, stacked vertically)
7. *(new)* Why do some standard multi-item orders fail with "output file missing" despite UXP reporting done? (section 17, item 8)

---

## 21. Session Changelog (2026-07-16 verification pass)

Fixes actually implemented and verified working in `batch_processor.py` /
`uxp-plugin/index.html` this session, in case future work builds on
assumptions from before these existed:

- Standard orders build their canvas dynamically in UXP instead of opening
  a template file (section 8) — eliminated a whole class of "No template
  found" failures.
- Golf towel SKU prefix (`AnyTxtTwl_`) was falling through to the generic
  `AnyTxt→adulttshirt` catch-all and getting the wrong canvas size — fixed
  in `SKU_MAP`.
- Pocket "left + right" 2-image combo now correctly splits into two zones
  with per-side font/colour resolution (was silently dropping the second
  image and using the wrong colour for both sides).
- Semi-custom Photoshop timeout raised from `120s × item count` to
  `max(600, 150 × item count)` — large jersey templates were genuinely
  taking longer than the old timeout, causing false "FAILED" results and
  duplicate output files on retry.
- Orders already marked `IsShipped=1` are now excluded from the processing
  queue entirely — this turned out to be the majority of the backlog (614
  → 45 unique orders in one before/after check).
- The daemon runs as a Windows Service (`VarsanyDaemon`, NSSM) — this was
  discovered mid-session (editing `start_daemon.bat` alone doesn't change
  what the live service runs; its `AppParameters` must be updated directly).
- **Migration finding:** `install_fonts.py` was pointing at a dead `A:\font\`
  drive and a ~20-font hardcoded subset from an earlier server setup —
  rewritten to install every font from any `--source` folder, matching the
  full 688-font set actually in use (see section 9).
- **Migration finding:** all of `uxp-plugin/index.html`'s JS was inline in a
  `<script>` block. This broke on a new server's older Photoshop build
  (v27.4.0) with `"Refusing to load inline script tag as executable code"`
  — a documented, version-inconsistent UXP restriction, unrelated to the
  `allowCodeGenerationFromStrings` manifest permission. Fixed by extracting
  all logic into `uxp-plugin/index.js`, loaded via `<script src="index.js">`
  (section 12) — this is the version-safe pattern and should be used for
  any future plugin changes, never inline `<script>` content again.

---

## 22. Changelog (2026-10-14 — new PC + layout rework)

Done while moving production to a new PC (nothing here changes the database schema):

- **Layout:** plugin v3.10b — width filled edge to edge, height trimmed to the content and never above
  the given size, label inside the file, text under the image, letters centred by their visible
  edges (section 12). Verified on a full day of orders (62 built, 0 failed, every size right).
- **Kids aged 1-4 → 16 × 16 cm** front/back for kids tees, sleepsuits and rainsuits (section 4).
- **PE bags → 22 × 24 cm** for every design via the new `pebag` product (section 4).
- **`--no-mark` test switch** in `batch_processor.py` (section 19) — build PSDs without writing to the DB.
- **Setup script:** `migration/setup_new_pc.ps1` now installs `VarsanyDaemon` as *manual* start (a reboot
  can no longer start it before cutover); `-GoLive` switches it to auto-start and starts it.
- **Findings, not fixed:** (1) the database holds no text/image position data; (2) clicking in the
  plugin's Photoshop session can stall it for minutes; (3) the new PC shut down unexpectedly four
  times in two weeks (Kernel-Power 41, no memory pressure) and one 8 GB RAM module vanished from
  Windows' view — hardware to be checked before it becomes the production machine;
  (4) football kids hoodies (`FballN…KidsHood`) map to the adult hoodie canvas (25 × 25 cm) — open question;
  (5) sizes shown as "E23" in labels, and football-shorts "12" shown as "1-2", are parser quirks.

## 23. Changelog (2026-10-15 — hoodies, cushions, pairs)

- **Football / gymnastic hoodie templates automated** (section 8): name replaced in the template text layer,
  case rule per design family, name fitted to 18 × 5.5 cm, 23 × 30 cm canvas (per the owner's Excel) with the
  design fitted inside it and the standard label, copies stacked with a 1 cm gap, mixed orders split. **All text
  stays editable** (one layer group per hoodie, live name + live label). Plugin v3.13c.
- **Cushions / pillows → 30 × 30 cm**; **socks and slippers always print two copies** (section 4).
- **Pair rule, label and gap:** label scales with the zone width, the copy gap is the single 1 cm cutting gap
  (plugin v3.11).
- **Birthday Top kids tees automated** (section 8): boy / girl template by SKU, name in capitals with one colour per
  letter (owner's lists), age number in its own colour, standard labels, 23 × 30 cm per zone (16 × 16 cm for
  ages 1-4), all text live. Plugin v3.14c. Mixed-order splitting now works per kind (hoodie / birthday / other).
- **Vinyl name templates automated** (section 8): the product's group from `vynil template.psd` is copied to a new canvas,
  text and number edited (template colours and sizes kept), copies stacked, canvas trimmed to the artwork, saved in
  `Output\<date>\Vinyl\`. Plugin v3.15b. **Hourly files:** vinyl orders are held; every UK clock hour (24 × 7) the finished
  hour's orders are packed into one trimmed PSD (max 30 × 80 cm, a second PSD if more) named after the hour, with a
  same-name `.txt` of order ids only; `--vinyl-batch` runs it on demand. Plugin v3.17a. **Vinyl is decided by `ItemType`**;
  luggage straps (`SingleBelt`, 61% of vinyl) were added on the Luggage Strap group (the first export missed them - they had no routing).
- **New NAS + own Backblaze bucket (set up and verified 2026-10-15, not live):** the new PC uploads to its own bucket **`autopsd`**
  (per-PC setting: `setup_new_pc.ps1 -Bucket autopsd` writes the git-ignored `b2_bucket.txt`, which `sync_to_b2.bat` reads; without that
  file the production bucket `varsaniautomation` is used, so the old PC is unchanged). The new Synology NAS (static LAN IP on its
  "LAN 2" port, set in DSM - not recorded in this public repo) has the share **`AutoPsd`** (Volume 1, Btrfs, checksum on, recycle bin on; the printing team should get Read Only).
  Its Cloud Sync task (Backblaze B2, bucket `autopsd`, local path `/AutoPsd`, **Bidirectional** by the owner's choice, consistency check
  off, encryption off) mirrors the bucket. **End-to-end test passed:** a one-time `rclone copy` of `Output\` (280 files) into `autopsd`
  arrived on the NAS byte-exact (10,213,411,790 bytes; only an empty local folder is not copied). The scheduled upload `VarsanyB2Sync` exists
  but is DISABLED (the daemon is not live); `-GoLive` enables it. Keys: one Read and Write key per machine (PC / NAS), each limited to
  `autopsd`, typed only into DSM / the setup script. Before go-live: the printing side must pull from `autopsd` (the old PC / UK NAS
  use `varsaniautomation`), and the test export (2026-10-14 orders) should be removed from `Output\`, the bucket and the NAS.
- Test export switches unchanged: `--reprocess --no-mark --order … --output …`.

---

## 24. Go-live handover (prepared 2026-10-15) - new PC takes over from tomorrow's ship-by orders

- **WENT LIVE 2026-10-15 16:32 UK** (`setup_new_pc.ps1 -GoLive`, YES typed in capitals - lower-case `yes` is refused). The old PC's daemon was
  stopped + disabled and its `VarsanyB2Sync` disabled and ended first (note: disabling a task does not end a running instance - use
  `schtasks /end`; and keep the rollback commands out of copy-paste blocks - they restarted the old daemon once by mistake). First live results:
  order `204-4196487-1631568` built in 45 s (`Output\2026-10-16\Automated`), the 2-item order built, `Topaz_Processed` set in the DB, files
  uploaded to `autopsd` within seconds; rhinestone / embroidery orders (e.g. GymHoodie1013) are skipped for the manual process as before.

- **Scope = ship-by date, not placed date.** `ConvertedShipByDate` is set per order by Amazon (it is the placed date, rolling to the next
  working day after a cutoff that varies, roughly 15:00-17:00 UK; Friday-Sunday orders go to Monday), so it cannot be derived from the
  time of day. The new PC takes only orders with ship-by **on or after 2026-10-16**: `VARSANY_SHIPBY_FROM=2026-10-16` in `.env` (or
  `--ship-by-from`). It filters the LIVE queue only (daemon sweep and the hourly vinyl build), never `--reprocess` / `--order` / `--date`
  test runs, and keeps orders that have no ship-by date. The service's own arguments are unchanged
  (`--daemon --date-after 2026-07-18`).
- **Cutover order (the old PC has NO ship-by filter and takes orders oldest first, so it must be stopped before it reaches ship-by 16):**
  1. Old PC: wait until the pending queue has no ship-by <= 2026-10-15 orders left, let its Photoshop job finish.
  2. Old PC (admin PowerShell): `net stop VarsanyDaemon`, `sc.exe config VarsanyDaemon start= disabled`; when its `rclone_sync.log` says
     `There was nothing to transfer`, `schtasks /change /tn VarsanyB2Sync /disable`.
  3. New PC (admin PowerShell): `powershell -ExecutionPolicy Bypass -File C:\gimpTest\migration\setup_new_pc.ps1 -GoLive` (asks for YES).
     It starts `VarsanyDaemon` (auto-start) and enables + starts `VarsanyB2Sync` (uploads to bucket `autopsd`).
  4. Check: `Get-Content C:\gimpTest\daemon.log -Tail 400 | Select-String -NotMatch 'Fonts +:' | Select-Object -Last 15` shows the
     ship-by filter line and jobs; `Get-Content C:\gimpTest\rclone_sync.log -Tail 10`; the files appear on the NAS share `AutoPsd`.
- **Printing side:** this PC uploads to **`autopsd`**, not `varsaniautomation`. Whoever prints must read `AutoPsd` on the new NAS, or the UK
  NAS needs a Cloud Sync task pulling `autopsd` (read-only key). Otherwise tomorrow's files never reach them.
- **What changes for the team:** vinyl orders now come as hourly files (`Output\<date>\Vinyl\<date>_<HHMM>-<HHMM>.psd` + same-name `.txt` of
  order ids) instead of one PSD per order; everything else is one PSD per order as before.
- **Readiness checklist:** Photoshop open with the plugin v3.17a loaded and the Varsany Orders panel open (reload after any Photoshop
  restart), nobody clicking in Photoshop; sleep / hibernate / screen-off are set to never (checked); keep the Windows session signed in;
  avoid a Windows Update restart.
- **Rollback:** `net stop VarsanyDaemon`, `schtasks /change /tn VarsanyB2Sync /disable` on the new PC; old PC: `sc.exe config VarsanyDaemon start= auto`,
  `net start VarsanyDaemon`, `schtasks /change /tn VarsanyB2Sync /enable`. Setting `VARSANY_SHIPBY_FROM` to an earlier date widens the new PC's scope.

---

## 25. Incidents 2026-10-16 (overnight stall, duplicate files, wrong Topaz pictures)

- **Overnight stall (17:37 on 15 Oct -> 05:54 on 16 Oct):** a Photoshop error dialog (the owner remembers only "no room ..."; the text is not logged
  anywhere) stayed open and blocked the plugin. `pollJobs` in `index.js` handles ONE job at a time and `processing` stays true while `processJob` hangs
  (here: the template open of a football hoodie job, then "Cannot read properties of null (reading 'name')" when the dialog was closed). No PSD was built for
  12.5 h; ~107 orders waited and were all built by ~08:00 (about 45 s per order). No crash, reboot, GPU or remote-session event in the Windows logs; the
  first sign was "Failed to create new document" (17:37:05). Nothing in the code detects this yet. **Proposed, not built:** a dialog catcher (log the dialog
  text, click OK), a stall alarm (a job older than 3 min not taken; needs an alert channel), and removing the daemon's own job file when a job times out.
- **Leftover job files after a stall are harmful:** on a timeout `ps_bridge.wait_for_completion` leaves its job file in `jobs\`; the plugin builds it later
  (alphabetical order) while the daemon has moved on, so the order has a PSD but is not flagged done, and the next sweep builds it AGAIN as `<id>_2.psd`
  (the `while os.path.exists(out_path)` numbering in `run_batch`) = duplicate prints. Hourly vinyl jobs piled up the same way (33 `VinylBatch_*` files).
  Handled once by hand: 16 orders with a valid PSD + done marker were flagged `Topaz_Processed = 1` (owner-approved, each checked first).
- **Wrong Topaz pictures (customer-visible!):** the external Topaz service sometimes writes ANOTHER customer's picture under a `<id>-1-front-Topaz.jpg/png`
  name (the image server itself serves the wrong file). Audit of the 49 Topaz orders built on 16 Oct (67 pictures): 4 orders were wrong -
  `203-9377975-2431537` (3 of its 4 rows; the customer uploaded the SAME picture four times, so one Topaz file was right and three were strangers'),
  `205-8351197-4077967`, `206-2315208-7017911` (got the picture of order 203-9377975), `204-5706647-8077927`. All 5 PSDs were rebuilt from the right
  picture and replaced in `Output\2026-10-16\DTF Front\...` (the wrong versions are in `Temp\wrong_versions_20261016`). The Amazon preview is the same
  picture for every row of an order (it shows the last upload), so it cannot be used to tell rows apart.
  **Guard in `batch_processor.py`:** `topaz_matches_original()` compares small edge maps of the Topaz file and the customer's own upload
  (`TOPAZ_MATCH_MIN = 0.6`; on 67 real pairs right ones scored >= 0.98, wrong ones <= 0.13); a mismatch logs `TOPAZ MISMATCH` and the original upload is
  used instead. Used in both builders (single and multi-item). A pair that cannot be checked (original not downloadable after 3 tries) is trusted as before.
  **ACTIVE since 2026-10-16 08:50** (the owner restarted the service from an administrator PowerShell; the new daemon process started 08:50:15,
  after the last edit of `batch_processor.py` at 08:30). Log lines to look for: `TOPAZ MISMATCH` (a picture was rejected) and `Topaz check skipped` (could not be checked).
  **It only runs after `VarsanyDaemon` is restarted - that needs an administrator PowerShell (`Restart-Service VarsanyDaemon`) and should be done between
  two orders.** The Topaz service itself still has to be fixed by whoever runs it; the same audit can be repeated on older days.
- **Claude's own shell limits:** it cannot stop / restart the service (needs administrator rights), and moving files out of the live `jobs\` folder was
  refused by the permission guard.

---

## 26. Dog garments, beanies, second stall (2026-10-16)

- **Dog garments added** (owner's table "MAX PRINT SIZE by size", written L x W; L = length from neck to tail = the canvas HEIGHT, W = the canvas WIDTH, tall shape
  confirmed by the owner): XS 10 x 10, S 10 x 15, M 15 x 18, L / XL / XXL 18 x 23 cm (W x L). `DOG_PRINT_AREA_CM`, `dog_size()`, product `doggarment`
  (`detect_product`: SKU starts `Anytxt_Dog` AND ends in a size XS..XXL: DogTee, DogTshirt, DogHood, DogDenimJckt, DogVarsityJckt - one table for all), `get_product_canvas`
  returns the size's area. An unsized dog SKU keeps the old rules; `PerDOG01BabyVest` / `PAwsmSince01KidsTee` are baby / kids garments and unchanged. About 206 items
  since March 2026 (20-35 a month). Regression on all 4,170 SKUs in the database: exactly the 20 `Anytxt_Dog...` SKUs change (canvas and label), nothing else.
  Tested on 7 real orders (widths exact, lengths inside the table). Label: `dog_label_parts()` -> "Front - Dog Tee - Black - S" (the printing team must know which
  garment it is).
- **Plugin v3.17b:** the width-fit text size had a 20 pt floor (`processZone`, "width rule"); a long single line on a small canvas (XS = 10 cm) ran off both sides. Floor
  is now 6 pt. Needs a plugin RELOAD in Photoshop (UXP Developer Tool); the Python changes need a daemon restart (administrator PowerShell).
- **Beanies (owner's canvas sizes, 2026-10-16 ~14:50 - this was the "beanie hat" request):** only two beanie listings exist. **`AnytxtBeanie_Gry / Nvy / Blk` (text) = 12 x 4 cm = 1512 x 504 px**
  (product `beanietext`, new) and **`AnytxtPatchBeanie_Gry / Blk / Nvy` (photo and/or text) = 9.5 x 4.5 cm = 1197 x 567 px** (product `beanie`, as before). `detect_product`: SKU starts
  `anytxtbeanie` -> `beanietext`; any other SKU containing beani / bennie / beany -> `beanie`. Regression on all 4,170 SKUs in the database: exactly the three `AnytxtBeanie_*` SKUs change
  (1197 x 567 -> 1512 x 504), nothing else. Nine real orders rebuilt into `Temp\beanie_test_20261016` with `--no-mark`: text beanies 12.00 cm wide (a long 29-character one-line
  text fills the width at 1.96 cm height, "L9" is limited by the 4.0 cm height, two lines fit 4.0 cm, quantity 2 stacks to 12 x 5.2 cm), patch beanies 9.50 cm wide. Python only
  (no plugin reload); **live after the next `VarsanyDaemon` restart**.
- **Stockings (built later the same day - see section 27):** the owner's templates `Stockings DTF SILVER Template.psd` / `... RED Template.psd` (in Downloads, 2026-10-16) are 28.1 x 46.9 cm gang
  sheets: SILVER 16 names (2 x 8), RED 6 names; each name box 11.1 x 4.5 cm, `KaushanScript-Regular`, a full-size foil texture smart object per name, SILVER with drop shadow +
  stroke, RED with stroke. They are very heavy - never open them in the live Photoshop.
- **Second stall 11:43-12:46:** someone had the SILVER stockings template open in the live Photoshop with the Layer Style window showing: every job failed with
  "Poll error: host is in a modal state" and Photoshop reported Not Responding; ~12 jobs piled up and my dog test builds were caught in it. Same cause as the overnight
  stall (a dialog in the live Photoshop). **Rule: nobody opens or edits files in the Photoshop the pipeline uses.**

---

## 27. Stockings gang sheets (2026-10-16, plugin v3.18b)

- **What:** the PRINT choice of the Christmas stockings (`NEWStockings_*`, `DGreyStock*`, `RedStock*`) is no longer built as a 30 cm text PSD per order. The names of one UK clock hour are
  **ganged on 28.1 x 46.9 cm sheets** (the template canvas, 3543 x 5906 px), 2 columns x 8 rows = 16 names per sheet in reading order (left to right, top to bottom), one sheet per hour AND text
  colour: `Output\<date>\Stockings\2026-10-16_1100-1200_SILVER.psd` + a same-name `.txt` holding only the order ids in slot order (quantity 2 = the id twice). More than 16 names in an hour ->
  `..._SILVER_2.psd` + `_2.txt`. Every sheet is trimmed to the artwork (16 names = about 25.2 x 44.6 cm). The date folder is the window's own date (like vinyl), not the ship-by date.
- **Templates:** `template\Stockings\Stockings DTF SILVER Template.psd` / `... RED Template.psd` (10 MB each, the owner's simplified versions): ONE group `Group 1` = live text layer `Aiden`
  (KaushanScript-Regular; SILVER with drop shadow + stroke, RED with stroke) + a foil texture smart object clipped to it. **Never open them in the live Photoshop** (that caused the 11:43 stall).
  The plugin copies the group once per name into a NEW document (group renamed `Stocking_<n>_<name>`; everything stays live text and effects), replaces the text, keeps the template's size
  (shrunk only to fit the template's own name box - measured 1421 x 591 px incl. effects = the owner's 11.1 x 4.5 cm, so a name with tall / hanging letters such as "Toby" ends up a little
  smaller than a short round one), centres the name on its slot (pitch 1741 x 718 px) and trims the sheet. First real run: 16 names in 10 s.
- **Rules (owner, 2026-10-16):** several names ganged on a sheet, one sheet per hour like the vinyl; name = first letter capital + the rest small (applied **per word**: "S And F", "Fi Fi" -
  to be confirmed); only the PRINT choice: the rhinestone choice (font label / print location) and the old black `Stocking##_` SKUs stay manual, plain / "no personalisation" rows are no
  sheets. Name = `FrontTextJSON` Text1, else the first line of `FrontText`; no name = left pending (one warning). Template colour from the order's text colour (red -> RED; grey / silver /
  white -> SILVER), else from the SKU (`RedStock...` / `...Red` -> RED, otherwise SILVER). Volume: 5,760 personalised stockings in 30 months, peak Nov-Dec (1,384 in Dec 2025); only 7 stocking
  rows exist since 1 Oct 2026 (season just starting).
- **Code:** `batch_processor.py`: `STOCKING_*` constants, `is_stocking_sku`, `stocking_colour`, `format_stocking_name`, `is_stocking_batch_row`, `_stocking_build_hour`, `run_stocking_batch`.
  Rows are **held** in the daily sweep (like vinyl) and built by `_vinyl_tick()` once per finished UK hour (state file `Output\vinyl_batch_state.json`, shared with vinyl). The build refuses to
  run unless the newest done marker says plugin >= v3.18 (`_plugin_supports_stockings`) - the names then wait. Rows get `Topaz_Processed = 1` only after the files are saved. CLI:
  `--stocking-batch`, `--stocking-one-window` (tests: `--reprocess --no-mark --order ... --output <folder>`); `VARSANY_STOCKING_BATCH=0` in `.env` switches the hold off (stockings are then
  built one by one as before). Plugin: `runStockingSheets` / `applyStockingText` in `index.js`, one plugin job per hour and colour. **Live only after a daemon restart (admin PowerShell) and with
  the plugin reloaded (v3.18b).**
- **Bug found in the first plugin test (v3.18a), real cause:** the FIRST `app.documents.add()` of a stocking job returns the already open TEMPLATE document instead of the new blank one (both
  templates; also with a canvas 8 px larger - only the second call returns a proper blank document, and a second "Untitled" document stays open). v3.18a trusted the returned document: the names
  were built inside the template, the template's own `Aiden` group stayed in the sheet under the first name, and closing the "sheet" closed the template (log: "The document with an id of ... does
  not exist"). v3.18b retried with other canvas sizes; **v3.18c replaced that by `addBlankDoc()` for every product (section 28)**, and the sheet must hold exactly the copied name groups (else the job
  fails and the names stay pending). **Lesson: never trust the document `documents.add` returns.** The UXP log (`%APPDATA%\Adobe\Adobe Photoshop 2026\Logs\UXPLogs_*.log`, a new file every 5 MB - read
  the newest by NAME, its size column can show 0) shows the loaded plugin version ("Polling every 3s [v3.18c-docfix]") and every `[Varsany]` line.
- **Tested 2026-10-16 (plugin v3.18b, `--no-mark`, into `Temp\stock_test_20261016\Stockings`):** made-up names (RED 16, SILVER 3) and 37 names of real orders (RED 18 -> 16 + 2, SILVER 19 -> 16 + 3;
  four sheets in 33 s). Every sheet was checked automatically: 2 x 8 grid, names in slot order = order of the `.txt` ids = the orders' formatted names, every name inside the template box (+ 2 %),
  no stray template group. A sweep with `--hold-vinyl` holds the PRINT stocking orders and builds nothing. **Real names** (4,079 PRINT rows in 30 months): length median 5, p95 7, max 8 letters;
  99 % plain letters, 51 two- or three-word names ("S and F", "Ben xx", "A J"), 6 with digits, none with accents or punctuation; 4 rows have no name at all (left pending, one warning).
- **Open:** per-word capitals ("Ben Xx", "S And F" - the literal rule "first letter capital, rest small" would give "Ben xx", "S and f") and the 16-name grid still to be confirmed by the owner;
  the printing side must read the `Stockings\` folder; Santa hats are on hold; the "beanie hat" is undefined.

---

## 28. Document handling quirk and the 14:23 dialog stall (2026-10-16, plugin v3.18c)

- **Quirk:** `app.documents.add()` does not reliably return the NEW blank document. With a template (or a leftover document) open it has returned the OPEN TEMPLATE / the previous document (seen in
  the stockings tests at 13:53 and 14:09 and in the vinyl hourly test at 14:23; an older note in the multi-item jersey code says the same: "activeDocument pointed back to the template"). It is
  intermittent: vinyl hourly jobs worked at 12:49-12:52. Suspected trigger (not proven): Photoshop's main window minimised (its window rectangle was 160 x 28 when the stall happened).
  Standard orders without a template had never shown it, but a leftover open document makes them unsafe too (the document the plugin would close at the end could be the wrong one).
- **The 14:23-14:28 stall (caused by my own vinyl test, no live order affected):** the vinyl hourly job measured its pieces inside the returned "bench" = the template, closed it, and the next step
  `select document <template id>` made Photoshop raise the dialog "Varsany Order Processor: The command "Select" is not currently available." (class `PSDialogBox`). The plugin is blocked until
  someone clicks OK; Photoshop still reports "Responding", CPU is 0, the job file stays in `jobs\`. After OK the job failed cleanly (error marker `select document 32231: batchPlay error`).
  Diagnose such a stall: list the visible top-level windows of the Photoshop process (EnumWindows, class `PSDialogBox`), capture only that window with PrintWindow and read the text;
  the dialog needs a human click. A blank `Untitled-N` document stayed open afterwards.
- **Fix, plugin v3.18c (`index.js`):** `addBlankDoc(opts)` remembers the open document ids, calls `documents.add`, takes the document that was NOT open before (not the returned object), selects it
  and traces `documents.add returned document X instead of the new document Y` when they differ - used by the standard path, vinyl (single and hourly sheets) and the stocking sheets.
  `selectDocById` first checks that the document is open and throws a plain error instead of asking Photoshop to select a closed one (that is what raises the dialog). `closeStrayDocs()` closes
  blank `Untitled-N` documents at the start of every job. A finished order file that stays open (e.g. `206-5039807-3495537.psd` after a 14:17 job that closed the wrong document) is harmless now
  and can be closed by hand. **Always reload the plugin after editing `index.js` (UXP Developer Tool) and check the version line in the UXP log.**
- **Verified after the reload (14:33):** a 16-name stocking canary (28 s), yesterday's six vinyl hourly files built into `Temp\hourly_test_20261015\Vinyl` (each ~6 s, orders and sizes identical to
  the dry run: 04-05 two luggage-strap names (12 and 6 letters) 25.0 x 7.3 cm, 08-09 one 6-letter name x2 15.2 x 7.6, 09-10 one 4-letter name, 12-13 one 7-letter name x4 16.4 x 16.2, 17-18 and 20-21 one two-word name each), and six real standard orders
  (incl. a 3-item order, a dog XS and a PE bag) rebuilt into `Temp\std_regress_20261016` with the same pixel sizes as the live files (only the PE bag differs by 3 px in height because the placed
  image scales 0.2 % differently; the live file was built with the window minimised).
- **Open:** a 12-letter luggage-strap name comes out 25.0 cm wide (the template limit) while the owner's size sheet gives 18 x 4 cm for belts - already an open question (section 8).
  Proposed, not built: a dialog catcher / stall alarm (a job older than 3 min not taken needs an alert channel).

---

## 29. After a PC restart - what continues by itself and what does not (checked 2026-10-16)

- **TESTED with a real reboot on 2026-10-16: boot 15:47:24, `VarsanyDaemon` started by itself at 15:47:46 (22 s later, before Photoshop was launched), the user started Photoshop 15:48:13
  and reloaded the plugin 15:49:05 ("Polling every 3s [v3.18c-docfix]"), the first order was built 15:49:56 and uploaded by the sync task at 15:50:05; nothing was lost or duplicated.**
- **Continues by itself (configured; proven by the reboot above; earlier boot was 2026-10-14 09:52):** the `VarsanyDaemon` service
  (StartMode Auto, runs as the local Windows user that owns `C:\gimpTest`, so it starts at boot even before anyone signs in; NSSM `AppExit Default = Restart`); the Backblaze upload
  (`VarsanyB2Sync`, a scheduled task as SYSTEM that fires every minute); the NAS Cloud Sync. Nothing is lost: the queue is the database
  (`Topaz_Processed`), `jobs\` / `done\` / `error\` are files, the hourly vinyl + stocking state is `Output\vinyl_batch_state.json` (finished hours are
  caught up one file per hour after downtime).
- **Does NOT come back by itself:** Windows auto sign-in is OFF (`AutoAdminLogon = 0`), Photoshop is not in any startup list, and the UXP plugin is loaded by hand
  (UXP Developer Tool, "reload after every Photoshop restart"). Until someone has done that, orders just wait.
- **Dangerous in-between state = Photoshop open but the plugin NOT loaded.** `ps_worker_running()` only checks that `Photoshop.exe` exists, so the daemon
  submits jobs nobody picks up: every order waits 300 s, times out and leaves its job file in `jobs\`; once the plugin is loaded those jobs are built late,
  the orders are not flagged done and the next sweep builds them again as `_2` (duplicates, section 25). With Photoshop CLOSED the daemon logs
  "Photoshop not running", changes nothing and retries every 30 s (safe).
- **Runbook after a restart:** (1) sign in to Windows; (2) `Get-Service VarsanyDaemon` is Running and `daemon.log` shows "Daemon started"; (3) start Photoshop, let it finish
  loading, dismiss any start-up dialog, open nothing; (4) load / reload the plugin in the UXP Developer Tool and open the Varsany Orders panel; check the newest
  `UXPLogs_*.log` for "Polling every 3s [v...]"; (5) within a minute `daemon.log` shows `OK` lines; (6) if jobs piled up while the plugin was missing, look for
  `_2` files and for old files in `jobs\` before flagging anything.
- **Proposed, not built:** a plugin heartbeat file that `ps_worker_running()` requires (no heartbeat = treated like "Photoshop not running"), which closes the dangerous
  state and also stops job pile-ups during a dialog stall; and a hands-free restart (auto sign-in, Photoshop started at logon, plugin installed as a permanent package).
  Also unexplained: four unexpected shutdowns in the first two weeks (Kernel-Power 41, section 22) - hardware / power (UPS) still to be checked.