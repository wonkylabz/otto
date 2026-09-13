"use strict";
/* ---- memory tab ---- */
/* All three stores used to render every row at once — ~90 fact events + up to 200 approaches +
   50 rules is a page you scroll forever. Each store is now a collapsible section (open state
   remembered, facts open by default) with its own search + source filter, and a long list paints
   MEM_PAGE rows at a time behind "show more" so the DOM stays small on a 200-row store. */
const MEM_PAGE=20;
const MEM_SECTS="otto.memory.sections";
let MEM={facts:[],sols:[],rules:[]};
const MEM_SHOWN={facts:MEM_PAGE,sols:MEM_PAGE,rules:MEM_PAGE};
/* Garbage collector: candidates only exist client-side after an explicit "Run GC" click (each
   run is several real LLM/claude -p turns, so it never fires on a page load) and are gone again
   once evicted or the tab reloads — this is a review scratchpad, not a fourth persistent store. */
let GC=[]; let GC_META={scanned:0,verify_skipped:0,ran:false};
/* A scan is several real claude -p turns (minutes on a real-sized store), so this must survive a
   re-render — switching tabs away and back calls loadMemory() again, which rebuilds the whole
   section from scratch; without a variable outside that render, the fresh "Run GC" button looks
   like the scan was cancelled even though the fetch (and the server-side work) is still running
   untouched in the background and will land whenever it resolves. */
let GC_RUNNING=false;
let GC_STARTED=null;   // Date.now() a scan started, for the "NNs" elapsed readout — client-side only
let GC_TICK=null;      // the elapsed-readout interval handle

const memSrc=o=>((o.capability||o.scope||"")+"").split(":").pop();
const memQ=k=>{const e=document.getElementById(k+"-q");return e?e.value.trim().toLowerCase():"";};
const memPick=k=>{const e=document.getElementById(k+"-src");return e?e.value:"";};
function memHits(key){
  const q=memQ(key), src=memPick(key);
  return MEM[key].filter(o=>(!src||memSrc(o)===src)
    && (!q||(o.text+" "+(o.request||"")+" "+memSrc(o)).toLowerCase().includes(q)));
}
/* Source filter options, by descending frequency so the caps you actually run are on top. */
function memSrcOptions(key,label){
  const n={};
  MEM[key].forEach(o=>{const c=memSrc(o); if(c) n[c]=(n[c]||0)+1;});
  return `<option value="">${label}</option>`+Object.entries(n).sort((a,b)=>b[1]-a[1])
    .map(([c,k])=>`<option value="${esc(c)}">${esc(c)} (${k})</option>`).join("");
}
function memTools(key,placeholder,srcLabel){
  return `<div class="captools">
    <input class="capsearch" id="${key}-q" type="text" placeholder="${placeholder}" autocomplete="off">
    <select class="setsel" id="${key}-src">${memSrcOptions(key,srcLabel)}</select>
    <span class="memstat memcount" id="${key}-n"></span></div>`;
}
const MEM_NOUN={facts:"runs",sols:"approaches",rules:"rules"};
/* Facts stored before memory._clip_fact landed were cut with a bare 200-char slice, so they end
   mid-word ("...retention decisions must a") and read as corrupt rather than clipped. The text is
   gone from the store and can't be recovered, so mark the cut at render time. Presentation only —
   nothing rewrites the row. Newer facts already carry their own marker and are left alone. */
