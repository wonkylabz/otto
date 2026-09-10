"use strict";
/* ---- board tab (read-only live view of Temporal workflows) ---- */
/* Counter on the Board tab: how many runs are waiting on a HUMAN (approval, clarification,
   needs-human, failed). Fed by loadBoard's own data while the tab is open (its 3.5s poll);
   a slower /api/needs-you poll below keeps it fresh from any other tab. */
function setBoardBadge(n){
  const b=document.getElementById("tabbadge-board");
  if(!b) return;
  b.hidden=!n;
  b.textContent=n>99?"99+":String(n);
}
/* Warning badge on the Admin tab: how many ENABLED MCP servers are broken (failed / need
   auth) PLUS how many LLM models last failed (unreachable / mis-configured / rejecting calls),
   so a config error like the one that derailed a session is visible before you run. Both count
   into the one badge on purpose — it means "something in Configuration needs you", and a model
   that only works because Claude keeps covering for it is exactly as invisible as a dead MCP
   server was. The title says which. */
function setAdminBadge(mcp, models){
  const b=document.getElementById("tabbadge-admin");
  if(!b) return;
  const n=(mcp||0)+(models||0);
  b.hidden=!n;
  b.textContent=n>99?"99+":String(n);
  const parts=[];
  if(mcp) parts.push(`${mcp} MCP server${mcp>1?"s":""}`);
  if(models) parts.push(`${models} LLM model${models>1?"s":""}`);
  b.title=n?`${parts.join(" and ")} ${n>1?"need":"needs"} attention`:"";
}
async function pollAdminBadge(){
  try{
    const d=await (await fetch("/api/health")).json();
    setAdminBadge((d.mcp||{}).unhealthy||0, (d.models||{}).unhealthy||0);
    // Same tick keeps the pause current, so a pause engaged from the CLI, another tab, or a
    // `touch data/ESTOP` shows up here within 15s without this page owning a poller.
    applyEstop(d.estop);
  }catch(e){}
}
setInterval(pollAdminBadge, 15000);
pollAdminBadge();
async function pollBoardBadge(){
  const bv=document.getElementById("boardview");
  if(bv && !bv.hidden) return;            // Board tab open — loadBoard owns the badge AND the mascot
  try{
    const d=await (await fetch("/api/needs-you")).json();
    const c=d.counts||{}, bk=d.buckets||{};
    setBoardBadge((c.needs_human||0)+(c.awaiting_clarification||0)+(c.awaiting_approval||0)+(c.failed||0));
    // Same payload, no extra request: this is how Otto knows about a Slack or scheduled run
    // while you are sat on the Admin tab.
    mascotFleet({run:bk.in_flight||[],
                 waiting:(c.awaiting_approval||0)+(c.awaiting_clarification||0),
                 needs:(c.needs_human||0)+(c.failed||0)});
  }catch(e){}
}
setInterval(pollBoardBadge, 15000);
pollBoardBadge();
// A run "needs review" when it ended needing a human: verify/QA/budget flagged it (needs_human),
// it was delivered unverified, or the workflow failed outright. Those are pulled OUT of the
// Finished column into a dedicated "Needs review" column so nothing rots silently. Distinct
// from "Awaiting your input": that's a run still IN FLIGHT, paused on an approve/deny or
// clarification signal to continue — "Needs review" is already done/dead and won't move on
// its own (Retry/Dismiss are the only ways forward). Module-level (not just loadBoard-local)
// so the card detail modal can reuse the same labels/hints.
const NEEDS_LABEL={verify_exhausted:"unverified",qa_fail:"QA failed",qa_inconclusive:"QA inconclusive",
                   review_fail:"review findings",review_inconclusive:"review inconclusive",
                   budget_exceeded:"over budget",workflow_error:"run failed",delivery_failed:"delivery failed",
                   workflow_dead:"worker died",stuck_timeout:"timed out",
                   local_fallback_disabled:"model failed, no fallback",
                   claude_auth_expired:"Claude login expired"};
