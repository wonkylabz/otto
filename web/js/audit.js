"use strict";
/* ---- audit tab (immutable) ---- */
const auTok=n=>{n=n||0;return n>=1e6?(n/1e6).toFixed(1)+"M":n>=1e3?(n/1e3).toFixed(1)+"k":String(n);};
// Claude ids collapse to their tier; a local model keeps its (basename) name — "gemma4-26b"
// is the useful fact, "opus/sonnet/haiku" the useful granularity for Claude.
const auTier=m=>{m=(m||"");const l=m.toLowerCase();return l.includes("opus")?"opus":l.includes("sonnet")?"sonnet":l.includes("haiku")?"haiku":((m.split("/").pop())||"?");};
const auDur=s=>s==null?"":s<60?s.toFixed(1)+"s":(s/60).toFixed(1)+"m";
// `reason` is only ever set on a row that needed attention (needs-human, supervisor kill/shadow,
// a decline) and carries Otto's own internal vocabulary (issue: "not sure what this is for") — map
// the known codes to plain English; anything unmapped (a review_/qa_ state, or a future code)
// falls back to the raw string rather than hiding it.
const AU_REASON_LABELS={workflow_error:"workflow crashed",delivery_failed:"delivery failed",
  budget_exceeded:"budget exceeded",verify_exhausted:"verify ladder exhausted",
  published_unverified:"failed after publishing, not retried",
  local_fallback_disabled:"local model failed, Claude fallback is off",
  claude_auth_expired:"Claude could not authenticate",
  supervisor_retry:"supervisor killed & retried",
  supervisor_would_retry:"supervisor flagged (shadow mode)",terminated:"terminated by a human",
  supervisor_steer:"supervisor steered the attempt",claude_usage_limit:"Claude usage limit reached",
  factory_no_pr:"factory run opened no PR",
  merge_failed:"merge refused",harness_exhausted:"the harness kept dying",gate_timeout:"approval timed out",merge_unreviewed:"merge without a passing review"};
const auReason=r=>{if(!r) return ""; if(AU_REASON_LABELS[r]) return AU_REASON_LABELS[r];
  const rv=/^review_(.+)$/.exec(r); if(rv) return "code review: "+rv[1];
  const qa=/^qa_(.+)$/.exec(r); if(qa) return "QA: "+qa[1];
  return r;};

/* Inspecting a row opens the shared detail modal (same shell as the run-debug drawer), NOT an
   in-place expansion: a 50k-char result used to shove every row below it off the screen, and the
   trail is meant to be scanned. The row itself stays pure metadata; request + output are fetched
   lazily from /api/audit/content, keyed on workflow + timestamp + attempt. */
