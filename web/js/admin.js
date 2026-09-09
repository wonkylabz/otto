"use strict";
/* Admin: repo conventions, appearance, runtime settings, secrets, the model/endpoint
   matrix, capabilities, MCP servers, project repos and the portable profile bundle. */
// Repo conventions: what the judge is actually enforcing for each project repo. CONV_BUSY lives
// OUTSIDE the render (same reason as GC_RUNNING) — renderAdmin rebuilds the whole panel, so an
// in-flight re-derivation would otherwise look cancelled the moment anything else re-renders,
// even though the fetch and the server-side work keep going.
let CONV_STATE={}, CONV_BUSY=new Set();
const CONV_LABEL={fresh:"in use", stale:"re-derives next run", none:"not derived yet", absent:"no CLAUDE.md"};

function convCell(path){
  const s=CONV_STATE[path]||{state:"none",count:0,rules:[]};
  // data-conv is on the CELL, so it stays findable in the busy state too (which has no buttons).
  if(CONV_BUSY.has(path))
    return `<div class="convcell" data-conv="${esc(path)}"><span class="sub"><span class="spin"></span> deriving…</span></div>`;
  const count=s.state==="absent"?`<button class="convcount" disabled>—</button>`
    :`<button class="convcount" data-convview="${esc(path)}" title="show the rules distilled from this repo's CLAUDE.md">${s.count} rule${s.count===1?"":"s"}</button>`;
  const refresh=s.state==="absent"?""
    :`<button class="clearbtn" data-convrefresh="${esc(path)}" style="margin-left:0" title="re-read this repo's CLAUDE.md and distil it again">Re-derive</button>`;
  return `<div class="convcell" data-conv="${esc(path)}">${count}
    <span class="convstate ${s.state}" title="${esc(CONV_LABEL[s.state]||"")}">${esc(CONV_LABEL[s.state]||s.state)}</span>
    ${refresh}</div>`;
}

function convView(path){
  const s=CONV_STATE[path]||{rules:[]};
  const body=openFormModal(`Conventions — <code>${esc(path.split('/').pop()||path)}</code>`);
  const rules=(s.rules||[]);
  body.innerHTML=`
    <p class="sub">Hard rules distilled from this repo's own <code>CLAUDE.md</code>. The judge gets
    the ones most relevant to each request — a repo usually has more than fit one prompt, and the
    judge is told when the list it received was partial.</p>
    ${rules.length?`<ol class="convrules">${rules.map(r=>`<li>${esc(r)}</li>`).join("")}</ol>`
      :`<p class="sub">Nothing derived yet — hit <b>Re-derive</b>, or it happens on this repo's next judged run.</p>`}`;
}

async function convRefresh(path){
  if(CONV_BUSY.has(path)) return;            // a stale click after switching tabs away and back
  CONV_BUSY.add(path);
  // A quoted attribute selector takes the value literally — CSS.escape() is for IDENTIFIERS and
  // would inject backslashes here. Paths carry no quotes, so the raw value is the correct form.
  const sel=`.projtable .convcell[data-conv="${path}"]`;
  const cell=document.querySelector(sel);
  if(cell) cell.innerHTML=`<span class="sub"><span class="spin"></span> deriving…</span>`;
  try{
    const r=await fetch("/api/conventions/refresh",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({path})});
    const s=await r.json();
    if(s && s.path) CONV_STATE[path]=s;
  }catch(e){ /* leave the old state; the cell repaints from CONV_STATE below */ }
  finally{
    CONV_BUSY.delete(path);
    // Re-query: the panel may have re-rendered under us while the derivation was in flight.
    const c=document.querySelector(sel);
    if(c) c.outerHTML=convCell(path);
    bindConv(document.getElementById("adminview"));
  }
}

function bindConv(el){
  if(!el) return;
  el.querySelectorAll("[data-convview]").forEach(b=>b.onclick=()=>convView(b.dataset.convview));
  el.querySelectorAll("[data-convrefresh]").forEach(b=>b.onclick=()=>convRefresh(b.dataset.convrefresh));
}

async function loadAdmin(){
  adminLoaded=true;
  const el=document.getElementById("adminview");
  el.innerHTML=`<div class="pageSpin"><span class="spin"></span></div>`;
  let data, models, health, stats, settings, conv;
  try { [data, models, health, stats, settings, conv]=await Promise.all([
    fetch("/api/policy").then(r=>r.json()), fetch("/api/models").then(r=>r.json()),
    fetch("/api/health").then(r=>r.json()).catch(()=>({})),
    fetch("/api/stats").then(r=>r.json()).catch(()=>({caps:[]})),
    fetch("/api/settings").then(r=>r.json()).catch(()=>({settings:{}})),
    fetch("/api/conventions").then(r=>r.json()).catch(()=>({repos:[]}))]); }
  catch(e){ el.innerHTML=`<p class="err">Couldn't load config (${e.message}). Is the server running?</p>`; adminLoaded=false; return; }
  GATEWAY_STATS=(health&&health.gateway)||{tasks:{},down:{}};
  MODEL_HEALTH=(models&&models.health)||{};   // fresh: /api/models re-probes stale local entries
  SCORECARD={}; ((stats&&stats.caps)||[]).forEach(c=>{ SCORECARD[c.name]=c; });
  POLICY_STATE={capabilities:{},mcps:{}};
  data.capabilities.forEach(c=>POLICY_STATE.capabilities[c.name]={risk:c.risk,enabled:c.enabled,tool_free:!!c.tool_free});
  data.mcps.forEach(m=>POLICY_STATE.mcps[m.name]={enabled:m.enabled});   // notes are server-owned — /api/mcp/note is their only writer
  MODEL_STATE={pool:models.pool, assign:models.assign, endpoints:models.endpoints||[],
               kinds:models.kinds||["local","hosted"], hosted_hosts:models.hosted_hosts||[]};
  CONV_STATE={}; ((conv&&conv.repos)||[]).forEach(r=>{ CONV_STATE[r.path]=r; });
  applyCaps(data.capabilities);   // keep the "/" popup + counts in sync with admin edits
  SECRETS=(settings&&settings.secrets)||null;
  renderAdmin(data, models, el, (settings&&settings.settings)||{});
  refreshMcpHealth(data.mcps);
}

// MCP health is the one slow thing on this panel: the check shells out to `claude mcp list`, which
// contacts every server. /api/policy serves the CACHED health so the panel paints immediately, and
// this tops it up afterwards — a no-op while the cache is warm, ~8s on the first open of the hour.
// Re-renders only when the health actually moved, so a warm open never flickers, and never chains
// (the re-render's own refresh finds the cache fresh).
let MCP_REFRESHING=false;
async function refreshMcpHealth(rendered){
  if(MCP_REFRESHING) return;
  MCP_REFRESHING=true;
  const btn=document.getElementById("mcp-recheck"), label=btn&&btn.textContent;
  if(btn){ btn.disabled=true; btn.textContent="Checking…"; }
  const key=rows=>(rows||[]).map(m=>`${m.name}:${m.health||""}`).join("|");
  let moved=false;
  try{
    const d=await (await fetch("/api/mcp/recheck",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({force:false})})).json();
    moved=!!(d.mcps && key(d.mcps)!==key(rendered));
  }catch(e){}
  // Restore before deciding: on the re-render path loadAdmin() replaces this button anyway, but
  // relying on that leaves the label stuck at "Checking…" the moment that stops being true.
  if(btn){ btn.disabled=false; btn.textContent=label; }
  MCP_REFRESHING=false;
  if(moved) loadAdmin();
}

// Runtime settings (config._SETTING_SPECS): the "run an experiment today" knobs, editable here
// instead of only via .env + a restart of BOTH server.py and worker.py. Env precedence is real —
// an env-pinned knob renders locked, because a control whose clicks were silently overridden would
// be worse than no control. Edits apply to the NEXT run: OttoWorkflow snapshots these once at
// start (activities.snapshot_settings) so a live edit can never diverge an in-flight replay.
const SETTING_HELP={
  local_fallback:["Claude fallback for local models",
    "Off (strict) makes a local failure stop the run instead of being quietly covered by Claude. The verify judge always keeps its fallback."],
  max_attempts:["Verify attempts","Attempts the verify→retry ladder takes; the last one escalates to the strongest Claude tier."],
  conventions_digest_chars:["Repo conventions per judge prompt",
    "Characters of the target repo's own CLAUDE.md rules carried into ONE judging prompt. Sized to fit the repo's whole rule set — too low and the judge enforces whichever rules rank highest for the request rather than the ones the change touched. Lower it only for a small-context judge model."],
  plan_mode:["Plan-then-execute","Decompose a big task into ordered atomic steps. opt-in = only when a run asks; auto-local = whenever the executor is local."],
  supervise:["Run supervisor","Judge a run mid-attempt for going off-course."],
  supervise_mode:["Supervisor mode","enforce: kill and restart an off-course attempt. shadow: only record what it would have done."],
  budget_soft_tokens:["Soft budget — output tokens","Past this, attempts downshift to the cheapest tier. 0 = off."],
  budget_hard_tokens:["Hard budget — output tokens","Past this, the run stops and surfaces for a human. 0 = off."],
  budget_soft_usd:["Soft budget — USD","Downshift threshold in notional dollars. 0 = off."],
  budget_hard_usd:["Hard budget — USD","Stop ceiling in notional dollars. 0 = off."],
  max_qa_rounds:["Post-PR QA fix rounds","Times a failed QA verdict is folded back into a fix on the same branch."],
  max_review_rounds:["Post-PR review fix rounds","Times unaddressed review findings are folded back into a fix on the same branch."],
  max_plan_revisions:["Plan revision rounds","Times you can send feedback on the approval-gate plan preview before it re-plans. 0 hides the option."],
  memory_gc_batch_size:["Memory GC batch size","How many stored facts/approaches/rules the garbage collector classifies per LLM call."],
  memory_gc_max_verify:["Memory GC verify cap","Max real claude -p tool-verification turns one GC run spends on current-state claims; the rest wait for next time."],
  route_confirmations:["Router confirmations","Times the router re-samples a WRITE pick before it stands — a write route arms the approval gate and the Opus plan preview, and claude -p has no temperature, so one sample is a coin flip. The majority wins; a READ pick never re-samples. 1 = old behaviour."],
  judge_confirmations:["Judge confirmations","Times a verify FAIL or supervisor RETRY must repeat before it costs a retry — claude -p has no temperature, so one sample is a coin flip. A PASS never re-samples. 1 = old behaviour."],
  board_retention_h:["Finished cards on the board — hours","How long a finished run keeps its card in the Swarm board's Finished column. Cards outlive Temporal's own workflow retention: Otto archives each one when it closes and serves it from there afterwards. 0 = keep every archived card forever."],
  gate_timeout_h:["Approval gate deadline — hours","How long a run waits at the approval gate before giving up. Expiry DECLINES and tells whoever asked; it never approves. 0 = wait forever."],
  max_harness_retries:["Harness-death retries","Retries for an attempt that died in the harness (crash, timeout) instead of being judged. These don't spend a verify rung — no judge ever read the attempt."],
  supervise_steer:["Supervisor steering","Let the supervisor CORRECT a live attempt instead of only killing it: the correction is delivered into the running session, which keeps everything done so far and costs no verify attempt. shadow: record the corrections it would have sent, deliver none. off: the option is never offered."],
  max_supervisor_steers:["Supervisor steers per attempt","Corrections one attempt may be sent. Each one stays in the agent's context for the rest of the run, so this bounds how far the supervisor can rewrite the task. 0 = off."],
  max_supervisor_kills:["Supervisor kills per run","Times the supervisor may kill and restart an attempt mid-flight. Never on the final rung, where nothing is left for the critique to steer."],
  cap_local_latch_fails:["Local-model latch — consecutive fails","Judged failures on the same capability+model before that capability stops being tried on that local model in later runs."],
  cap_local_latch_ttl_s:["Local-model latch — expiry (seconds)","How long the latch holds before that capability gets one probationary run on the model again."],
  effort:["Effort level","How hard the model thinks before answering. Applies to execution attempts and the approval-gate plan preview — not to the cheap judge/routing calls, where there are ~10 per run and a text verdict gains nothing. Higher reasons longer and costs more; default lets each backend decide. A chat can override it in the composer. Advisory on local models: an endpoint that doesn't implement reasoning effort accepts the value and ignores it."],
};

/* Runtime settings in deliberate groups: twenty rows in store order is a wall you scan linearly
   for the one knob you came for. Any key not named here still renders, under "Other" — a
   hardcoded list must never be able to silently drop a setting the server grew. */
