# Varsany Print Automation — Project Knowledge Base

> Business: Varsany / Fullymerched — Amazon custom print-on-demand
> Last updated: 2026-10-15 (hoodies, birthday tops, cushions, pairs — see section 23; layout rework in section 22; earlier changes in section 21)

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
- Test export switches unchanged: `--reprocess --no-mark --order … --output …`.