const NEEDS_HINT={verify_exhausted:"Didn't pass automated verification after all attempts — read the result, then Retry, or Accept it if the judge was wrong.",
                  qa_fail:"Post-PR QA found real problems on the draft PR — review its findings, then Retry.",
                  qa_inconclusive:"Post-PR QA couldn't reach a verdict — check the draft PR yourself.",
                  review_fail:"The PR code review still has unaddressed findings after all fix rounds — review the draft PR, then Retry.",
                  review_inconclusive:"The PR code review couldn't reach a verdict — check the draft PR yourself.",
                  budget_exceeded:"Hit its cost/token ceiling before finishing — Retry to give it a fresh budget.",
                  workflow_error:"The run crashed — read the error below, then Retry.",
                  delivery_failed:"It finished but the result couldn't be delivered — check the target, then Retry.",
                  workflow_dead:"The worker process died mid-run — safe to Retry.",
                  local_fallback_disabled:"The model's endpoint couldn't do the work and OTTO_LOCAL_FALLBACK=0 forbids Claude from covering for it — nothing ran. The result names the endpoint — fix it (or flip the flag), then Retry.",
                  claude_auth_expired:"Claude rejected our credentials, so nothing ran — the subscription session on the worker host expired. Run `claude /login` there (or fix ANTHROPIC_API_KEY), then Retry.",
                  stuck_timeout:"Ran past its time limit without finishing — Retry or investigate.",
                  unverified:"Delivered but never passed verification — read the result, then Retry, or Accept it if the judge was wrong.",
                  "run failed":"The workflow ended in a failed/terminated state — check the audit log, then Retry."};
// Poll-flicker guard: the 3.5s board poll used to rebuild the whole board (and the health strip)
// on every tick even when nothing had changed, visibly flashing every card. We now fingerprint
// the render-affecting fields and skip the DOM rebuild when the signature is unchanged, so a
// quiet poll is invisible (mirrors loadChatList). A real state change moves the signature and
// repaints normally; a full (non-silent) load always repaints and reseeds the signature.
let _boardSig=null, _healthSig=null;
const _boardSignature=d=>JSON.stringify({t:d.temporal,ui:d.ui,items:(d.items||[]).map(it=>[
  it.id,it.run_id,it.status,it.phase,it.stage,it.verified,it.needs_human,it.pr,it.outcome,it.model,
  it.fallback_from,it.fallback_reason,it.retried_to,it.question,it.risk,it.risk_reason,it.cap,
  it.repo,it.in_place,it.scheduled,it.chat_key,it.start,it.end,it.kind,it.archived])});