const SETTING_GROUPS=[
  ["Routing",              ["route_confirmations"]],
  ["Execution & fallback", ["effort","local_fallback","plan_mode","max_attempts","max_harness_retries"]],
  ["Approval gate",        ["gate_timeout_h","max_plan_revisions"]],
  ["Swarm board",          ["board_retention_h"]],
  ["Supervisor",           ["supervise","supervise_mode","max_supervisor_kills",
                            "supervise_steer","max_supervisor_steers"]],
  ["Cost budgets",         ["budget_soft_tokens","budget_hard_tokens","budget_soft_usd","budget_hard_usd"]],
  ["Verify & post-PR loops",["judge_confirmations","conventions_digest_chars",
                            "max_qa_rounds","max_review_rounds"]],
  ["Local-model latch",    ["cap_local_latch_fails","cap_local_latch_ttl_s"]],
  ["Memory GC",            ["memory_gc_batch_size","memory_gc_max_verify"]],
];

/* ---------------------------------------------------------------------------------------------
   Appearance: how this browser renders Otto. Deliberately NOT a runtime setting — settings.json
   is one Otto shared by every ingress and every viewer, while a palette is a property of the
   person looking at it, so it lives in localStorage and needs no server round trip.

   THEMES is the single registry: the picker renders from it, and every name here must have a
   matching CSS block above (Chocolate Truffle excepted — it IS :root). Guarded by
   test_core.ThemeUiTests, which compares the two lists.
   --------------------------------------------------------------------------------------------- */
const THEMES=[
  {id:"chocolate-truffle", name:"Chocolate Truffle",
   desc:"Warm cream and caramel over espresso ink. Otto's original."},
  {id:"lush-forest", name:"Lush Forest",
   desc:"Deep pine on a pale leaf ground."},
  {id:"wisteria-bloom", name:"Wisteria Bloom",
   desc:"Soft lavender under a violet ink."},
  {id:"ink-wash", name:"Ink Wash",
   desc:"Monochrome sumi charcoal. Dark."},
  {id:"blue-eclipse", name:"Blue Eclipse",
   desc:"Midnight blue and periwinkle. Dark."},
];

function appearanceSection(){
  const cur=currentTheme();
  const cards=THEMES.map(t=>`<button class="themecard${t.id===cur?' sel':''}" data-theme="${esc(t.id)}"
      title="${esc(t.name)} — click to switch. Applies immediately, to this browser only.">
    <span class="themebar"><span class="s1"></span><span class="s2"></span><span class="s3"></span><span class="s4"></span><span class="s5"></span><span class="s6"></span></span>
    <span class="themebody">
      <span class="themename">${esc(t.name)}<span class="cur">current</span></span>
      <span class="themedesc">${esc(t.desc)}</span>
    </span></button>`).join("");
  return `<div class="asection coll collapsed" data-sect="appearance"><h3><span class="secttoggle" title="collapse / expand">
      <span class="gcaret">&#9662;</span>Appearance<span class="sectcount" title="the palette this browser is using">${esc((THEMES.find(t=>t.id===cur)||THEMES[0]).name)}</span></span></h3>
    <div class="asection-body">
    <p class="sub" style="margin:0 0 8px">Stored in this browser, not on the server &mdash; every other viewer of this Otto keeps their own.</p>
    <div class="uigroup"><h4>Theme</h4>
      <p class="sub" style="margin:0">Each card previews itself: its swatches and text are drawn with the palette it would apply.</p>
      <div class="themegrid" id="theme-grid">${cards}</div></div>
    <div class="uigroup"><h4>Companion</h4>
      <p class="sub" style="margin:0 0 8px">Otto animates whatever the pipeline is doing &mdash; on every tab, including the ones that show no runs. Drag him wherever you want him; double-click sends him home. Dismissing him with his own &times; lands back here.</p>
      <label class="mtoggle${mascotShown()?"":" off"}"><span class="switch${mascotShown()?" on":""}" data-mascot="1" title="show / hide Otto"></span>Show Otto</label></div>
    </div></div>`;
}

/* Repaints the picker in place rather than re-rendering Admin: loadAdmin refetches six endpoints
   and would drop every open section, which is a lot of motion for a colour change. */
function pickTheme(id){
  try { localStorage.setItem(OTTO_THEME_KEY, id); } catch(e){}
  applyTheme(id);
  paintFavicon();          // the tab icon is part of the palette, and it cannot inherit one
  const grid=document.getElementById("theme-grid");
  if(!grid) return;
  grid.querySelectorAll(".themecard").forEach(c=>c.classList.toggle("sel",c.dataset.theme===id));
  const count=grid.closest(".asection").querySelector(".sectcount");
  if(count) count.textContent=(THEMES.find(t=>t.id===id)||THEMES[0]).name;
}

function settingsSection(settings){
  const setRow=([name,s])=>{
    const [label,help]=SETTING_HELP[name]||[name,""];
    const pinned=s.env_pinned;
    let ctl;
    if(s.kind==="bool"){
      ctl=`<span class="switch${s.value?" on":""}${pinned?" locked":""}" data-setting="${esc(name)}" data-kind="bool"></span>`;
    }else if(s.kind.startsWith("choice:")){
      ctl=`<select class="setsel" data-setting="${esc(name)}" data-kind="${esc(s.kind)}"${pinned?" disabled":""}>${
        s.kind.split(":")[1].split(",").map(o=>`<option value="${esc(o)}"${o===s.value?" selected":""}>${esc(o)}</option>`).join("")}</select>`;
    }else{
      const step=s.kind==="float"?"0.01":"1";
      ctl=`<input class="setnum" type="number" min="0" step="${step}" value="${esc(String(s.value))}" data-setting="${esc(name)}" data-kind="${esc(s.kind)}"${pinned?" disabled":""}>`;
    }
    const tag=pinned?`<span class="srctag" title="pinned by the ${esc(s.env)} environment variable — unset it in .env (and restart) to edit here">env-pinned</span>`
                    :(s.stored?`<span class="srctag" title="changed from the code default (${esc(String(s.default))})">edited</span>`:"");
    return `<tr class="arow setrow${pinned?" off":""}">
      <td><span class="setinfo"><span class="mn">${esc(label)}</span><small>${esc(help)}</small>
        <small class="setenv">${esc(s.env)}</small></span></td>
      <td class="c-src"><span class="settag">${tag}</span></td>
      <td class="c-val r"><span class="ctl r setctl">${ctl}</span></td></tr>`;
  };
  // group in the declared order, then sweep whatever is left into "Other" so a setting added
  // server-side still appears instead of vanishing from the panel
  const left=new Map(Object.entries(settings||{}));
  const grouped=SETTING_GROUPS.map(([title,keys])=>{
    const got=keys.filter(k=>left.has(k)).map(k=>{ const e=[k,left.get(k)]; left.delete(k); return e; });
    return got.length?`<tr class="setgroup"><td colspan="3">${esc(title)}</td></tr>`+got.map(setRow).join(""):"";
  }).join("");
  const rest=[...left.entries()];
  const rows=grouped+(rest.length?`<tr class="setgroup"><td colspan="3">Other</td></tr>`+rest.map(setRow).join(""):"");
  return `<div class="asection coll collapsed" data-sect="settings"><h3><span class="secttoggle" title="collapse / expand">
      <span class="gcaret">&#9662;</span>Runtime settings</span></h3>
    <div class="asection-body">
    <p class="sub" style="margin:0 0 8px">Applies to the <b>next</b> run. An <code>env-pinned</code> knob comes from <code>.env</code> and wins over anything set here.</p>
    <table class="ctable settable">
      <colgroup><col><col class="c-src"><col class="c-val"></colgroup>
      <thead><tr><th>Setting</th><th class="c-src">Source</th><th class="c-val r">Value</th></tr></thead>
      <tbody><tr class="setgroup"><td colspan="3">Secrets</td></tr>${secretRows()}${rows}</tbody></table></div></div>`;
}

/* Secrets are READ-ONLY here on purpose: this API has no auth, so a helper command settable over
   HTTP would be a helper command an attacker can set. Reports where each one came from — never a
   value, and never whether one that IS set is the right one. */
function secretRows(){
  const st=SECRETS;
  if(!st) return "";
  const via=Object.entries(st.secrets||{});
  const set=via.filter(([,s])=>s.set);
  const detail=via.map(([n,s])=>`${n}: ${s.source}`).join("\n");
  const val=st.command
    ? `<code>${esc(st.command)}</code>`
    : `<span class="sub">none — env / <code>.env</code> only</span>`;
  return `<tr class="arow setrow off">
    <td><span class="setinfo"><span class="mn">Secret provider</span>
      <small>Resolves the secrets still empty in <code>.env</code> from a password manager. Edit <code>.env</code> and restart — never settable from here.</small>
      <small class="setenv">OTTO_SECRET_COMMAND</small></span></td>
    <td class="c-src"><span class="settag"><span class="srctag" title="this one is env-only by design — the web API is unauthenticated, so an arbitrary command must not be writable over it">env-only</span></span></td>
    <td class="c-val r"><span class="ctl r setctl" title="${esc(detail)}">${val}
      <small class="sub">${set.length}/${via.length} secrets set</small></span></td></tr>`;
}

/* Health pill for one model: the LAST real outcome (a run's own call) or a probe. No entry at
   all is "nothing has used or probed this yet" — rendered blank, not as a scary "unknown". */
function modelHealthPill(name){
  const h=MODEL_HEALTH[name];
  if(!h) return "";
  const when=h.at?new Date(h.at*1000).toLocaleString():"";
  const src=h.via==="probe"?"reachability probe":"its last real call";
  return `<span class="hpill ${h.ok?'ok':'bad'}" title="${esc(`${src}${when?` · ${when}`:''}${h.detail?` — ${h.detail}`:''}`)}">${h.ok?'ok':'failing'}</span>`;
}
/* Broken models, scoped to the CURRENT pool — MODEL_HEALTH can still hold an entry for a model
   that was removed after it broke, and warning about a config that no longer exists is noise. */
function modelIssues(){
  return (((MODEL_STATE||{}).pool)||[]).filter(p=>MODEL_HEALTH[p.name]&&!MODEL_HEALTH[p.name].ok).length;
}
function refreshAdminBadge(){ setAdminBadge(MCP_ISSUES, modelIssues()); }

/* Endpoints: one OpenAI-compatible server, configured ONCE and shared by every model on it.
   The URL and key live here rather than on each model, so the server's second model is a pick
   out of "discover" and rotating its key is a single edit instead of one per model. */
function nHeaders(e){ return Object.keys(e.headers||{}).length; }