async function openAuditDetail(e){
  if(!e) return;
  const modal=document.getElementById("cardModal"), title=document.getElementById("cardModalTitle"),
        body=document.getElementById("cardModalBody");
  const cap=(e.capability||"—").split(":").pop(), wid=e.workflow||"", denied=e.outcome==="denied";
  title.innerHTML=`<b>${esc(cap)}</b>${e.attempt?` · try ${esc(String(e.attempt))}`:''}`
    +`${denied?' · <span style="color:var(--danger)">declined</span>':''}<br>`
    +`${esc(shortWhen(e.at))} · ${esc(e.at||'')}${wid?' · '+esc(wid):''}`
    +(wid?` · <a href="#" id="ad-debug">full run detail ↗</a>`:'');
  const meta=[];
  if(e.model) meta.push(`<span class="k">${esc((e.backend==='local'?'⌂ ':'')+auTier(e.model))}</span>`);
  if(e.fallback_from) meta.push(`<span style="color:var(--warn)" title="${esc(e.fallback_reason||'')}">${esc(auTier(e.fallback_from))} ⇢ ${esc(auTier(e.model||'?'))}</span>`);
  if(e.risk) meta.push(`risk: <span class="k">${esc(e.risk)}</span>`);
  if(e.repo) meta.push(`repo: <span class="k">${esc(e.repo)}</span>`);
  if(e.duration_s!=null) meta.push(esc(auDur(e.duration_s)));
  const t=e.tokens;
  if(t) meta.push(`<span title="${esc(`${t.output} output · ${t.input} input · ${t.cache_read} cache-read · ${t.cache_write} cache-write`)}">${auTok(t.output)} out · ${auTok(t.input)} in</span>`);
  meta.push(`≈ $${(e.cost_usd||0).toFixed(4)} <span class="aest">API-equiv</span>`);
  if(e.verified===true) meta.push(`<span style="color:var(--ok)">verified</span>`);
  else if(e.verified===false) meta.push(`<span style="color:var(--danger)">unverified</span>`);
  if(e.reason) meta.push(`<span title="${esc(e.reason)}">${esc(auReason(e.reason))}</span>`);
  body.innerHTML=`<div class="dbgmeta">${meta.join(" &nbsp;·&nbsp; ")}</div><p class="sub">loading…</p>`;
  modal.hidden=false;
  const dbg=document.getElementById("ad-debug");
  if(dbg) dbg.addEventListener("click",ev=>{ ev.preventDefault(); openRunDebug(wid, cap, null); });
  const qs=new URLSearchParams({wid: wid, at: e.at||""});
  if(e.attempt!=null) qs.set("attempt", String(e.attempt));
  let c;
  try { c=await (await fetch("/api/audit/content?"+qs.toString())).json(); }
  catch(err){ body.querySelector(".sub").outerHTML=`<p class="err">Couldn't load details (${esc(err.message)}).</p>`; return; }
  if(modal.hidden) return;                     // closed while the fetch was in flight
  const req=c.request?`<div class="dbgsec">request</div><div class="areq-full">${esc(c.request)}</div>`:"";
  const hasResult=c.result && c.result!=="(no output)";
  const truncated=hasResult && c.result.length>=50000;
  const res=hasResult?`<div class="dbgsec">${denied?'outcome':'output'}</div><div class="result">${renderMD(c.result)}</div>`
    :`<div class="dbgsec">output</div><p class="sub">${denied?'Declined at the approval gate — the capability never ran.':'(no output)'}</p>`;
  // `detail` is structured forensics on the row (today: the repos an in-place-edit guard caught).
  const det=c.detail?`<div class="dbgsec">detail</div><div class="dbgresult">${esc(typeof c.detail==="string"?c.detail:JSON.stringify(c.detail,null,2))}</div>`:"";
  body.querySelector(".sub").outerHTML=req+res+det
    +(truncated?'<div class="aout-trunc">… output truncated to 50k chars — full text in data/audit-content.log</div>':'');
}

/* ---- health report (insights.report via /api/insights) ----
   What broke this window, by cause and by WHO judged it, against the window before — the
   questions the audit-runs skill used to answer with hand-written SQL. Cached OUTSIDE the
   render: a filter change rebuilds the whole tab, and re-scanning the trail for an unchanged
   window on every keystroke is the cost /api/stats already avoids. */