const FACT_CUT=200;
function factText(f){
  f=String(f||"");
  return (f.length===FACT_CUT && !/[\u2026.!?)"`\]]$/.test(f)) ? f.replace(/[ ,;:-]+$/,"")+"\u2026" : f;
}
/* Shared tail for every store list: the count label and, when the filter still matches more than
   is painted, one button that reveals the next chunk. */
function memPaint(key,rows,empty){
  const hits=memHits(key), shown=hits.slice(0,MEM_SHOWN[key]), rest=hits.length-shown.length;
  const n=document.getElementById(key+"-n");
  const noun=MEM_NOUN[key]||"rows";
  if(n) n.textContent=hits.length===MEM[key].length?`${hits.length} ${noun}`
    :`${hits.length} of ${MEM[key].length} ${noun}`;
  const list=document.getElementById(key+"-list");
  if(!list) return;
  list.innerHTML=(shown.map(rows).join("")||`<p class="memempty">${empty}</p>`)
    +(rest>0?`<button class="linkbtn memmore" data-more="${key}">show ${Math.min(rest,MEM_PAGE)} more &middot; ${rest} left</button>`:"");
  const more=list.querySelector("[data-more]");
  if(more) more.addEventListener("click",()=>{ MEM_SHOWN[key]+=MEM_PAGE; memRender(key); });
  return list;
}
/* Clamp any body taller than the cap; a click on it expands that one row. Must run while the
   section is VISIBLE — scrollHeight is 0 inside a display:none body, which reads as "fits". */
function memClamp(root){
  if(!root) return;
  root.querySelectorAll(".approach, .rule").forEach(b=>{
    if(b.dataset.clamp || b.scrollHeight<=88) return;
    b.dataset.clamp="1";
    b.classList.add("clamped"); b.title="click to expand";
    b.addEventListener("click",()=>{ b.classList.remove("clamped"); b.removeAttribute("title"); });
  });
}
function memRender(key){
  if(key==="facts"){
    /* One "forget" per FACT, not per row: a run stores up to 3 facts together, so the handle is
       the row id + that fact's text (see engine.delete_fact). Clearing the store was the only
       option before, which meant one wrong fact cost every right one. */
    const list=memPaint("facts",e=>`<div class="event">
      ${(e.facts||[]).map(f=>`<div class="fact"><span class="ftext">${esc(factText(f))}</span><button class="factdel" data-id="${esc(String(e.id||""))}" data-fact="${esc(f)}" title="forget this one fact">forget</button></div>`).join("")}
      <div class="esrc">learned from <b>${esc(memSrc(e))}</b> · <span title="${esc(e.at||'')}">${esc(shortWhen(e.at))}</span> · &ldquo;${esc((e.request||'').slice(0,70))}&rdquo;</div>
    </div>`,"Nothing matches.");
    if(list) list.querySelectorAll(".factdel").forEach(b=>b.addEventListener("click",async()=>{
      if(!confirm("Forget this fact?\n\n"+b.dataset.fact)) return;
      await fetch("/api/memory/delete",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({id:b.dataset.id,fact:b.dataset.fact})});
      mascotEvict(1);
      loadMemory();
    }));
  } else if(key==="sols"){
    const list=memPaint("sols",s=>`<div class="event">
      <div class="approach">${renderMD(s.approach||"")}</div>
      <div class="esrc">approach from <b>${esc(memSrc(s))}</b> · <span title="${esc(s.at||'')}">${esc(shortWhen(s.at))}</span> · &ldquo;${esc((s.request||'').slice(0,70))}&rdquo;
        <button class="soldel" data-id="${esc(s.id||'')}">delete</button></div>
    </div>`,"Nothing matches.");
    if(list) memClamp(list);
    if(list) list.querySelectorAll(".soldel").forEach(b=>b.addEventListener("click",async()=>{
      await fetch("/api/solutions/delete",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({id:b.dataset.id})});
      loadMemory();
    }));
  } else {
    const list=memPaint("rules",b=>`<div class="event">
      <div class="rule">${renderMD(b.rule||"")}</div>
      <div class="esrc"><span class="rscope">${esc(memSrc(b))}</span> · <span title="${esc(b.at||'')}">${esc(shortWhen(b.at))}</span>
        <button class="ruleedit" data-id="${esc(b.id||'')}" data-rule="${esc(b.rule||'')}">edit</button>
        <button class="ruledel" data-id="${esc(b.id||'')}">delete</button></div>
    </div>`,"Nothing matches.");
    if(list){
      memClamp(list);
      list.querySelectorAll(".ruledel").forEach(b=>b.addEventListener("click",async()=>{
        await fetch("/api/behaviors/delete",{method:"POST",headers:{"Content-Type":"application/json"},
          body:JSON.stringify({id:b.dataset.id})});
        loadMemory();
      }));
      list.querySelectorAll(".ruleedit").forEach(b=>b.addEventListener("click",async()=>{
        const next=prompt("Edit rule:",b.dataset.rule); if(next===null) return;
        await fetch("/api/behaviors/update",{method:"POST",headers:{"Content-Type":"application/json"},
          body:JSON.stringify({id:b.dataset.id,rule:next})});
        loadMemory();
      }));
    }
  }
}
/* `countTitle` exists because the badge does NOT always count rows: one memory row holds up to
   3 facts (each with its own "forget" button), so the facts badge counts FACTS — matching the
   page header — while sols/rules genuinely count rows. */
function memSection(key,title,lede,count,body,open,countTitle){
  return `<section class="memsec${open?"":" collapsed"}" data-sect="${key}">
    <div class="phead sec memtoggle" title="collapse / expand"><span class="gcaret">&#9662;</span>
      <h2>${title}</h2><p class="sub">${lede}</p><span class="sectcount" title="${countTitle||"rows in this list"}">${count}</span></div>
    <div class="memsec-body">${body}</div></section>`;
}