async function loadBoard(silent){
  const el=document.getElementById("boardview");
  if(!silent) el.innerHTML=`<p class="sub">loading…</p>`;
  let data;
  try { data=await (await fetch("/api/board")).json(); }
  catch(e){ if(!silent) el.innerHTML=`<p class="err">Couldn't load the board (${esc(e.message)}).</p>`; return; }
  if(!data.temporal){
    el.innerHTML=`<div class="phead"><h1>Swarm board</h1><p class="sub">Needs Temporal — start Otto with <code>./run.sh</code>.</p></div>`;
    _boardSig=null;
    return;
  }
  const sig=_boardSignature(data);
  if(silent && sig===_boardSig){ loadBoardHealth(); return; }  // nothing changed — no DOM churn
  _boardSig=sig;
  const needsYou=it=>{
    if(it.needs_human) return NEEDS_LABEL[it.needs_human]||"needs review";
    // A repo-mode run that opened a draft PR is DONE — the PR is the deliverable and its review
    // happens on GitHub, so a failed automated verify is advisory, not an Otto hold (it shows a
    // soft "unverified" badge in Finished instead). Only an unverified run with no PR to catch a
    // bad result lands in "Needs review".
    if(it.status==="COMPLETED" && it.verified===false && !it.pr) return "unverified";
    if(it.status!=="COMPLETED" && it.status!=="RUNNING") return "run failed";
    return null;
  };
  const cols={needs:[],running:[],waiting:[],done:[]};
  const byId={};
  (data.items||[]).forEach(it=>{
    byId[it.id]=it;
    const nn=needsYou(it);
    if(nn){ it._needs=nn; cols.needs.push(it); }
    else if(it.status==="RUNNING") (/(awaiting)/.test(it.phase||"")?cols.waiting:cols.running).push(it);
    else cols.done.push(it);
  });
  // A closed run is filed by WHEN IT FINISHED. The server's window is ordered by start time, so
  // a long run started first but closed last would otherwise sit below results that landed before
  // it — the newest thing to finish must be the top card. Running cards keep start order.
  const closedAt=it=>it.end||it.start||"";
  cols.done.sort((a,b)=>closedAt(b).localeCompare(closedAt(a)));
  cols.needs.sort((a,b)=>closedAt(b).localeCompare(closedAt(a)));
  setBoardBadge(cols.needs.length+cols.waiting.length);
  mascotFleet({run:cols.running, waiting:cols.waiting.length, needs:cols.needs.length});
  const card=it=>{
    const failed=it.status!=="COMPLETED" && it.status!=="RUNNING";
    const risk=it.risk?`<span class="badge ${esc(it.risk)}">${esc(it.risk)}</span>`:"";
    // Temporal deletes a closed execution at the namespace retention TTL; past that the card is
    // served from Otto's own durable archive (`archived`) and its history link would 404, so the
    // link is dropped and a chip says where the card is coming from instead of leaving a dead one.
    const link=(data.ui&&!it.archived)?`${data.ui}/namespaces/default/workflows/${encodeURIComponent(it.id)}/${encodeURIComponent(it.run_id)}/history`:null;
    const archived=it.archived?`<span class="bchip" title="Temporal no longer holds this run's history \u2014 the card is served from Otto's own board archive, kept for the retention window in Admin \u2192 Runtime settings">archived</span>`:'';
    // verified===false is either a "Needs review" card (it._needs → its own ⚠ badge) or a
    // done-with-PR card (the `advisory` link below) — so `sub` stays a plain "done" here.
    const sub=it.phase || (it.status==="COMPLETED"?(it.verified===true?"done · verified":"done"):it.status.toLowerCase());
    // in flight RIGHT NOW — RUNNING but not parked at the gate and not held for a human.
    const live=it.status==="RUNNING" && !it._needs && !/awaiting/.test(it.phase||"");
    const askApprove=/awaiting approval/.test(it.phase||"");
    const askClarify=/awaiting clarification/.test(it.phase||"");
    const isSub=/-s\d+$/.test(it.id||"");          // a swarm child workflow (parent-id + -sN)
    const repo=it.repo?`<span class="bchip" title="ran in an isolated clone of this repo">&#9095; ${esc(it.repo)}</span>`:'';
    // Execution model of the newest attempt (from the transcript meta). Long served ids like
    // "google/gemma-4-26B-A4B-it" keep only their basename; a "local ·" / "hosted ·" prefix
    // says WHICH CLASS of model served it — Otto's own runtime drives both a model on this box
    // and a frontier vendor API, and they are not the same thing to read. No kind (Claude, or a
    // transcript predating the field) takes no prefix; `a → b` marks a FALLBACK (the chosen
    // model couldn't run this attempt — reason in the tooltip; the full record lives in Audit).
    const mshort=(it.model||'').split('/').pop();
    const fbshort=(it.fallback_from||'').split('/').pop();
    const mtitle=it.fallback_from
      ?`chosen model '${it.fallback_from}' couldn't run this attempt — ${it.fallback_reason||'no reason recorded'}. Ran on ${it.model} instead (details in the Audit tab).`
      :`execution model of the newest attempt${it.kind==='local'?" — running on Otto's local agent runtime (no claude -p)":(it.kind==='hosted'?" — running on a hosted frontier model via Otto's agent runtime (no claude -p)":' — via claude -p')}`;
    const model=it.model?`<span class="bchip ${it.kind==='local'?'local':(it.kind==='hosted'?'hosted':'')} ${it.fallback_from?'bfell':''}" title="${esc(mtitle)}">${it.fallback_from?esc(fbshort)+' → ':''}${it.kind==='local'?'local · ':(it.kind==='hosted'?'hosted · ':'')}${esc(mshort)}</span>`:'';
    const inplace=it.in_place?`<span class="bchip warn" title="edited a live checkout outside a workspace">&#9888; in-place edit</span>`:'';
    // WHICH stage of the pipeline this run is in. `phase` collapses everything before the first
    // attempt to a bare "running", so a card sat unchanged through routing, a 15-minute plan
    // preview and the gate — which reads as a stalled run rather than a working one.
    const STAGE_HELP={ROUTER:"choosing which capability handles this",
      CLARIFY:"deciding whether to ask a clarifying question first",
      DECOMPOSE:"checking whether this is really several independent sub-tasks",
      PLAN:"writing the plan you'll be asked to approve (read-only pass, up to 15 min)",
      GATE:"waiting on a human decision at the write gate",
      RUN:"executing the capability — running it, judging the result, retrying if needed",
      PR:"pushing the branch and opening the draft PR",
      REVIEW:"reviewing the PR's diff, and fixing what the review asks for",
      QA:"empirically validating the PR, and fixing what QA finds",
      DELIVER:"recording the run and delivering the result"};
    const stage=(it.status==="RUNNING"&&it.stage)
      ?`<span class="bchip stage s-${esc(it.stage.toLowerCase())}" title="${esc(STAGE_HELP[it.stage]||'current pipeline stage')}">${esc(it.stage.toLowerCase())}</span>`:'';
    const hint=it._needs?NEEDS_HINT[it._needs]:null;
    const needs=it._needs?`<span class="bchip warn" title="${esc(hint||'this run needs review')}">&#9888; ${esc(it._needs)}</span>`:'';
    // A completed repo-mode run that opened a draft PR but didn't pass automated verify is DONE
    // (not held) — show a soft advisory so the human knows to review the PR extra carefully.
    const advisory=(!it._needs && it.status==="COMPLETED" && it.verified===false && it.pr)
      ?`<a class="bchip warn" href="${esc(it.pr)}" target="_blank" rel="noopener" title="automated verification didn't pass — review this draft PR carefully before merging">&#9888; unverified — review PR &#8599;</a>`:'';
    // Retry starts a brand-new, unrelated workflow — the original card otherwise gives no sign
    // it did anything. Persisted server-side (survives reloads/polls) so it stays visible.
    const retriedLink=it.retried_to&&data.ui?`${data.ui}/namespaces/default/workflows/${encodeURIComponent(it.retried_to)}`:null;
    const retried=it.retried_to?`<div class="bretried">✓ retried as ${retriedLink?`<a href="${retriedLink}" target="_blank" rel="noopener">${esc(it.retried_to)}</a>`:esc(it.retried_to)} — see it under Running/Awaiting/Finished above</div>`:'';
    // The card only has room for a clipped preview — clicking it (or "Read full result") opens
    // the full, comfortably-laid-out text in a modal instead of squinting at a cramped 3-line box.
    const truncated=(it.outcome||"").endsWith("…");
    const statusCls=it.verified===true?'ok':(failed?'bad':(it._needs?'warn':''));
    const chips=[needs,advisory,stage,model,repo,it.scheduled?'<span class="bchip">scheduled</span>':'',isSub?'<span class="bchip">sub-task</span>':'',inplace,archived].filter(Boolean).join('');
    return `<div class="bcard ${failed?'failed':''}" data-run="${esc(it.id)}">
      <div class="btop"><span class="bcap">${esc(it.cap||'—')}</span>${risk}</div>
      <div class="bstatus${live?' live':''}"><span class="bdot ${statusCls}"></span>${esc(sub)}${live?'<span class="bell"><i>.</i><i>.</i><i>.</i></span>':''}</div>
      <div class="bchips">${chips}</div>
      ${hint?`<div class="bhint">${esc(hint)}</div>`:''}
      ${askApprove&&it.risk_reason?`<div class="bhint">why: ${esc(it.risk_reason)}</div>`:''}
      ${it.outcome?`${hint?'<div class="boutlabel">Result</div>':''}<div class="bout ${truncated?'clip':''}" data-expand="${esc(it.id)}" title="Click to read the full result">${renderMD(it.outcome)}</div>${truncated?`<div class="bmore" data-expand="${esc(it.id)}">Read full result →</div>`:''}`:''}
      ${retried}
      ${askClarify?`${it.question?`<div class="bquestion">&#10022; ${esc(it.question)}</div>`:''}<div class="bclarify"><input type="text" data-clarinput="${esc(it.id)}" placeholder="Type your answer to continue…" /><button class="btn approve sm" data-clarify="${esc(it.id)}">Send</button></div>`:''}
      ${askApprove?`<div class="bapprove"><button class="btn approve sm" data-approve="${esc(it.id)}">Approve</button><button class="btn decline sm" data-deny="${esc(it.id)}">Deny</button></div>`:''}
      ${it._needs?`<div class="bapprove"><button class="btn approve sm" data-retry="${esc(it.id)}">${it.retried_to?'Retry again':'Retry'}</button><button class="btn accept sm" data-accept="${esc(it.id)}" title="the work is fine \u2014 the judge was wrong. Records the override and files the approach in solutions.">Accept</button><button class="btn decline sm" data-dismiss="${esc(it.id)}">Dismiss</button></div>`:''}
      ${it.status==="RUNNING"&&!it._needs?`<div class="bapprove"><button class="btn danger sm" data-terminate="${esc(it.id)}" title="hard-stop this run now — no cleanup, the card disappears">Terminate</button></div>`:''}
      <div class="bfoot"><span class="bwhen" title="${esc(it.end?`finished ${it.end} \u00b7 started ${it.start||'?'}`:`started ${it.start||'?'}`)}">${esc(shortWhen(it.end||it.start))}</span><a href="#" class="bdebug" data-debug="${esc(it.id)}" title="attempts, verify critiques, transcript, failure reason">🔍 Debug</a>${it.chat_key?`<a href="#" data-openchat="${esc(it.chat_key)}" title="open this run's conversation">Chat</a>`:''}${link?`<a href="${link}" target="_blank" rel="noopener">Temporal ↗</a>`:''}</div>
    </div>`;
  };
  const col=(title,items,cls,tip)=>`<div class="bcol${items.length?'':' empty'}">
    <div class="bcolhead ${cls}" title="${tip}">${title} <span>${items.length}</span></div>
    ${items.map(card).join("") || '<p class="memempty">none</p>'}</div>`;
  el.innerHTML=`
    <div class="phead"><h1>Swarm board</h1>
      <p class="sub">Live Temporal runs, refreshed automatically.</p>
      <div class="pside" id="board-health"></div></div>
    <div class="board">
      ${col("Awaiting you",cols.waiting,"wait","paused mid-flight — approve, deny, or answer the clarification")}
      ${col("Running",cols.running,"run","in flight now")}
      ${col("Needs review",cols.needs,"wait","finished or died needing a look — retry, accept, or dismiss")}
      ${col("Finished",cols.done,"done","done, with each card's verify verdict")}
    </div>`;
  loadBoardHealth();
  el.querySelectorAll("[data-approve]").forEach(b=>b.addEventListener("click",()=>boardSignal(b.dataset.approve,true)));
  el.querySelectorAll("[data-deny]").forEach(b=>b.addEventListener("click",()=>boardSignal(b.dataset.deny,false)));
  el.querySelectorAll("[data-clarify]").forEach(b=>b.addEventListener("click",()=>boardClarify(b.dataset.clarify,b)));
  el.querySelectorAll("[data-clarinput]").forEach(i=>i.addEventListener("keydown",e=>{
    if(e.key==="Enter"){ e.preventDefault(); const b=el.querySelector(`[data-clarify="${CSS.escape(i.dataset.clarinput)}"]`); if(b) boardClarify(i.dataset.clarinput,b); }
  }));
  el.querySelectorAll("[data-retry]").forEach(b=>b.addEventListener("click",()=>needsYouRetry(b.dataset.retry,b)));
  el.querySelectorAll("[data-accept]").forEach(b=>b.addEventListener("click",()=>needsYouAccept(b.dataset.accept,b)));
  el.querySelectorAll("[data-dismiss]").forEach(b=>b.addEventListener("click",()=>needsYouDismiss(b.dataset.dismiss)));
  el.querySelectorAll("[data-terminate]").forEach(b=>b.addEventListener("click",()=>boardTerminate(b.dataset.terminate,b)));
  el.querySelectorAll("[data-expand]").forEach(x=>x.addEventListener("click",e=>{ e.preventDefault(); openCardModal(byId[x.dataset.expand], data.ui); }));
  el.querySelectorAll("[data-debug]").forEach(a=>a.addEventListener("click",e=>{ e.preventDefault(); const it=byId[a.dataset.debug]; openRunDebug(a.dataset.debug, it&&it.cap, data.ui); }));
  el.querySelectorAll("[data-openchat]").forEach(a=>a.addEventListener("click",async e=>{
    e.preventDefault();
    const key=a.dataset.openchat;
    // The board card's chat_key IS the chat thread id for unattended runs (board: gh-issue-N,
    // schedule: chat-<sid>). Confirm the thread exists, then open it (which switches to the
    // Chat tab and refreshes the sidebar).
    let chat; try { chat=await (await fetch("/api/chats/get?id="+encodeURIComponent(key))).json(); } catch(err){ chat=null; }
    if(chat&&chat.id) openChat(key);
    else alert("No conversation was recorded for this run.");
  }));
  focusPendingRun();
}
async function loadBoardHealth(){
  const box=document.getElementById("board-health");
  if(!box) return;
  let d;
  try { d=await (await fetch("/api/board-health")).json(); } catch(e){ return; }
  const h=d.health; if(!h) return;
  const dot=(ok,label,title)=>`<span class="hchip ${ok?'ok':'bad'}" title="${esc(title||'')}">${ok?'●':'○'} ${esc(label)}</span>`;
  const poll=h.board_poll||{}, reaper=h.reaper||{};
  const bits=[dot(h.connected,"Temporal","worker + server connection")];
  if(h.board_enabled){
    bits.push(dot(poll.exists && !poll.paused, "Board poll",
      poll.last_run?("last poll "+shortWhen(poll.last_run)):"no recent poll"));
    bits.push(dot(reaper.exists && !reaper.paused, "Reaper",
      reaper.last_run?("last sweep "+shortWhen(reaper.last_run)):"stuck-card sweeper"));
  }
  // Spend observability (from the audit log) — today's and all-time cost.
  const c=d.costs||{};
  const today=new Date().toISOString().slice(0,10);
  const spentToday=(c.by_day&&c.by_day[today])||0;
  bits.push(`<span class="hchip" title="notional API-equivalent spend today">$ ${spentToday.toFixed(2)} today</span>`);
  bits.push(`<span class="hchip" title="notional API-equivalent spend, all runs in the audit log">$ ${(c.total_usd||0).toFixed(2)} total</span>`);
  const b=c.budget||{};
  if(b.hard_tokens||b.hard_usd)
    bits.push(`<span class="hchip" title="per-run hard budget ceiling">budget: ${b.hard_tokens?b.hard_tokens+' tok':''}${b.hard_usd?(' $'+b.hard_usd):''}/run</span>`);
  const html=`<div class="healthstrip">${bits.join("")}</div>`;
  // Skip the repaint only when unchanged AND the strip is already painted — a full board
  // rebuild recreates an EMPTY #board-health, so we must repaint into it even if the html
  // string matches the last one.
  if(html===_healthSig && box.innerHTML) return;
  _healthSig=html;
  box.innerHTML=html;
}
async function boardSignal(id, ok){
  try { await fetch("/api/wf/signal",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id,signal:"approve",value:ok})}); }
  catch(e){}
  loadBoard(true);
}
// Answer an awaiting-clarification run straight from the board. Sends the provide_clarification
// signal (same one the chat's clarify card uses) so a run paused mid-flight can be unblocked
// without hunting for its originating chat — the board is often where it's noticed first, and
// an interactive web-* run has no chat_key / "Open conversation" link back.
async function boardClarify(id, btn){
  const card=btn.closest(".bcard");
  const input=card&&card.querySelector(`[data-clarinput="${CSS.escape(id)}"]`);
  const answer=(input&&input.value||"").trim();
  if(!answer){ if(input) input.focus(); return; }
  btn.disabled=true; btn.textContent="Sending…"; if(input) input.disabled=true;
  try { await fetch("/api/wf/signal",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id,signal:"clarify",value:answer})}); }
  catch(e){ btn.disabled=false; btn.textContent="Send"; if(input) input.disabled=false; alert("Couldn't send the answer: "+e.message); return; }
  loadBoard(true);   // repaint: the run leaves "Awaiting your input" and resumes under Running
}
/* "Needs review" card actions (#116): a dead/unverified run has no other way to act on it from
   Otto itself — Retry re-submits the same request as a brand-new run (fresh routing,
   verification, and approval gate); Dismiss just hides the card (the underlying workflow +
   audit trail are untouched). */
