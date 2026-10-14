const {app,core,action,constants} = require("photoshop");
const uxp = require("uxp");
const fs  = uxp.storage.localFileSystem;

const PLUGIN_VERSION = "v3.10b-fill-width";
const POLL_MS   = 3000;
const LABEL_PX  = 126;  // 1cm at 320dpi (halved from 2cm)
const GAP_PX    = 126;  // 1cm gap between zones
const CANVAS_W  = 3780; // default canvas width px

let VARSANY_ROOT=null, DATA_FOLDER=null, JOBS_FOLDER=null, DONE_FOLDER=null, ERROR_FOLDER=null;
let processing = false;
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
async function addLabel(labelText, yTop, canvasW){
  await bp([{_obj:"make",_target:[{_ref:"textLayer"}],
    using:{_obj:"textLayer",textKey:labelText,
      textClickPoint:{_obj:"paint",horizontal:{_unit:"percentUnit",_value:1},vertical:{_unit:"percentUnit",_value:1}},
      textStyleRange:[{_obj:"textStyleRange",from:0,to:labelText.length,
        textStyle:{_obj:"textStyle",fontPostScriptName:"Arial-BoldMT",fontName:"Arial",fontStyleName:"Bold",
          size:{_unit:"pointsUnit",_value:12},color:{_obj:"RGBColor",red:0,green:0,blue:0}}
      }]
    }
  }]);
  await bp([{_obj:"set",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"layer"}]},to:{_obj:"layer",name:"Label_"+labelText}}]);
  // Align left then move to correct Y
  await bp([{_obj:"align",null:{_ref:[{_enum:"ordinal",_value:"targetEnum",_ref:"layer"}]},using:{_enum:"alignDistributeSelector",_value:"ADSLefts"},alignToCanvas:true}]);
  const lb = await getLayerBounds();
  await moveLayerToY(lb.top, yTop + Math.round(LABEL_PX/2) - Math.round((lb.bottom-lb.top)/2));
  await bp([{_obj:"move",_target:[{_ref:"layer",_enum:"ordinal",_value:"targetEnum"}],
    to:{_obj:"offset",horizontal:{_unit:"pixelsUnit",_value:GAP_PX},vertical:{_unit:"pixelsUnit",_value:0}}}]);
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
  const areaH     = Math.max(zoneH - LABEL_PX, 200);
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
      sizePt=Math.min(Math.max(sizePt*(targetPx/(lb.w||1)),20),1200);
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

// ── Main job processor ────────────────────────────────────────────────────────
async function processJob(jobEntry){
  let job, orderId=jobEntry.name.replace(".json","");
  try{job=await readJSON(jobEntry);orderId=job.order_id||orderId;}
  catch(e){await writeJSON(ERROR_FOLDER,jobEntry.name,{order_id:orderId,error:"Parse: "+e.message,failed_at:ts()});await jobEntry.delete();return;}
  log("Processing: "+orderId);
  TRACE = [];

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
      if(tplEntry){
        // Semi-custom: real template file with pre-built named layers (jersey crest, "Player"/"08" text).
        const tplToken=fs.createSessionToken(tplEntry);
        await bp([{_obj:"open",null:{_path:tplToken,_kind:"local"},_options:{dialogOptions:"dontDisplay"}}]);
        doc=app.activeDocument;
        log("Opened: "+doc.name);
      } else {
        // Standard: blank canvas created from scratch at the exact product/zone size —
        // no template file to keep in sync, no "No template found" failure mode.
        doc = await app.documents.add({
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

        if(items.length === 1){
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
      const QTY_GAP_PX  = 126;  // 1cm gap between copies at 320dpi (for cutting)
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
            await addLabel(labelText, currentY, itemMaxW);
            currentY += LABEL_PX;
            const zoneW = data.zone_w_px||docW;
            const zoneH = data.zone_h_px||docW;
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
            await addLabel(labelText, currentY, docW);
            currentY += LABEL_PX;

            // Place image + text for this zone
            const zoneW = data.zone_w_px || docW;
            const zoneH = data.zone_h_px || docW;
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
    await writeJSON(DONE_FOLDER,jobEntry.name,{order_id:orderId,output_filename:outEntry.name,completed_at:ts(),plugin:PLUGIN_VERSION,trace:TRACE});
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