/* One eviction candidate: which store it came from, the flagged text, why, and a checkbox
   (pre-checked — reviewing then unchecking the rare false positive is less friction than having
   to opt every real one in). The whole row is the click target, and visibly dims once unchecked
   so "will this be evicted?" reads at a glance. `data-i` indexes back into GC, not a filtered/paged view. */
function gcRow(c,i){
  return `<label class="gccand">
    <input type="checkbox" class="gcpick" data-i="${i}" checked>
    <span class="gcbody">
      <span class="rscope">${esc(c.store||"")}</span> <span class="ftext">${esc(c.text||"")}</span>
      <div class="esrc">${esc(c.reason||"")}${c.context?` &middot; &ldquo;${esc((c.context||"").slice(0,70))}&rdquo;`:""}</div>
    </span>
  </label>`;
}
/* Seconds since the scan started, for a live " NNs" readout next to the spinner — real
   reassurance it hasn't stalled on a multi-minute pass, cheap to compute since it's just wall-clock. */
function gcElapsed(){
  return (GC_RUNNING && GC_STARTED) ? ` ${Math.max(0,Math.round((Date.now()-GC_STARTED)/1000))}s` : "";
}
function gcBody(){
  const runCtl=GC_RUNNING
    ?`<span class="runspin"><span class="spin"></span>scanning<span id="gc-elapsed">${gcElapsed()}</span></span>`
    :`<button class="addbtn" id="gc-run" title="classify every stored fact/approach/rule and flag what looks stale or no longer true — several real LLM turns, so this is a button, not a page load">&#8635; Run GC</button>`;
  const toolbar=(GC.length&&!GC_RUNNING)
    ?`<button class="linkbtn" id="gc-selall">select all</button><button class="linkbtn" id="gc-selnone">select none</button>
      <button class="btn approve sm" id="gc-evict" style="margin-left:auto">Evict selected</button>`:"";
  const hint=GC_RUNNING?`<p class="sub${GC.length?"":" gccenter"}">Keeps running even if you switch tabs — a real store can take a few minutes.</p>`
    :GC_META.ran?`<p class="sub">Scanned ${GC_META.scanned} item(s)`
      +(GC_META.verify_skipped?`, ${GC_META.verify_skipped} left for next run (verification cap)`:"")+`.</p>`:"";
  const list=GC.length?GC.map(gcRow).join("")
    :`<p class="memempty">${GC_RUNNING?"":GC_META.ran?"Nothing flagged — memory looks clean.":"Not run yet."}</p>`;
  return `<div class="memhead${GC.length?"":" gccenter"}">${runCtl}${toolbar}</div>
    ${hint}
    <div id="gc-list"${GC.length?"":' class="gccenter"'}>${list}</div>`;
}
function refreshGCSection(){
  const body=document.querySelector('.memsec[data-sect="gc"] .memsec-body');
  if(body) body.innerHTML=gcBody();
  const cnt=document.querySelector('.memsec[data-sect="gc"] .sectcount');
  if(cnt) cnt.textContent=GC.length;
  wireGC();
}
/* The 1s elapsed-readout tick doubles as the poll for the scan actually finishing server-side —
   gc/run no longer blocks the request for the scan's whole duration (see runGC/gcTrack below), so
   nothing else would ever notice completion. Cheap: /gc/status is an in-memory dict lookup. */
async function gcTick(){
  const el=document.getElementById("gc-elapsed");
  if(el) el.textContent=gcElapsed();
  if(!GC_RUNNING) return;
  let s; try { s=await (await fetch("/api/memory/gc/status")).json(); } catch(e){ return; }
  if(s.running) return;
  clearInterval(GC_TICK); GC_TICK=null;
  GC_RUNNING=false; GC_STARTED=null;
  const r=s.result||{};
  GC=r.candidates||[]; GC_META={scanned:r.scanned||0,verify_skipped:r.verify_skipped||0,ran:true};
  if(r.error) alert("GC run failed: "+r.error);
  refreshGCSection();
}
function gcSetAll(checked){
  document.querySelectorAll(".gcpick").forEach(cb=>{
    cb.checked=checked;
    cb.closest(".gccand").classList.toggle("gcunchecked",!checked);
  });
}
/* Reattach to a scan already running server-side (e.g. a page refresh mid-scan, or switching back
   to a tab that started one) instead of assuming GC_RUNNING's default false — started_at is
   seconds since epoch from the server, converted to the ms gcElapsed() expects. */