function endpointsBlock(m){
  const eps=m.endpoints||[];
  if(!eps.length) return "";      // nothing to explain: "+ add endpoint" lives in the section head
  // Same table idiom as the models below — its own header row is what makes it read as a sibling
  // section rather than an orphan fragment of the model table.
  const rows=eps.map(e=>{
    const used=(e.models||[]);
    return `<tr class="mrow">
      <td><span class="minfo"><span class="mn">${esc(e.name)}</span>
        <small>${esc(e.base_url||'')} &middot; ${esc(e.kind||'local')}${e.api_key_env?' &middot; key set':''}${nHeaders(e)?` &middot; ${nHeaders(e)} header${nHeaders(e)===1?'':'s'}`:''}</small></span></td>
      <td class="c-tag"><span class="srctag" title="${esc(used.join(', ')||'no models yet')}">${used.length}</span></td>
      <td class="c-test"><span class="mtest"><button class="addbtn" data-discover="${esc(e.name)}" title="ask this server which models it serves, and add them">discover</button>
        <button class="addbtn" data-editep="${esc(e.name)}" title="change the URL, key, kind or headers — every model on this endpoint follows">edit</button></span></td>
      <td class="c-rm r"><button class="remove" data-delep="${esc(e.name)}" title="remove this endpoint and its models">&times;</button></td></tr>`;
  }).join("");
  return `<table class="ctable modtable eptable">
    <colgroup><col><col class="c-tag"><col class="c-test"><col class="c-rm"></colgroup>
    <thead><tr><th title="an OpenAI-compatible server (vLLM / Ollama / a hosted API): its URL, key and any extra headers are set once here, and every model on it inherits them">Endpoint</th>
      <th class="c-tag" title="how many models run on this endpoint">Models</th><th class="c-test"></th><th class="c-rm"></th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function modelsSection(m){
  // The tag is the model's CLASS (gateway.model_kind: claude | hosted | local), a stored fact on
  // its endpoint — never derived from `provider`, which only says the request travels as
  // OpenAI-compatible HTTP and would tag a vendor's frontier model the same as a 4B on a laptop.
  const plabel=p=>kindOf(p);
  const pname=p=>p.provider==="claude"?(p.name.replace(/^claude[-\s]?/i,"")||p.name):p.name;
  const radio=(p,phase)=>{
    const nonClaude=p.provider!=="claude";   // TRANSPORT: dispatched over OpenAI-compatible HTTP
    // PREVIEW used to be DISABLED for a non-Claude model: only `claude -p --permission-mode
    // plan` could write a plan, so a pick there was a silent substitution — sonnet ran, the
    // radio kept showing the model that never did. Otto now has plan mode of its own on the
    // local backend (plans._local_preview), so the pick is real and only needs a warning: this
    // is the one phase with no verify rung above it, and its output is what a human approves.
    // memory_gc is HALF servable (batch classifier yes, live tool-check no), so it warns too.
    let title='';
    if(nonClaude && phase==="execution")
      title=' title="runs execution on Otto&#39;s own agent runtime (OpenAI tool-calling) instead of claude -p — no Claude escalation: retries stay on this model. Caps needing a repo .mcp.json stay on Claude."';
    else if(nonClaude && phase==="preview")
      title=' title="writes the approval plan on Otto&#39;s own read-only tool loop instead of claude -p --permission-mode plan (same Read/Grep/Glob + scoped gh reads, enforced by Otto). This phase has NO retry ladder above it and its output is what you approve — a weak model here is signed, not caught."';
    else if(nonClaude && phase==="memory_gc")
      title=' title="the batch classifier runs on this model; the live tool-verification turn cannot (it needs claude -p with read-only tools) and falls back to sonnet."';
    return `<span class="rc"><input type="radio" name="as-${phase}" value="${esc(p.name)}" ${m.assign[phase]===p.name?'checked':''}${title}></span>`;
  };
  const PHASE_HELP=[
    ["Route","Router #1 — picks which capability handles the request (LLM choice over the catalogue)"],
    ["Split","Swarm planner (the DECOMPOSE stage) — decides if a request is several independent sub-tasks and fans them out. NOT the approval preview — that is Plan"],
    ["Plan","Preview (the PLAN stage) — writes the plan you approve at the write gate. Runs once with no retry ladder above it, so this is the model whose judgement you are actually reading"],
    ["Clar","Clarify — decides whether to ask a clarifying question before running"],
    ["Mem","Memory — distils durable facts & solutions from a finished run to inject into later ones"],
    ["Ver","Verify — judges whether an attempt actually satisfied the request; a fail drives retry/escalate"],
    ["Sup","Supervise — watches a run live mid-attempt and can kill & steer one going off-course"],
    ["GC","Memory GC — the on-demand memory scan (Memory tab): classifies every stored fact/solution/rule KEEP/STALE/VERIFY, then re-checks the VERIFY ones live with read-only tools. This is the model that decides which memories are proposed for deletion; the live check needs Claude, so a local pick there falls back to sonnet"],
    ["Exec","Execution — runs the capability itself. Claude runs claude -p (full skills/agents/MCP); a local model runs Otto's own runtime instead — no Claude, no MCP, no escalation"],
  ];
  const head=`<thead><tr><th>Model</th><th class="c-tag">Type</th>
    <th class="c-health">Health</th>
    <th class="c-phases"><span class="mradios">${PHASE_HELP.map(([l,t])=>`<span class="rc h" title="${esc(t)}">${l}</span>`).join("")}</span></th>
    <th class="c-turns" title="local agent runtime's per-run turn budget (model call + tool round = one turn) — blank uses the global default (60), raise for a stronger model that needs more room">Turns</th>
    <th class="c-test">Check</th><th class="c-rm"></th></tr></thead>`;
  const rows=m.pool.map(p=>`<tr class="mrow">
      <td><span class="minfo"><span class="mn">${plabel(p)} · ${esc(pname(p))}</span>
        <small>${p.provider==='claude'?esc(p.model):esc(p.endpoint||p.base_url||'')+' · '+esc(p.model||'')}</small></span></td>
      <td class="c-tag"><span class="srctag" title="${p.provider==='claude'?'runs through claude -p':'kind is set on the endpoint (edit it to change)'}">${esc(kindOf(p))}</span></td>
      <td class="c-health" data-mhealth="${esc(p.name)}">${modelHealthPill(p.name)}</td>
      <td class="c-phases"><span class="mradios">${radio(p,'routing')}${radio(p,'plan')}${radio(p,'preview')}${radio(p,'clarify')}${radio(p,'memory')}${radio(p,'verify')}${radio(p,'supervise')}${radio(p,'memory_gc')}${radio(p,'execution')}</span></td>
      <td class="c-turns">${p.provider!=='claude'?`<input type="number" min="1" step="1" data-turns="${esc(p.name)}" value="${p.max_turns||''}" placeholder="60">`:''}</td>
      <td class="c-test"><span class="mtest"><button class="addbtn testbtn" data-testmodel="${esc(p.name)}">test</button><span class="tres" data-tres="${esc(p.name)}"></span></span></td>
      <td class="c-rm r">${p.provider!=='claude'?`<button class="remove" data-delmodel="${esc(p.name)}" title="remove">&times;</button>`:''}</td>
    </tr>`).join("");
  const gs=(typeof GATEWAY_STATS!=="undefined"&&GATEWAY_STATS)||{tasks:{},down:{}};
  const fb=Object.entries(gs.tasks||{}).filter(([,v])=>(v.calls||0)>0)
    .map(([t,v])=>{const pct=Math.round(100*(v.fallbacks||0)/v.calls);
      return `<span class="memstat" title="${v.fallbacks||0} of ${v.calls} ${esc(t)} calls fell back to Claude">${esc(t)} <b class="${pct>25?'gold':''}">${pct}%</b></span>`;})
    .join(" ");
  const down=Object.entries(gs.down||{})
    .map(([n,s])=>`<span class="memstat" title="marked down after a failure; skipped for ${s}s more">&#9888; ${esc(n)} down</span>`).join(" ");
  // What the judges cost. Every tier call here (verify, supervise, routing, clarify, plan
  // critique, memory) is spend the AUDIT TRAIL cannot hold — an audit row exists only for an
  // execution attempt — so the scorecard's "avg $" is execution-only and this is the missing
  // half. Shown next to the fallback rate because both answer "what is this tier costing me".
  const oh=gs.overhead_usd||0;
  const ohtip=Object.entries(gs.tasks||{}).filter(([,v])=>(v.cost_usd||0)>0)
    .sort((a,b)=>(b[1].cost_usd||0)-(a[1].cost_usd||0))
    .map(([t,v])=>`${t} $${(v.cost_usd||0).toFixed(2)}`).join(", ");
  const ohbadge=oh>0?`<span class="memstat" title="Claude spend on judging rather than executing${ohtip?` \u2014 ${ohtip}`:''}. Counted apart from the audit trail, which only records execution attempts.">judging <b>$${oh.toFixed(2)}</b></span>`:"";
  const badge=(fb||down||ohbadge)?`<div class="memhead" style="margin:6px 0 2px">
      <span class="memstat">Claude-fallback rate</span> ${fb} ${ohbadge} ${down}</div>`:"";
  // Broken models, named. Rendered OUTSIDE .asection-body (like the MCP one) so it survives the
  // section being collapsed — which it is by default, and a warning you have to expand to see is
  // the failure mode this whole thing exists to fix.
  const bad=(m.pool||[]).filter(p=>MODEL_HEALTH[p.name]&&!MODEL_HEALTH[p.name].ok);
  // What the broken model is USED for is the actionable half — and the per-capability pin is the
  // one that bites hardest (every attempt of that cap runs on the dead endpoint) while the phase
  // table above still looks perfectly healthy.
  const usedBy=p=>{
    const phases=Object.entries(m.assign||{}).filter(([,n])=>n===p.name).map(([t])=>t);
    const caps=Object.entries({...(m.cap_exec||{}),...(m.cap_local_exec||{})}).filter(([,n])=>n===p.name).map(([c])=>c);
    const parts=[];
    if(phases.length) parts.push(phases.join(", "));
    if(caps.length) parts.push(caps.join(", "));
    return parts.length?` &mdash; used by ${esc(parts.join(" · "))}`:" &mdash; not assigned to anything";
  };
  const warn=bad.length?`<div class="mcpwarn"><span class="msg"><b>${bad.length}</b> LLM model${bad.length>1?'s':''}
      ${bad.length>1?'are':'is'} failing. ${bad.map(p=>`<code>${esc(p.name)}</code>${usedBy(p)}${MODEL_HEALTH[p.name].detail?`: ${esc(MODEL_HEALTH[p.name].detail)}`:''}`).join("<br>")}
      <br>Whatever ${bad.length>1?'they run':'it runs'} keeps working only while Claude covers the call. Fix ${bad.length>1?'them':'it'} below, then Recheck.</span></div>`:'';
  return `<div class="asection coll collapsed" data-sect="models"><h3><span class="secttoggle" title="collapse / expand">
      <span class="gcaret">&#9662;</span>LLM models<span class="sectcount">${m.pool.length}</span></span>
    <span class="h3btns"><button class="addbtn" id="model-recheck" title="re-test every model for reachability — Claude entries included, so this costs one claude -p turn each">&#8635; Recheck health</button>
    <button class="addbtn" id="add-endpoint" title="an OpenAI-compatible server (vLLM / Ollama / a hosted API): set its URL, key and any extra headers once, then add its models with discover">+ add endpoint</button>
    <button class="addbtn" id="add-model">+ add model</button></span></h3>
    ${warn}
    <div id="model-form"></div>
    <div class="asection-body">
    ${endpointsBlock(m)}
    <p class="sub" style="margin:2px 0 8px">One model per phase — hover a column header for what it does. The cheap phases take a local model happily.</p>
    ${badge}
    <table class="ctable modtable mpool">
      <colgroup><col><col class="c-tag"><col class="c-health"><col class="c-phases"><col class="c-turns"><col class="c-test"><col class="c-rm"></colgroup>
      ${head}<tbody>${rows}</tbody></table></div></div>`;
}

