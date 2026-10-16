const {app,core,action,constants} = require("photoshop");
const uxp = require("uxp");
const fs  = uxp.storage.localFileSystem;

const PLUGIN_VERSION = "v3.18c-docfix";
const POLL_MS   = 3000;
const LABEL_PX  = 126;  // 1cm at 320dpi (halved from 2cm)
const GAP_PX    = 126;  // 1cm gap between zones
const CANVAS_W  = 3780; // default canvas width px

// Label size and label-strip height scale with the zone width, so small zones (socks, slippers, pockets) are not
// swamped by a label sized for a T-shirt: 12 pt and a 1 cm strip from 22 cm wide (2772 px) upwards,
// proportionally smaller below that (never under 5 pt / 0.5 cm).
function labelPt(zoneW){ return Math.max(5, Math.min(12, 12 * (zoneW||CANVAS_W) / 2772)); }
function labelStrip(zoneW){ return Math.max(64, Math.min(LABEL_PX, Math.round(LABEL_PX * (zoneW||CANVAS_W) / 2772))); }

let VARSANY_ROOT=null, DATA_FOLDER=null, JOBS_FOLDER=null, DONE_FOLDER=null, ERROR_FOLDER=null;
let processing = false;
let RESULT = null;   // structured result of the current job (the vinyl sheets: where every piece went), written into the done marker
let TRACE = [];   // layout numbers of the current job; written into the done/error marker so a run can be debugged without screenshots
function trace(m){ TRACE.push(m); if(TRACE.length > 80) TRACE.shift(); }