let AU_INSIGHT={days:7, data:null, at:0, busy:null};
try { const d=+localStorage.getItem("ottoInsightDays"); if(d===7||d===30) AU_INSIGHT.days=d; } catch(e){}
const auPct=v=>v==null?"—":Math.round(v*100)+"%";
const auMs=ms=>ms==null?"—":ms<1000?ms+"ms":ms<60000?(ms/1000).toFixed(1)+"s":(ms/60000).toFixed(1)+"m";
// The arrow says which way it moved; the colour says whether that is good. For most numbers
// down is good (failures, spend); `up` names the ones where rising is the good direction.
function auDelta(now, prev, up, fmt){
  if(now==null || prev==null || now===prev) return `<span class="kdelta flat" title="previous window: ${esc(fmt?fmt(prev):String(prev??"—"))}">=</span>`;
  const rose=now>prev, good=up?rose:!rose;
  return `<span class="kdelta ${good?'good':'bad'}" title="previous window: ${esc(fmt?fmt(prev):String(prev))}">${rose?'▲':'▼'} from ${esc(fmt?fmt(prev):String(prev))}</span>`;
}
async function fetchInsights(force){
  const fresh=AU_INSIGHT.data && AU_INSIGHT.data.window && AU_INSIGHT.data.window.days===AU_INSIGHT.days
              && Date.now()-AU_INSIGHT.at<60000;
  if(fresh && !force) return AU_INSIGHT.data;
  // An in-flight request is reused only for the SAME window: switching 7d→30d mid-load
  // otherwise painted the 7-day report under the 30-day label.
  const days=AU_INSIGHT.days;
  if(AU_INSIGHT.busy && AU_INSIGHT.busyDays===days) return AU_INSIGHT.busy;
  const req=getJSON("/api/insights?days="+days)
    .then(d=>{ if(AU_INSIGHT.days===days){ AU_INSIGHT.data=d; AU_INSIGHT.at=Date.now(); } return d; })
    .finally(()=>{ if(AU_INSIGHT.busy===req) AU_INSIGHT.busy=null; });
  AU_INSIGHT.busy=req; AU_INSIGHT.busyDays=days;
  return req;
}
async function loadInsights(force){
  const box=document.getElementById("au-health");
  if(!box) return;
  if(!AU_INSIGHT.data) box.innerHTML=`<p class="sub">reading the trail…</p>`;
  let r;
  try { r=await fetchInsights(force); }
  catch(e){ box.innerHTML=`<p class="err">Couldn't build the health report (${esc(e.message)}).</p>`; return; }
  if(r.window && r.window.days!==AU_INSIGHT.days) return;   // superseded by a later window pick
  const live=document.getElementById("au-health");      // the tab may have re-rendered meanwhile
  if(live) live.innerHTML=renderInsights(r);
  wireInsights();
}
function renderInsights(r){
  const n=r.now||{}, p=r.prev||{}, d=(r.window&&r.window.days)||AU_INSIGHT.days, h=r.health||{};
  const kpi=(label,val,delta,title)=>`<div class="kpi" title="${esc(title)}"><div class="klabel">${label}</div><div class="kval">${val}</div>${delta}</div>`;
  const warns=[];
  if(h.estop&&h.estop.engaged) warns.push(`<span class="hwarn bad">● Global pause is ON — nothing new starts</span>`);
  (h.unhealthy_models||[]).forEach(m=>{ const uses=[...(m.phases||[]),...(m.caps||[])];
    warns.push(`<span class="hwarn bad" title="${esc(m.detail||'')}">● ${esc(m.name||m.model||'a model')} last failed${uses.length?' · used by '+esc(uses.join(', ')):''}</span>`); });
  Object.entries(h.down||{}).forEach(([m,s])=>warns.push(`<span class="hwarn" title="skipped until the cooldown ends">○ ${esc(m)} marked down for ${Math.ceil(s/60)}m</span>`));
  // The five biggest moves; the rest wait behind a tooltip rather than stretching the card.
  const ch=r.changes||[], chMore=ch.length>5?`<li class="dim" title="${esc(ch.slice(5).join("\n"))}">+${ch.length-5} more</li>`:'';
  const changes=ch.length
    ?`<ul class="hchanges">${ch.slice(0,5).map(c=>`<li>${esc(c)}</li>`).join("")}${chMore}</ul>`
    :`<p class="sub hquiet">Nothing moved beyond noise against the previous ${d} days.</p>`;
  const days=r.by_day||[], dmax=Math.max(1,...days.map(x=>x.runs));
  const spark=days.length?`<div class="hdays" role="img" aria-label="runs per day">${days.map((x,i)=>{
      const hgt=x.runs?Math.max(4,(x.runs/dmax)*100):0, att=x.runs?(x.attention/x.runs)*100:0;
      return `<div class="hday${x.runs?'':' quiet'}" title="${esc(x.day)}: ${x.runs} runs · ${x.attention} needing attention · ≈ $${x.cost_usd.toFixed(2)}"><i style="height:${hgt.toFixed(0)}%"><b style="height:${att.toFixed(0)}%"></b></i><span>${(days.length<=14||i%5===0)?esc(x.day.slice(8)):'&nbsp;'}</span></div>`;
    }).join("")}</div><div class="hlegend"><span class="lk runs"></span>runs <span class="lk att"></span>share needing attention</div>`:'';
  const HROWS=8;
  const tbl=(head,rows)=>rows.length?`<table class="htable"><thead><tr>${head.map(x=>`<th${x[1]?` class="num"`:''}>${x[0]}</th>`).join("")}</tr></thead><tbody>${(INSIGHT_ALL?rows:rows.slice(0,HROWS)).join("")}</tbody></table>`
      +(rows.length>HROWS?`<button class="linkbtn hall" data-iall>${INSIGHT_ALL?'show fewer':`show all ${rows.length}`}</button>`:''):`<p class="memempty">None in this window.</p>`;
  const cause=tbl([["Cause"],["Count",1],["Before",1]], (r.attention||[]).map(a=>{
      const lbl=a.outcome==="needs_human"?auReason(a.reason):(a.reason&&a.reason!==a.outcome?auReason(a.reason):a.outcome.replace(/_/g," "));
      return `<tr><td title="${esc(a.outcome+(a.reason?' / '+a.reason:''))}">${esc(lbl)}</td><td class="num">${a.count}</td><td class="num dim">${a.prev}</td></tr>`;}));
  const judges=tbl([["Who decided"],["Model"],["Fails",1]], (r.fail_sources||[]).map(f=>
      `<tr><td>${esc(f.source)}</td><td title="${esc(f.model)}">${esc(f.model?auTier(f.model):'—')}</td><td class="num">${f.count}</td></tr>`));
  const tiers=tbl([["Tier"],["Calls",1],["Failed",1],["Fell back",1],["p50",1],["Top decisions"]], (r.tiers||[]).map(t=>
      `<tr><td>${esc(t.tier)}</td><td class="num">${t.calls}</td><td class="num${t.failed?' bad':''}">${t.failed}</td><td class="num">${t.fell_back}</td><td class="num">${auMs(t.p50_ms)}</td><td class="hdec">${t.decisions.slice(0,3).map(x=>`<span class="hchip" title="${esc(x.decision)}">${esc(x.decision.slice(0,28))} ×${x.count}</span>`).join("")}</td></tr>`));
  const stages=tbl([["Stage"],["Runs",1],["Median",1],["p90",1]], ((r.stages||{}).stages||[]).map(s=>
      `<tr><td>${esc(s.stage.toLowerCase())}</td><td class="num">${s.runs}${s.open?` <span class="dim" title="still open on the last row written">+${s.open}</span>`:''}</td><td class="num">${auMs(s.p50_ms)}</td><td class="num">${auMs(s.p90_ms)}</td></tr>`));
  const caps=tbl([["Capability"],["Judged",1],["Pass",1],["Overruled",1],["Escalated",1],["Avg $",1]], (r.caps||[]).filter(c=>c.runs).map(c=>
      `<tr><td title="${esc(c.capability)}">${esc(c.name)}</td><td class="num">${c.runs}</td><td class="num${c.pass_rate!=null&&c.pass_rate<.7?' bad':''}">${auPct(c.pass_rate)}</td><td class="num" title="failed runs a human accepted — a judge problem, not a capability problem">${c.accepted||0}</td><td class="num">${auPct(c.escalation_rate)}</td><td class="num">${c.avg_cost_usd!=null?c.avg_cost_usd.toFixed(2):'—'}</td></tr>`));
  const ledgerNote=r.ledger_covers?`<p class="sub">Tier-call ledger holds calls since ${esc(shortWhen(r.ledger_covers))}.</p>`:'';
  // One breakdown at a time behind tabs: five stacked collapsibles read as five more sections of
  // the page, all the same weight as the trail below them.
  const tabs=[["cause","Causes",(r.attention||[]).length,cause],
    ["judges","Judges",(r.fail_sources||[]).length,judges],
    ["caps","Capabilities",(r.caps||[]).filter(c=>c.runs).length,caps],
    ["stages","Stages",((r.stages||{}).stages||[]).length,stages],
    ["tiers","Tier calls",(r.tiers||[]).length,ledgerNote+tiers]];
  const cur=tabs.find(t=>t[0]===INSIGHT_TAB)||tabs[0];
  const meta=`attempt p50 <b>${auDur(n.attempt_p50_s)||'—'}</b> · p90 <b>${auDur(n.attempt_p90_s)||'—'}</b> · <b>${n.fallbacks??0}</b> fallback${n.fallbacks===1?'':'s'}`;
  return `<div class="hhead"><h2>Health</h2>
      <div class="hseg" role="group" aria-label="window">${[7,30].map(x=>`<button class="addbtn${x===d?' on':''}" data-idays="${x}" aria-pressed="${x===d}">${x}d</button>`).join("")}</div>
      <button class="addbtn" id="ins-refresh" title="re-read the trail now">refresh</button></div>
    ${warns.length?`<div class="hwarns">${warns.join("")}</div>`:''}
    <div class="kpis">
      ${kpi("Runs",n.runs??0,auDelta(n.runs,p.runs,true),"distinct runs with any row in the window")}
      ${kpi("Judged pass",auPct(n.pass_rate),auDelta(n.pass_rate,p.pass_rate,true,auPct),"runs a JUDGE passed, over runs a judge decided — supervisor kills and harness deaths excluded")}
      ${kpi("Attention",n.attention??0,auDelta(n.attention,p.attention,false),"rows that were not routine work: needs-human, supervisor actions, declines, overrides")}
      ${kpi("Overruled",n.accepted??0,auDelta(n.accepted,p.accepted,false),"failed runs a human accepted — each one is a judge that was wrong")}
      ${kpi("Spend",`$${(n.cost_usd||0).toFixed(2)}`,auDelta(n.cost_usd,p.cost_usd,false,v=>'$'+(v||0).toFixed(2)),"API-equivalent estimate — on a subscription this is not billed")}
    </div>
    <div class="hcard hsum"><div class="hchart"><h4>Last ${d} days</h4>${spark||'<p class="memempty">No runs.</p>'}</div>
      <div class="hnotes"><h4>What changed</h4>${changes}<p class="hmeta">${meta}</p></div></div>
    <div class="hcard hbreak"><div class="htabs" role="tablist">${tabs.map(t=>`<button role="tab" class="htab${t===cur?' on':''}" data-itab="${t[0]}" aria-selected="${t===cur}">${t[1]} <span>${t[2]}</span></button>`).join("")}</div>
      ${cur[3]}</div>`;
}
let INSIGHT_TAB="cause", INSIGHT_ALL=false;
function wireInsights(){
  const box=document.getElementById("au-health"); if(!box) return;
  box.querySelectorAll("[data-idays]").forEach(b=>b.addEventListener("click",()=>{
    AU_INSIGHT.days=+b.dataset.idays;
    try { localStorage.setItem("ottoInsightDays",String(AU_INSIGHT.days)); } catch(e){}
    loadInsights();
  }));
  const rf=box.querySelector("#ins-refresh"); if(rf) rf.addEventListener("click",()=>loadInsights(true));
  box.querySelectorAll("[data-itab]").forEach(b=>b.addEventListener("click",()=>{
    INSIGHT_TAB=b.dataset.itab; INSIGHT_ALL=false; box.innerHTML=renderInsights(AU_INSIGHT.data); wireInsights();
  }));
  const all=box.querySelector("[data-iall]");
  if(all) all.addEventListener("click",()=>{ INSIGHT_ALL=!INSIGHT_ALL; box.innerHTML=renderInsights(AU_INSIGHT.data); wireInsights(); });
}