function wireModels(el){
  el.querySelectorAll('.mradios input[type=radio]').forEach(r=>r.addEventListener("change",()=>{
    MODEL_STATE.assign[r.name.slice(3)]=r.value; saveModels();   // as-routing -> routing
  }));
  el.querySelectorAll("[data-delmodel]").forEach(b=>b.addEventListener("click",async()=>{
    if(!confirm(`Remove model "${b.dataset.delmodel}"?`)) return;
    MODEL_STATE.pool=MODEL_STATE.pool.filter(p=>p.name!==b.dataset.delmodel);
    const fallback=(MODEL_STATE.pool.find(p=>p.provider==='claude')||MODEL_STATE.pool[0]).name;
    for(const t in MODEL_STATE.assign){ if(MODEL_STATE.assign[t]===b.dataset.delmodel) MODEL_STATE.assign[t]=fallback; }
    await saveModels(); loadAdmin();
  }));
  el.querySelectorAll("[data-turns]").forEach(inp=>inp.addEventListener("change",()=>{
    const p=MODEL_STATE.pool.find(x=>x.name===inp.dataset.turns);
    if(!p) return;
    const raw=inp.value.trim();
    if(!raw){ delete p.max_turns; saveModels(); return; }
    const n=parseInt(raw,10);
    if(!Number.isInteger(n)||n<1){ inp.value=p.max_turns||""; return; }   // reject silently, restore last-good value
    p.max_turns=n; saveModels();
  }));
  el.querySelectorAll("[data-testmodel]").forEach(b=>b.addEventListener("click",async()=>{
    const name=b.dataset.testmodel, res=el.querySelector(`[data-tres="${name}"]`);
    res.className="tres"; res.textContent="…";
    try {
      const r=await (await fetch("/api/models/test",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name})})).json();
      res.className="tres "+(r.ok?"ok":"bad");
      res.textContent=r.ok?`✓ ${r.ms||0}ms`:`✗ ${r.detail||"failed"}`;
      res.title=r.detail||"";
      // The server recorded this outcome as model health, so the pill and the tab badge move
      // with it — otherwise a test that just went red leaves a green pill beside it. Patched in
      // place rather than via loadAdmin(), which would re-render the view over the result the
      // user just asked for.
      MODEL_HEALTH[name]={ok:!!r.ok, detail:r.detail||"", at:Date.now()/1000, via:"probe"};
      const cell=el.querySelector(`[data-mhealth="${name}"]`);
      if(cell) cell.innerHTML=modelHealthPill(name);
      refreshAdminBadge();
    } catch(e){ res.className="tres bad"; res.textContent="✗ "+e.message; }
  }));
  const recheck=document.getElementById("model-recheck");
  if(recheck) recheck.addEventListener("click",async()=>{
    recheck.disabled=true; const was=recheck.textContent; recheck.textContent="checking…";
    try{
      const r=await (await fetch("/api/models/recheck",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"})).json();
      MODEL_HEALTH=r.health||{};
    } catch(e){}
    recheck.disabled=false; recheck.textContent=was;
    loadAdmin();
  });
  const add=document.getElementById("add-model");
  if(add) add.addEventListener("click",showModelForm);
  const addEp=document.getElementById("add-endpoint");
  if(addEp) addEp.addEventListener("click",()=>showEndpointForm());
  el.querySelectorAll("[data-editep]").forEach(b=>b.addEventListener("click",()=>showEndpointForm(b.dataset.editep)));
  el.querySelectorAll("[data-discover]").forEach(b=>b.addEventListener("click",()=>showDiscovery(b.dataset.discover)));
  el.querySelectorAll("[data-delep]").forEach(b=>b.addEventListener("click",async()=>{
    const name=b.dataset.delep;
    const on=MODEL_STATE.pool.filter(p=>p.endpoint===name).map(p=>p.name);
    // Deleting the endpoint out from under its models would leave them dialling nowhere, and the
    // only symptom is a failing health pill — so remove them together, named, or not at all.
    if(!confirm(on.length?`Remove endpoint "${name}" and its ${on.length} model(s): ${on.join(", ")}?`
                         :`Remove endpoint "${name}"?`)) return;
    MODEL_STATE.pool=MODEL_STATE.pool.filter(p=>p.endpoint!==name);
    MODEL_STATE.endpoints=(MODEL_STATE.endpoints||[]).filter(e=>e.name!==name);
    repointAssignments(on);
    await saveModels(); loadAdmin();
  }));
}

/* Any phase/capability still pointing at a removed model must land somewhere real, or the next
   run resolves nothing. Mirrors the single-model delete path. */
function repointAssignments(gone){
  if(!gone.length) return;
  const fallback=(MODEL_STATE.pool.find(p=>p.provider==='claude')||MODEL_STATE.pool[0]||{}).name;
  for(const t in MODEL_STATE.assign){ if(gone.includes(MODEL_STATE.assign[t])) MODEL_STATE.assign[t]=fallback; }
}

/* An endpoint's KIND — what the model behind it is. `hosted` is a frontier model behind a
   vendor API; `local` is a weak model on a box we run, which is what the write latch, the
   cross-run cap latch and the "fix the local server" copy were written for. The guess only
   PRE-SELECTS from the server's host list (gateway.HOSTED_HOSTS via /api/models) so the two
   sides can never disagree; the operator's pick is what is stored. */
const kindOf=p=>p.provider==="claude"?"claude":(p.kind==="hosted"?"hosted":"local");
function epKindGuess(url){
  let host=""; try{ host=new URL(url||"").hostname.toLowerCase(); }catch(_){ return "local"; }
  return (MODEL_STATE.hosted_hosts||[]).some(h=>host===h||host.endsWith("."+h))?"hosted":"local";
}
function kindSelect(id,cur){
  const tip={local:"a model on a server you run (vLLM / Ollama / llama.cpp): weak-model safeguards apply — a write that fails verification escalates to Claude, and a capability that keeps failing here is latched off it",
             hosted:"a frontier model behind a vendor API (OpenAI, OpenRouter, Together…): no weak-model safeguards — retries stay on it like any strong model"};
  return `<select id="${id}" title="what the model behind this endpoint is — every model on the endpoint shares it">`
    +(MODEL_STATE.kinds||["local","hosted"]).map(k=>`<option value="${k}" ${k===cur?"selected":""} title="${esc(tip[k]||"")}">${k}</option>`).join("")+`</select>`;
}
function bindKindGuess(urlId,kindId){
  const u=document.getElementById(urlId), k=document.getElementById(kindId);
  if(!u||!k) return;
  let touched=false;
  k.addEventListener("change",()=>{ touched=true; });
  u.addEventListener("input",()=>{ if(!touched) k.value=epKindGuess(u.value); });
}

function showEndpointForm(name){
  const c=document.getElementById("model-form");
  const e=(MODEL_STATE.endpoints||[]).find(x=>x.name===name)||{};
  c.innerHTML=`<div class="aform">
    <label>Name</label><input id="ep-name" placeholder="e.g. gpu-box  &middot;  deepseek" value="${esc(e.name||"")}">
    <label>Base URL (OpenAI-compatible)</label><input id="ep-url" placeholder="http://localhost:11434/v1" value="${esc(e.base_url||"")}">
    <label>Kind</label>${kindSelect("ep-kind", e.kind||(name?"local":epKindGuess(e.base_url)))}
    <label>API key (or the name of an env var holding it)</label><input id="ep-key" placeholder="blank for a local server" value="${esc(e.api_key_env||"")}">
    <label>Extra headers (optional, one per line)</label><textarea id="ep-headers" rows="3" placeholder="X-Tenant: sre&#10;X-Api-Key: VLLM_GATEWAY_KEY" title="sent on every call to this endpoint — models, chat and embeddings alike. A value that names an env var (or an OTTO_SECRET_COMMAND secret) resolves to it, so a credential need not be stored here.">${esc(headerLines(e.headers))}</textarea>
    <div class="ferr" id="ep-err"></div>
    <div class="factions"><button class="btn approve" id="ep-save">${name?"Save endpoint":"Add endpoint"}</button>
      <button class="btn decline" id="ep-cancel">Cancel</button></div>
  </div>`;
  document.getElementById("ep-cancel").onclick=()=>c.innerHTML="";
  if(!name) bindKindGuess("ep-url","ep-kind");   // a NEW endpoint follows the URL until the operator picks
  document.getElementById("ep-save").onclick=async()=>{
    const err=document.getElementById("ep-err");
    const nm=val("ep-name"), url=val("ep-url");
    if(!nm||!url){ err.textContent="name and base URL are required"; return; }
    if(nm!==name && (MODEL_STATE.endpoints||[]).some(x=>x.name===nm)){ err.textContent="an endpoint with that name already exists"; return; }
    const hdrs=parseHeaders(document.getElementById("ep-headers").value);
    if(hdrs===null){ err.textContent="each header needs a name: one line is missing its 'Name: value'"; return; }
    const ep={name:nm, base_url:url, kind:val("ep-kind"), api_key_env:val("ep-key"), headers:hdrs};
    MODEL_STATE.endpoints=MODEL_STATE.endpoints||[];
    const i=MODEL_STATE.endpoints.findIndex(x=>x.name===name);
    if(i<0) MODEL_STATE.endpoints.push(ep);
    else {
      MODEL_STATE.endpoints[i]=ep;
      // A rename must carry its models with it — the reference is the name.
      MODEL_STATE.pool.forEach(p=>{ if(p.endpoint===name) p.endpoint=nm; });
    }
    await saveModels(); loadAdmin();
  };
}

/* Ask an endpoint what it serves and add the picks as pool entries. The point of the whole
   feature: the URL and key are already configured, so a new model costs two clicks. */
async function showDiscovery(epName){
  const c=document.getElementById("model-form");
  // Not .sub on the body: that caps width at 760px (prose measure) and a model id is not prose.
  c.innerHTML=`<div class="aform discpanel"><label>Models on <b>${esc(epName)}</b></label>
    <div id="disc-body"><span class="sub">asking the server…</span></div></div>`;
  let r;
  try {
    r=await (await fetch("/api/models/discover",{method:"POST",headers:{"Content-Type":"application/json"},
                                                 body:JSON.stringify({endpoint:epName})})).json();
  } catch(err){ r={ok:false,detail:err.message}; }
  const body=document.getElementById("disc-body");
  if(!body) return;
  if(!r.ok){ body.innerHTML=`<span class="tres bad">&#10007; ${esc(r.detail||"discovery failed")}</span>`; return; }
  if(!(r.models||[]).length){ body.innerHTML="the server lists no models"; return; }
  const have=new Set(MODEL_STATE.pool.filter(p=>p.endpoint===epName).map(p=>p.model));
  // One row per MODEL, not per id: a served alias and its canonical repo path are the same
  // weights (grouped on `root` server-side). Ids render verbatim in mono — upper-casing them
  // made two genuinely different models read as one repeated.
  const ctx=n=>n?` &middot; ${n>=1024?Math.round(n/1024)+"k":n} ctx`:"";
  const added=e=>have.has(e.id)||(e.aliases||[]).some(a=>have.has(a));
  const dropped=(r.served||r.models.length)-r.models.length;
  body.innerHTML=`<p class="sub" style="margin:0 0 6px">${r.models.length} model${r.models.length===1?'':'s'}${dropped?` &middot; ${dropped} alias${dropped===1?'':'es'} folded in`:""}${have.size?` &middot; ${have.size} already added`:""}</p>
    <div class="disclist">${r.models.map(e=>`<label class="discrow">
      <input type="checkbox" value="${esc(e.id)}" ${added(e)?'disabled':''}><code>${esc(e.id)}</code>
      <span class="disalias">${(e.aliases||[]).map(esc).join(" · ")}${ctx(e.context)}</span>
      ${added(e)?'<span class="dishave">added</span>':''}</label>`).join("")}</div>
    <div class="ferr" id="disc-err"></div>
    <div class="factions"><button class="btn approve" id="disc-add">Add selected</button>
      <button class="btn decline" id="disc-cancel">Cancel</button></div>`;
  document.getElementById("disc-cancel").onclick=()=>c.innerHTML="";
  document.getElementById("disc-add").onclick=async()=>{
    const picks=[...body.querySelectorAll("input[type=checkbox]:checked")].map(i=>i.value);
    if(!picks.length){ document.getElementById("disc-err").textContent="nothing selected"; return; }
    const taken=new Set(MODEL_STATE.pool.map(p=>p.name));
    picks.forEach(id=>{
      // A model id is the natural name; the same id served by two endpoints needs the endpoint
      // to tell them apart (pool names are the key every assignment references).
      let name=id, n=2;
      if(taken.has(name)) name=`${id} (${epName})`;
      while(taken.has(name)) name=`${id} (${epName}) ${n++}`;
      taken.add(name);
      MODEL_STATE.pool.push({name, provider:"openai", endpoint:epName, model:id});
    });
    await saveModels(); loadAdmin();
  };
}


/* An endpoint's optional extra headers are edited as `Name: value` lines — the shape a curl
   -H reads like, and the only one that round-trips a dict without a nested row editor.
   parseHeaders returns null on a line with no name, so the form can say so instead of
   silently dropping it. */
function headerLines(h){ return Object.entries(h||{}).map(([k,v])=>`${k}: ${v}`).join("\n"); }
function parseHeaders(text){
  const out={};
  for(const raw of (text||"").split("\n")){
    const line=raw.trim(); if(!line) continue;
    const i=line.indexOf(":");
    if(i<1) return null;
    out[line.slice(0,i).trim()]=line.slice(i+1).trim();
  }
  return out;
}

function showModelForm(){
  const c=document.getElementById("model-form");
  const eps=MODEL_STATE.endpoints||[];
  c.innerHTML=`<div class="aform">
    <label>Type</label>
    <select id="lm-prov"><option value="claude">Claude (claude -p)</option><option value="openai">OpenAI-compatible endpoint (local or hosted)</option></select>
    <label>Name</label><input id="lm-name" placeholder="e.g. claude-opus  ·  local-qwen">
    <label>Model id</label><input id="lm-model" placeholder="claude-opus-4-8  ·  qwen3-coder:30b">
    <div id="lm-local" style="display:none;flex-direction:column;gap:10px">
      <label>Endpoint</label>
      <select id="lm-ep">${eps.map(e=>`<option value="${esc(e.name)}">${esc(e.name)} · ${esc(e.base_url||"")}</option>`).join("")}<option value="">+ new endpoint…</option></select>
      <div id="lm-newep" style="display:${eps.length?"none":"flex"};flex-direction:column;gap:10px">
        <label>Endpoint name</label><input id="lm-epname" placeholder="e.g. gpu-box">
        <label>Base URL (OpenAI-compatible)</label><input id="lm-url" placeholder="http://localhost:11434/v1">
        <label>Kind</label>${kindSelect("lm-kind","local")}
        <label>API key (optional — an env var name, or the key itself)</label><input id="lm-key" placeholder="blank for local servers">
        <label>Extra headers (optional, one per line)</label><textarea id="lm-headers" rows="2" placeholder="X-Tenant: sre" title="sent on every call to this endpoint; a value naming an env var resolves to it"></textarea>
      </div>
      <label>Max turns (optional)</label><input id="lm-turns" type="number" min="1" placeholder="blank = default 60 — raise for a stronger model, e.g. a hosted API">
    </div>
    <div class="ferr" id="lm-err"></div>
    <div class="factions"><button class="btn approve" id="lm-save">Add model</button><button class="btn decline" id="lm-cancel">Cancel</button></div>
  </div>`;
  const prov=document.getElementById("lm-prov"), localDiv=document.getElementById("lm-local");
  const epSel=document.getElementById("lm-ep"), newEp=document.getElementById("lm-newep");
  prov.onchange=()=>{ localDiv.style.display = prov.value==="openai" ? "flex" : "none"; };
  epSel.onchange=()=>{ newEp.style.display = epSel.value ? "none" : "flex"; };
  bindKindGuess("lm-url","lm-kind");
  if(!eps.length) epSel.value="";
  document.getElementById("lm-cancel").onclick=()=>c.innerHTML="";
  document.getElementById("lm-save").onclick=async()=>{
    const err=document.getElementById("lm-err");
    const name=val("lm-name"), model=val("lm-model");
    if(!name||!model){ err.textContent="name and model id are required"; return; }
    if(MODEL_STATE.pool.some(p=>p.name===name)){ err.textContent="a model with that name already exists"; return; }
    let m;
    if(prov.value==="claude"){ m={name,provider:"claude",model}; }
    else {
      let ep=epSel.value;
      if(!ep){   // "+ new endpoint" — define it once here, then every later model just picks it
        const url=val("lm-url"); ep=val("lm-epname")||url;
        if(!url){ err.textContent="base URL is required for a new endpoint"; return; }
        if((MODEL_STATE.endpoints||[]).some(e=>e.name===ep)){ err.textContent="an endpoint with that name already exists"; return; }
        const hdrs=parseHeaders(document.getElementById("lm-headers").value);
        if(hdrs===null){ err.textContent="each header needs a name: one line is missing its 'Name: value'"; return; }
        MODEL_STATE.endpoints=[...(MODEL_STATE.endpoints||[]), {name:ep, base_url:url, kind:val("lm-kind"), api_key_env:val("lm-key"), headers:hdrs}];
      }
      m={name,provider:"openai",endpoint:ep,model};
      const turns=val("lm-turns");
      if(turns){
        const n=parseInt(turns,10);
        if(!Number.isInteger(n)||n<1){ err.textContent="max turns must be a positive integer"; return; }
        m.max_turns=n;
      }
    }
    MODEL_STATE.pool.push(m); await saveModels(); loadAdmin();
  };
}

async function saveModels(){
  await fetch("/api/models",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(MODEL_STATE)});
  const s=document.getElementById("saved"); if(s){ s.classList.add("show"); setTimeout(()=>s.classList.remove("show"),1200); }
}