// ── Helpers ───────────────────────────────────────────────────────────────────
function ts(){const d=new Date(),p=n=>String(n).padStart(2,"0");return`${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;}
function log(msg,isErr){
  console.log("[Varsany] "+msg);
  document.getElementById("log").innerHTML=`<span class="${isErr?"err":""}">[${ts()}] ${msg}</span><br>`+document.getElementById("log").innerHTML;
  if(!isErr) document.getElementById("status").innerHTML='<span class="dot"></span>'+msg;
}
async function getOrCreate(parent,name){try{return await parent.getEntry(name);}catch(e){return await parent.createFolder(name);}}
async function readJSON(e){return JSON.parse(await e.read({format:uxp.storage.formats.utf8}));}
async function writeJSON(folder,filename,data){const f=await folder.createFile(filename,{overwrite:true});await f.write(JSON.stringify(data,null,2),{format:uxp.storage.formats.utf8});}

// ── Memory hygiene ────────────────────────────────────────────────────────────
// No purge/history-state calls existed anywhere in this file before — every
// batchPlay op (placement, transform, text, rasterize) leaves an undo history
// state that accumulates for the life of the Photoshop session. Call this
// right after each doc.close() (must run while still inside executeAsModal).
async function purgeCaches(){
  try{ await app.purge(constants.PurgeTarget.ALL_CACHES); }
  catch(e){ /* non-fatal — older PS builds / API shape may differ */ }
}
// Cap undo history so old states get evicted instead of piling up all session.
// Safe to call once at startup; doesn't need executeAsModal.
async function configurePerformance(){
  try{ app.preferences.numberOfHistoryStates = 5; log("History states capped at 5"); }
  catch(e){ log("Could not set history state limit: "+e.message, true); }
}

// ── Token / folder init ───────────────────────────────────────────────────────
async function loadRootToken(){
  // Try gimpTest first, then legacy Varsany
  for(const rootPath of ["file:///C:/gimpTest","file:///C:/Varsany"]){
    try{const v=await fs.getEntryWithUrl(rootPath);const tf=await v.getEntry("uxp_token.txt");return await tf.read({format:uxp.storage.formats.utf8});}
    catch(e){}
  }
  return null;
}
async function setupFolders(root){
  VARSANY_ROOT=root; DATA_FOLDER=root;
  JOBS_FOLDER  = await getOrCreate(root,"jobs");
  DONE_FOLDER  = await getOrCreate(root,"done");
  ERROR_FOLDER = await getOrCreate(root,"error");
}
async function initFolders(){
  try{
    const token=await loadRootToken();
    if(token){const r=await fs.getEntryForPersistentToken(token.trim());await setupFolders(r);log("Ready: "+r.nativePath);return;}
  }catch(e){log("Token failed: "+e.message,true);}
  try{
    const r=await fs.getEntryWithUrl("file:///C:/gimpTest");await setupFolders(r);log("Ready: C:\\gimpTest (URL)");return;
  }catch(e){}
  log("📁 Click 'Set Root Folder' to start",true);
}
async function pickRoot(){
  try{
    const folder=await fs.getFolder();if(!folder){log("No folder selected.",true);return;}
    await setupFolders(folder);
    const token=await fs.createPersistentToken(folder);
    const tf=await folder.createFile("uxp_token.txt",{overwrite:true});
    await tf.write(token,{format:uxp.storage.formats.utf8});
    log("Ready: "+folder.nativePath);
  }catch(e){log("Pick error: "+e.message,true);}
}

// ── Path resolver ─────────────────────────────────────────────────────────────
function stripRootPrefix(absPath){
  // Dynamically strip the VARSANY_ROOT native path prefix so we get a relative path
  const normalized = absPath.replace(/\\/g,"/");
  if(VARSANY_ROOT && VARSANY_ROOT.nativePath){
    const rootNorm = VARSANY_ROOT.nativePath.replace(/\\/g,"/").replace(/\/?$/,"/");
    if(normalized.toLowerCase().startsWith(rootNorm.toLowerCase()))
      return normalized.slice(rootNorm.length);
  }
  // Fallback: strip known roots
  return normalized
    .replace(/^[A-Za-z]:\/gimpTest\//,"")
    .replace(/^[A-Za-z]:\/Varsany\//,"");
}
async function getEntry(absPath){
  if(VARSANY_ROOT){
    const rel=stripRootPrefix(absPath);
    const parts=rel.split("/").filter(Boolean);
    let e=VARSANY_ROOT;
    for(const p of parts) e=await e.getEntry(p);
    return e;
  }
  return await fs.getEntryWithUrl("file:///"+absPath.replace(/\\/g,"/").replace(/^\/+/,""));
}
async function getOutputEntry(outputPath){
  try{
    // Build path segment by segment from VARSANY_ROOT, creating folders as needed
    const rel = stripRootPrefix(outputPath);
    const parts = rel.split("/").filter(Boolean);
    const filename = parts.pop();
    let dir = VARSANY_ROOT;
    for(const p of parts) dir = await getOrCreate(dir, p);
    try{return await dir.getEntry(filename);}catch(e){return await dir.createFile(filename,{overwrite:true});}
  }catch(e){
    const outFolder=await getOrCreate(DATA_FOLDER,"output");
    const filename=outputPath.replace(/\\/g,"/").split("/").pop();
    try{return await outFolder.getEntry(filename);}catch(e2){return await outFolder.createFile(filename,{overwrite:true});}
  }
}
async function getImageEntry(imgPath){
  const baseName=imgPath.replace(/\\/g,"/").split("/").pop();
  const tmp=await VARSANY_ROOT.getEntry("Temp").catch(()=>getOrCreate(VARSANY_ROOT,"Temp"));
  const imgs=await tmp.getEntry("OrderImages").catch(()=>getOrCreate(tmp,"OrderImages"));
  const entries=await imgs.getEntries();
  const match=entries.find(e=>e.name===baseName||e.name.endsWith("_"+baseName));
  if(!match) throw new Error("Image not found: "+baseName);
  return match;
}

// ── batchPlay helpers ─────────────────────────────────────────────────────────
async function bp(desc){return await action.batchPlay(desc,{synchronousExecution:true});}
// Same as bp(), but a failed step stops the job (batchPlay itself only returns an error descriptor and carries on,
// which let a failed paste go unnoticed and the following move/rename hit the wrong layer).
async function bpc(desc, tag){
  const r = await action.batchPlay(desc,{synchronousExecution:true});
  const bad = (r||[]).find(x => x && (x._obj === "error" || (typeof x.message === "string" && x.message.length)));
  if(bad){ trace("[bp-error] "+tag+": "+(bad.message || JSON.stringify(bad))); throw new Error(tag+": "+(bad.message || "batchPlay error")); }
  return r;
}

// Move layer to Y offset using translate
async function moveLayerToY(currentY, targetY){
  const delta = targetY - currentY;
  if(Math.abs(delta) < 1) return;
  await bp([{_obj:"move",_target:[{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}],
    to:{_obj:"offset",horizontal:{_unit:"pixelsUnit",_value:0},vertical:{_unit:"pixelsUnit",_value:delta}}}]);
}

// Move the active layer sideways by dx pixels
async function moveLayerByX(dx){
  if(Math.abs(dx) < 1) return;
  await bp([{_obj:"move",_target:[{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}],
    to:{_obj:"offset",horizontal:{_unit:"pixelsUnit",_value:dx},vertical:{_unit:"pixelsUnit",_value:0}}}]);
}

// Get layer bounds in pixels
async function getLayerBounds(){
  const r=await bp([{_obj:"get",_target:[{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}],_options:{dialogOptions:"dontDisplay"}}]);
  const b=r[0].bounds;
  return {top:b.top._value,left:b.left._value,right:b.right._value,bottom:b.bottom._value,
          w:Math.abs(b.right._value-b.left._value),h:Math.abs(b.bottom._value-b.top._value)};
}

// Get doc size in pixels
async function getDocSize(){
  const r=await bp([{_obj:"get",_target:[{_ref:"document",_enum:"ordinal",_value:"targetEnum"}],_options:{dialogOptions:"dontDisplay"}}]);
  const res=r[0].resolution._value;
  return {w:Math.round(r[0].width._value*res/72), h:Math.round(r[0].height._value*res/72), res};
}

// Build textStyleRange array: Noto Color Emoji for emoji chars, customer font for everything else.
// Produces a single editable type layer that shows emojis in their correct colours.
function buildTextStyleRanges(text, normPS, normFamily, normStyle, colourProp, sizePt) {
  // Matches emoji sequences: base + optional variation selector + optional ZWJ chains
  const EMOJI_RE  = /\p{Extended_Pictographic}[︎️]?(‍\p{Extended_Pictographic}[︎️]?)*/gu;
  const EMOJI_PS  = "NotoColorEmoji";
  const EMOJI_FAM = "Noto Color Emoji";
  const EMOJI_STY = "Regular";
  const normSty  = {fontPostScriptName:normPS, fontName:normFamily, fontStyleName:normStyle,
                    size:{_unit:"pointsUnit",_value:sizePt}, ...colourProp,
                    autoKern:{_enum:"autoKern",_value:"metricsKern"}, tracking:0};
  const emojiSty = {fontPostScriptName:EMOJI_PS, fontName:EMOJI_FAM, fontStyleName:EMOJI_STY,
                    size:{_unit:"pointsUnit",_value:sizePt}};
  const ranges = [];
  let lastEnd = 0, match;
  while ((match = EMOJI_RE.exec(text)) !== null) {
    if (match.index > lastEnd)
      ranges.push({_obj:"textStyleRange", from:lastEnd, to:match.index,
                   textStyle:{_obj:"textStyle", ...normSty}});
    ranges.push({_obj:"textStyleRange", from:match.index, to:match.index + match[0].length,
                 textStyle:{_obj:"textStyle", ...emojiSty}});
    lastEnd = match.index + match[0].length;
  }
  if (lastEnd < text.length)
    ranges.push({_obj:"textStyleRange", from:lastEnd, to:text.length,
                 textStyle:{_obj:"textStyle", ...normSty}});
  if (ranges.length === 0)
    return [{_obj:"textStyleRange", from:0, to:text.length,
             textStyle:{_obj:"textStyle", ...normSty}}];
  return ranges;
}

// Add black label text at given Y offset (top of label strip)
async function addLabel(labelText, yTop, canvasW, zoneW){
  const lblPt = labelPt(zoneW || canvasW), strip = labelStrip(zoneW || canvasW);
  await bp([{_obj:"make",_target:[{_ref:"textLayer"}],
    using:{_obj:"textLayer",textKey:labelText,
      textClickPoint:{_obj:"paint",horizontal:{_unit:"percentUnit",_value:1},vertical:{_unit:"percentUnit",_value:1}},
      textStyleRange:[{_obj:"textStyleRange",from:0,to:labelText.length,
        textStyle:{_obj:"textStyle",fontPostScriptName:"Arial-BoldMT",fontName:"Arial",fontStyleName:"Bold",
          size:{_unit:"pointsUnit",_value:lblPt},color:{_obj:"RGBColor",red:0,green:0,blue:0}}
      }]
    }
  }]);
  await bp([{_obj:"set",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"layer"}]},to:{_obj:"layer",name:"Label_"+labelText}}]);
  // Align left then move to correct Y
  await bp([{_obj:"align",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"layer"}]},using:{_enum:"alignDistributeSelector",_value:"ADSLefts"},alignToCanvas:true}]);
  const lb = await getLayerBounds();
  await moveLayerToY(lb.top, yTop + Math.round(strip/2) - Math.round((lb.bottom-lb.top)/2));
  await bp([{_obj:"move",_target:[{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}],
    to:{_obj:"offset",horizontal:{_unit:"pixelsUnit",_value:Math.round(GAP_PX * lblPt / 12)},vertical:{_unit:"pixelsUnit",_value:0}}}]);
}

// ── Process one zone: place image + text at given Y offset ────────────────────
// Returns the bottom Y of the zone content
async function processZone(doc, zone, data, imgEntry, yOffset, zoneW, zoneH, res, alignLeft=false, copyIdx=0){
  // FILL-WIDTH / TRIMMED-HEIGHT LAYOUT (v3.10)
  // Width: the content fills the zone width (side margin MARGIN_PX), text included.
  // Height: the block is only as tall as its content (+ EDGE_PX above and below) - no empty space - but never
  // taller than the given zone (zoneH, label strip included): the image and text are fitted INSIDE the
  // design area areaH = zoneH - LABEL_PX, text under the image (designer rule). A kids tee (2898x3780) whose
  // content fills the height is exactly 2898x3780; short text gives a short block. Returns the bottom of the block.
  const MARGIN_PX = 38;    // side margin around the image
  const TEXT_GAP  = 63;    // gap between image and text (0.5cm)
  const areaH     = Math.max(zoneH - labelStrip(zoneW), 200);
  const EDGE_PX   = 38;    // content stays this far from the top and bottom edge of the design area (0.3cm)
  const fitH      = Math.max(areaH - EDGE_PX*2, 100);   // height the image + text may actually use
  const areaTop   = yOffset + EDGE_PX;
  const hasText   = !!(data.text_lines && data.text_lines.length);
  // text may use at most this much height: 30% of the usable height when there is an image too, else all of it
  const maxTextH  = Math.round(fitH * (imgEntry ? 0.30 : 1.0));
  let textName = null, textH = 0, sizePt = 100;

  // ── 1. TEXT first: width rule (80% of zone width), then a height cap, so we know the room left for the image ─
  if(hasText){
    textName = "Text_"+zone+"_"+Date.now();
    const text = data.text_lines.join("\r");
    const hex  = data.colour_hex||"";
    const useSvgColour = !hex;   // premium SVG colour fonts (Football, Camo, ...): Photoshop renders their own texture
    const cr=hex?parseInt(hex.slice(1,3),16):255, cg=hex?parseInt(hex.slice(3,5),16):255, cb=hex?parseInt(hex.slice(5,7),16):255;
    const colourProp = useSvgColour ? {} : {color:{_obj:"RGBColor",red:cr,green:cg,blue:cb}};
    const fontPS     = data.font_ps_name  || "Arial-BoldMT";
    const fontFamily = data.font_family   || "Arial";
    const fontStyle  = data.font_style    || "Bold";
    const targetPx   = Math.max(zoneW - MARGIN_PX*2, 100);   // fill the zone width
    const styleFor   = (pt) => buildTextStyleRanges(text,fontPS,fontFamily,fontStyle,colourProp,pt);
    const applySize  = async (pt) => {
      await bp([{_obj:"set",null:{_ref:[{_ref:"layer",_name:textName}]},
        to:{_obj:"textLayer",textKey:text,textStyleRange:styleFor(pt)}}]);
    };

    await bp([{_obj:"make",_target:[{_ref:"textLayer"}],
      using:{_obj:"textLayer",textKey:text,
        textClickPoint:{_obj:"paint",horizontal:{_unit:"percentUnit",_value:50},vertical:{_unit:"percentUnit",_value:50}},
        textStyleRange:styleFor(100),
        paragraphStyleRange:[{_obj:"paragraphStyleRange",from:0,to:text.length,
          paragraphStyle:{_obj:"paragraphStyle",alignment:{_enum:"alignmentType",_value:"center"}}
        }]
      }
    }]);
    await bp([{_obj:"set",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"layer"}]},to:{_obj:"layer",name:textName}}]);

    // width rule: largest size whose widest line is 80% of the zone width (designer rule)
    for(let pass=0;pass<2;pass++){
      const lb=await getLayerBounds();
      sizePt=Math.min(Math.max(sizePt*(targetPx/(lb.w||1)),6),1200);   // v3.17b: floor 6pt (was 20pt: a long line on a small canvas, e.g. a 10 cm dog area, ran off both sides)
      await applySize(sizePt);
    }
    // height cap: many lines / blank lines can make the block taller than its share of the area; shrink to fit
    let tb = await getLayerBounds();
    if(tb.h > maxTextH){
      for(let pass=0; pass<3 && tb.h > maxTextH*1.01; pass++){
        sizePt=Math.max(sizePt*(maxTextH/(tb.h||1)),6);
        await applySize(sizePt);
        tb = await getLayerBounds();
      }
      log("Text height-capped ("+zone+"): "+Math.round(sizePt)+"pt, block "+Math.round(tb.h)+"px (limit "+maxTextH+"px)");
    }
    textH = tb.h;
    trace(zone+" area="+zoneW+"x"+areaH+" text size="+Math.round(sizePt)+"pt block="+Math.round(tb.w)+"x"+Math.round(tb.h)+" (limit h="+maxTextH+")");
    // horizontal placement now, while this text layer is still the active one (pocket = left, others = centre).
    // Photoshop's "align to canvas" centres the text FRAME (letter side-spacing included), which leaves the visible
    // letters a few px off - so place by the layer's own bounds (the letters) instead.
    const dsz = await getDocSize();
    const tb2 = await getLayerBounds();
    await moveLayerByX((alignLeft ? MARGIN_PX : Math.round((dsz.w - tb2.w)/2)) - tb2.left);
  }

  // ── 2. IMAGE: fit into what is left of the area above the text ──────────────────────────────
  let imgH = 0;
  if(imgEntry){
    const availH = Math.max(fitH - (hasText ? textH + TEXT_GAP : 0), 100);
    const imgToken = fs.createSessionToken(imgEntry);
    await bp([{_obj:"placeEvent",null:{_path:imgToken,_kind:"local"},freeTransformCenterState:{_enum:"quadCenterState",_value:"QCSAverage"}}]);

    const pb = await getLayerBounds();
    const lw = pb.w, lh = pb.h;

    if(lw>0 && lh>0){
      const scalePct = Math.min((zoneW - MARGIN_PX*2)/lw, availH/lh)*100;
      await bp([{_obj:"transform",null:{_ref:[{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}]},
        freeTransformCenterState:{_enum:"quadCenterState",_value:"QCSAverage"},
        width:{_unit:"percentUnit",_value:scalePct},height:{_unit:"percentUnit",_value:scalePct},
        constrainProportions:true,interfaceIconFrameDimmed:{_enum:"interpolationType",_value:"bicubicAutomatic"}}]);

      // Pocket = left-aligned, others = centre
      await bp([{_obj:"align",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"layer"}]},
        using:{_enum:"alignDistributeSelector",_value:alignLeft?"ADSLefts":"ADSCentersH"},alignToCanvas:true}]);

      // vertical: image sits at the top of the block; the block is trimmed to its content below
      const pb2 = await getLayerBounds();
      imgH = pb2.h;
      trace(zone+" image src="+Math.round(lw)+"x"+Math.round(lh)+" scale="+Math.round(scalePct)+"% -> "+Math.round(pb2.w)+"x"+Math.round(pb2.h)+" availH="+Math.round(availH));
      await moveLayerToY(pb2.top, areaTop);
    }

    await bp([{_obj:"rasterizeLayer",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"layer"}]}}]);
    // Name the image layer with zone key so multi-item orders don't overwrite each other
    const imgLayerName = (imgEntry.name||zone).replace(/\.[^.]+$/,"");
    await bp([{_obj:"set",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"layer"}]},to:{_obj:"layer",name:imgLayerName}}]);
    await doc.layers[0].move(doc.layers[doc.layers.length-1],"placeBefore");
    log("Placed image: "+zone);

    // Place preview image as invisible rasterized layer
    if(data.preview_image){
      try{
        const previewEntry = await getImageEntry(data.preview_image);
        const previewToken = fs.createSessionToken(previewEntry);
        await bp([{_obj:"placeEvent",null:{_path:previewToken,_kind:"local"},freeTransformCenterState:{_enum:"quadCenterState",_value:"QCSAverage"}}]);
        // Rasterize FIRST — before any transform to avoid Smart Object issues
        await bp([{_obj:"rasterizeLayer",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"layer"}]}}]);
        // Scale to fit the design area — use zoneW not docW
        const ppb = await getLayerBounds();
        if(ppb.w>0&&ppb.h>0){
          const pscale = Math.min((zoneW-MARGIN_PX*2)/ppb.w, areaH/ppb.h)*100;
          await bp([{_obj:"transform",null:{_ref:[{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}]},
            freeTransformCenterState:{_enum:"quadCenterState",_value:"QCSAverage"},
            width:{_unit:"percentUnit",_value:pscale},height:{_unit:"percentUnit",_value:pscale},
            constrainProportions:true,interfaceIconFrameDimmed:{_enum:"interpolationType",_value:"bicubicAutomatic"}}]);
          await bp([{_obj:"align",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"layer"}]},
            using:{_enum:"alignDistributeSelector",_value:"ADSCentersH"},alignToCanvas:true}]);
          const ppb2 = await getLayerBounds();
          await moveLayerToY(ppb2.top, yOffset);
        }
        // Name preview layer uniquely per zone AND copy so quantity>1 never collides
        await bp([{_obj:"set",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"layer"}]},to:{_obj:"layer",name:"Preview_"+zone+"_"+copyIdx}}]);
        // Hide the ACTIVE layer directly — no name search needed, works for all quantities
        await bp([{_obj:"hide",null:[{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}]}]);
        log("Preview placed (hidden): "+zone);
      }catch(pe){log("Preview error ("+zone+"): "+pe.message,true);}
    }
  }

  // ── 3. Move the text to its final spot: under the image, or centred in the area when there is no image ─────
  if(hasText){
    const underImage = imgEntry && imgH > 0;
    const textTop = underImage ? areaTop + imgH + TEXT_GAP : areaTop;
    await bp([{_obj:"select",_target:[{_ref:"layer",_name:textName}],makeVisible:false}]);
    const before = await getLayerBounds();
    await moveLayerToY(before.top, textTop);
    const after = await getLayerBounds();
    trace(zone+" text: "+Math.round(sizePt)+"pt h="+Math.round(textH)+" wanted top="+Math.round(textTop)+" (was "+Math.round(before.top)+", now "+Math.round(after.top)+")");
    log("Text: "+zone+" ("+Math.round(sizePt)+"pt) top:"+Math.round(after.top)+" h:"+Math.round(textH));
  }

  const contentH = (imgEntry && imgH > 0) ? imgH + (hasText ? TEXT_GAP + textH : 0) : textH;
  const usedH    = Math.min(areaH, Math.round(contentH + EDGE_PX*2));
  trace(zone+" block: content="+Math.round(contentH)+" used="+usedH+" of area "+areaH);
  return yOffset + usedH;
}

// ── Football / gymnastic hoodie templates: the single text layer holds the customer's NAME ───────────────
// Writes the name into the template's only text layer (found by type - its layer name is just the old placeholder),
// shrinks it to fit the sleeve box (never enlarges), centres it horizontally and keeps the placeholder's vertical
// centre, puts ALL template layers into one layer GROUP (the name stays a live, editable text layer), fits that group
// inside the canvas (below the label strip, inside the side margins - shrunk only if it does not already fit, never
// enlarged) and adds the standard top-left label as a live text layer inside the same group.
// cropW only matters when it differs from the template width (the owner's canvas is the template's own 23 x 30 cm).
// groupName names the group. The group is left selected, so the caller can read its bounds or send it to another document.
// Move the active layer by dx, dy pixels
async function moveLayerBy(dx, dy){
  if(Math.abs(dx) < 1 && Math.abs(dy) < 1) return;
  await bp([{_obj:"move",_target:[{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}],
    to:{_obj:"offset",horizontal:{_unit:"pixelsUnit",_value:dx},vertical:{_unit:"pixelsUnit",_value:dy}}}]);
}
// VISIBLE extent of everything in a document: a layer group's own bounds also count pixels hanging off the canvas and
// the clipped pattern layers, so the real extent is measured on a throw-away merged copy of the document.
async function measureVisible(doc){
  const tmp = await doc.duplicate("hoodie_measure");
  let m = null;
  try{
    await selectDocById(tmp);
    try{ await bpc([{_obj:"mergeVisible"}], "merge (measure)"); }
    catch(e){ await bpc([{_obj:"mergeLayersNew"}], "merge group (measure)"); }
    m = await getLayerBounds();
  } finally {
    try{ await tmp.close(constants.SaveOptions.DONOTSAVECHANGES); }catch(ce){ trace("measure copy close: "+ce.message); }
    await selectDocById(doc);
  }
  return m;
}
// A layer group's bounds are NOT usable for placing it (Photoshop reports the whole canvas), so a hoodie group is located
// by its NAME text layer, whose bounds are exact. Returns the group id and the top-left of the name text.
function hoodieRef(d, groupName){
  const g = d.layers.find(l => l.name === groupName);
  if(!g) throw new Error("hoodie group '"+groupName+"' not found");
  const t = g.layers.find(l => l.kind === "text" && !String(l.name).startsWith("Label_"));
  if(!t) throw new Error("hoodie name text layer not found in '"+groupName+"'");
  return {groupId: g.id, left: t.bounds.left, top: t.bounds.top};
}
async function closeQuietly(d){
  if(!d) return;
  try{ await d.close(constants.SaveOptions.DONOTSAVECHANGES); }catch(e){}
}
function openDocIds(){ const s = new Set(); for(let i = 0; i < app.documents.length; i++) s.add(app.documents[i].id); return s; }
function docIsOpen(d){ if(!d) return false; for(let i = 0; i < app.documents.length; i++){ if(app.documents[i].id === d.id) return true; } return false; }
async function selectDocById(d){
  // Selecting a CLOSED document makes Photoshop raise "The command Select is not currently available" - a dialog that blocks the whole
  // plugin until someone clicks OK (vinyl hourly test 2026-10-16 14:23). So check first: no dialog, a plain error instead.
  if(!docIsOpen(d)) throw new Error("select document "+(d ? d.id : "?")+": the document is not open (closed already)");
  await bpc([{_obj:"select",_target:[{_ref:"document",_id:d.id}],_options:{dialogOptions:"dontDisplay"}}], "select document "+d.id);
}
// Do NOT trust the document that app.documents.add() returns. With a template (or a leftover document) open it has handed back the OPEN
// TEMPLATE / the previous document instead of the new blank one (stockings test 13:53 and 14:09, vinyl hourly test 14:23, 2026-10-16): the
// template then got used as the work canvas and closed. So: remember the open documents, add, and take the document that was NOT open before.
async function addBlankDoc(opts){
  const before = openDocIds();
  const ret = await app.documents.add(opts);
  let fresh = null;
  for(let i = 0; i < app.documents.length; i++){ const d = app.documents[i]; if(!before.has(d.id)) fresh = d; }
  if(!fresh && ret && !before.has(ret.id)) fresh = ret;
  if(!fresh) throw new Error("documents.add did not create a new document");
  if(ret && ret.id !== fresh.id) trace("documents.add returned document "+ret.id+" instead of the new document "+fresh.id+" - used the new one");
  await selectDocById(fresh);
  return fresh;
}
// Blank "Untitled-N" documents are leftovers of a failed job (this Photoshop belongs to the plugin; nobody keeps unsaved work here): close them
// before the next job, so documents.add / activeDocument can never pick one of them up.
async function closeStrayDocs(){
  for(let i = app.documents.length - 1; i >= 0; i--){
    const d = app.documents[i];
    if(d && /^Untitled-\d+$/i.test(String(d.name || ""))){ trace("closing a leftover document '"+d.name+"' (id "+d.id+") from an earlier job"); await closeQuietly(d); }
  }
}
function dbg(tag){
  try{ const d = app.activeDocument; trace("[doc] "+tag+": open="+app.documents.length+" active='"+d.name+"' id="+d.id+" "+d.width+"x"+d.height); }
  catch(e){ trace("[doc] "+tag+": "+e.message); }
}
async function applyHoodieName(name, fitW, fitH, cropW, docRef, label, groupName){
  const doc = docRef || app.activeDocument;
  await selectDocById(doc);
  let tl = null;
  for(const l of doc.layers){ if(l.kind === "text"){ tl = l; break; } }
  if(!tl) throw new Error("Hoodie template has no text layer");
  const id = tl.id;
  await bp([{_obj:"select",_target:[{_ref:"layer",_id:id}],makeVisible:false}]);
  const before = await getLayerBounds();                      // the placeholder name: remember its vertical centre
  // Replace the text through the DOM (textItem.contents): it keeps the layer's character style AND its transform.
  // The batchPlay "set textKey" used first re-stored the layer as a small base size and lost the template's scale,
  // so every name came out about a third of the template size.
  let szBefore = null, szAfter = null, viaDom = true;
  try{ szBefore = tl.textItem.characterStyle.size; }catch(e){}
  try{ tl.textItem.contents = name; }
  catch(e){
    viaDom = false; trace("DOM contents failed ("+e.message+") - using batchPlay");
    await bp([{_obj:"set",_target:[{_ref:"layer",_id:id}],to:{_obj:"textLayer",textKey:name}}]);
  }
  // Replacing the text drops the template's scale factor (Photoshop then reports the small base size: 203 -> 52).
  // Measure that drop and scale the layer back up by exactly the same factor, so the new name keeps the template size.
  let restoreScale = 1;
  try{
    szAfter = tl.textItem.characterStyle.size;
    if(szBefore && szAfter && szAfter < szBefore * 0.9) restoreScale = szBefore / szAfter;
  }catch(e){}
  trace("text set via "+(viaDom?"DOM":"batchPlay")+"; font size before "+szBefore+", after "+szAfter+", restore x"+restoreScale.toFixed(2));
  await bp([{_obj:"select",_target:[{_ref:"layer",_id:id}],makeVisible:false}]);
  let b = await getLayerBounds();
  if(restoreScale > 1.1){
    await bp([{_obj:"transform",null:{_ref:[{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}]},
      freeTransformCenterState:{_enum:"quadCenterState",_value:"QCSAverage"},
      width:{_unit:"percentUnit",_value:restoreScale*100},height:{_unit:"percentUnit",_value:restoreScale*100},
      constrainProportions:true,interfaceIconFrameDimmed:{_enum:"interpolationType",_value:"bicubicAutomatic"}}]);
    const rb = await getLayerBounds();
    trace("size restored: "+Math.round(b.w)+"x"+Math.round(b.h)+" -> "+Math.round(rb.w)+"x"+Math.round(rb.h));
    b = rb;
  }
  const scale = Math.min(1, fitW/(b.w||1), fitH/(b.h||1));
  if(scale < 0.995){
    await bp([{_obj:"transform",null:{_ref:[{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}]},
      freeTransformCenterState:{_enum:"quadCenterState",_value:"QCSAverage"},
      width:{_unit:"percentUnit",_value:scale*100},height:{_unit:"percentUnit",_value:scale*100},
      constrainProportions:true,interfaceIconFrameDimmed:{_enum:"interpolationType",_value:"bicubicAutomatic"}}]);
    b = await getLayerBounds();
  }
  const {w:docW, h:docH} = await getDocSize();
  await moveLayerByX(Math.round(docW/2 - (b.left + b.right)/2));
  await moveLayerToY(b.top, Math.round((before.top + before.bottom)/2 - b.h/2));
  trace("hoodie name '"+name+"': "+Math.round(b.w)+"x"+Math.round(b.h)+"px, scale "+Math.round(scale*100)+"%, placeholder was "+Math.round(before.w)+"x"+Math.round(before.h));
  if(cropW && cropW !== docW) await doc.resizeCanvas(cropW, docH, constants.AnchorPosition.MIDDLECENTER);
  const {w:cW, h:cH} = await getDocSize();

  // ONE GROUP for the whole design: every template layer (graphic, clipped pattern layers, the name TEXT layer) goes
  // into a layer group, exactly like Ctrl+G, so the name stays a live editable text layer and the clip chains stay
  // intact. The group is then fitted and moved as a unit.
  await bpc([{_obj:"selectAllLayers",_target:[{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}]}], "select all layers");
  await bpc([{_obj:"make",_target:[{_ref:"layerSection"}],from:{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}}], "group layers");
  await bp([{_obj:"set",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"layer"}]},to:{_obj:"layer",name:groupName || ("Hoodie_"+name)}}]);
  const gr = await bpc([{_obj:"get",_target:[{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}]}], "group id");
  const gid = gr[0].layerID;
  const selectGroup = async () => { await bp([{_obj:"select",_target:[{_ref:"layer",_id:gid}],makeVisible:false}]); };

  // FIT TO THE CANVAS: the design must lie inside the side margins and below the label strip. Shrink it only when it
  // does not already fit (never enlarged), then shift it into the free area.
  let d = await measureVisible(doc);
  await selectGroup();
  const MARGIN_PX = 38, EDGE_PX = 38, strip = labelStrip(cW);
  const aL = MARGIN_PX, aR = cW - MARGIN_PX, aT = (label ? strip : 0) + EDGE_PX, aB = cH - EDGE_PX;
  const fitScale = Math.min(1, (aR - aL)/(d.w||1), (aB - aT)/(d.h||1));
  if(fitScale < 0.995){
    await bp([{_obj:"transform",null:{_ref:[{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}]},
      freeTransformCenterState:{_enum:"quadCenterState",_value:"QCSAverage"},
      width:{_unit:"percentUnit",_value:fitScale*100},height:{_unit:"percentUnit",_value:fitScale*100},
      constrainProportions:true,interfaceIconFrameDimmed:{_enum:"interpolationType",_value:"bicubicAutomatic"}}]);
    d = await measureVisible(doc);
    await selectGroup();
  }
  let dx = 0, dy = 0;
  if(d.left < aL) dx = aL - d.left; else if(d.right > aR) dx = aR - d.right;
  if(d.top < aT) dy = aT - d.top; else if(d.bottom > aB) dy = aB - d.bottom;
  await moveLayerBy(Math.round(dx), Math.round(dy));
  trace("hoodie design fit: "+Math.round(d.w)+"x"+Math.round(d.h)+" at ("+Math.round(d.left)+","+Math.round(d.top)+") canvas "+cW+"x"+cH+", scale "+Math.round(fitScale*100)+"%, shift "+Math.round(dx)+","+Math.round(dy));

  // The label goes INSIDE the group (a new layer is created above the selected layer, in the same container), so each
  // stacked copy carries its own label and moves as one unit.
  if(label){
    const grp = doc.layers.find(l => l.id === gid);
    const kid = grp && grp.layers && grp.layers.length ? grp.layers[0] : null;
    if(kid) await bp([{_obj:"select",_target:[{_ref:"layer",_id:kid.id}],makeVisible:false}]);
    await addLabel(label, 0, cW, cW);
  }
  await selectGroup();
  return {w:b.w, h:b.h, scale:scale};
}

// ── Birthday Top kids tees: the template holds a "front" zone (fixed artwork) and a "back" zone (name + age number) ──
// The name gets ONE COLOUR PER LETTER (item.name_colours, null for spaces), the number one colour. Text stays live.
function hexToRgbObj(hex){
  const h = String(hex).replace("#","");
  return {_obj:"RGBColor", red:parseInt(h.slice(0,2),16), grain:parseInt(h.slice(2,4),16), blue:parseInt(h.slice(4,6),16)};
}
// Replace the text of a template text layer, colour it, and give it back the template's size (replacing the text makes
// Photoshop drop the layer's scale factor, exactly as seen on the hoodies). colours: one hex (or null) per character, or
// a single hex string for the whole text.
async function replaceTemplateText(tl, text, colours){
  const id = tl.id;
  const sel = async () => { await bp([{_obj:"select",_target:[{_ref:"layer",_id:id}],makeVisible:false}]); };
  await sel();
  const before = await getLayerBounds();
  let szBefore = null, fontPS = null;
  try{ szBefore = tl.textItem.characterStyle.size; }catch(e){}
  try{ fontPS = tl.textItem.characterStyle.font; }catch(e){}
  tl.textItem.contents = text;
  // A style range REPLACES the whole style: with only a colour in it, Photoshop falls back to Myriad Pro at a default
  // size (seen in the first test). So every range carries the template's font and size again, plus the colour.
  const baseStyle = {fontPostScriptName: fontPS || "MagicalSnow", fontName:"Magical Snow", fontStyleName:"Regular"};
  if(szBefore) baseStyle.size = {_unit:"pointsUnit", _value:szBefore};
  const mk = (from, to, hex) => ({_obj:"textStyleRange", from:from, to:to, textStyle:{_obj:"textStyle", ...baseStyle, color:hexToRgbObj(hex)}});
  const ranges = [];
  if(typeof colours === "string"){
    if(text.length) ranges.push(mk(0, text.length, colours));
  } else if(Array.isArray(colours)){
    // Every character gets a range - also the spaces (colour null): a character without its own range falls back to a
    // default style and a tiny size, which made the space between two words vanish. A space takes the previous colour.
    let lastHex = colours.find(c => c) || null;
    for(let i = 0; i < text.length; i++){
      if(colours[i]){ lastHex = colours[i]; ranges.push(mk(i, i+1, colours[i])); }
      else if(lastHex) ranges.push(mk(i, i+1, lastHex));
    }
  }
  if(ranges.length){
    await sel();
    await bpc([{_obj:"set",_target:[{_ref:"textLayer",_enum:"ordinal",_value:"targetEnum"}],to:{_obj:"textLayer",textStyleRange:ranges}}], "colour letters");
  }
  let szAfter = null; try{ szAfter = tl.textItem.characterStyle.size; }catch(e){}
  await sel();
  let b = await getLayerBounds();
  let restore = 1;
  if(szBefore && szAfter && szAfter < szBefore*0.9) restore = szBefore/szAfter;
  else if(before.h > 0 && b.h > 0 && b.h < before.h*0.7) restore = before.h/b.h;     // fallback: compare glyph heights
  if(restore > 1.1){
    await bp([{_obj:"transform",null:{_ref:[{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}]},
      freeTransformCenterState:{_enum:"quadCenterState",_value:"QCSAverage"},
      width:{_unit:"percentUnit",_value:restore*100},height:{_unit:"percentUnit",_value:restore*100},
      constrainProportions:true,interfaceIconFrameDimmed:{_enum:"interpolationType",_value:"bicubicAutomatic"}}]);
    b = await getLayerBounds();
  }
  trace("text '"+text+"': font "+fontPS+", size "+szBefore+" -> "+szAfter+", restore x"+restore.toFixed(2)+", box "+Math.round(b.w)+"x"+Math.round(b.h)+" (template "+Math.round(before.w)+"x"+Math.round(before.h)+")");
  return {before:before, after:b};
}
// Scale a layer about its own centre (only when factor < 1), then put its centre at (cxT, cyT).
async function placeLayer(l, factor, cxT, cyT){
  await bp([{_obj:"select",_target:[{_ref:"layer",_id:l.id}],makeVisible:false}]);
  let b = await getLayerBounds();
  if(factor < 0.995){
    await bp([{_obj:"transform",null:{_ref:[{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}]},
      freeTransformCenterState:{_enum:"quadCenterState",_value:"QCSAverage"},
      width:{_unit:"percentUnit",_value:factor*100},height:{_unit:"percentUnit",_value:factor*100},
      constrainProportions:true,interfaceIconFrameDimmed:{_enum:"interpolationType",_value:"bicubicAutomatic"}}]);
    b = await getLayerBounds();
  }
  await moveLayerBy(Math.round(cxT - (b.left + b.right)/2), Math.round(cyT - (b.top + b.bottom)/2));
  return b;
}
function layerBox(l){ const b = l.bounds; return {left:b.left, top:b.top, right:b.right, bottom:b.bottom}; }
function unionBox(a){
  return {left:Math.min(...a.map(x=>x.left)), top:Math.min(...a.map(x=>x.top)), right:Math.max(...a.map(x=>x.right)), bottom:Math.max(...a.map(x=>x.bottom))};
}

// Build ONE birthday tee in the given (template) document: texts, colours, labels, zone layout, canvas, one layer group.
// Ages 5+ keep the template's own layout (zone canvas 23 x 30 cm); ages 1-4 (zone 16 x 16 cm) get every zone scaled to
// fit its 16 x 16 cm block below the standard label strip, and the canvas is cut to the zone width.
async function applyBirthdayCopy(doc, item, job, groupName){
  await selectDocById(doc);
  const MARGIN_PX = 38, EDGE_PX = 38, ZGAP = 126;
  const zoneW = job.zone_w_px || 2898, zoneH = job.zone_h_px || 3780;
  const {w:docW0, h:docH0} = await getDocSize();

  // 1. find the template's layers
  let frontLbl = null, backLbl = null, nameL = null, numL = null;
  const art = [];
  for(const l of doc.layers){
    if(l.kind === "text"){
      const c = String(l.textItem.contents || "").trim(), nm = String(l.name || "").trim().toLowerCase();
      if(nm === "front") frontLbl = l; else if(nm === "back") backLbl = l;
      else if(/^\d+$/.test(c)) numL = l; else nameL = l;
    } else if(l.kind !== "group"){
      const bb = l.bounds;
      if((bb.right - bb.left) > 0 && (bb.bottom - bb.top) > 0) art.push(l);
    }
  }
  if(!nameL || !numL || !backLbl) throw new Error("Birthday template: name / number / back label layer not found");
  const backTop0 = backLbl.bounds.top;
  const frontArt = art.filter(l => l.bounds.top < backTop0);
  const nb0 = layerBox(nameL), mb0 = layerBox(numL);
  const fa = frontArt.length ? unionBox(frontArt.map(layerBox)) : null;
  const ba = unionBox([nb0, mb0]);

  // 2. the customer's name and number, coloured
  const nameRes = await replaceTemplateText(nameL, item.name, item.name_colours);
  const numRes  = await replaceTemplateText(numL, item.number, item.number_colour);

  // 3. layout
  const small = zoneW < docW0;
  const strip = labelStrip(zoneW);
  let sF = 1, sB = 1, frontTop = 0, backTop = backTop0, frontContentTop = 0, backContentTop = 0, totalH = docH0;
  if(small){
    const aw = zoneW - 2*MARGIN_PX, ah = zoneH - strip - 2*EDGE_PX;
    if(fa) sF = Math.min(1, aw/(fa.right - fa.left), ah/(fa.bottom - fa.top));
    sB = Math.min(1, aw/(ba.right - ba.left), ah/(ba.bottom - ba.top));
    frontContentTop = strip + EDGE_PX;
    const frontH = fa ? strip + EDGE_PX + (fa.bottom - fa.top)*sF + EDGE_PX : 0;
    backTop = frontH + (fa ? ZGAP : 0);
    backContentTop = backTop + strip + EDGE_PX;
    totalH = Math.round(backContentTop + (ba.bottom - ba.top)*sB + EDGE_PX);
    // front artwork: every layer scaled by sF about its own centre, positions kept relative to the union
    if(fa){
      const ucx = (fa.left + fa.right)/2;
      for(const l of frontArt){
        const b = layerBox(l);
        await placeLayer(l, sF, zoneW/2 + ((b.left + b.right)/2 - ucx)*sF, frontContentTop + ((b.top + b.bottom)/2 - fa.top)*sF);
      }
    }
  }
  const textScale = small ? sB : 1;
  const availW = zoneW - 2*MARGIN_PX;
  for(const [l, tb, res] of [[nameL, nb0, nameRes], [numL, mb0, numRes]]){
    const f = Math.min(1, availW/((res.after.w * textScale) || 1));          // long names shrink to fit the zone width
    const c0y = (tb.top + tb.bottom)/2;
    const cyT = small ? backContentTop + (c0y - ba.top)*sB : c0y;            // vertical centre of the template placeholder
    await placeLayer(l, textScale * f, zoneW/2, cyT);
  }

  // 4. standard labels instead of the template's own front / back labels
  if(frontLbl) frontLbl.delete();
  backLbl.delete();
  if(item.label_front && fa) await addLabel(item.label_front, frontTop, zoneW, zoneW);
  if(item.label_back) await addLabel(item.label_back, backTop, zoneW, zoneW);

  // 5. canvas: the template's own for ages 5+, the 16 cm zone width for ages 1-4
  if(small) await doc.resizeCanvas(zoneW, totalH, constants.AnchorPosition.TOPLEFT);
  trace("birthday '"+item.name+"' "+item.number+": zone "+zoneW+"x"+zoneH+(small ? ", scale front "+sF.toFixed(2)+" back "+sB.toFixed(2)+", canvas "+zoneW+"x"+totalH : ", template layout kept")+", back label at y="+Math.round(backTop));

  // 6. everything into ONE layer group (name / number / labels stay live text)
  await bpc([{_obj:"selectAllLayers",_target:[{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}]}], "select all layers");
  await bpc([{_obj:"make",_target:[{_ref:"layerSection"}],from:{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}}], "group layers");
  await bp([{_obj:"set",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"layer"}]},to:{_obj:"layer",name:groupName}}]);
}

// ── Vinyl name templates (template\vynil template.psd) ───────────────────────────────────────────────────────────────
// One hidden GROUP per product holds its live text layers (name / initials / number, with bevel, stroke and glow effects).
// The job copies that whole group into a NEW document, edits ONLY the text (the template's own colours stay), keeps the
// template's text size (shrunk only to the "Max ..cm" written in the layer name), keeps every layer's alignment, stacks
// copies 1 cm apart and trims the canvas to the artwork (no margin).
function vinylTextLayers(layers, out){
  for(const l of layers){
    if(l.kind === "text") out.push(l);
    else if(l.kind === "group" && l.layers) vinylTextLayers(l.layers, out);
  }
  return out;
}
// Effective text size = font size x the layer's own scale factor (read from the layer descriptor).
async function textMetrics(layerId){
  try{
    const r = await bp([{_obj:"get",_target:[{_ref:"layer",_id:layerId}],_options:{dialogOptions:"dontDisplay"}}]);
    const tk = r[0] && r[0].textKey;
    let xx = null, size = null;
    try{ xx = tk.textShape[0].transform.xx; }catch(e){}
    try{ size = tk.textStyleRange[0].textStyle.size._value; }catch(e){}
    return {xx:xx, size:size, eff:(xx && size) ? xx*size : null};
  }catch(e){ return {xx:null, size:null, eff:null}; }
}
// "Max 20cm Width" / "Max 15CM Height" / "2x Name - Light Blue - Max 12CM" (two names, each at most 12 cm) in a layer name.
function parseVinylMax(name){
  const m = /max\s*(\d+(?:\.\d+)?)\s*cm(?:\s*(width|height))?/i.exec(name || "");
  if(!m) return null;
  const n = /^\s*(\d+)\s*x\b/i.exec(name || "");
  return {cm:parseFloat(m[1]), dim:(m[2] || "width").toLowerCase(), copies:n ? parseInt(n[1], 10) : 1};
}
// Replace the text of one template layer, give it back the template's size, honour its "Max" limit and keep its alignment.
async function applyVinylText(l, value, tplW){
  const PXCM = 125.984;
  const id = l.id, lname = String(l.name || ""), placeholder = String(l.textItem.contents || "");
  const multiLine = /[\r\n]/.test(placeholder);
  let just = "center"; try{ just = String(l.textItem.paragraphStyle.justification || "center").toLowerCase(); }catch(e){}
  const sel = async () => { await bp([{_obj:"select",_target:[{_ref:"layer",_id:id}],makeVisible:false}]); };
  await sel();
  const b0 = await getLayerBounds(), m0 = await textMetrics(id);
  l.textItem.contents = value;
  await sel();
  let b = await getLayerBounds();
  const m1 = await textMetrics(id);
  let s = 1;
  if(m0.eff && m1.eff) s = m0.eff / m1.eff;                          // replacing the text drops the layer's scale factor
  else if(!multiLine && !/[\r\n]/.test(value) && b0.h > 0 && b.h > 0 && b.h < b0.h*0.7) s = b0.h / b.h;   // fallback: glyph heights
  const lim = parseVinylMax(lname);
  let limTxt = "";
  if(lim){
    const dimPx = (lim.dim === "height" ? b.h : b.w) * s;
    const limPx = lim.dim === "height" ? lim.cm*PXCM : (lim.cm*lim.copies + (lim.copies > 1 ? 0.6 : 0))*PXCM;
    if(dimPx > limPx){ s *= limPx/dimPx; limTxt = " (shrunk to Max "+lim.cm+"cm "+lim.dim+")"; }
  }
  const wF = b.w * s; if(wF > tplW - 40) s *= (tplW - 40)/wF;           // never wider than the canvas
  if(Math.abs(s - 1) > 0.01){
    await bp([{_obj:"transform",null:{_ref:[{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}]},
      freeTransformCenterState:{_enum:"quadCenterState",_value:"QCSAverage"},
      width:{_unit:"percentUnit",_value:s*100},height:{_unit:"percentUnit",_value:s*100},
      constrainProportions:true,interfaceIconFrameDimmed:{_enum:"interpolationType",_value:"bicubicAutomatic"}}]);
  }
  const b1 = await getLayerBounds();
  let dx = 0;
  if(just === "left") dx = b0.left - b1.left; else if(just === "right") dx = b0.right - b1.right;
  else dx = (b0.left + b0.right)/2 - (b1.left + b1.right)/2;
  const dy = multiLine ? (b0.top - b1.top) : ((b0.top + b0.bottom)/2 - (b1.top + b1.bottom)/2);
  await moveLayerBy(Math.round(dx), Math.round(dy));
  trace("vinyl '"+lname+"': '"+placeholder.replace(/[\r\n]+/g,"/")+"' -> '"+String(value).replace(/[\r\n]+/g,"/")+"'  size eff "+(m0.eff ? m0.eff.toFixed(1) : "?")+" -> "+(m1.eff ? m1.eff.toFixed(1) : "?")+
        ", scale "+s.toFixed(2)+limTxt+", "+just+", box "+Math.round(b1.w)+"x"+Math.round(b1.h)+" (template "+Math.round(b0.w)+"x"+Math.round(b0.h)+")");
}

// Edit the text of a copied template group in the ACTIVE document: numeric placeholder = number (text2), anything else =
// name / initials (text1); an empty value, or a layer that is not the variant for this order, is removed.
// p = {text1, text2, keep_layer}. Returns the text layers that are left.
async function vinylEditGroup(grp, p, tplW){
  for(const l of vinylTextLayers(grp.layers, [])){
    const ph = String(l.textItem.contents || "").trim(), lname = String(l.name || "");
    const isNum = /^\d+$/.test(ph);
    if(p.keep_layer && !lname.toLowerCase().includes(String(p.keep_layer).toLowerCase())){
      trace("vinyl '"+lname+"': not the variant for this order (keeping '"+p.keep_layer+"') - removed"); l.delete(); continue;
    }
    let val = isNum ? (p.text2 || "") : (p.text1 || "");
    if(!val){ trace("vinyl '"+lname+"': nothing to print for this layer - removed"); l.delete(); continue; }
    if(!isNum){
      const mult = /^\s*(\d+)\s*x\b/i.exec(lname);
      if(mult) val = Array(parseInt(mult[1], 10)).fill(val).join(" ");                // "2x Name": one name per boot
      else if(/[\r\n]/.test(String(l.textItem.contents || ""))) val = Array.from(val).join("\r");   // vertical sock text
    }
    await applyVinylText(l, val, tplW);
  }
  return vinylTextLayers(grp.layers, []);
}

// ── Vinyl MORNING SHEETS: every pending vinyl order packed onto 30 x 80 cm canvases ───────────────────────────────
// Phase A measures every piece (the edited group, one copy) in a work canvas; phase B packs the pieces onto sheets
// (shelf packing, tallest first); phase C builds each sheet and saves it. A piece = one order line with all its copies.
const VINYL_EP = 40;      // room around a piece for the stroke / glow
async function vinylPlaceItem(tplDoc, grpT, targetDoc, item, tplW, destX, destY, copies, gap){
  const known = new Set(targetDoc.layers.map(l => l.id));
  await selectDocById(tplDoc);
  await bp([{_obj:"select",_target:[{_ref:"layer",_id:grpT.id}],makeVisible:false}]);
  await bpc([{_obj:"duplicate",_target:[{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}],to:{_ref:"document",_id:targetDoc.id},version:5}], "copy vinyl group");
  await selectDocById(targetDoc);
  const grp = targetDoc.layers.find(l => !known.has(l.id));
  if(!grp) throw new Error("copied vinyl group not found ("+item.group+")");
  grp.visible = true;
  const left = await vinylEditGroup(grp, item, tplW);
  if(!left.length) throw new Error("no text left to print in '"+item.group+"' ("+item.order_id+")");
  const ub = unionBox(left.map(layerBox));
  const hb = (ub.bottom - ub.top) + 2*VINYL_EP, wb = (ub.right - ub.left) + 2*VINYL_EP;
  await bp([{_obj:"select",_target:[{_ref:"layer",_id:grp.id}],makeVisible:false}]);
  await moveLayerBy(Math.round(destX + VINYL_EP - ub.left), Math.round(destY + VINYL_EP - ub.top));
  for(let k = 1; k < copies; k++){
    await bpc([{_obj:"duplicate",_target:[{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}],version:5}], "duplicate piece (copy "+(k+1)+")");
    await moveLayerBy(0, Math.round(hb + gap));
  }
  return {grp:grp, wb:wb, hb:hb};
}
// MaxRects packing (best short side fit): free rectangles are kept per sheet, every piece goes into the free rectangle it
// fits tightest, and the free space is split around it. Several orderings are tried and the best result is kept: fewest
// sheets, then the least used height. Each piece is packed with its 1 cm gap (G) added, inside a usable area enlarged by G,
// so neighbouring pieces end up exactly G apart and the outer margin M stays free. No rotation (the text must read upright).
function vinylMaxRects(order, blocks, SW, SH, M, G){
  const UW = SW - 2*M + G, UH = SH - 2*M + G;
  const sheets = [];
  const newSheet = () => { const s = {free:[{x:0, y:0, w:UW, h:UH}], placed:[]}; sheets.push(s); return s; };
  const fit = (s, w, h) => {
    let best = null;
    for(const r of s.free){
      if(w <= r.w && h <= r.h){
        const lw = r.w - w, lh = r.h - h, sh = Math.min(lw, lh), lg = Math.max(lw, lh);
        if(!best || sh < best.sh || (sh === best.sh && lg < best.lg)) best = {r:r, sh:sh, lg:lg};
      }
    }
    return best;
  };
  const place = (s, x, y, w, h) => {
    const nf = [];
    for(const f of s.free){
      if(x >= f.x + f.w || x + w <= f.x || y >= f.y + f.h || y + h <= f.y){ nf.push(f); continue; }
      if(x > f.x) nf.push({x:f.x, y:f.y, w:x - f.x, h:f.h});
      if(x + w < f.x + f.w) nf.push({x:x + w, y:f.y, w:f.x + f.w - (x + w), h:f.h});
      if(y > f.y) nf.push({x:f.x, y:f.y, w:f.w, h:y - f.y});
      if(y + h < f.y + f.h) nf.push({x:f.x, y:y + h, w:f.w, h:f.y + f.h - (y + h)});
    }
    s.free = nf.filter((a, i) => !nf.some((b, j) => j !== i && a.x >= b.x && a.y >= b.y && a.x + a.w <= b.x + b.w && a.y + a.h <= b.y + b.h
      && !(a.x === b.x && a.y === b.y && a.w === b.w && a.h === b.h && j > i)));
  };
  for(const i of order){
    const b = blocks[i];
    if(b.w > SW - 2*M) throw new Error("vinyl piece "+b.order_id+" is "+b.w+" px wide - wider than a sheet");
    if(b.h > SH - 2*M) throw new Error("vinyl piece "+b.order_id+" is "+b.h+" px tall - taller than a sheet");
    const w = b.w + G, h = b.h + G;
    let s = null, f = null;
    for(const cand of sheets){ f = fit(cand, w, h); if(f){ s = cand; break; } }
    if(!s){ s = newSheet(); f = fit(s, w, h); }
    s.placed.push({b:b, x:f.r.x + M, y:f.r.y + M});
    place(s, f.r.x, f.r.y, w, h);
  }
  return sheets;
}
function vinylPack(blocks, SW, SH, M, G){
  const idx = blocks.map((b, i) => i);
  const orders = [
    idx.slice().sort((a, b) => (blocks[b].w*blocks[b].h) - (blocks[a].w*blocks[a].h)),                         // area, biggest first
    idx.slice().sort((a, b) => (blocks[b].h - blocks[a].h) || (blocks[b].w - blocks[a].w)),                    // tallest first
    idx.slice().sort((a, b) => (blocks[b].w - blocks[a].w) || (blocks[b].h - blocks[a].h)),                    // widest first
    idx.slice().sort((a, b) => Math.max(blocks[b].w, blocks[b].h) - Math.max(blocks[a].w, blocks[a].h)),       // longest side first
  ];
  let best = null, bestScore = null;
  for(const o of orders){
    const sheets = vinylMaxRects(o, blocks, SW, SH, M, G);
    let usedH = 0; for(const s of sheets) usedH += Math.max(...s.placed.map(p => p.y + p.b.h));
    const score = sheets.length*1e9 + usedH;
    if(bestScore === null || score < bestScore){ best = sheets; bestScore = score; }
  }
  return best;
}
async function runVinylSheets(job, tplDoc, tplW){
  const SW = job.sheet_w_px || 3780, SH = job.sheet_h_px || 10079, M = 0, G = 126, PXCM = 125.984;   // M = 0: every sheet is trimmed to the artwork anyway
  const items = job.vinyl_items || [];
  if(!items.length) throw new Error("vinyl sheets: no items");
  const mk = async (name) => addBlankDoc({width:SW, height:SH, resolution:job.dpi || 320, mode:constants.NewDocumentMode.CMYK,
    fill:constants.DocumentFill.TRANSPARENT, depth:8, profile:"U.S. Web Coated (SWOP) v2"});
  const groupT = {};
  for(const g of new Set(items.map(i => i.group))){
    const t = tplDoc.layers.find(l => l.kind === "group" && l.name === g);
    if(!t) throw new Error("Vinyl template has no group '"+g+"'");
    groupT[g] = t;
  }
  // Phase A: measure one copy of every order line
  let bench = null;
  const blocks = [];
  try{
    bench = await mk("bench");
    for(const it of items){
      const m = await vinylPlaceItem(tplDoc, groupT[it.group], bench, it, tplW, 0, 0, 1, G);
      m.grp.delete();
      const maxCopies = Math.max(1, Math.floor((SH - 2*M + G) / (m.hb + G)));
      let left = Math.max(1, Math.round(it.copies || 1));
      while(left > 0){
        const c = Math.min(left, maxCopies); left -= c;
        blocks.push({item:it, order_id:it.order_id, key:it.key, copies:c, hb:m.hb, w:Math.ceil(m.wb), h:Math.ceil(c*m.hb + (c - 1)*G)});
      }
    }
  } finally { await closeQuietly(bench); }
  trace("vinyl sheets: measured "+items.length+" order line(s) -> "+blocks.length+" piece(s)");
  // Phase B: pack
  const sheets = vinylPack(blocks, SW, SH, M, G);
  trace("vinyl sheets: packed onto "+sheets.length+" sheet(s) of "+SW+"x"+SH+" px");
  // Phase C: build and save every sheet
  const out = [];
  for(let si = 0; si < sheets.length; si++){
    const n = si + 1, s = sheets[si];
    let sd = null;
    try{
      sd = await mk("sheet"+n);
      const placed = [];
      for(const pl of s.placed){
        await vinylPlaceItem(tplDoc, groupT[pl.b.item.group], sd, pl.b.item, tplW, pl.x, pl.y, pl.b.copies, G);
        placed.push({key:pl.b.key, order_id:pl.b.order_id, copies:pl.b.copies, x_cm:pl.x/PXCM, y_cm:pl.y/PXCM, w_cm:pl.b.w/PXCM, h_cm:pl.b.h/PXCM});
      }
      // file name: the hour window (sheet_stem) - the first sheet as is, a second one gets _2, a third _3 ...
      const path = job.sheet_stem ? (String(job.sheet_stem) + (n === 1 ? "" : "_" + n) + ".psd")
                                  : String(job.sheet_pattern).replace("{n}", String(n));
      const entry = await getOutputEntry(path);
      await selectDocById(sd);
      // crop the sheet to the artwork: no empty space (the 1 cm gaps between pieces are inside the artwork box)
      await bpc([{_obj:"trim",trimBasedOn:{_enum:"trimBasedOn",_value:"transparency"},top:true,bottom:true,left:true,right:true}], "trim the sheet to the artwork");
      const fin = await getDocSize();
      trace("vinyl sheet "+n+" trimmed to "+fin.w+"x"+fin.h+"px ("+(fin.w/PXCM).toFixed(1)+" x "+(fin.h/PXCM).toFixed(1)+" cm)");
      await bp([{_obj:"save",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"document"}]},
        in:{_path:fs.createSessionToken(entry),_kind:"local"},as:{_obj:"photoshop35Format",embedProfiles:true}}]);
      log("Saved vinyl sheet "+n+"/"+sheets.length+": "+entry.name+" ("+placed.length+" pieces)");
      out.push({n:n, path:path, w_px:fin.w, h_px:fin.h, blocks:placed});
      await sd.close(constants.SaveOptions.DONOTSAVECHANGES); sd = null;
    } finally { await closeQuietly(sd); }
    await purgeCaches();
  }
  RESULT = {sheets:out, sheet_w_px:SW, sheet_h_px:SH};
}

// ── Stockings gang sheets (template\Stockings\Stockings DTF SILVER | RED Template.psd) ─────────────────────────────────
// The template holds ONE group: a live name text layer ("Aiden", KaushanScript, with its own effects) plus a foil texture smart
// object clipped to it. Per UK hour and colour the names are ganged on one or more sheets: the group is copied once per name into a
// new document, the name is replaced (font, effects and size of the template stay; the name is shrunk only to fit the template's own
// name box, never enlarged), the copy is moved into its grid slot (reading order: left to right, top to bottom) and every sheet is
// trimmed to the artwork. Everything stays live text.
async function applyStockingText(l, value){
  const id = l.id;
  const sel = async () => { await bp([{_obj:"select",_target:[{_ref:"layer",_id:id}],makeVisible:false}]); };
  await sel();
  const b0 = await getLayerBounds(), m0 = await textMetrics(id);
  l.textItem.contents = value;
  await sel();
  const b = await getLayerBounds();
  const m1 = await textMetrics(id);
  let s = 1;
  if(m0.eff && m1.eff) s = m0.eff / m1.eff;                              // replacing the text drops the layer's scale factor
  else if(b0.h > 0 && b.h > 0 && b.h < b0.h*0.7) s = b0.h / b.h;         // fallback: glyph heights
  const fit = Math.min(1, b0.w/((b.w*s) || 1), b0.h/((b.h*s) || 1));    // shrink only, to the template's own name box
  s *= fit;
  if(Math.abs(s - 1) > 0.01){
    await bp([{_obj:"transform",null:{_ref:[{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}]},
      freeTransformCenterState:{_enum:"quadCenterState",_value:"QCSAverage"},
      width:{_unit:"percentUnit",_value:s*100},height:{_unit:"percentUnit",_value:s*100},
      constrainProportions:true,interfaceIconFrameDimmed:{_enum:"interpolationType",_value:"bicubicAutomatic"}}]);
  }
  const b1 = await getLayerBounds();
  await moveLayerBy(Math.round((b0.left + b0.right)/2 - (b1.left + b1.right)/2), Math.round((b0.top + b0.bottom)/2 - (b1.top + b1.bottom)/2));
  trace("stocking '"+value+"': size eff "+(m0.eff ? m0.eff.toFixed(1) : "?")+" -> "+(m1.eff ? m1.eff.toFixed(1) : "?")+", scale "+s.toFixed(2)+(fit < 1 ? " (shrunk to the name box)" : "")+
        ", box "+Math.round(b1.w)+"x"+Math.round(b1.h)+" (template "+Math.round(b0.w)+"x"+Math.round(b0.h)+")");
}
async function runStockingSheets(job, tplDoc, tplW){
  const SW = job.sheet_w_px || 3543, SH = job.sheet_h_px || 5906, PXCM = 125.984;
  const items = job.stocking_items || [];
  if(!items.length) throw new Error("stocking sheets: no names");
  const cols = job.slot_cols || 2, rows = job.slot_rows || 8, px = job.slot_pitch_x_px || 1741, py = job.slot_pitch_y_px || 718, cap = cols*rows;
  const grpT = tplDoc.layers.find(l => l.kind === "group");
  if(!grpT) throw new Error("Stockings template has no layer group");
  if(!vinylTextLayers(grpT.layers, []).length) throw new Error("Stockings template group has no text layer");
  const out = [], nSheets = Math.ceil(items.length / cap);
  for(let si = 0; si < nSheets; si++){
    const n = si + 1, chunk = items.slice(si*cap, (si + 1)*cap);
    let sd = null;
    const openBefore = new Set(); for(let di = 0; di < app.documents.length; di++) openBefore.add(app.documents[di].id);
    try{
      // app.documents.add() has returned the TEMPLATE document instead of the new blank one (v3.18a test: the names were built inside the
      // template and closing the "sheet" closed the template): addBlankDoc() takes the document that was not open before.
      sd = await addBlankDoc({width:SW, height:SH, resolution:job.dpi || 320, mode:constants.NewDocumentMode.CMYK,
        fill:constants.DocumentFill.TRANSPARENT, depth:8, profile:"U.S. Web Coated (SWOP) v2"});
      if(sd.id === tplDoc.id) throw new Error("stocking sheet "+n+": the sheet document is the template document - nothing was built");
      const startIds = new Set(sd.layers.map(l => l.id)), placedIds = new Set();
      trace("stocking sheet "+n+": sheet document id "+sd.id+" (template id "+tplDoc.id+"), starts with layers: "+(sd.layers.map(l => l.name+"#"+l.id).join(", ") || "none"));
      const placed = [];
      for(let k = 0; k < chunk.length; k++){
        const it = chunk[k], col = k % cols, row = Math.floor(k / cols);
        const known = new Set(sd.layers.map(l => l.id));
        await selectDocById(tplDoc);
        await bp([{_obj:"select",_target:[{_ref:"layer",_id:grpT.id}],makeVisible:false}]);
        await bpc([{_obj:"duplicate",_target:[{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}],to:{_ref:"document",_id:sd.id},version:5}], "copy stocking group");
        await selectDocById(sd);
        const grp = sd.layers.find(l => !known.has(l.id));
        if(!grp) throw new Error("copied stocking group not found ("+it.order_id+")");
        grp.visible = true;
        placedIds.add(grp.id);
        try{ grp.name = "Stocking_"+(k + 1)+"_"+it.name; }catch(re){ trace("stocking: could not rename the group: "+re.message); }
        await applyStockingText(vinylTextLayers(grp.layers, [])[0], it.name);
        await bp([{_obj:"select",_target:[{_ref:"layer",_id:grp.id}],makeVisible:false}]);
        await moveLayerBy(col*px, row*py);
        placed.push({key:it.key, order_id:it.order_id, name:it.name, slot:k + 1});
      }
      // Nothing but the copied name groups may be in the sheet (a stray template group once printed "Aiden" under the first name).
      const stray = sd.layers.filter(l => !startIds.has(l.id) && !placedIds.has(l.id));
      if(stray.length || sd.layers.length !== startIds.size + chunk.length)
        throw new Error("stocking sheet "+n+": unexpected layers ("+sd.layers.map(l => l.name+"#"+l.id).join(", ")+") - expected "+chunk.length+" name group(s)");
      const path = job.sheet_stem ? (String(job.sheet_stem) + (n === 1 ? "" : "_" + n) + ".psd")
                                  : String(job.sheet_pattern).replace("{n}", String(n));
      const entry = await getOutputEntry(path);
      await selectDocById(sd);
      await bpc([{_obj:"trim",trimBasedOn:{_enum:"trimBasedOn",_value:"transparency"},top:true,bottom:true,left:true,right:true}], "trim the stocking sheet to the artwork");
      const fin = await getDocSize();
      trace("stocking sheet "+n+" ("+chunk.length+" names) trimmed to "+fin.w+"x"+fin.h+"px ("+(fin.w/PXCM).toFixed(1)+" x "+(fin.h/PXCM).toFixed(1)+" cm)");
      await bp([{_obj:"save",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"document"}]},
        in:{_path:fs.createSessionToken(entry),_kind:"local"},as:{_obj:"photoshop35Format",embedProfiles:true}}]);
      log("Saved stocking sheet "+n+"/"+nSheets+": "+entry.name+" ("+placed.length+" names)");
      out.push({n:n, path:path, w_px:fin.w, h_px:fin.h, blocks:placed});
      await sd.close(constants.SaveOptions.DONOTSAVECHANGES); sd = null;
    } finally {
      await closeQuietly(sd);
      for(let di = app.documents.length - 1; di >= 0; di--){                      // never leave a stray document open in the live Photoshop
        const d = app.documents[di];
        if(d && !openBefore.has(d.id)){ trace("stocking sheet "+n+": closing a leftover document '"+d.name+"' (id "+d.id+")"); await closeQuietly(d); }
      }
    }
    await purgeCaches();
  }
  RESULT = {sheets:out, sheet_w_px:SW, sheet_h_px:SH};
}

// ── Main job processor ────────────────────────────────────────────────────────
async function processJob(jobEntry){
  let job, orderId=jobEntry.name.replace(".json","");
  try{job=await readJSON(jobEntry);orderId=job.order_id||orderId;}
  catch(e){await writeJSON(ERROR_FOLDER,jobEntry.name,{order_id:orderId,error:"Parse: "+e.message,failed_at:ts()});await jobEntry.delete();return;}
  log("Processing: "+orderId);
  TRACE = []; RESULT = null;

  const isCombined = job.combined === true;
  let tplEntry, imgEntries={}, outEntry;
  const multiItemImgEntries = [];  // per-item image entries for multi_items orders
  try{
    // Semi-custom jobs (real jersey templates with named text layers) still need a
    // file. Standard jobs create a blank canvas from scratch — no file, no lookup.
    if(job.template){ tplEntry = await getEntry(job.template); }
    for(const [zone,data] of Object.entries(job.zones)){
      if(data.customer_image) imgEntries[zone]=await getImageEntry(data.customer_image);
    }
    // Pre-load images for multi_items (different design per item)
    if(job.multi_items && job.multi_items.length > 1){
      for(const item of job.multi_items){
        const imgs={};
        for(const [zone,data] of Object.entries(item.zones||{})){
          if(data.customer_image){
            try{ imgs[zone]=await getImageEntry(data.customer_image); }
            catch(e){ log("Multi-item image lookup failed ("+zone+"): "+e.message,true); }
          }
        }
        multiItemImgEntries.push(imgs);
      }
    }
    outEntry = await getOutputEntry(job.output_path);
  }catch(e){
    await writeJSON(ERROR_FOLDER,jobEntry.name,{order_id:orderId,error:"File lookup: "+e.message,failed_at:ts()});
    await jobEntry.delete();return;
  }

  let success=false, errorMsg="";
  await core.executeAsModal(async()=>{
    let doc=null;
    try{
      await closeStrayDocs();      // blank leftovers of an earlier failed job must not be picked up as "the active document"
      if(tplEntry){
        // Semi-custom: real template file with pre-built named layers (jersey crest, "Player"/"08" text).
        const tplToken=fs.createSessionToken(tplEntry);
        await bp([{_obj:"open",null:{_path:tplToken,_kind:"local"},_options:{dialogOptions:"dontDisplay"}}]);
        doc=app.activeDocument;
        log("Opened: "+doc.name);
      } else {
        // Standard: blank canvas created from scratch at the exact product/zone size —
        // no template file to keep in sync, no "No template found" failure mode.
        doc = await addBlankDoc({
          width:      job.canvas_w_px  || 3780,
          height:     job.canvas_h_px  || 3780,
          resolution: job.dpi || 320,
          mode:       constants.NewDocumentMode.CMYK,
          fill:       constants.DocumentFill.TRANSPARENT,
          depth:      8,
          profile:    "U.S. Web Coated (SWOP) v2"
        });
        log("Created blank canvas: "+job.canvas_w_px+"x"+job.canvas_h_px+"px");
      }

      const {w:docW, h:docH, res} = await getDocSize();

      // ── SEMI-CUSTOM: replace named text layers; stack vertically when multiple items ─
      if(job.type === "semi_custom"){
        // Normalise: both single-item (job.text_replacements) and multi-item (job.items) formats
        const items = (job.items && job.items.length > 0)
          ? job.items
          : [{text_replacements: job.text_replacements || {}, colour_hex: job.colour_hex || null, label: ""}];

        // Apply text replacements to the ACTIVE document
        // Set text content only — existing font/size/colour/layer effects are preserved
        async function applySemiCustomText(replacements){
          for(const [layerName, newText] of Object.entries(replacements)){
            if(!newText && newText !== "0"){ log("Semi-custom: skipping empty '"+layerName+"'"); continue; }
            try{
              await bp([{_obj:"set",_target:[{_ref:"layer",_name:layerName}],to:{_obj:"textLayer",textKey:newText}}]);
              log("  '"+layerName+"' -> '"+newText+"'");
            }catch(e){ log("  Failed '"+layerName+"': "+e.message, true); }
          }
        }

        if(job.vinyl_sheets){
          // ── Vinyl morning sheets: all pending vinyl orders on 30 x 80 cm canvases ───────────────────────────────────
          try{ await runVinylSheets(job, doc, docW); success = true; }
          finally { /* the template is closed below in every case */ }
          try{ await doc.close(constants.SaveOptions.DONOTSAVECHANGES); }catch(ce){ log("Close warning (non-fatal): "+ce.message, true); }
          doc = null;
          await purgeCaches();

        } else if(job.stocking_sheets){
          // ── Stockings: the names of one UK hour and colour ganged on 28 x 47 cm sheets ───────────────────────────────
          try{ await runStockingSheets(job, doc, docW); success = true; }
          finally { /* the template is closed below in every case */ }
          try{ await doc.close(constants.SaveOptions.DONOTSAVECHANGES); }catch(ce){ log("Close warning (non-fatal): "+ce.message, true); }
          doc = null;
          await purgeCaches();

        } else if(job.vinyl){
          // ── Vinyl names: copy the product's group into a new canvas, edit the text only, stack copies, trim ──────────
          const tplDoc = doc, tplW = docW, copies = Math.max(1, Math.round(job.copies || 1)), HGAP = 126, PAD = 120;
          let newDoc = null;
          try{
            const grpT = tplDoc.layers.find(l => l.kind === "group" && l.name === job.group);
            if(!grpT) throw new Error("Vinyl template has no group '"+job.group+"'");
            const textsT = vinylTextLayers(grpT.layers, []);
            if(!textsT.length) throw new Error("Vinyl group '"+job.group+"' has no text layers");
            const ubT = unionBox(textsT.map(layerBox));
            let docH2 = Math.ceil(ubT.bottom + 400 + (copies - 1)*((ubT.bottom - ubT.top)*1.5 + HGAP + PAD));
            if(docH2 > 29500) throw new Error("vinyl: "+copies+" copies of '"+job.group+"' need "+docH2+" px (limit 30000)");
            newDoc = await addBlankDoc({width:tplW, height:docH2, resolution:job.dpi || 320, mode:constants.NewDocumentMode.CMYK,
              fill:constants.DocumentFill.TRANSPARENT, depth:8, profile:"U.S. Web Coated (SWOP) v2"});
            log("Vinyl canvas "+tplW+"x"+docH2+"px for group '"+job.group+"', "+copies+" cop"+(copies > 1 ? "ies" : "y"));
            // copy the whole group (live text + effects) from the template into the new canvas
            await selectDocById(tplDoc);
            await bp([{_obj:"select",_target:[{_ref:"layer",_id:grpT.id}],makeVisible:false}]);
            await bpc([{_obj:"duplicate",_target:[{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}],to:{_ref:"document",_id:newDoc.id},version:5}], "copy group to the new canvas");
            await selectDocById(newDoc);
            const grp = newDoc.layers.find(l => l.kind === "group" && l.name === job.group);
            if(!grp) throw new Error("copied vinyl group not found in the new canvas");
            grp.visible = true;                                           // the template groups are hidden
            const left = await vinylEditGroup(grp, job, tplW);
            if(!left.length) throw new Error("vinyl: no text left to print in group '"+job.group+"'");
            // stack the copies (1 cm apart), making the canvas taller first if the edited text needs it
            let ubE = unionBox(left.map(layerBox));
            // A longer name grows to BOTH sides of its centre and used to run past the canvas edge (and was clipped).
            // Move the whole group so everything lies inside the canvas, with room for the glow / stroke.
            const EDGE = 80;
            const sx = ubE.left < EDGE ? Math.round(EDGE - ubE.left) : 0, sy = ubE.top < EDGE ? Math.round(EDGE - ubE.top) : 0;
            await bp([{_obj:"select",_target:[{_ref:"layer",_id:grp.id}],makeVisible:false}]);
            if(sx || sy){
              await moveLayerBy(sx, sy);
              ubE = {left:ubE.left + sx, right:ubE.right + sx, top:ubE.top + sy, bottom:ubE.bottom + sy};
              trace("vinyl: group moved by "+sx+","+sy+" px to lie inside the canvas");
            }
            const dyCopy = Math.ceil((ubE.bottom - ubE.top) + HGAP + PAD);
            const need = Math.ceil(ubE.bottom + 300 + (copies - 1)*dyCopy);
            const needW = Math.max(tplW, Math.ceil(ubE.right + EDGE));
            if(need > 29500) throw new Error("vinyl: "+copies+" copies need "+need+" px (limit 30000)");
            if(need > docH2 || needW > tplW) await newDoc.resizeCanvas(needW, Math.max(need, docH2), constants.AnchorPosition.TOPLEFT);
            for(let k = 1; k < copies; k++){
              await bpc([{_obj:"duplicate",_target:[{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}],version:5}], "duplicate group (copy "+(k+1)+")");
              await moveLayerBy(0, dyCopy);
            }
            // trim the canvas to the artwork: no empty space (effects such as the glow are part of the artwork)
            await selectDocById(newDoc);
            await bpc([{_obj:"trim",trimBasedOn:{_enum:"trimBasedOn",_value:"transparency"},top:true,bottom:true,left:true,right:true}], "trim to the artwork");
            const fin = await getDocSize();
            trace("vinyl canvas trimmed to "+fin.w+"x"+fin.h+"px ("+(fin.w/125.984).toFixed(1)+" x "+(fin.h/125.984).toFixed(1)+" cm)");
            const outToken = fs.createSessionToken(outEntry);
            log("Saving vinyl: "+outEntry.nativePath);
            await bp([{_obj:"save",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"document"}]},
              in:{_path:outToken,_kind:"local"},as:{_obj:"photoshop35Format",embedProfiles:true}}]);
            log("Saved: "+outEntry.name);
            success = true;
            try{ await newDoc.close(constants.SaveOptions.DONOTSAVECHANGES); }catch(ce){ log("Close warning (non-fatal): "+ce.message, true); }
            newDoc = null;
          }catch(ve){ await closeQuietly(newDoc); throw ve; }
          try{ await doc.close(constants.SaveOptions.DONOTSAVECHANGES); }catch(ce){ log("Close warning (non-fatal): "+ce.message, true); }
          doc = null;
          await purgeCaches();

        } else if(job.hoodie || job.birthday){
          // ── Football / gymnastic hoodie: write each name into the template, stack copies 1 cm apart ──────
          const fitW = job.fit_w_px || 2268, fitH = job.fit_h_px || 693, cropW = job.crop_w_px || 2520;
          const HGAP = 126;
          const pfx = job.birthday ? "Birthday" : "Hoodie";
          // one copy = one layer group; the builder differs per template family
          const buildCopy = async (d, it, gName) => {
            if(job.birthday) await applyBirthdayCopy(d, it, job, gName);
            else await applyHoodieName(it.name, fitW, fitH, cropW, d, it.label, gName);
          };
          dbg("hoodie start");
          // Re-opening the template file does not give a second, independent copy (the edits of copy 2 landed in
          // the first document). So: make one pristine spare with duplicate() now, before the main document is
          // changed, and make every further copy from it. Documents are always picked by id, never "the active one".
          let spare = null;
          if(items.length > 1){
            spare = await doc.duplicate("hoodie_spare");
            await selectDocById(doc);
          }
          let copyDoc = null;
          try{
          await buildCopy(doc, items[0], items.length > 1 ? pfx+"_1_"+items[0].name : pfx+"_"+items[0].name);
          const {w:tplW, h:tplH} = await getDocSize();               // cropW x 3780 after the crop
          log("Hoodie 1/"+items.length+": "+items[0].name);
          dbg("hoodie 1 done");
          if(items.length > 1){
            // Copy 1 is one layer GROUP (design + live name + label), exactly like every further copy.
            await doc.resizeCanvas(tplW, tplH*items.length + HGAP*(items.length-1), constants.AnchorPosition.TOPLEFT);
            for(let k = 1; k < items.length; k++){
              const copy = await spare.duplicate("hoodie_copy_"+(k+1));
              copyDoc = copy;
              await selectDocById(copy);
              dbg("copy "+(k+1)+" made");
              await buildCopy(copy, items[k], pfx+"_"+(k+1)+"_"+items[k].name);
              // No clipboard: the copy is one layer GROUP (design + live name + label, left selected); send that group to
              // the main document and place it by its own bounds: same x, y shifted by this copy's slot offset.
              const gName = pfx+"_"+(k+1)+"_"+items[k].name;
              const ref0  = hoodieRef(copy, gName);                    // name text position inside the copy (exact)
              await bpc([{_obj:"duplicate",_target:[{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}],
                to:{_ref:"document",_id:doc.id},version:5}], "send group to main document");
              await copy.close(constants.SaveOptions.DONOTSAVECHANGES);
              copyDoc = null;
              await purgeCaches();
              await selectDocById(doc);
              const ref1  = hoodieRef(doc, gName);                     // where the sent group's name text landed
              const slotY = k * (tplH + HGAP);
              const mdx = Math.round(ref0.left - ref1.left), mdy = Math.round(ref0.top + slotY - ref1.top);
              await bp([{_obj:"select",_target:[{_ref:"layer",_id:ref1.groupId}],makeVisible:false}]);
              await moveLayerBy(mdx, mdy);
              trace("hoodie "+(k+1)+": sent group landed at ("+Math.round(ref1.left)+","+Math.round(ref1.top)+"), moved by "+mdx+","+mdy+" (slot offset "+slotY+")");
              log("Hoodie "+(k+1)+"/"+items.length+": "+items[k].name);
            }
            try{ await spare.close(constants.SaveOptions.DONOTSAVECHANGES); }catch(se){ log("Spare close warning: "+se.message, true); }
            await selectDocById(doc);
            dbg("before save");
          }
          }catch(he){ await closeQuietly(copyDoc); await closeQuietly(spare); throw he; }
          const outToken = fs.createSessionToken(outEntry);
          log("Saving hoodie: "+outEntry.nativePath);
          await bp([{_obj:"save",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"document"}]},
            in:{_path:outToken,_kind:"local"},as:{_obj:"photoshop35Format",embedProfiles:true}}]);
          log("Saved: "+outEntry.name);
          success=true;
          try{ await doc.close(constants.SaveOptions.DONOTSAVECHANGES); }catch(ce){ log("Close warning (non-fatal): "+ce.message, true); }
          doc=null;
          await purgeCaches();

        } else if(items.length === 1){
          // ── Single item / multi-slot template ────────────────────────────────────
          // For multi-slot orders, text_replacements contains ALL slots at once
          // e.g. {"Player":"Scott","08":"3","Player copy":"Karis","08 copy":"9",...}
          // applySemiCustomText edits every named layer in one pass — no canvas tricks.
          await applySemiCustomText(items[0].text_replacements || {});

          // Delete unused-slot layers, then crop canvas to exactly the active slots.
          // job.canvas_h_px = height of one slot (px), job.quantity = number of items (slots actually filled)
          if(job.canvas_h_px > 0){
            const qty = (job.quantity && job.quantity >= 1) ? Math.round(job.quantity) : 1;
            log(qty+" item(s) in this order");

            // docH here is the FULL template height, captured before any crop —
            // dividing by slot height tells us how many slots the template has.
            const maxSlots = Math.max(qty, Math.round(docH / job.canvas_h_px));

            if(qty < maxSlots){
              // Layer names repeat across slots (e.g. "Vector Smart Object copy 6"
              // appears more than once in the SAME slot, and similar names repeat
              // across different slots), so name matching can't tell slots apart.
              // Position is reliable: each slot's layers cluster within that
              // slot's own vertical band, so a layer's top edge / slot height
              // tells us exactly which slot it belongs to.
              const toDelete = [];
              for(let li=0; li<doc.layers.length; li++){
                const layer = doc.layers[li];
                const slotIdx = Math.floor(Math.min(layer.bounds.top, layer.bounds.bottom) / job.canvas_h_px);
                if(slotIdx >= qty && slotIdx < maxSlots) toDelete.push(layer);
              }
              log("Identified "+toDelete.length+" layer(s) to delete from "+(maxSlots-qty)+" unused slot(s)");
              for(let di=0; di<toDelete.length; di++){
                try{ toDelete[di].delete(); }  // synchronous DOM API — not a promise
                catch(e){ log("  Could not delete layer '"+toDelete[di].name+"': "+e.message, true); }
              }
              log("Deleted "+toDelete.length+" layer(s) from "+(maxSlots-qty)+" unused slot(s)");
            }

            const {w:docW} = await getDocSize();
            const targetH = qty * job.canvas_h_px + Math.max(0, qty - 1) * GAP_PX;
            await app.activeDocument.resizeCanvas(docW, targetH, constants.AnchorPosition.TOPLEFT);
            log("Cropped to "+qty+" slot(s): "+docW+"x"+targetH+"px");
          }

          const outToken = fs.createSessionToken(outEntry);
          log("Saving semi-custom: "+outEntry.nativePath);
          await bp([{_obj:"save",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"document"}]},
            in:{_path:outToken,_kind:"local"},as:{_obj:"photoshop35Format",embedProfiles:true}}]);
          log("Saved: "+outEntry.name);
          await doc.close(constants.SaveOptions.DONOTSAVECHANGES);
          doc=null; success=true;
          await purgeCaches();

        } else {
          // ── Multi-item v3.7: expand template in-place, stack items ───────────────
          // FIX: v3.6 closed the template and tried to create a blank combined canvas,
          // but app.activeDocument pointed back to the template (not the new canvas),
          // so all pastes landed in the template at wrong Y positions outside canvas.
          //
          // New approach: KEEP the already-open template as the main document.
          // Apply item[0] text directly into it, resize its canvas downward to the
          // full combined height, then for each subsequent item open a FRESH copy,
          // replace text, copyMerged, and CLOSE THE COPY before pasting — leaving
          // exactly ONE document open so Photoshop cannot paste into the wrong target.
          const GAP_PX = 126;  // 1 cm gap between items at 320 dpi

          const {w:tplW, h:tplH} = await getDocSize();
          log("Multi-item semi-custom: "+items.length+" items  template="+tplW+"x"+tplH+"px");

          // Step 1: Apply FIRST item's text directly into the already-open template
          await applySemiCustomText(items[0].text_replacements || {});
          log("Item 1/"+items.length+" text applied"+(items[0].label?" ("+items[0].label+")":""));

          // Step 2: Expand the template canvas downward to fit all items stacked
          const combinedH = tplH * items.length + GAP_PX * (items.length - 1);
          await app.activeDocument.resizeCanvas(tplW, combinedH, constants.AnchorPosition.TOPLEFT);
          log("Canvas expanded to "+tplW+"x"+combinedH+"px");

          // Step 3: For items 1…n-1: open fresh copy, replace text, copyMerged,
          //         close copy (only expanded template remains open), paste & position.
          for(let itemIdx = 1; itemIdx < items.length; itemIdx++){
            const item = items[itemIdx];
            log("Item "+(itemIdx+1)+"/"+items.length+(item.label?" ("+item.label+")":""));

            // Open a fresh copy of the template (now 2 docs open)
            const itemTplToken = fs.createSessionToken(tplEntry);
            await bp([{_obj:"open",null:{_path:itemTplToken,_kind:"local"},_options:{dialogOptions:"dontDisplay"}}]);

            // Replace text in the fresh copy
            await applySemiCustomText(item.text_replacements || {});

            // Select all and copy merged (preserves all layer effects + transparency)
            await bp([{_obj:"set",_target:[{_ref:"channel",_property:"selection"}],to:{_obj:"whole"}}]);
            await bp([{_obj:"copyMerged"}]);
            log("  Copied");

            // Close the fresh copy — now ONLY the expanded template doc is open
            await app.activeDocument.close(constants.SaveOptions.DONOTSAVECHANGES);
            await purgeCaches();  // this loop can open/close a full-size CMYK doc per item — purge each time

            // Paste into expanded template (only 1 doc open — unambiguous target)
            await bp([{_obj:"paste"}]);

            // Move top edge to correct Y position for this item
            const targetY = itemIdx * (tplH + GAP_PX);
            const bounds  = await getLayerBounds();
            await moveLayerToY(bounds.top, targetY);

            // Left-align
            await bp([{_obj:"align",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"layer"}]},
              using:{_enum:"alignDistributeSelector",_value:"ADSLefts"},alignToCanvas:true}]);

            // Name layer so printing team can identify each item
            const lName = item.label ? "Item_"+(itemIdx+1)+"_"+item.label : "Item_"+(itemIdx+1);
            await bp([{_obj:"set",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"layer"}]},
              to:{_obj:"layer",name:lName}}]);

            log("  Placed at y="+targetY);
          }

          // Save the expanded template as the output PSD
          const outToken = fs.createSessionToken(outEntry);
          log("Saving multi-item: "+outEntry.nativePath);
          await bp([{_obj:"save",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"document"}]},
            in:{_path:outToken,_kind:"local"},as:{_obj:"photoshop35Format",embedProfiles:true}}]);
          log("Saved: "+outEntry.name);
          success=true;  // mark success before close — file is on disk regardless
          try{ await doc.close(constants.SaveOptions.DONOTSAVECHANGES); }catch(ce){ log("Close warning (non-fatal): "+ce.message, true); }
          doc=null;
          await purgeCaches();
        }

      } else {
      // ── STANDARD: zone processing ─────────────────────────────────────────────

      const quantity    = (job.quantity && job.quantity > 1) ? Math.round(job.quantity) : 1;
      const QTY_GAP_PX  = 0;    // extra gap between copies: none - the GAP_PX after each block is already the 1cm cutting gap
      const ZONE_ORDER  = ["pocket","front","back","sleeve"];

      // ── MULTI-ITEM MODE: different design per item, all stacked on one canvas ──
      if(job.multi_items && job.multi_items.length > 1){
        const multiItems = job.multi_items;

        // Calculate total canvas size across all items
        let totalH = 0, maxW = 0;
        for(let ii=0;ii<multiItems.length;ii++){
          if(ii>0) totalH += QTY_GAP_PX;
          for(const [,d] of Object.entries(multiItems[ii].zones||{})){
            totalH += LABEL_PX + (d.zone_h_px||docH) + GAP_PX;
            maxW    = Math.max(maxW, d.zone_w_px||docW);
          }
        }
        const targetW = maxW||docW;
        const targetH = Math.max(totalH, docH);
        await app.activeDocument.resizeCanvas(targetW, targetH, constants.AnchorPosition.TOPLEFT);
        log("Multi-item canvas: "+targetW+"x"+targetH+"px ("+multiItems.length+" items)");

        let currentY = 0;
        for(let itemIdx=0;itemIdx<multiItems.length;itemIdx++){
          if(itemIdx>0) currentY += QTY_GAP_PX;
          const item     = multiItems[itemIdx];
          const itemImgs = multiItemImgEntries[itemIdx]||{};
          const sortedZ  = Object.entries(item.zones||{}).sort(([a],[b])=>{
            const pA=ZONE_ORDER.findIndex(p=>a.startsWith(p));
            const pB=ZONE_ORDER.findIndex(p=>b.startsWith(p));
            return (pA<0?99:pA)-(pB<0?99:pB);
          });
          const itemMaxW = Math.max(...Object.values(item.zones||{}).map(d=>d.zone_w_px||docW));
          for(const [zone,data] of sortedZ){
            const isPocket   = zone.startsWith("pocket");
            const labelText  = (data.label||zone.toUpperCase()).toUpperCase();
            const zoneW = data.zone_w_px||docW;
            const zoneH = data.zone_h_px||docW;
            await addLabel(labelText, currentY, itemMaxW, zoneW);
            currentY += labelStrip(zoneW);
            // Use zone+itemIdx as unique key so layer names never collide
            const zoneKey = zone+"_"+itemIdx;
            const zoneBottom = await processZone(doc, zoneKey, data, itemImgs[zone]||null, currentY, zoneW, zoneH, res, isPocket, itemIdx);
            currentY = zoneBottom + GAP_PX;
          }
          log("Multi-item "+(itemIdx+1)+"/"+multiItems.length+" done (sku="+item.sku+")");
        }

        // Crop to actual content
        const finalH = currentY - GAP_PX;
        await app.activeDocument.resizeCanvas(maxW||docW, finalH, constants.AnchorPosition.TOPLEFT);
        log("Cropped to "+(maxW||docW)+"x"+finalH+"px");

      } else {

      // Calculate exact canvas height needed for single-item / quantity mode
      const perCopyH    = isCombined
        ? Object.values(job.zones).reduce((sum, d) => sum + LABEL_PX + (d.zone_h_px || docH) + GAP_PX, 0)
        : docH;
      const neededH = isCombined
        ? perCopyH * quantity + Math.max(0, quantity - 1) * QTY_GAP_PX
        : docH;
      const targetH = Math.max(neededH, docH);

      if(docH < targetH){
        await app.activeDocument.resizeCanvas(docW, targetH, constants.AnchorPosition.TOPLEFT);
        const newSize = await getDocSize();
        log("Canvas expanded: "+newSize.w+"x"+newSize.h+"px (needed "+targetH+"px, qty="+quantity+")");
      } else {
        log("Canvas ok: "+docW+"x"+docH+"px (qty="+quantity+")");
      }

      if(isCombined){
        // ── COMBINED MODE: stack zones vertically, repeated quantity times ────
        let currentY = 0;
        // Zone order: pocket first, then front, then back (by zone key prefix)
        const ZONE_ORDER = ["pocket","front","back","sleeve"];
        const allZones   = Object.entries(job.zones);
        const zones      = allZones.sort(([a], [b]) => {
          // Sort by prefix order, then by suffix index
          const prefixA = ZONE_ORDER.findIndex(p => a.startsWith(p));
          const prefixB = ZONE_ORDER.findIndex(p => b.startsWith(p));
          if(prefixA !== prefixB) return (prefixA<0?99:prefixA) - (prefixB<0?99:prefixB);
          return a.localeCompare(b);
        });

        for(let copyIdx = 0; copyIdx < quantity; copyIdx++){
          // 1cm separator line between copies (except before the first)
          if(copyIdx > 0) currentY += QTY_GAP_PX;
          if(quantity > 1) log("Copy "+(copyIdx+1)+"/"+quantity+" starting at y="+currentY);

          for(const [zone, data] of zones){
            const isPocket = zone.startsWith("pocket");
            // Add label — append copy number when quantity > 1 so layers are identifiable
            const baseLabel = (data.label || zone.toUpperCase()).toUpperCase();
            const labelText = quantity > 1 ? baseLabel+" #"+(copyIdx+1) : baseLabel;
            const zoneW = data.zone_w_px || docW;
            const zoneH = data.zone_h_px || docW;
            await addLabel(labelText, currentY, docW, zoneW);
            currentY += labelStrip(zoneW);

            // Place image + text for this zone
            const zoneBottom = await processZone(doc, zone, data, imgEntries[zone], currentY, zoneW, zoneH, res, isPocket, copyIdx);
            currentY = zoneBottom + GAP_PX;

            log("Zone done: "+zone+" (copy "+(copyIdx+1)+"/"+quantity+") bottom at "+zoneBottom);
          }
        }

        // Crop canvas to actual content height using pixels directly
        const finalH = currentY - GAP_PX;
        log("Cropping to "+finalH+"px via DOM ("+quantity+" cop"+(quantity===1?"y":"ies")+")...");
        await app.activeDocument.resizeCanvas(docW, finalH, constants.AnchorPosition.TOPLEFT);
        const newSize = await getDocSize();
        log("Canvas after crop: "+newSize.w+"x"+newSize.h+"px");

      } else {
        // ── SINGLE ZONE MODE (existing behaviour) ─────────────────────────────
        for(const [zone,data] of Object.entries(job.zones)){
          if(imgEntries[zone]){
            try{
              const imgToken=fs.createSessionToken(imgEntries[zone]);
              await bp([{_obj:"placeEvent",null:{_path:imgToken,_kind:"local"},freeTransformCenterState:{_enum:"quadCenterState",_value:"QCSAverage"}}]);
              const pb=await getLayerBounds();
              const lw=pb.w,lh=pb.h;
              if(lw>0&&lh>0){
                const scalePct=Math.min((docW-76)/lw,docH/lh)*100; // 76px = 0.6cm total margin
                await bp([{_obj:"transform",null:{_ref:[{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}]},
                  freeTransformCenterState:{_enum:"quadCenterState",_value:"QCSAverage"},
                  width:{_unit:"percentUnit",_value:scalePct},height:{_unit:"percentUnit",_value:scalePct},
                  constrainProportions:true,interfaceIconFrameDimmed:{_enum:"interpolationType",_value:"bicubicAutomatic"}}]);
                for(const dir of ["ADSCentersH","ADSCentersV"]){
                  await bp([{_obj:"align",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"layer"}]},
                    using:{_enum:"alignDistributeSelector",_value:dir},alignToCanvas:true}]);
                }
                log("Fit: "+zone+" ("+Math.round(scalePct)+"%)");
              }
              await bp([{_obj:"rasterizeLayer",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"layer"}]}}]);
              await doc.layers[0].move(doc.layers[1],"placeAfter");
              log("Placed: "+zone);
            }catch(e){log("Place error ("+zone+"): "+e.message,true);}
          }
        }

        for(const [zone,data] of Object.entries(job.zones)){
          if(data.text_lines?.length){
            const layer=doc.layers.find(l=>l.name==="CustomerText_"+zone)||
                        (()=>{for(const l of doc.layers){if(l.name==="CustomerText_"+zone)return l;}})();
            if(layer&&layer.kind==="text"){
              const text=data.text_lines.join("\r");
              const hex=data.colour_hex||"";
              const useSvgColour=!hex;
              const cr=hex?parseInt(hex.slice(1,3),16):255,cg=hex?parseInt(hex.slice(3,5),16):255,cb=hex?parseInt(hex.slice(5,7),16):255;
              const colourProp=useSvgColour?{}:{color:{_obj:"RGBColor",red:cr,green:cg,blue:cb}};
              const fontPS=data.font_ps_name||"Arial-BoldMT";
              const fontFamily=data.font_family||"Arial";
              const fontStyle=data.font_style||"Bold";
              const targetPx=docW*0.80;
              const initFontPS2=useSvgColour?"Arial-BoldMT":fontPS;
              const initFamily2=useSvgColour?"Arial":fontFamily;
              const initStyle2=useSvgColour?"Bold":fontStyle;
              async function applyText(sizePt){
                await bp([{_obj:"set",null:{_ref:[{_ref:"layer",_name:"CustomerText_"+zone}]},
                  to:{_obj:"textLayer",textKey:text,
                    textStyleRange:buildTextStyleRanges(text,initFontPS2,initFamily2,initStyle2,colourProp,sizePt),
                    paragraphStyleRange:[{_obj:"paragraphStyleRange",from:0,to:text.length,
                      paragraphStyle:{_obj:"paragraphStyle",alignment:{_enum:"alignmentType",_value:"center"}}
                    }]
                  }
                }]);
              }
              async function applySvgSize(sizePt){
                await bp([{_obj:"set",null:{_ref:[{_ref:"layer",_name:"CustomerText_"+zone}]},
                  to:{_obj:"textLayer",
                    textStyleRange:buildTextStyleRanges(text,fontPS,fontFamily,fontStyle,{},sizePt)
                  }
                }]);
              }
              let sizePt=100;
              await applyText(sizePt);
              if(useSvgColour) await applySvgSize(sizePt);
              for(let pass=0;pass<2;pass++){
                const lb=await getLayerBounds();
                sizePt=Math.min(Math.max(sizePt*(targetPx/(lb.w||1)),20),400);
                if(useSvgColour){await applySvgSize(sizePt);}else{await applyText(sizePt);}
              }
              await bp([{_obj:"align",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"layer"}]},using:{_enum:"alignDistributeSelector",_value:"ADSCentersH"},alignToCanvas:true}]);
              if(imgEntries[zone]){
                await bp([{_obj:"align",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"layer"}]},using:{_enum:"alignDistributeSelector",_value:"ADSBottoms"},alignToCanvas:true}]);
                const margin=Math.round(docH*0.04);
                if(margin>0) await bp([{_obj:"move",_target:[{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}],to:{_obj:"offset",horizontal:{_unit:"pixelsUnit",_value:0},vertical:{_unit:"pixelsUnit",_value:-margin}}}]);
              }else{
                await bp([{_obj:"align",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"layer"}]},using:{_enum:"alignDistributeSelector",_value:"ADSCentersV"},alignToCanvas:true}]);
              }
              log("Text: "+zone+" font:"+fontFamily+" ("+Math.round(sizePt)+"pt)");
            }
          }
        }

        // Label strip at top
        const LABEL_PX2=252;
        const newH_pt=(docH+LABEL_PX2)*72/res, docW_pt2=docW*72/res;
        await bp([{_obj:"resizeCanvas",width:{_unit:"pointsUnit",_value:docW_pt2},height:{_unit:"pointsUnit",_value:newH_pt},anchor:{_enum:"quadCenterState",_value:"QCSBottomLeft"}}]);
        const labelText=Object.keys(job.zones).map(z=>z.toUpperCase()).join(" / ");
        await bp([{_obj:"make",_target:[{_ref:"textLayer"}],using:{_obj:"textLayer",textKey:labelText,
          textClickPoint:{_obj:"paint",horizontal:{_unit:"percentUnit",_value:1},vertical:{_unit:"percentUnit",_value:1}},
          textStyleRange:[{_obj:"textStyleRange",from:0,to:labelText.length,textStyle:{_obj:"textStyle",fontPostScriptName:"Arial-BoldMT",fontName:"Arial",fontStyleName:"Bold",size:{_unit:"pointsUnit",_value:24},color:{_obj:"RGBColor",red:0,green:0,blue:0}}}]
        }}]);
        await bp([{_obj:"set",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"layer"}]},to:{_obj:"layer",name:"ZoneLabel"}}]);
        await bp([{_obj:"align",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"layer"}]},using:{_enum:"alignDistributeSelector",_value:"ADSLefts"},alignToCanvas:true}]);
        await bp([{_obj:"align",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"layer"}]},using:{_enum:"alignDistributeSelector",_value:"ADSTops"},alignToCanvas:true}]);
        await bp([{_obj:"move",_target:[{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}],to:{_obj:"offset",horizontal:{_unit:"pixelsUnit",_value:126},vertical:{_unit:"pixelsUnit",_value:Math.round(LABEL_PX2/2)}}}]);
        log("Label: "+labelText);
      }

      } // end single-item / quantity-stacking mode

      // Save
      const outToken=fs.createSessionToken(outEntry);
      log("Saving to: "+outEntry.nativePath);
      await bp([{_obj:"save",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"document"}]},in:{_path:outToken,_kind:"local"},as:{_obj:"photoshop35Format",embedProfiles:true}}]);
      log("Saved: "+outEntry.name);
      await doc.close(constants.SaveOptions.DONOTSAVECHANGES);
      doc=null; success=true;
      await purgeCaches();

      } // end else (standard zone processing)
    }catch(e){errorMsg=e.message||String(e);if(doc)try{await doc.close(constants.SaveOptions.DONOTSAVECHANGES);}catch(x){}await purgeCaches();}
  },{commandName:"Process "+orderId});

  if(success){
    await writeJSON(DONE_FOLDER,jobEntry.name,{order_id:orderId,output_filename:outEntry.name,completed_at:ts(),plugin:PLUGIN_VERSION,trace:TRACE,result:RESULT});
    await jobEntry.delete();log("Done: "+orderId);
  }else{
    await writeJSON(ERROR_FOLDER,jobEntry.name,{order_id:orderId,error:errorMsg,failed_at:ts(),plugin:PLUGIN_VERSION,trace:TRACE});
    try{await jobEntry.delete();}catch(x){}
    log("Error: "+orderId+" — "+errorMsg,true);
  }
}

// ── Build all product templates from BUILD_TEMPLATES.json ────────────────────
async function buildAllTemplates(jobEntry){
  let job;
  try{ job = await readJSON(jobEntry); } catch(e){ await jobEntry.delete(); return; }
  const products = job.products || {};
  log("Building "+Object.keys(products).length+" product templates...");
  let done = 0, failed = 0;

  await core.executeAsModal(async()=>{
    for(const [product, dims] of Object.entries(products)){
      const fname = product+".psd";
      let newDoc = null;
      try{
        // DOM API — batchPlay make document fails in PS2026 UXP
        newDoc = await app.documents.add({
          width:      dims.width_px,
          height:     dims.height_px,
          resolution: 320,
          mode:       constants.NewDocumentMode.CMYK,
          fill:       constants.DocumentFill.TRANSPARENT,
          depth:      8,
          profile:    "U.S. Web Coated (SWOP) v2"
        });
        const tplFolder = await getOrCreate(VARSANY_ROOT,"template");
        const f = await tplFolder.createFile(fname,{overwrite:true});
        const token = fs.createSessionToken(f);
        // Use saveAs DOM API — same as createCombinedTemplate which works
        await newDoc.saveAs.psd(f, {embedColorProfile:true}, true);
        await newDoc.close(constants.SaveOptions.DONOTSAVECHANGES);
        newDoc=null; done++;
        log("Template: "+fname+" ("+dims.width_px+"x"+dims.height_px+"px)");
      }catch(e){
        failed++;
        log("FAILED: "+fname+" — "+e.message, true);
        if(newDoc) try{ await newDoc.close(constants.SaveOptions.DONOTSAVECHANGES); }catch(x){}
      }finally{
        await purgeCaches();
      }
    }
  },{commandName:"Build all templates"});

  await jobEntry.delete();
  log("Templates done: "+done+" built, "+failed+" failed.");
}

async function pollJobs(){
  if(processing||!JOBS_FOLDER)return;
  processing=true;
  try{
    const entries=await JOBS_FOLDER.getEntries();
    const jobs=entries.filter(e=>e.name.endsWith(".json")).sort((a,b)=>a.name<b.name?-1:1);
    if(jobs.length>0){
      log("Found "+jobs.length+" job(s)");
      // Check if it's a build-templates job
      const first = jobs[0];
      if(first.name === "BUILD_TEMPLATES.json"){
        await buildAllTemplates(first);
      } else {
        await processJob(first);
      }
    }
  }catch(e){log("Poll error: "+e.message,true);}
  processing=false;
}

// ── Create combined template (3780 x 15000 blank CMYK PSD) ───────────────────
async function createCombinedTemplate(){
  if(!VARSANY_ROOT){log("Pick C:\\Varsany root first",true);return;}
  if(processing){log("Busy",true);return;}
  processing=true;
  try{
    await core.executeAsModal(async()=>{
      await bp([{_obj:"make",new:{_obj:"document",artboard:false,autoPromoteBackgroundLayer:false,
        preset:"Custom",width:{_unit:"pixelsUnit",_value:3780},height:{_unit:"pixelsUnit",_value:15000},
        resolution:{_unit:"densityUnit",_value:320},pixelScaleFactor:1,mode:{_class:"CMYKColorMode"},
        fill:{_enum:"fill",_value:"transparent"},depth:8,profile:"U.S. Web Coated (SWOP) v2"}}]);
      const tplFolder=await getOrCreate(VARSANY_ROOT,"template");
      const f=await tplFolder.createFile("combined_template.psd",{overwrite:true});
      const token=fs.createSessionToken(f);
      await bp([{_obj:"save",as:{_obj:"photoshop35Format",maximizeCompatibility:true},in:{_path:token,_kind:"local"}}]);
      await app.activeDocument.close(constants.SaveOptions.DONOTSAVECHANGES);
      log("Combined template created!");
    },{commandName:"Create combined template"});
  }catch(e){log("Error: "+e.message,true);}
  finally{processing=false;}
}

// ── Create all product templates ──────────────────────────────────────────────
async function createAllTemplates(){
  if(!VARSANY_ROOT){log("Pick C:\\Varsany root first",true);return;}
  if(processing){log("Busy — wait for current job to finish",true);return;}
  processing=true;
  const DPI=320, PX=cm=>Math.round(cm*DPI/2.54);
  // One combined template per product — width = widest zone, height = 15000px (cropped at runtime)
  const PRODUCTS = [
    ["adulttshirt",   30],  // width cm
    ["kidstshirt",    23],
    ["adulthoodie",   25],
    ["kidshoodie",    23],
    ["totebag",       28],
    ["backpack",      18],
    ["makeupbag",     23],
    ["shoebag",       23],
    ["shoebag2",      14],
    ["stringbag",     22],
    ["knittingbag",   25],
    ["buckethat",     18],
    ["beanie",        9.5],
    ["socks",          6],
    ["seatbelt",      18],
    ["babyvest",      15],
    ["sleepsuit",     13],
    ["hodieblanket",  17],
    ["cushion",       30],
    ["memorialplaque",13],
    ["golftowel",     17],
    ["golfcase",      15],
    ["slipper",        6],
  ];
  try{
    const tplFolder = await getOrCreate(VARSANY_ROOT,"template");
    log("Creating "+PRODUCTS.length+" templates...");
    await core.executeAsModal(async()=>{
      let closed = 0;
      for(const [name,wcm] of PRODUCTS){
        // FIX: previously docs were pushed to an openDocs[] list and all closed
        // together at the very end — meaning up to 23 large (wpx x 15000px)
        // CMYK docs could sit open simultaneously, and any doc from an
        // iteration that threw before the push was never closed at all.
        // Now every doc is closed (and purged) immediately, success or failure.
        let newDoc = null;
        try{
          const wpx = PX(wcm);
          await bp([{_obj:"make",new:{_obj:"document",artboard:false,autoPromoteBackgroundLayer:false,
            preset:"Custom",
            width:{_unit:"pixelsUnit",_value:wpx},
            height:{_unit:"pixelsUnit",_value:15000},
            resolution:{_unit:"densityUnit",_value:DPI},
            pixelScaleFactor:1,
            mode:{_class:"CMYKColorMode"},
            fill:{_enum:"fill",_value:"transparent"},
            depth:8,
            profile:"U.S. Web Coated (SWOP) v2"
          }}]);
          newDoc = app.activeDocument;
          // Save via UXP entry (uses VARSANY_ROOT dynamically)
          const tplFileEntry = await tplFolder.createFile(name+"_combined.psd",{overwrite:true});
          await newDoc.saveAs.psd(tplFileEntry,{embedColorProfile:true},true);
          log("Saved: "+name+"_combined.psd ("+wpx+"x15000px)");
        }catch(e){
          log("Error: "+name+" — "+e.message,true);
        }finally{
          if(newDoc){
            try{await newDoc.close(constants.SaveOptions.DONOTSAVECHANGES); closed++;}catch(x){}
            await purgeCaches();
          }
        }
      }
      log("Closed "+closed+" documents.");
    },{commandName:"Create all product templates"});
    log("All templates created in template\\ folder");
  }catch(e){log("Error: "+e.message,true);}
  finally{processing=false;}
}

// ── Resize all templates to 60000px tall ─────────────────────────────────────
async function resizeAllTemplates(){
  if(!VARSANY_ROOT){log("Pick C:\\Varsany root first",true);return;}
  if(processing){log("Busy",true);return;}
  processing=true;
  const TARGET_H = 30000; // Max PSD height (30000px = ~237cm at 320dpi)
  try{
    const tplFolder = await getOrCreate(VARSANY_ROOT,"template");
    const entries   = await tplFolder.getEntries();
    const psds      = entries.filter(e=>e.name.endsWith(".psd")&&!e.name.includes("combined"));
    log("Resizing "+psds.length+" templates to "+TARGET_H+"px...");
    let done=0, skipped=0;
    await core.executeAsModal(async()=>{
      for(const entry of psds){
        try{
          const token = fs.createSessionToken(entry);
          await bp([{_obj:"open",null:{_path:token,_kind:"local"},_options:{dialogOptions:"dontDisplay"}}]);
          const doc = app.activeDocument;
          const {w:curW, h:curH} = await getDocSize();
          if(curH >= TARGET_H){
            await doc.close(constants.SaveOptions.DONOTSAVECHANGES);
            await purgeCaches();
            skipped++; log("Skip: "+entry.name+" (already "+curH+"px)"); continue;
          }
          // Resize canvas to TARGET_H
          await app.activeDocument.resizeCanvas(curW, TARGET_H, constants.AnchorPosition.TOPLEFT);
          // Save using saveAs.psd DOM API
          await doc.saveAs.psd(entry, {embedColorProfile:true}, true);
          await doc.close(constants.SaveOptions.DONOTSAVECHANGES);
          await purgeCaches();
          done++;
          log("Resized: "+entry.name+" ("+curW+"x"+TARGET_H+"px)");
        }catch(e){
          log("Error: "+entry.name+" — "+e.message, true);
          try{await app.activeDocument.close(constants.SaveOptions.DONOTSAVECHANGES);}catch(x){}
          await purgeCaches();
        }
      }
    },{commandName:"Resize templates"});
    log("Done: "+done+" resized, "+skipped+" skipped.");
  }catch(e){log("Error: "+e.message,true);}
  finally{processing=false;}
}

// ── Start ─────────────────────────────────────────────────────────────────────
initFolders().finally(()=>{
  configurePerformance();
  document.getElementById("pickBtn").addEventListener("click", pickRoot);
  document.getElementById("mkCombinedBtn").addEventListener("click", createCombinedTemplate);
  document.getElementById("mkAllTplBtn").addEventListener("click", createAllTemplates);
  document.getElementById("resizeTplBtn").addEventListener("click", resizeAllTemplates);
  document.getElementById("buildBtn").addEventListener("click", ()=>log("Use Build Templates from settings",true));
  setInterval(pollJobs, POLL_MS);
  log("Polling every "+(POLL_MS/1000)+"s ["+PLUGIN_VERSION+"]");
});