// The trail is paged on the server: the whole trail was one response and one <tbody>, which
// grows without bound on an install that never clears it (the trail is immutable).
const AU_PAGE=50;
let AUDIT_OFFSET=0;
async function loadAudit(){
  const el=document.getElementById("auditview");
  el.innerHTML=`<p class="sub">loading…</p>`;
  let data;
  const qs=new URLSearchParams();
  if(AUDIT_FILTER.wid) qs.set("wid",AUDIT_FILTER.wid);
  if(AUDIT_FILTER.cap) qs.set("cap",AUDIT_FILTER.cap);
  if(AUDIT_FILTER.verified) qs.set("verified",AUDIT_FILTER.verified);
  qs.set("offset",String(AUDIT_OFFSET)); qs.set("limit",String(AU_PAGE));
  try { data=await (await fetch("/api/audit?"+qs.toString())).json(); }
  catch(e){ el.innerHTML=`<p class="err">Couldn't load audit (${esc(e.message)}).</p>`; return; }
  // A server older than the paging (assets ship on refresh, the route only on restart) ignores
  // offset/limit and sends the whole trail; page it here so the pager never lies.
  if(data.limit==null) data.entries=data.entries.slice(AUDIT_OFFSET,AUDIT_OFFSET+AU_PAGE);
  const rows=data.entries.map((e,i)=>{
    const capRaw=(e.capability||"").split(":").pop();
    const cap=(!capRaw||capRaw==="?")?"unknown":capRaw;
    const denied=e.outcome==="denied";
    const t=e.tokens;
    const tok=t?auTok(t.output):`<span class="anone">—</span>`;
    const tokTitle=t?`${t.output} output · ${t.input} input · ${t.cache_read} cache-read · ${t.cache_write} cache-write\n≈ $${(e.cost_usd||0).toFixed(4)} API-equiv${e.model?" · "+e.model:""}`:`≈ $${(e.cost_usd||0).toFixed(4)} API-equiv (no token data)`;
    // Operational metadata only — no chat/request text in these always-visible cells (that lives
    // in the detail modal, fetched lazily from /api/audit/content). ⌂ = Otto's local
    // runtime (no claude -p).
    const attempt=e.attempt?`<span class="abadge">try ${esc(String(e.attempt))}</span>`:"";
    // Verdict + reason share one cell: on a needs-human/supervisor row `verified` is never set
    // (nothing to show), and on a plain verified/unverified row `reason` is never set — the two
    // fields never both had content, so a stacked badge+reason beats two columns that were each
    // empty half the time.
    const reasonText=e.reason?auReason(e.reason):"";
    const reasonLine=reasonText?`<div class="areason" title="${esc(e.reason||'')}">${esc(reasonText)}</div>`:"";
    const badge=denied?`<span class="abadge bad">declined</span>`
               :e.outcome==="needs_human"?`<span class="abadge bad">needs human</span>`
               :e.outcome==="supervisor_kill"?`<span class="abadge fell">supervisor kill</span>`
               :e.outcome==="supervisor_shadow"?`<span class="abadge shadow">supervisor shadow</span>`
               :e.verified===true?`<span class="abadge ok">verified</span>`
               :e.verified===false?`<span class="abadge bad">unverified</span>`:"";
    const verdict=badge||reasonLine?`<div class="averdict">${badge}${reasonLine}</div>`:"";
    // The chosen model couldn't run this attempt and another substituted — its own column (not
    // crammed into Model, which used to overflow the cell and clip mid-word — user-reported) so
    // "did this fall back?" is a column to scan, not text to parse out of the model name.
    // `fallback_reason` is one fixed summary per wall; `fallback_detail` is the endpoint's own
  // words (which HTTP code, which socket error), which is what tells a 503 from a refusal.
  const fellTitle=e.fallback_from?esc(`fell back — chosen model '${e.fallback_from}' could not run this attempt: ${e.fallback_reason||'no reason recorded'}.${e.fallback_detail?` (${e.fallback_detail})`:''} Ran on ${e.model||'unknown'} instead.`):"";
    const model=e.model?`<span class="amodel ${e.fallback_from?'fell':''}" title="${fellTitle||esc(e.model)}">${esc((e.backend==='local'?'⌂ ':'')+auTier(e.model))}</span>`:`<span class="anone">—</span>`;
    return `<tr class="arec ${denied?'denied':''}" data-i="${i}" role="button" tabindex="0" title="Inspect this action">
      <td class="c-when awhen" title="${esc(e.at||'')}">${esc(shortWhen(e.at))}</td>
      <td class="c-cap acap" title="${esc(e.capability||'')}">${esc(cap)}</td>
      <td class="c-run arun" title="${esc(e.workflow||'')}">${esc(e.workflow||'')}</td>
      <td class="c-try">${attempt}</td>
      <td class="c-verdict">${verdict}</td>
      <td class="c-model">${model}</td>
      <td class="c-took num adur">${esc(auDur(e.duration_s))}</td>
      <td class="c-out num aout ${denied?'denied':'ran'}" title="${esc(tokTitle)}">${denied?`<span class="anone">—</span>`:tok}</td>
      <td class="c-caret acaret">›</td>
    </tr>`;
  }).join("");
  // table-layout is fixed, so the column widths live here rather than being re-derived per row.
  const table=`<table class="atable"><colgroup>
      <col class="c-when"><col class="c-cap"><col class="c-run"><col class="c-try"><col class="c-verdict">
      <col class="c-model"><col class="c-took"><col class="c-out"><col class="c-caret">
    </colgroup>
    <thead><tr><th class="c-when">When</th><th class="c-cap">Capability</th><th class="c-run">Run</th>
      <th class="c-try">Attempt</th>
      <th class="c-verdict" title="A blank badge alone means the run just ran normally. A second line appears only on a run that needed attention: a budget stop, an exhausted verify ladder, a supervisor kill/shadow, or a terminated run.">Verdict</th>
      <th class="c-model">Model</th>
      <th class="c-took num">Took</th>
      <th class="c-out num">Output</th>
      <th class="c-caret"></th></tr></thead>
    <tbody>${rows}</tbody></table>`;
  const tt=data.total_tokens||{};
  const tierRows=Object.entries(data.tokens_by_model||{}).filter(([,d])=>(d.output||0)>0)
    .sort((a,b)=>(b[1].output||0)-(a[1].output||0));
  const tierMax=tierRows.length?(tierRows[0][1].output||0):0;
  const tierSum=tierRows.reduce((a,[,d])=>a+(d.output||0),0);
  // bars are proportional to the largest row and start at zero — a truncated baseline would
  // misstate every ratio the table exists to show
  const tiers=tierRows.map(([m,d])=>{
    const v=d.output||0, w=tierMax?Math.max(0.6,(v/tierMax)*100):0;
    const pct=tierSum?(v/tierSum)*100:0;
    return `<tr><td class="tkname" title="${esc(m)}">${esc(auTier(m))}</td>`
      +`<td class="tkbar"><i style="width:${w.toFixed(1)}%"></i></td>`
      +`<td class="tkval">${auTok(v)}</td>`
      +`<td class="tkpct">${pct>=0.1?pct.toFixed(1):"&lt;0.1"}%</td></tr>`;
  }).join("");
  const tokTotTitle=`${(tt.output||0)} output · ${(tt.input||0)} input · ${(tt.cache_read||0)} cache-read · ${(tt.cache_write||0)} cache-write`;
  el.innerHTML=`
    <div class="phead"><h1>Audit</h1>
      <p class="sub">How Otto is doing, then every action it took — including declined writes and failures. Click any row to inspect its request and output.</p></div>
    <section id="au-health" class="ahealth" aria-label="health report"></section>
    <h2 class="atrailhead">Trail <span class="immutable">append-only</span></h2>
    <div class="memhead">
      <span class="memstat"><b>${data.count}</b> actions</span>
      <span class="memstat" title="${esc(tokTotTitle)}">output tokens <b class="gold">${auTok(tt.output)}</b></span>
      <span class="memstat">≈ <b>$${(data.total_cost||0).toFixed(2)}</b> <span class="aest">API-equiv · subscription, not billed</span></span>
    </div>
    ${tiers?`<div class="tokwrap"><div class="asection coll${TOK_OPEN?'':' collapsed'}" data-sect="au-tokens">
      <h3><span class="secttoggle"><span class="gcaret">&#9662;</span>Output tokens by model</span>
        <span class="sectcount">${tierRows.length}</span></h3>
      <div class="asection-body"><table class="toktable">${tiers}</table></div>
    </div></div>`:''}
    <div class="memhead" style="gap:8px;flex-wrap:wrap">
      <select id="af-cap" class="addbtn" title="filter by capability">
        <option value="">all capabilities</option>
        ${(data.capabilities||[]).map(c=>`<option value="${esc(c)}" ${AUDIT_FILTER.cap===c?'selected':''}>${esc(c.split(':').pop())}</option>`).join("")}
      </select>
      <select id="af-ver" class="addbtn" title="filter by verify verdict">
        <option value="">any verdict</option>
        <option value="true" ${AUDIT_FILTER.verified==='true'?'selected':''}>verified</option>
        <option value="false" ${AUDIT_FILTER.verified==='false'?'selected':''}>unverified</option>
      </select>
      <input id="af-wid" class="addbtn" placeholder="workflow id" value="${esc(AUDIT_FILTER.wid)}"
        title="filter by workflow id (press Enter)" style="min-width:180px">
      ${(AUDIT_FILTER.wid||AUDIT_FILTER.cap||AUDIT_FILTER.verified)?'<button class="addbtn" id="af-clear">clear filters</button>':''}
    </div>
    ${rows ? table : '<p class="memempty">No actions match.</p>'}
    ${data.count>AU_PAGE?`<div class="apager"><button class="addbtn" data-apage="-1"${AUDIT_OFFSET?'':' disabled'}>‹ Newer</button>
      <span class="sub">${AUDIT_OFFSET+1}–${Math.min(AUDIT_OFFSET+AU_PAGE,data.count)} of ${data.count}</span>
      <button class="addbtn" data-apage="1"${AUDIT_OFFSET+AU_PAGE<data.count?'':' disabled'}>Older ›</button></div>`:''}`;
  el.querySelector("#af-cap").addEventListener("change",e=>{AUDIT_FILTER.cap=e.target.value; AUDIT_OFFSET=0; loadAudit();});
  el.querySelector("#af-ver").addEventListener("change",e=>{AUDIT_FILTER.verified=e.target.value; AUDIT_OFFSET=0; loadAudit();});
  el.querySelector("#af-wid").addEventListener("keydown",e=>{
    if(e.key==="Enter"){AUDIT_FILTER.wid=e.target.value.trim(); AUDIT_OFFSET=0; loadAudit();}});
  const clr=el.querySelector("#af-clear");
  if(clr) clr.addEventListener("click",()=>{AUDIT_FILTER={wid:"",cap:"",verified:""}; AUDIT_OFFSET=0; loadAudit();});
  const tokSec=el.querySelector('.asection[data-sect="au-tokens"]');
  enhanceToggles(el,".secttoggle",".asection","collapsed");
  if(tokSec){ tokSec.querySelector(".secttoggle").addEventListener("click",()=>{
    TOK_OPEN=!tokSec.classList.toggle("collapsed");
    try { localStorage.setItem("ottoTokOpen",TOK_OPEN?"1":"0"); } catch(e){}
  }); }
  el.querySelectorAll("[data-apage]").forEach(b=>b.addEventListener("click",async()=>{
    AUDIT_OFFSET=Math.max(0,AUDIT_OFFSET+(+b.dataset.apage)*AU_PAGE);
    await loadAudit();
    const head=document.querySelector("#auditview .atrailhead"); if(head) head.scrollIntoView({block:"start"});
  }));
  loadInsights();
  el.querySelectorAll("tr.arec").forEach(r=>{
    const open=()=>openAuditDetail(data.entries[+r.dataset.i]);
    r.addEventListener("click",open);
    r.addEventListener("keydown",ev=>{ if(ev.key==="Enter"||ev.key===" "){ ev.preventDefault(); open(); }});
  });
}