function gcTrack(startedAtS){
  GC_RUNNING=true; GC_STARTED=startedAtS?startedAtS*1000:Date.now();
  clearInterval(GC_TICK); GC_TICK=setInterval(gcTick,1000);
}
async function runGC(){
  if(GC_RUNNING) return;   // already in flight (e.g. a stale click after switching tabs away and back)
  let r;
  try { r=await (await fetch("/api/memory/gc/run",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"})).json(); }
  catch(e){ alert("GC run failed: "+e.message); return; }
  if(!r.started) return;   // a scan was already running server-side — just let the tick pick it up
  gcTrack();
  refreshGCSection();      // reflect "scanning…" immediately; GC_RUNNING outlives any later re-render
}
async function evictGC(){
  const picks=[...document.querySelectorAll(".gcpick:checked")].map(el=>GC[+el.dataset.i]).filter(Boolean);
  if(!picks.length) return;
  if(!confirm(`Evict ${picks.length} item(s)? This can't be undone (the audit trail keeps a record).`)) return;
  try{
    await fetch("/api/memory/gc/evict",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({candidates:picks})});
  } catch(e){ alert("Eviction failed: "+e.message); return; }
  mascotEvict(picks.length);
  GC=[]; GC_META={scanned:0,verify_skipped:0,ran:false};
  loadMemory();
}
function wireGC(){
  const run=document.getElementById("gc-run");
  if(run) run.addEventListener("click",runGC);
  const ev=document.getElementById("gc-evict");
  if(ev) ev.addEventListener("click",evictGC);
  const selall=document.getElementById("gc-selall");
  if(selall) selall.addEventListener("click",()=>gcSetAll(true));
  const selnone=document.getElementById("gc-selnone");
  if(selnone) selnone.addEventListener("click",()=>gcSetAll(false));
  document.querySelectorAll(".gcpick").forEach(cb=>cb.addEventListener("change",()=>{
    cb.closest(".gccand").classList.toggle("gcunchecked",!cb.checked);
  }));
}

async function loadMemory(){
  const el=document.getElementById("memoryview");
  el.innerHTML=`<p class="sub">loading…</p>`;
  let data, sdata, bdata, gcs;
  try {
    [data, sdata, bdata, gcs] = await Promise.all([
      fetch("/api/memory").then(r=>r.json()),
      fetch("/api/solutions").then(r=>r.json()),
      fetch("/api/behaviors").then(r=>r.json()),
      fetch("/api/memory/gc/status").then(r=>r.json()).catch(()=>({running:false})),
    ]);
  }
  catch(e){ el.innerHTML=`<p class="err">Couldn't load memory (${esc(e.message)}).</p>`; return; }
  // Reattach to a GC scan already running server-side — a page refresh otherwise resets GC_RUNNING
  // to its default false while the real scan (several claude -p turns, real minutes) keeps going
  // untracked on the server.
  if(gcs && gcs.running && !GC_RUNNING) gcTrack(gcs.started_at);
  // `text` is the searchable body of each row, normalised so one filter works over all three.
  MEM.facts=(data.events||[]).filter(e=>(e.facts||[]).length).map(e=>({...e,text:(e.facts||[]).join(" ")}));
  MEM.sols=(sdata.solutions||[]).map(s=>({...s,text:s.approach||""}));
  MEM.rules=(bdata.behaviors||[]).map(b=>({...b,text:b.rule||"",capability:b.scope}));
  Object.keys(MEM_SHOWN).forEach(k=>MEM_SHOWN[k]=MEM_PAGE);
  let openState={}; try { openState=JSON.parse(localStorage.getItem(MEM_SECTS))||{facts:1}; } catch(e){ openState={facts:1}; }
  el.innerHTML=`
    <div class="phead"><h1>Persistent memory</h1>
      <p class="sub">What Otto carries into later runs. Every action is in <b>Audit</b>.</p>
      <span class="pside memstat"><b>${data.facts_total||0}</b> facts · <b>${sdata.count||0}</b> approaches · <b>${bdata.count||0}</b> rules</span></div>
    ${memSection("facts","Learned facts","Distilled from finished runs, injected as context on the next one.",data.facts_total||0,
      `<div class="memhead"><button class="clearbtn" id="mem-clear" style="margin-left:0">Clear facts</button></div>
       ${memTools("facts","Search facts, requests or capability…","every capability")}
       <div id="facts-list"></div>`, openState.facts, "facts stored")}
    ${memSection("sols","Solved-task approaches","Methods from verified runs, replayed as worked examples on similar requests.",sdata.count||0,
      `<div class="memhead"><button class="clearbtn" id="sol-clear" style="margin-left:0">Clear approaches</button></div>
       ${memTools("sols","Search approaches…","every capability")}
       <div id="sols-list"></div>`, openState.sols)}
    ${memSection("rules","Behaviour rules","Directives injected into runs, globally or per capability. Advisory: never changes the gate or tools.",bdata.count||0,
      `<div class="memhead"><button class="addbtn addnew" id="bx-add" style="margin-left:0">+ Add rule</button></div>
      ${memTools("rules","Search rules…","every scope")}
      <div id="rules-list"></div>`, openState.rules)}
    ${memSection("gc","Garbage collector","Classifies stored facts, approaches and rules and flags what looks stale or no longer true. Read-only until you pick candidates and confirm — nothing is deleted on its own.",GC.length,gcBody(),openState.gc)}`;
  ["facts","sols","rules"].forEach(k=>{
    memRender(k);
    const q=document.getElementById(k+"-q"), src=document.getElementById(k+"-src");
    const redraw=()=>{ MEM_SHOWN[k]=MEM_PAGE; memRender(k); };
    if(q) q.addEventListener("input",redraw);
    if(src) src.addEventListener("change",redraw);
  });
  el.querySelectorAll(".memtoggle").forEach(t=>t.addEventListener("click",()=>{
    const sec=t.closest(".memsec"), open=!sec.classList.toggle("collapsed");
    if(open) memClamp(sec.querySelector(".memsec-body"));
    let st={}; try { st=JSON.parse(localStorage.getItem(MEM_SECTS))||{}; } catch(e){}
    if(open) st[sec.dataset.sect]=1; else delete st[sec.dataset.sect];
    try { localStorage.setItem(MEM_SECTS,JSON.stringify(st)); } catch(e){}
  }));
  const cb=document.getElementById("mem-clear");
  if(cb) cb.addEventListener("click",async()=>{
    if(!confirm("Clear working memory? The audit trail is kept.")) return;
    await fetch("/api/memory/clear",{method:"POST"}); loadMemory();
  });
  const sc=document.getElementById("sol-clear");
  if(sc) sc.addEventListener("click",async()=>{
    if(!confirm("Clear all stored solution approaches?")) return;
    await fetch("/api/solutions/clear",{method:"POST"}); loadMemory();
  });
  const addRule=document.getElementById("bx-add");
  if(addRule) addRule.addEventListener("click",showBehaviorRuleForm);
  wireGC();
}

/* "+ Add rule" opens the shared config-form modal. NOT `showRuleForm` — events.js already owns
   that name for its webhook rules, and every script here shares one global scope, so the file
   that loads last would silently answer both buttons., like every other add control in the app
   (ui.md). The paste box used to sit open above the list, so the section led with an empty
   textarea and mentioned how many rules were stored underneath it, in passing. */
function showBehaviorRuleForm(){
  const c=openFormModal("<b>New behaviour rule</b><br>a directive injected into runs &mdash; advisory, never a gate");
  c.innerHTML=`<div class="aform">
    <label>Rule</label>
    <textarea id="bx-rule" rows="3" placeholder="e.g. Always run the tests before opening a PR"></textarea>
    <label>Scope</label><select id="bx-scope">${scopeOptions('global')}</select>
    <div class="ferr" id="bx-err"></div>
    <div class="factions"><button class="btn approve" id="bx-save">Add rule</button>
      <button class="btn decline" id="bx-cancel">Cancel</button></div>
  </div>`;
  document.getElementById("bx-rule").focus();
  document.getElementById("bx-cancel").onclick=closeFormModal;
  document.getElementById("bx-save").onclick=async()=>{
    const rule=(document.getElementById("bx-rule").value||"").trim();
    if(!rule){ document.getElementById("bx-err").textContent="write the rule first"; return; }
    const scope=document.getElementById("bx-scope").value||"global";
    await fetch("/api/behaviors/add",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({rule,scope})});
    closeFormModal(); loadMemory();
  };
}

/* Build <option>s for a behaviour-rule scope picker: Global + each enabled capability
   (value = "<kind>:<name>", matching engine.applicable_behaviors). */
function scopeOptions(sel){
  const g=`<option value="global"${sel==='global'?' selected':''}>Global — every run</option>`;
  const caps=(CAPS_LIST||[]).filter(c=>c.enabled!==false).map(c=>{
    const v=`${c.kind}:${c.name}`;
    return `<option value="${esc(v)}"${sel===v?' selected':''}>${esc(c.name)}</option>`;
  }).join("");
  return g+caps;
}