async function needsYouRetry(id,btn){
  // Retry starts a brand-new workflow and the server auto-dismisses the source card
  // (retrying IS acknowledging it). Remove the card from the column the moment the server
  // confirms — a card that lingers after a click reads as "did that even work?".
  if(btn){ btn.disabled=true; btn.textContent="Retrying…"; }
  let r;
  try {
    r=await (await fetch("/api/needs-you/retry",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id})})).json();
    if(r.error){ alert("Couldn't retry: "+r.error); if(btn){ btn.disabled=false; btn.textContent="Retry"; } return; }
  } catch(e){ alert("Couldn't retry: "+e.message); if(btn){ btn.disabled=false; btn.textContent="Retry"; } return; }
  if(btn){ const c=btn.closest(".bcard"); if(c) c.remove(); }
  loadBoard(true);   // repaint: the retried run appears under Running with its new id
}
async function boardTerminate(id,btn){
  if(!confirm("Terminate this run? It stops immediately — no cleanup, no result — and the card disappears. This can't be undone (you can Retry the same request later from the audit trail).")) return;
  if(btn){ btn.disabled=true; btn.textContent="Terminating…"; }
  let r={};
  try { r=await (await fetch("/api/wf/terminate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id})})).json(); }
  catch(e){ r={error:e.message}; }
  if(r.error){ alert("Couldn't terminate: "+r.error); if(btn){ btn.disabled=false; btn.textContent="Terminate"; } return; }
  if(btn){ const c=btn.closest(".bcard"); if(c) c.remove(); }
  loadBoard(true);
}
/* Accept: the opposite verdict to Dismiss. Dismiss means "stale, hide it"; Accept means the
   automated judges were wrong and this result stands — the one label Otto can't produce for
   itself. The server records it (scorecard false-fail rate + solutions) and dismisses the card. */
async function needsYouAccept(id,btn){
  if(btn){ btn.disabled=true; btn.textContent="Accepting\u2026"; }
  let r={};
  try { r=await (await fetch("/api/needs-you/accept",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id})})).json(); }
  catch(e){ r={error:e.message}; }
  if(r.error){ alert("Couldn't accept: "+r.error); if(btn){ btn.disabled=false; btn.textContent="Accept"; } return; }
  if(btn){ const c=btn.closest(".bcard"); if(c) c.remove(); }
  loadBoard(true);
}
async function needsYouDismiss(id){
  if(!confirm("Dismiss this run? It disappears from the board (nothing about the run itself is deleted).")) return;
  try { await fetch("/api/needs-you/dismiss",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id})}); }
  catch(e){}
  const b=document.querySelector(`[data-dismiss="${(window.CSS&&CSS.escape)?CSS.escape(id):id}"]`);
  if(b){ const c=b.closest(".bcard"); if(c) c.remove(); }
  loadBoard(true);
}
/* Card detail modal: a board card only has room for a clipped 2-3 line preview (illegible for
   anything but the shortest results) — clicking it fetches the run's FULL, untruncated result
   (not shipped in the polled /api/board list to keep that endpoint light) and shows it laid out
   like a chat message. */
