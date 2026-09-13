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
  local_fallback_disabled:"local model failed, Claude fallback is off",
  claude_auth_expired:"Claude could not authenticate",
  supervisor_retry:"supervisor killed & retried",
  supervisor_would_retry:"supervisor flagged (shadow mode)",terminated:"terminated by a human"};
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

async function loadAudit(){
  const el=document.getElementById("auditview");
  el.innerHTML=`<p class="sub">loading…</p>`;
  let data;
  const qs=new URLSearchParams();
  if(AUDIT_FILTER.wid) qs.set("wid",AUDIT_FILTER.wid);
  if(AUDIT_FILTER.cap) qs.set("cap",AUDIT_FILTER.cap);
  if(AUDIT_FILTER.verified) qs.set("verified",AUDIT_FILTER.verified);
  try { data=await (await fetch("/api/audit"+(qs.toString()?"?"+qs.toString():""))).json(); }
  catch(e){ el.innerHTML=`<p class="err">Couldn't load audit (${esc(e.message)}).</p>`; return; }
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
    const fellTitle=e.fallback_from?esc(`fell back — chosen model '${e.fallback_from}' could not run this attempt: ${e.fallback_reason||'no reason recorded'}. Ran on ${e.model||'unknown'} instead.`):"";
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
    <div class="phead"><h1>Audit trail <span class="immutable">append-only</span></h1>
      <p class="sub">Every action, including declined writes and failures. Click any row to inspect its request and output.</p></div>
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
    ${rows ? table : '<p class="memempty">No actions match.</p>'}`;
  el.querySelector("#af-cap").addEventListener("change",e=>{AUDIT_FILTER.cap=e.target.value; loadAudit();});
  el.querySelector("#af-ver").addEventListener("change",e=>{AUDIT_FILTER.verified=e.target.value; loadAudit();});
  el.querySelector("#af-wid").addEventListener("keydown",e=>{
    if(e.key==="Enter"){AUDIT_FILTER.wid=e.target.value.trim(); loadAudit();}});
  const clr=el.querySelector("#af-clear");
  if(clr) clr.addEventListener("click",()=>{AUDIT_FILTER={wid:"",cap:"",verified:""}; loadAudit();});
  const tokSec=el.querySelector('.asection[data-sect="au-tokens"]');
  enhanceToggles(el,".secttoggle",".asection","collapsed");
  if(tokSec){ tokSec.querySelector(".secttoggle").addEventListener("click",()=>{
    TOK_OPEN=!tokSec.classList.toggle("collapsed");
    try { localStorage.setItem("ottoTokOpen",TOK_OPEN?"1":"0"); } catch(e){}
  }); }
  el.querySelectorAll("tr.arec").forEach(r=>{
    const open=()=>openAuditDetail(data.entries[+r.dataset.i]);
    r.addEventListener("click",open);
    r.addEventListener("keydown",ev=>{ if(ev.key==="Enter"||ev.key===" "){ ev.preventDefault(); open(); }});
  });
}