function renderAdmin(data, models, el, settings){
  // ONE Execution control per capability. It used to be three (an exec dropdown, a local
  // dropdown and a TOOL-FREE pill) whose interaction was invisible: the tool-free pair silently
  // took attempt 1 away from whatever exec: was set to, so "exec: local + local: <same model>"
  // read as belt-and-braces while actually meaning "first attempt has NO tools and the ladder
  // never reaches Claude". The three stores are unchanged (cap_exec, cap_local_exec, the policy
  // tool_free flag) — they're just written together as one choice, so they cannot contradict
  // each other. Option values: "" | "exec|<pool model>" | "toolfree|<local model>".
  const claudeModels=(models.pool||[]).filter(p=>p.provider==='claude');
  const localModels=(models.pool||[]).filter(p=>p.provider!=='claude');
  const capExec=models.cap_exec||{}, capLocal=models.cap_local_exec||{};
  const mshort=p=>p.name.replace(/^claude[-\s]?/i,"")||p.name;
  // The stored trio read back as one value. cap_exec wins the same way engine.run_attempt
  // resolves it, and tool-free only counts where the engine honours it (read risk, issue #42).
  const capBackend=c=> capExec[c.name] ? "exec|"+capExec[c.name]
    : (capLocal[c.name] && c.tool_free && c.risk==='read' ? "toolfree|"+capLocal[c.name] : "");
  const execSelect=c=>{
    const cur=capBackend(c);
    const opt=(v,label,tip,off)=>`<option value="${esc(v)}" ${cur===v?'selected':''} ${off?'disabled':''} title="${esc(tip)}">${esc(label)}</option>`;
    let opts=opt("","default","runs on the phase-level execution model via claude -p — full skills/agents/MCP fidelity");
    if(claudeModels.length)
      opts+=`<optgroup label="Claude · claude -p">`+claudeModels.map(p=>
        opt("exec|"+p.name,mshort(p),"pin this capability to "+p.name+" instead of the phase-level execution model")).join("")+`</optgroup>`;
    // A cap that needs a claude.ai connector (Gmail/Calendar/Slack) CANNOT run on the local
    // backend — their OAuth lives inside Claude Code, so there is nothing for mcp_client to
    // spawn. Refuse the pick here, with the blocking servers named: unguarded it looked like a
    // valid choice and failed three attempts and ~1.1M tokens later (sre-secretary on DeepSeek,
    // 2026-08-04). stdio servers (New Relic, k8s, Grafana, AWS, Vanta) are served, so those caps
    // are unaffected.
    const blockers=(c.local_blockers||[]);
    // A LATCHED pairing is the opposite provenance to a blocker: not declared up front,
    // earned by failing verification three times running on that model. Shown per-model,
    // because the latch is keyed that way — sre-pm works on one local model and not
    // another. Deliberately NOT disabled: the latch expires on its own and a composer pick
    // overrides it, so greying the row out would misstate both.
    const latched=(c.local_latched||[]);
    const localTip=blockers.length
      ? "unavailable: this capability needs MCP servers the local backend cannot serve ("
        +blockers.join(", ")+"). claude.ai connectors are authenticated inside Claude Code, so "
        +"only the Claude backend can reach them."
      : "";
    localModels.forEach(p=>{
      const isLatched=latched.indexOf(p.name)>=0;
      const latchTip=isLatched
        ? "latched: this capability failed verification on "+p.name+" three times running, "
          +"so runs fall back to Claude until it is re-tested (automatically, or via the "
          +"Clear button). Picking it in the composer still runs it here."
        : "";
      opts+=`<optgroup label="${esc(p.name)} · ${esc(kindOf(p))}${blockers.length?' · unavailable':(isLatched?' · latched':'')}">`
        +opt("exec|"+p.name,"with tools · never Claude",
             localTip||latchTip||"runs on Otto's local agent runtime: real tools, MCP for stdio servers, and NO Claude — retries and the final escalation stay on this model, so a struggling run surfaces to you instead of falling back",
             blockers.length>0)
        // Rendered for write caps too, just disabled — the engine ignores tool_free off a write
        // cap anyway, and a greyed row explains the restriction where an absent one wouldn't.
        // It also means flipping the risk pill only has to toggle `disabled`, never re-render.
        +opt("toolfree|"+p.name,"text-only · Claude backstop",
             "attempt 1 is a single completion with NO tools at all (summarize / classify / draft only) — if it fails verification the ladder escalates to Claude. Read-risk capabilities only; pick 'with tools' for anything that has to read a file, run a command or call gh.",
             c.risk!=='read' || blockers.length>0)
        +`</optgroup>`;
    });
    // The latch's escape hatch. Rendered ONLY while a latch is live, beside the control it
    // affects: an automatic fallback nobody can see is one nobody can undo, and the operator who
    // just fixed the capability should not have to wait out the TTL to prove it.
    const clearBtn=latched.length
      ? `<button class="latchclear" data-cap="${esc(c.name)}" title="This capability is latched off `
        +`${esc(latched.join(', '))} after three failed verifications in a row. Clear to give the `
        +`local backend another chance on the next run.">latched \u21ba</button>`
      : '';
    return `<select class="capexec ${cur?'set':''}" data-cap="${esc(c.name)}" title="which model runs this capability, and on which backend">${opts}</select>${clearBtn}`;
  };
  // Per-capability reliability scorecard (issue #102), aggregated from the audit trail — shown
  // right beside the exec dropdown so a "downgrade this cap to a local model" decision has
  // evidence. Only rendered when the cap has judged runs; colour by verify pass rate.
  const scoreChip=c=>{
    const s=SCORECARD[c.name];
    if(!s||!s.runs) return '';
    const pass=Math.round(s.pass_rate*100);
    const cls=pass>=80?'ok':(pass>=50?'warn':'danger');
    // Compact visible chip — the full breakdown (runs, esc, fallback, cost, models) is in the tooltip.
    const parts=[`${pass}% · ~${s.avg_attempts} try`];
    const models=Object.entries(s.models||{}).map(([m,n])=>`${m} x${n}`).join(", ");
    const tip=`${s.runs} judged runs - verify pass ${pass}% - avg ${s.avg_attempts} attempts`
      +(s.avg_attempts_to_pass!=null?` (${s.avg_attempts_to_pass} to pass)`:'')
      +` - escalated ${Math.round(s.escalation_rate*100)}% - fell back to Claude ${Math.round(s.fallback_rate*100)}%`
      +` - ~${s.avg_output_tokens} output tokens/run - avg $${s.avg_cost_usd}`
      +(models?` - exec: ${models}`:'')+(s.last_at?` - last ${s.last_at.slice(0,10)}`:'');
    return `<span class="scorechip ${cls}" title="${esc(tip)}">${esc(parts.join(' · '))}</span>`;
  };
  // How many times this capability has actually run (every distinct run in the audit trail,
  // judged or not — so resumes and unjudged runs count, unlike the reliability chip's `runs`).
  const usedCell=c=>{
    const s=SCORECARD[c.name], used=(s&&s.used)||0;
    if(!used) return `<span class="usedchip zero" title="never run">0</span>`;
    const tip=`${used} run${used===1?'':'s'} (all time)`
      +(s.runs?`, ${s.runs} judged`:`, none judged — every run was a resume/continuation`)
      +(s.last_at?` · last ${s.last_at.slice(0,10)}`:'');
    return `<span class="usedchip" title="${esc(tip)}">${used}</span>`;
  };
  // Small badge marking where a cap comes from: bundled with Otto, tied to one repo
  // (source="project", carries cap.cwd), or discovered from the user's own ~/.claude
  // (source="builtin" — works across every repo, not scoped to any one of them).
  const sourceTag=c=>{
    if(c.source==='stock') return '<span class="stocktag" title="ships bundled with Otto, works without your own ~/.claude">stock</span>';
    if(c.source==='project') return '<span class="stocktag repo" title="belongs to a specific project repo">repo</span>';
    if(c.source==='builtin') return '<span class="stocktag user" title="discovered from your own ~/.claude, not tied to any one repo">user</span>';
    return '';
  };
  // One capability row. `search` carries lowercased name+description so the live filter can
  // match without re-reading the DOM text. The description is ellipsised by the cell's fixed
  // width, so it MUST carry a title= or the truncated tail is unreadable.
  const capRowHtml=c=>`
    <tr class="arow ${c.enabled?'':'off'}" data-cap="${esc(c.name)}" data-search="${esc((c.name+' '+(c.description||'')).toLowerCase())}">
      <td><span class="nm" title="${esc(c.name)}">${esc(c.name)}${sourceTag(c)}${c.description?`<small title="${esc(c.description)}">${esc(c.description)}</small>`:''}</span></td>
      <td class="c-used r"><span class="cused">${usedCell(c)}</span></td>
      <td class="c-score"><span class="cscore">${scoreChip(c)}</span></td>
      <td class="c-exec"><span class="cmodel">${execSelect(c)}</span></td>
      <td class="c-risk"><span class="riskpill ${c.risk}" data-cap="${esc(c.name)}" title="click to flip read/write">${c.risk}</span></td>
      <td class="c-on"><span class="switch ${c.enabled?'on':''}" data-cap="${esc(c.name)}" title="enable / disable"></span></td>
      <td class="c-act r"><span class="ctl r cact">
        ${c.source==='otto'?`<button class="remove edit" data-editcap="${esc(c.name)}" title="edit">&#9998;</button><button class="remove" data-delcap="${esc(c.name)}" title="remove">&times;</button>`:''}
      </span></td>
    </tr>`;
  // One table shell per group / plugin sub-group. Identical fixed column widths in every one,
  // so the columns line up across groups even though each renders its own <table>.
  const CAP_COLS=`<colgroup><col><col class="c-used"><col class="c-score"><col class="c-exec"><col class="c-risk"><col class="c-on"><col class="c-act"></colgroup>`;
  const CAP_HEAD=`<thead><tr><th>Capability</th>
    <th class="c-used r" title="how many times this capability has run (all time, from the audit trail)">Runs</th>
    <th class="c-score">Reliability</th>
    <th class="c-exec">Execution</th><th class="c-risk">Risk</th><th class="c-on">On</th><th class="c-act"></th></tr></thead>`;
  const capTable=(rows,head=true)=>`<table class="ctable captable">${CAP_COLS}${head?CAP_HEAD:''}<tbody>${rows}</tbody></table>`;
  // Bucket each capability into one category (mirrors the old data-group logic).
  // Stock AND project caps fold into the Agents/Skills section they conceptually belong to
  // (a "stock"/"repo" badge on the row marks them, see sourceTag/capRowHtml) rather than
  // sitting in their own separate section — same treatment for both, so a repo-scoped skill
  // sits next to the global skills it competes with at routing time, not off in its own silo.
  const catOf=c=> c.source==='stock' ? (c.stock_kind==='skill'?'skill':'agent') : (c.source==='project' ? (c.kind==='skill'?'skill':'agent') : (c.plugin ? 'plugin' : (c.kind==='agent'?'agent':(c.kind==='skill'?'skill':'custom'))));
  const onCount=list=>list.filter(c=>c.enabled).length;
  // Header shared by category groups and plugin sub-groups: caret, label, count, "N on",
  // and a bulk enable/disable link (toggles every cap in the set).
  const bulkLink=(key,list,kind)=>{
    const allOn=list.length && onCount(list)===list.length;
    return `<span class="gon">${onCount(list)}/${list.length} on</span>`+
           `<button class="linkbtn capbulk" data-bulk="${esc(key)}" data-bkind="${kind}">${allOn?'disable all':'enable all'}</button>`;
  };
  const CATS=[['agent','Agents'],['skill','Skills'],['plugin','Plugin skills'],['custom','Custom']];
  const capListHtml=CATS.map(([key,label])=>{
    const list=data.capabilities.filter(c=>catOf(c)===key);
    if(!list.length) return "";
    // Every category starts collapsed so the panel opens tidy — just the headers + counts.
    const collapsed='collapsed';
    let body;
    if(key==='plugin'){
      // Sub-group plugin skills by their plugin so a whole noisy plugin folds in one click.
      const byPlugin={};
      list.forEach(c=>{ (byPlugin[c.plugin]=byPlugin[c.plugin]||[]).push(c); });
      // Header once for the whole category — repeating it per plugin would out-shout the rows.
      body=`<table class="ctable captable caphead">${CAP_COLS}${CAP_HEAD}</table>`+
        Object.keys(byPlugin).sort().map(pl=>{
          const sub=byPlugin[pl];
          return `<div class="asub collapsed" data-sub="${esc(pl)}">
            <div class="asub-head"><span class="asub-caret">&#9662;</span><span class="subname">${esc(pl)}</span>
              <span class="gcount">${sub.length}</span>${bulkLink('plugin:'+pl,sub,'cap')}</div>
            <div class="asub-body">${capTable(sub.map(capRowHtml).join(""),false)}</div>
          </div>`;
        }).join("");
    } else {
      body=capTable(list.map(capRowHtml).join(""));
    }
    return `<div class="agroup ${collapsed}" data-grp="${key}">
      <div class="agroup-head"><span class="gcaret">&#9662;</span><span class="glabel">${label}</span>
        <span class="gcount">${list.length}</span>${bulkLink('cat:'+key,list,'cap')}</div>
      <div class="agroup-body">${body}</div>
    </div>`;
  }).join("");
  const HLABEL={connected:"connected",failed:"failed",needs_auth:"needs auth",pending:"pending",unknown:"unknown"};
  const mcpUnhealthy=m=>m.enabled&&m.confirmed!==false&&["failed","needs_auth","pending"].includes(m.health);
  // `claude mcp login` fixes AUTH (HTTP/SSE/connector), not a broken stdio binary/env — only
  // offer Reconnect where it can actually help; a failed local server just shows its pill.
  const mcpCanReconnect=m=>m.enabled&&(m.health==="needs_auth"||(m.health==="failed"&&m.source==="connector"));
  const mcpIssues=data.mcps.filter(mcpUnhealthy).length;
  MCP_ISSUES=mcpIssues;
  setAdminBadge(mcpIssues, modelIssues());
  // An Otto-registered server is stored INACTIVE: its command line is spawned as the operator on
  // the next run that uses it, so the human confirming it has to SEE the argv, not just a name.
  const pending=m=>m.source==="otto"&&m.confirmed===false;
  const mcpRows=data.mcps.map(m=>`
    <tr class="arow ${m.enabled&&!pending(m)?'':'off'}">
      <td><span class="nm">${esc(m.display||m.name)}</span>
        ${pending(m)?`<div class="mcppending"><span class="abadge warn">not activated</span>
          <code class="mcpcmd">${esc(m.command||"")}</code>
          <button class="mcpbtn go" data-actmcp="${esc(m.name)}"
            title="allow Otto to spawn this exact command on runs that use this server">Activate</button>
          </div>`:''}</td>
      <td class="c-health"><span class="ctl">
        ${m.health?`<span class="hpill ${esc(m.health)}" title="last claude mcp list health check">${HLABEL[m.health]||esc(m.health)}</span>`:''}
        ${mcpCanReconnect(m)?`<button class="mcpbtn" data-reconnect="${esc(m.name)}" title="claude mcp login — opens a browser to re-authenticate this server">Reconnect</button>`:''}
      </span></td>
      <td class="c-src"><span class="srctag">${m.source}</span></td>
      <td class="c-note"><div class="mcpnote">
        <textarea class="mn-text" data-mcpnote="${esc(m.name)}" rows="2"
          title="e.g. 'always pass region=us-east-1' — read by the model before it calls this server. It cannot fix a server that fails to start."
          placeholder="How to drive this server (not discoverable from its tool schemas)"></textarea>
        <button class="clearbtn mn-save" data-mcpnote="${esc(m.name)}">Save</button>
      </div></td>
      <td class="c-on"><span class="switch ${m.enabled?'on':''}" data-mcp="${esc(m.name)}" title="enable / disable"></span></td>
      <td class="c-act r">${m.source==='otto'?`<button class="remove" data-delmcp="${esc(m.name)}" title="remove">&times;</button>`:''}</td>
    </tr>`).join("");
  const mcpTable=`<table class="ctable mcptable">
    <colgroup><col><col class="c-health"><col class="c-src"><col><col class="c-on"><col class="c-act"></colgroup>
    <thead><tr><th>Server</th><th class="c-health">Health</th><th class="c-src">Source</th>
      <th title="Instructions the model reads before calling this server's tools — the setup or usage step its schemas don't state. Prose cannot fix a server that fails to START: that needs a wrapper in its own command.">Usage notes</th>
      <th class="c-on">On</th><th class="c-act"></th></tr></thead>
    <tbody>${mcpRows}</tbody></table>`;
  const projectRows=(data.projects||[]).map(p=>{
    const meta=(data.project_meta||{})[p]||{};
    return `
    <tr class="arow">
      <td class="c-repo"><span class="nm">${esc(p.split('/').pop()||p)}<small title="${esc(meta.url||p)}">${esc(meta.url||p)}</small>${meta.url&&meta.checkout?`<small class="sub" title="Otto clones from this local checkout instead of the remote, and write-denies it">local: ${esc(meta.checkout)}</small>`:''}</span></td>
      <td class="c-conv">${convCell(p)}</td>
      <td class="c-instr"><div class="projinstr">
        <textarea class="pi-text" data-path="${esc(p)}" rows="2"
          title="e.g. 'tag all resources team=sre; never touch prod without asking'"
          placeholder="Standing instructions — injected into every run in this repo"></textarea>
        <button class="clearbtn pi-save" data-path="${esc(p)}" style="margin-left:0">Save</button>
      </div></td>
      <td class="c-ns"><span class="srctag" title="memory namespace">ns: ${esc(meta.namespace||'')}</span></td>
      <td class="c-act r"><button class="remove" data-delproject="${esc(p)}" title="remove">&times;</button></td>
    </tr>`;
  }).join("");
  const projectTable=projectRows?`<table class="ctable projtable">
    <colgroup><col class="c-repo"><col class="c-conv"><col><col class="c-ns"><col class="c-act"></colgroup>
    <thead><tr><th class="c-repo">Repo</th>
      <th class="c-conv" title="Hard rules distilled from this repo's own CLAUDE.md and given to the judge. Ranked against each request, so only the most relevant fit one prompt.">Conventions</th>
      <th>Standing instructions</th>
      <th class="c-ns">Memory namespace</th><th class="c-act"></th></tr></thead>
    <tbody>${projectRows}</tbody></table>`
    :`<p class="sub" style="margin:6px 0 0">No project repos imported yet.</p>`;
  const extraWrite=data.writeTools.filter(t=>!data.readTools.includes(t));
  el.innerHTML=`
    <div class="phead"><h1>Configuration</h1>
      <p class="sub"><b style="color:var(--accent)">Read</b> caps run on their own; <b style="color:var(--warn)">write</b> caps wait for approval — click a risk pill to flip it. Hover any control for what it does.</p>
      <span class="pside saved" id="saved">saved ✓</span></div>
    ${appearanceSection()}
    ${modelsSection(models)}
    ${settingsSection(settings)}
    <div class="asection coll collapsed" data-sect="caps"><h3><span class="secttoggle" title="collapse / expand">
        <span class="gcaret">&#9662;</span>Capabilities<span class="sectcount" title="enabled / discovered — the header counts only the enabled ones, this table lists them all">${data.capabilities.filter(c=>c.enabled).length} / ${data.capabilities.length}</span></span>
      <button class="addbtn" id="add-cap">+ capability</button></h3>
      <div id="cap-form"></div>
      <div class="asection-body">
      <div class="captools">
        <input id="cap-search" class="capsearch" type="text" placeholder="Search ${data.capabilities.length} capabilities by name or description…" autocomplete="off">
        <button class="linkbtn" id="cap-expand">expand all</button>
        <button class="linkbtn" id="cap-collapse">collapse all</button>
      </div>
      <div id="cap-list">${capListHtml}</div>
      <p class="capnone" id="cap-empty" hidden>No capabilities match your search.</p></div></div>
    <div class="asection coll collapsed" data-sect="mcp"><h3><span class="secttoggle" title="collapse / expand"><span class="gcaret">&#9662;</span>MCP servers<span class="sectcount">${data.mcps.length}</span></span>
      <span class="h3btns"><button class="addbtn" id="mcp-recheck" title="re-run claude mcp list (health check)">&#8635; Recheck health</button>
      <button class="addbtn" id="add-mcp">+ MCP server</button></span></h3>
      ${mcpIssues?`<div class="mcpwarn"><span class="msg"><b>${mcpIssues}</b> enabled MCP server${mcpIssues>1?'s':''} ${mcpIssues>1?'are':'is'} unreachable or ${mcpIssues>1?'need':'needs'} authentication &mdash; runs that use ${mcpIssues>1?'them':'it'} may fail. Fix ${mcpIssues>1?'them':'it'} below, then Recheck.</span></div>`:''}
      <div id="mcp-form"></div>
      <div class="asection-body">${mcpTable}</div></div>
    <div class="asection coll collapsed" data-sect="projects"><h3><span class="secttoggle" title="collapse / expand"><span class="gcaret">&#9662;</span>Project repos<span class="sectcount">${(data.projects||[]).length}</span></span><button class="addbtn" id="add-project">+ project repo</button></h3>
      <div id="project-form"></div>
      <div class="asection-body">
      <p class="sub" style="margin:10px 0 10px">Registered by remote URL — Otto keeps its own clone unless you point at a local checkout. Skills &amp; subagents from a repo's <code>.claude/</code> dir are imported and run from its root, so the repo's <code>.mcp.json</code> resolves. Listed above as <code>&lt;repo&gt;:&lt;name&gt;</code>.</p>
      ${projectTable}</div></div>
    <div class="asection coll collapsed" data-sect="share"><h3><span class="secttoggle" title="collapse / expand">
        <span class="gcaret">&#9662;</span>Share extensions</span></h3>
      <div class="asection-body">
      <p class="sub" style="margin:10px 0 10px">Export or import custom capabilities &amp; MCP servers. Built-ins are never overwritten, clashes are renamed, and <b>MCP secret values are blanked</b> — set them after importing.</p>
      <div class="bundlebtns">
        <button class="addbtn" id="export-bundle">⤓ Export bundle</button>
        <button class="addbtn" id="import-bundle">⤒ Import bundle</button>
        <input type="file" id="bundle-file" accept="application/json,.json" hidden>
        <span class="bundlemsg" id="bundle-msg"></span>
      </div>
      </div>
    </div>
    <div class="asection coll collapsed" data-sect="tools"><h3><span class="secttoggle" title="collapse / expand">
        <span class="gcaret">&#9662;</span>Base tool allowlists</span></h3>
      <div class="asection-body">
      <table class="ctable tooltable">
        <colgroup><col class="c-kind"><col></colgroup>
        <thead><tr><th class="c-kind">Risk</th><th>Tools granted</th></tr></thead>
        <tbody>
          <tr><td class="c-kind">Read may use</td><td class="wrap"><div class="toolchips">${data.readTools.map(t=>`<span>${esc(t)}</span>`).join("")}</div></td></tr>
          <tr><td class="c-kind">Write adds</td><td class="wrap"><div class="toolchips">${extraWrite.map(t=>`<span>${esc(t)}</span>`).join("")}</div></td></tr>
          <tr><td class="c-kind">Every enabled MCP adds</td><td class="wrap"><div class="toolchips"><span>mcp__&lt;name&gt;</span></div></td></tr>
        </tbody></table>
      </div>
    </div>`;
  el.querySelectorAll(".themecard").forEach(c=>c.addEventListener("click",()=>pickTheme(c.dataset.theme)));
  el.querySelectorAll(".riskpill").forEach(p=>p.addEventListener("click",()=>{
    const n=p.dataset.cap, nx=POLICY_STATE.capabilities[n].risk==="read"?"write":"read";
    POLICY_STATE.capabilities[n].risk=nx; p.className="riskpill "+nx; p.textContent=nx;
    // A write is never tool-free (apply_policy enforces this too), so the Execution control's
    // text-only rows go dead with the flip — and if one was the live choice it has to be
    // unpicked in the STORE, not just in the DOM, or cap_local_exec keeps a dangling entry.
    const sel=p.closest(".arow").querySelector(".capexec");
    if(sel){
      sel.querySelectorAll('option[value^="toolfree|"]').forEach(o=>{ o.disabled = nx==="write"; });
      if(nx==="write" && sel.value.startsWith("toolfree|")){ sel.value=""; setCapBackend(sel); }
    }
    queueSave();
  }));
  // Recompute the "N/M on" label + bulk-link text for a group/sub-group from its live switches.
  const syncContainer=cont=>{
    if(!cont) return;
    const head=cont.querySelector(":scope > .agroup-head, :scope > .asub-head");
    if(!head) return;
    const rows=cont.querySelectorAll(".arow");
    const on=[...rows].filter(r=>r.querySelector(".switch.on")).length;
    const gon=head.querySelector(".gon"); if(gon) gon.textContent=`${on}/${rows.length} on`;
    const bulk=head.querySelector(".capbulk"); if(bulk) bulk.textContent=(rows.length&&on===rows.length)?'disable all':'enable all';
  };
  const syncAncestors=row=>{ syncContainer(row.closest(".asub")); syncContainer(row.closest(".agroup")); };
  el.querySelectorAll(".switch").forEach(s=>s.addEventListener("click",()=>{
    if(s.dataset.setting) return;          // runtime settings save through saveSetting(), not queueSave
    if(s.dataset.mascot){                  // browser-local like the theme - never a server setting
      const on=!s.classList.contains("on");
      s.classList.toggle("on",on); s.closest(".mtoggle").classList.toggle("off",!on);
      showMascot(on);
      return;
    }
    const on=!s.classList.contains("on"); s.classList.toggle("on",on);
    const row=s.closest(".arow"); row.classList.toggle("off",!on);
    if(s.dataset.cap){ POLICY_STATE.capabilities[s.dataset.cap].enabled=on; syncAncestors(row); }
    if(s.dataset.mcp) POLICY_STATE.mcps[s.dataset.mcp].enabled=on;
    queueSave();
  }));
  // Runtime settings: each control saves ITSELF (one POST per edit), unlike the debounced bulk
  // policy save — these are few, consequential, and each one is a deliberate act.
  const saveSetting=async(name,value,ctl)=>{
    try{
      const r=await fetch("/api/settings",{method:"POST",headers:{"Content-Type":"application/json"},
                                           body:JSON.stringify({settings:{[name]:value}})});
      const j=await r.json();
      const row=ctl.closest(".setrow"), tag=row&&row.querySelector(".settag");
      const s=(j.settings||{})[name];
      if(tag) tag.innerHTML=(s&&s.stored)?`<span class="srctag" title="changed from the code default (${esc(String(s.default))})">edited</span>`:"";
      const saved=document.getElementById("saved");
      if(saved){ saved.classList.add("show"); setTimeout(()=>saved.classList.remove("show"),1200); }
    }catch(e){ alert("Couldn't save setting: "+e.message); }
  };
  el.querySelectorAll('.switch[data-setting]').forEach(s=>s.addEventListener("click",()=>{
    if(s.classList.contains("locked")) return;     // env-pinned: the click would be discarded
    const on=!s.classList.contains("on");
    s.classList.toggle("on",on);
    s.closest(".setrow").classList.toggle("off",!on);
    saveSetting(s.dataset.setting,on,s);
  }));
  el.querySelectorAll(".setsel").forEach(sel=>sel.addEventListener("change",()=>
    saveSetting(sel.dataset.setting,sel.value,sel)));
  el.querySelectorAll(".setnum").forEach(inp=>inp.addEventListener("change",()=>{
    // Empty / negative reads as 0 = "meter disabled", matching the server-side coercion.
    const v=inp.value===""?0:Math.max(0,inp.dataset.kind==="float"?parseFloat(inp.value):parseInt(inp.value,10));
    if(Number.isNaN(v)){ inp.value="0"; }
    inp.value=String(Number.isNaN(v)?0:v);
    saveSetting(inp.dataset.setting,inp.value,inp);
  }));
  // Collapse / expand a category group or a plugin sub-group (ignore clicks on the bulk link).
  el.querySelectorAll(".agroup-head, .asub-head").forEach(h=>h.addEventListener("click",e=>{
    if(e.target.closest(".capbulk")) return;
    h.parentElement.classList.toggle("collapsed");
  }));
  // Section-level collapse: the title toggles its .asection.coll. Which sections you left open is
  // remembered, so Admin doesn't re-collapse everything on every reload/refresh.
  const SECTS="otto.admin.sections";
  const sread=()=>{try{return JSON.parse(localStorage.getItem(SECTS))||{};}catch(e){return{};}};
  const swrite=st=>{try{localStorage.setItem(SECTS,JSON.stringify(st));}catch(e){}};
  const remembered=sread();
  el.querySelectorAll(".asection.coll[data-sect]").forEach(sec=>{
    if(remembered[sec.dataset.sect]) sec.classList.remove("collapsed");
  });
  el.querySelectorAll(".secttoggle").forEach(t=>t.addEventListener("click",()=>{
    const sec=t.closest(".asection"), st=sread();
    const open=!sec.classList.toggle("collapsed");
    if(open) st[sec.dataset.sect]=1; else delete st[sec.dataset.sect];
    swrite(st);
  }));
  // A "+ add" / recheck button in a collapsed header would otherwise reveal a form below a hidden
  // body — expand the section so the result is actually visible.
  el.querySelectorAll(".asection.coll h3 .addbtn").forEach(b=>b.addEventListener("click",()=>{
    const sec=b.closest(".asection");
    if(!sec.classList.contains("collapsed")) return;
    sec.classList.remove("collapsed");
    const st=sread(); st[sec.dataset.sect]=1; swrite(st);
  }));
  const setAllCollapsed=v=>el.querySelectorAll("#cap-list .agroup, #cap-list .asub").forEach(g=>g.classList.toggle("collapsed",v));
  const exp=el.querySelector("#cap-expand"), col=el.querySelector("#cap-collapse");
  if(exp) exp.addEventListener("click",()=>setAllCollapsed(false));
  if(col) col.addEventListener("click",()=>setAllCollapsed(true));
  // Bulk enable/disable every capability inside a group or sub-group.
  el.querySelectorAll(".capbulk").forEach(b=>b.addEventListener("click",e=>{
    e.stopPropagation();
    const cont=b.closest(".asub")||b.closest(".agroup");
    const rows=[...cont.querySelectorAll(".arow")];
    const turnOn=!rows.every(r=>r.querySelector(".switch.on"));   // any off -> enable all; all on -> disable all
    rows.forEach(r=>{
      const sw=r.querySelector(".switch"), name=sw&&sw.dataset.cap;
      if(!name) return;
      sw.classList.toggle("on",turnOn); r.classList.toggle("off",!turnOn);
      POLICY_STATE.capabilities[name].enabled=turnOn;
    });
    syncContainer(b.closest(".asub")); syncContainer(b.closest(".agroup"));
    if(b.closest(".asub")) syncContainer(cont.closest(".agroup"));
    queueSave();
  }));
  // Live search across all categories: filter rows, hide empty groups, auto-expand matches.
  const capSearch=el.querySelector("#cap-search"), capEmpty=el.querySelector("#cap-empty");
  if(capSearch) capSearch.addEventListener("input",()=>{
    const q=capSearch.value.trim().toLowerCase();
    let any=false;
    el.querySelectorAll("#cap-list .arow").forEach(r=>{
      const hit=!q||(r.dataset.search||"").includes(q);
      r.classList.toggle("hide",!hit); if(hit) any=true;
    });
    el.querySelectorAll("#cap-list .asub").forEach(s=>{
      const has=[...s.querySelectorAll(".arow")].some(r=>!r.classList.contains("hide"));
      s.classList.toggle("hide",!has);
      s.classList.toggle("collapsed", q?!has:true);            // expand matching subs while searching
    });
    el.querySelectorAll("#cap-list .agroup").forEach(g=>{
      const has=[...g.querySelectorAll(".arow")].some(r=>!r.classList.contains("hide"));
      g.classList.toggle("hide",!has);
      g.classList.toggle("collapsed", q?!has:true);   // searching expands matches; cleared collapses all
    });
    if(capEmpty) capEmpty.hidden=any;
  });
  el.querySelectorAll(".capexec").forEach(s=>s.addEventListener("change",()=>setCapBackend(s)));
  el.querySelectorAll(".latchclear").forEach(b=>b.addEventListener("click",async()=>{
    b.disabled=true; b.textContent="clearing\u2026";
    try{ await api("/api/cap-latch/clear",{name:b.dataset.cap}); }catch(e){}
    loadAdmin();   // the button is gone once the latch is, so re-render rather than hide
  }));
  el.querySelectorAll("[data-editcap]").forEach(b=>b.addEventListener("click",()=>{
    const cap=(data.capabilities||[]).find(c=>c.name===b.dataset.editcap);
    if(cap) showCapForm(cap);
  }));
  el.querySelectorAll("[data-delcap]").forEach(b=>b.addEventListener("click",()=>removeItem("/api/capability/remove",{name:b.dataset.delcap})));
  el.querySelectorAll("[data-delmcp]").forEach(b=>b.addEventListener("click",()=>removeItem("/api/mcp/remove",{name:b.dataset.delmcp})));
  el.querySelectorAll("[data-actmcp]").forEach(b=>b.addEventListener("click",async()=>{
    b.disabled=true; b.textContent="Activating\u2026";
    try{ await fetch("/api/mcp/activate",{method:"POST",headers:{"Content-Type":"application/json"},
                                          body:JSON.stringify({name:b.dataset.actmcp})}); }catch(e){}
    loadAdmin();
  }));
  const recheck=document.getElementById("mcp-recheck");
  if(recheck) recheck.addEventListener("click",async()=>{
    recheck.disabled=true; const t=recheck.textContent; recheck.textContent="Checking…";
    try{ await fetch("/api/mcp/recheck",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"}); }catch(e){}
    loadAdmin();   // re-renders from the now-fresh cached health
  });
  el.querySelectorAll("[data-reconnect]").forEach(b=>b.addEventListener("click",async()=>{
    b.disabled=true; b.textContent="Opening…";
    let d={};
    try{ d=await (await fetch("/api/mcp/reconnect",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({name:b.dataset.reconnect})})).json(); }catch(e){ d={error:e.message}; }
    if(d.ok){ b.textContent="Finish in browser →"; b.title="Complete sign-in in the browser window, then click Recheck health"; }
    else { b.disabled=false; b.textContent="Reconnect"; alert("Couldn't start reconnect: "+(d.error||"unknown error")); }
  }));
  // MCP usage notes: values set from JS (quotes/newlines), and the textarea is found by walking
  // the cells rather than an attribute selector — CSS.escape() is for identifiers, and a quoted
  // [data-x="..."] selector would inject backslashes the stored name doesn't have.
  const mnBox=name=>Array.from(el.querySelectorAll(".mn-text")).find(t=>t.dataset.mcpnote===name);
  el.querySelectorAll(".mn-text").forEach(t=>{
    const row=data.mcps.find(m=>m.name===t.dataset.mcpnote)||{};
    t.value=row.notes||"";
  });
  el.querySelectorAll(".mn-save").forEach(b=>b.addEventListener("click",async()=>{
    const ta=mnBox(b.dataset.mcpnote), name=b.dataset.mcpnote;
    b.disabled=true; b.textContent="Saving…";
    try{
      const r=await (await fetch("/api/mcp/note",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({name, notes:ta?ta.value:""})})).json();
      if(r.error) throw new Error(r.error);
      // Keep the render source in step, so a re-render of this table shows what was saved.
      if(ta) ta.value=r.notes||"";
      const row=data.mcps.find(m=>m.name===name); if(row) row.notes=r.notes||"";
      b.textContent="Saved ✓";
    }catch(e){ b.textContent="Save failed"; }
    finally{ setTimeout(()=>{ b.disabled=false; b.textContent="Save"; },1200); }
  }));
  bindConv(el);
  el.querySelectorAll("[data-delproject]").forEach(b=>b.addEventListener("click",()=>removeItem("/api/project/remove",{path:b.dataset.delproject})));
  // per-project instructions (issue #69): set values (may contain quotes/newlines) + wire save
  el.querySelectorAll(".pi-text").forEach(t=>{ const m=(data.project_meta||{})[t.dataset.path]||{}; t.value=m.instructions||""; });
  el.querySelectorAll(".pi-save").forEach(b=>b.addEventListener("click",async()=>{
    const ta=el.querySelector(`.pi-text[data-path="${(window.CSS&&CSS.escape)?CSS.escape(b.dataset.path):b.dataset.path}"]`);
    b.disabled=true; b.textContent="Saving…";
    try { await fetch("/api/project/instructions",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({path:b.dataset.path, instructions:ta?ta.value:""})}); b.textContent="Saved ✓"; }
    catch(e){ b.textContent="Save failed"; }
    finally { setTimeout(()=>{ b.disabled=false; b.textContent="Save instructions"; }, 1200); }
  }));
  document.getElementById("add-cap").addEventListener("click",()=>showCapForm());
  document.getElementById("add-mcp").addEventListener("click",showMcpForm);
  document.getElementById("add-project").addEventListener("click",showProjectForm);
  wireBundle(el);
  wireModels(el);
}

async function exportBundle(){
  const data=await (await fetch("/api/bundle/export")).json();
  const blob=new Blob([JSON.stringify(data,null,2)],{type:"application/json"});
  const a=document.createElement("a");
  a.href=URL.createObjectURL(blob); a.download="otto-bundle.json";
  document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(a.href);
  const n=(data.capabilities||[]).length, m=Object.keys(data.mcp_servers||{}).length;
  setBundleMsg(`exported ${n} capability(ies) + ${m} MCP server(s) → otto-bundle.json`);
}

function wireBundle(el){
  const file=el.querySelector("#bundle-file");
  el.querySelector("#export-bundle").addEventListener("click",exportBundle);
  el.querySelector("#import-bundle").addEventListener("click",()=>{ file.value=""; file.click(); });
  file.addEventListener("change",async()=>{
    const f=file.files[0]; if(!f) return;
    let bundle;
    try { bundle=JSON.parse(await f.text()); }
    catch(e){ setBundleMsg("not valid JSON", true); return; }
    let res, data;
    try { res=await fetch("/api/bundle/import",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(bundle)});
          data=await res.json(); }
    catch(e){ setBundleMsg("import failed: "+e.message, true); return; }
    if(!res.ok||data.error){ setBundleMsg("import failed: "+(data.error||res.status), true); return; }
    const caps=(data.capabilities_added||[]).length, mcps=(data.mcps_added||[]).length;
    const renamed=[...(data.capabilities_renamed||[]),...(data.mcps_renamed||[])];
    let msg=`imported ${caps} capability(ies) + ${mcps} MCP server(s)`;
    if(renamed.length) msg+=` · renamed: ${renamed.map(r=>`${r.from}→${r.to}`).join(", ")}`;
    if((data.needs_env||[]).length) msg+=` · set env for: ${data.needs_env.join(", ")}`;
    await loadAdmin();        // re-render the lists first (rebuilds the panel)…
    setBundleMsg(msg);        // …then post the summary into the fresh element
  });
}

function setBundleMsg(text, bad){
  const m=document.getElementById("bundle-msg");
  if(!m) return;
  m.textContent=text; m.className="bundlemsg"+(bad?" bad":" ok");
}

async function removeItem(path, body){
  if(!confirm(`Remove "${body.name||body.path}"?`)) return;
  await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  loadAdmin();
}

async function postForm(path, body, errEl){
  const res=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const data=await res.json();
  if(!res.ok||data.error){ errEl.textContent=data.error||("HTTP "+res.status); return false; }
  return true;
}

function showCapForm(cap){
  const editing = !!(cap && cap.source==='otto');
  const c=document.getElementById("cap-form");
  c.innerHTML=`<div class="aform">
    <label>Name${editing?' (read-only)':''}</label><input id="cf-name" placeholder="e.g. summarize-pr" ${editing?'readonly':''}>
    <label>Description (used for routing)</label><input id="cf-desc" placeholder="what it does + when to use it">
    <div class="frow">
      <div><label>Risk</label><select id="cf-risk"><option value="read">read (auto-runs)</option><option value="write">write (needs approval)</option></select></div>
    </div>
    <label>Prompt — use {request} where the user's text goes</label>
    <textarea id="cf-prompt" placeholder="Summarize the following GitHub PR and list risks:\n{request}"></textarea>
    <div class="ferr" id="cf-err"></div>
    <div class="factions"><button class="btn approve" id="cf-save">${editing?'Save changes':'Add capability'}</button><button class="btn decline" id="cf-cancel">Cancel</button></div>
  </div>`;
  if(editing){
    document.getElementById("cf-name").value=cap.name;
    document.getElementById("cf-desc").value=cap.description||"";
    document.getElementById("cf-prompt").value=cap.prompt||"";
    document.getElementById("cf-risk").value=cap.risk||"write";
  }
  document.getElementById("cf-cancel").onclick=()=>c.innerHTML="";
  document.getElementById("cf-save").onclick=async()=>{
    const ok=await postForm(editing?"/api/capability/edit":"/api/capability/add",{
      name:document.getElementById("cf-name").value.trim(),
      description:document.getElementById("cf-desc").value.trim(),
      risk:document.getElementById("cf-risk").value,
      prompt:document.getElementById("cf-prompt").value,
    },document.getElementById("cf-err"));
    if(ok) loadAdmin();
  };
}

function showMcpForm(){
  const c=document.getElementById("mcp-form");
  c.innerHTML=`<div class="aform">
    <label>Name</label><input id="mf-name" placeholder="e.g. github">
    <label>Command</label><input id="mf-cmd" placeholder="npx">
    <label>Arguments (one per line)</label><textarea id="mf-args" placeholder="-y&#10;@modelcontextprotocol/server-github"></textarea>
    <p class="sub" style="margin:2px 0 0">Added servers are stored <b>inactive</b> — Otto spawns this command on your machine, so you activate it from the list below after checking the command line.</p>
    <div class="ferr" id="mf-err"></div>
    <div class="factions"><button class="btn approve" id="mf-save">Add MCP server</button><button class="btn decline" id="mf-cancel">Cancel</button></div>
  </div>`;
  document.getElementById("mf-cancel").onclick=()=>c.innerHTML="";
  document.getElementById("mf-save").onclick=async()=>{
    const args=document.getElementById("mf-args").value.split("\n").map(s=>s.trim()).filter(Boolean);
    const ok=await postForm("/api/mcp/add",{
      name:document.getElementById("mf-name").value.trim(),
      command:document.getElementById("mf-cmd").value.trim(),
      args:args,
    },document.getElementById("mf-err"));
    if(ok) loadAdmin();
  };
}

function showProjectForm(){
  const c=document.getElementById("project-form");
  c.innerHTML=`<div class="aform">
    <label>Repo URL</label><input id="pf-url" placeholder="https://github.com/owner/repo">
    <p class="sub" style="margin:2px 0 0">GitHub or GitLab remote (<code>git@…</code> works too). Otto clones it under <code>data/repos/</code> using your <code>gh</code> login.</p>
    <label style="margin-top:10px">Local checkout <span class="sub">(optional)</span></label><input id="pf-path" placeholder="/home/you/repositories/repo">
    <p class="sub" style="margin:2px 0 0">Already have it on disk? Point at it and Otto clones from there instead — faster, offline, and the checkout gets write-protected.</p>
    <div class="ferr" id="pf-err"></div>
    <div class="factions"><button class="btn approve" id="pf-save">Add project repo</button><button class="btn decline" id="pf-cancel">Cancel</button></div>
  </div>`;
  document.getElementById("pf-cancel").onclick=()=>c.innerHTML="";
  document.getElementById("pf-save").onclick=async(e)=>{
    // The clone runs inside this request, so the button must say so — a silent multi-second
    // wait on a form with no feedback reads as a dead button and gets clicked again.
    const btn=e.target, was=btn.textContent;
    btn.disabled=true; btn.textContent="Cloning…";
    const ok=await postForm("/api/project/add",{
      url:document.getElementById("pf-url").value.trim(),
      path:document.getElementById("pf-path").value.trim(),
    },document.getElementById("pf-err"));
    btn.disabled=false; btn.textContent=was;
    if(ok) loadAdmin();
  };
}

function queueSave(){ clearTimeout(saveTimer); saveTimer=setTimeout(saveAdmin,400); }
// The ONE writer of a capability's execution backend. The merged Execution dropdown spans three
// stores — cap_exec (models.json), cap_local_exec (models.json) and the policy tool_free flag —
// and they only ever move together here, so no combination can exist that the dropdown couldn't
// have produced. Order matters: cap_exec is cleared BEFORE cap_local_exec is set, because "both
// set" is precisely the state that silently takes attempt 1 away from the exec pick.
async function setCapBackend(sel){
  const v=sel.value, i=v.indexOf("|");
  const mode=i<0?"":v.slice(0,i), model=i<0?"":v.slice(i+1);   // a pool name may contain "|"
  const cap=sel.dataset.cap;
  sel.classList.toggle("set",!!v);
  const post=(path,model)=>fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({name:cap,model:model})});
  try {
    await post("/api/models/capexec", mode==="exec"?model:"");
    await post("/api/models/caplocal", mode==="toolfree"?model:"");
  } catch(e){}
  const st=POLICY_STATE&&POLICY_STATE.capabilities[cap];
  if(st && st.tool_free!==(mode==="toolfree")){ st.tool_free=(mode==="toolfree"); queueSave(); }
  const sv=document.getElementById("saved"); if(sv){ sv.classList.add("show"); setTimeout(()=>sv.classList.remove("show"),1200); }
}
async function saveAdmin(){
  try {
    await fetch("/api/policy",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(POLICY_STATE)});
    const s=document.getElementById("saved"); if(s){ s.classList.add("show"); setTimeout(()=>s.classList.remove("show"),1200); }
    // Recount through applyCaps rather than inline, so this path and the initial load can't
    // drift apart on what "enabled" means (this one used truthiness, applyCaps used
    // `!==false` — they disagree the moment a cap arrives without the field).
    applyCaps(Object.values(POLICY_STATE.capabilities));
  } catch(e){}
}