function closeCardModal(){ document.getElementById("cardModal").hidden=true; }
async function openCardModal(it,ui){
  if(!it) return;
  const modal=document.getElementById("cardModal"), title=document.getElementById("cardModalTitle"),
        body=document.getElementById("cardModalBody");
  const link=ui?`${ui}/namespaces/default/workflows/${encodeURIComponent(it.id)}/${encodeURIComponent(it.run_id)}/history`:null;
  title.innerHTML=`<b>${esc(it.cap||'—')}</b><br>${esc(it.id)}${it.repo?' · repo: '+esc(it.repo):''}${link?` · <a href="${link}" target="_blank" rel="noopener">Temporal ↗</a>`:''}`;
  const hint=it._needs?NEEDS_HINT[it._needs]:null;
  body.innerHTML=(hint?`<div class="modalHint">⚠ ${esc(hint)}</div>`:'')+`<p class="sub">Loading full result…</p>`;
  modal.hidden=false;
  let r;
  try { r=await (await fetch("/api/board/full?id="+encodeURIComponent(it.id))).json(); }
  catch(e){ body.innerHTML+=`<p class="err">Couldn't load the full result (${esc(e.message)}).</p>`; return; }
  const hintHtml=hint?`<div class="modalHint">⚠ ${esc(hint)}</div>`:'';
  if(r.error){ body.innerHTML=hintHtml+`<p class="err">${esc(r.error)}</p>`; return; }
  if(r.status==="RUNNING"){ body.innerHTML=hintHtml+`<p class="sub">Still running — the full result isn't available until it finishes.</p>`; return; }
  body.innerHTML=hintHtml+`<div class="result">${renderMD(r.result||"(no output)")}</div>`;
}
// Run-detail debug drawer (#96): one place to see WHY a run did what it did — every verify
// attempt with its critique, the compacted execution transcript (tool calls + results), model/
// backend/fallback, and the terminal reason. Reuses the cardModal shell; all data from
// /api/run/detail (audit + content logs + transcripts — works even after the workflow is gone).
function _dbgTraceHtml(events){
  if(!events||!events.length) return `<div class="sub">No transcript captured (a local-runtime or already-swept run).</div>`;
  return `<div class="dbgtrace">`+events.map(l=>{
    const cls=/^tool_use/.test(l)?"tu":(/^tool_result/.test(l)?"tr":"as");
    return `<span class="${cls}">${esc(l)}</span>`;
  }).join("\n")+`</div>`;
}
async function openRunDebug(wid, capLabel, ui){
  if(!wid) return;
  const modal=document.getElementById("cardModal"), title=document.getElementById("cardModalTitle"),
        body=document.getElementById("cardModalBody");
  const link=ui?`${ui}/namespaces/default/workflows/${encodeURIComponent(wid)}/history`:null;
  title.innerHTML=`<b>${esc(capLabel||'run')}</b> · debug<br>${esc(wid)}${link?` · <a href="${link}" target="_blank" rel="noopener">Temporal ↗</a>`:''}`;
  body.innerHTML=`<p class="sub">Loading run detail…</p>`;
  modal.hidden=false;
  let r;
  try { r=await (await fetch("/api/run/detail?id="+encodeURIComponent(wid))).json(); }
  catch(e){ body.innerHTML=`<p class="err">Couldn't load run detail (${esc(e.message)}).</p>`; return; }
  if(!r.found){ body.innerHTML=`<p class="sub">No audit record found for this run yet.</p>`; return; }
  const meta=[];
  if(r.cap) meta.push(`<span class="k">${esc(r.cap)}</span>${r.risk?' · '+esc(r.risk):''}`);
  if(r.repo) meta.push(`repo: <span class="k">${esc(r.repo)}</span>`);
  if(r.needs_human) meta.push(`status: <span class="k" style="color:var(--warn)">needs human — ${esc(r.needs_human)}</span>`);
  else if(r.attempts.length) { const v=r.attempts[r.attempts.length-1].verified; meta.push(`status: <span class="k">${v===true?'verified':(v===false?'completed (unverified)':'done')}</span>`); }
  const totCost=r.attempts.reduce((s,a)=>s+(a.cost_usd||0),0);
  meta.push(`${r.attempts.length} attempt${r.attempts.length===1?'':'s'}`+(totCost?` · $${totCost.toFixed(2)}`:''));
  const hint=r.needs_human?(NEEDS_HINT[NEEDS_LABEL[r.needs_human]]||NEEDS_HINT[r.needs_human]):null;
  const atts=r.attempts.map(a=>{
    const v=a.verified===true?'<span class="dbgverd pass">✓ verify pass</span>'
      :(a.verified===false?'<span class="dbgverd fail">✗ verify fail</span>':'<span class="dbgverd none">no verify</span>');
    const fb=a.fallback_from?` · <span title="${esc(a.fallback_reason||'')}">${esc((a.fallback_from||'').split('/').pop())} ⇢ ${esc((a.model||'').split('/').pop())}</span>`:(a.model?` · ${esc((a.model||'').split('/').pop())}`:'');
    const dur=a.duration_s!=null?` · ${a.duration_s}s`:'';
    const cost=a.cost_usd?` · $${(a.cost_usd).toFixed(2)}`:'';
    const openFirst=(a.verified===false)?' open':'';   // failed attempts expanded by default
    return `<div class="dbgatt${openFirst}"><div class="dbgatt-h" data-dbgtoggle>
        <span class="an">attempt ${a.attempt}</span>${v}<span>${fb}${dur}${cost}${a.backend?' · '+esc(a.backend):''}</span>
      </div><div class="dbgbody">
        ${a.critique?`<div class="dbgsec">verify critique</div><div class="dbgcrit">${esc(a.critique)}</div>`:''}
        <div class="dbgsec">execution transcript</div>${_dbgTraceHtml(a.events)}${a.events_truncated?'<div class="sub">(transcript truncated)</div>':''}
        ${a.result?`<div class="dbgsec">attempt result</div><div class="dbgresult">${esc((a.result||'').slice(0,4000))}</div>`:''}
      </div></div>`;
  }).join("");
  body.innerHTML=(hint?`<div class="modalHint">⚠ ${esc(hint)}</div>`:'')
    +`<div class="dbgmeta">${meta.join(" &nbsp;·&nbsp; ")}</div>`
    +(r.request?`<div class="dbgsec">request</div><div class="dbgresult" style="margin-bottom:14px">${esc((r.request||'').slice(0,2000))}</div>`:'')
    +(r.terminal&&r.terminal.detail?`<div class="dbgsec">terminal detail</div><div class="dbgresult" style="margin-bottom:14px">${esc((r.terminal.detail||'').slice(0,2000))}</div>`:'')
    +(atts||`<p class="sub">No attempts recorded yet — the run is still in flight, or it ended before executing.</p>`);
  body.querySelectorAll("[data-dbgtoggle]").forEach(h=>h.addEventListener("click",()=>h.closest(".dbgatt").classList.toggle("open")));
}
document.getElementById("cardModalClose").addEventListener("click",closeCardModal);
document.getElementById("cardModal").addEventListener("click",e=>{ if(e.target.id==="cardModal") closeCardModal(); });
document.addEventListener("keydown",e=>{ if(e.key==="Escape" && !document.getElementById("cardModal").hidden) closeCardModal(); });
