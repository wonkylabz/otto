"use strict";
/* Chat: the composer, the live run watcher, the pipeline strip, the approval gate, and the
   persisted chat list. The interactive ingress — every other view is read-mostly. */
const HLABEL_SHORT={"scheduled-job":"cron"};   // display only — the stored label is unchanged
const PIPE = [
  ["INGRESS","received your request"], ["DECOMPOSE","checking for independent sub-tasks"],
  ["ROUTER","picking a capability"],
  ["CLARIFY","checking for missing info"], ["PLAN","previewing what it will do"],
  ["GATE","approval check"],
  ["RUN","executing capability"], ["AUDIT","recording the run"],
];


const pipeEl = document.getElementById("pipe");
function buildPipe(){ pipeEl.innerHTML=""; PIPE.forEach(([lbl,det])=>{ const n=document.createElement("div"); n.className="node"; n.dataset.lbl=lbl;
  n.title=det;
  n.innerHTML=`<div class="gut"><div class="tile"></div><div class="stem"></div></div><div class="ncontent"><div class="lbl">${lbl}<span class="timer"></span></div><span class="detail"></span></div>`; pipeEl.appendChild(n);}); stageAnchor=performance.now();  pipeEl.classList.add("idle"); }
function addPick(cap){
  // remember the routed capability on the active chat (so history shows a label even in
  // direct mode, which has no session); a session, when present, refines this in persistChat.
  if(recording && activeChat) activeChat.cap={name:cap.name, risk:cap.risk};
  const n=[...pipeEl.children].find(c=>c.dataset.lbl==="ROUTER"); if(!n) return;
  const box=document.createElement("div"); box.className="pick "+cap.risk;
  box.innerHTML=`<span class="pdot"></span><span class="pkind">${cap.kind}</span> ${cap.name}`;
  n.querySelector(".ncontent").appendChild(box);
}
// ---- per-stage timing ----
// `stageAnchor` marks when the previous stage finished (or the pipe was (re)built). A stage's
// duration is measured from its own start (when it first goes active/gatehold) if it had one,
// else from the anchor — so even stages set straight to "done" (a skipped clarify/gate, or the
// Temporal watcher seeing a stage already complete) get the wall-clock elapsed to reach them.
let stageAnchor=0;

// Wall-clock span of a finished run: earliest stage start to latest stage finish, from the
// workflow's own per-stage `times` (PLAN/ROUTER/CLARIFY/GATE/RUN) — real elapsed time, including
// any clarify/approval wait, not just execution.
function totalDurMs(times){
  const stamps=Object.values(times||{}).filter(t=>t&&t.start!=null);
  if(!stamps.length) return null;
  const starts=stamps.map(t=>t.start), ends=stamps.map(t=>t.start+(t.dur||0));
  return Math.max(...ends)-Math.min(...starts);
}
buildPipe();
// `opts.startMs`/`opts.durMs` (wall-clock epoch ms) let a stage's timer be seeded from the
// WORKFLOW's own record of when it entered/left that stage (OttoWorkflow._times, surfaced via
// the status query) rather than always anchoring to `performance.now()` — which is meaningless
// across a chat switch/reattach (issue #117: reattaching used to always restart every timer,
// since the pipe is rebuilt from scratch and performance.now() at reattach time was the only
// anchor available). `performance.now()` isn't wall-clock, so an epoch `startMs` is converted via
// the elapsed-since-then offset.
// details that mean a stage was bypassed rather than actually run — painted muted (see .skipped CSS)
const SKIP_DETAILS=new Set(["skipped","no questions","read-only · auto-approved","no approval needed"]);
function setNode(lbl,state,detail,opts){ opts=opts||{}; const n=[...pipeEl.children].find(c=>c.dataset.lbl===lbl); if(!n)return;
  if(state) pipeEl.classList.remove("idle");
  const wasTerminal=/\b(done|failed)\b/.test(n.className);
  const skipped = state==="done" && SKIP_DETAILS.has(detail);
  n.className="node"+(state?" "+state:"")+(skipped?" skipped":""); if(detail!==undefined)n.querySelector(".detail").textContent=detail;
  const t=performance.now();
  // Reset to pending: drop the elapsed time with it, or the stage keeps a number that reads as
  // "this took 31s" for work that hasn't happened yet.
  if(!state){ delete n.dataset.tstart;
    const tm0=n.querySelector(".timer"); if(tm0){ tm0.textContent=""; tm0.classList.remove("set"); } }
  // `restart` re-anchors a stage that legitimately runs TWICE in one turn (PLAN/GATE across a
  // "request changes" round). Without it the second pass inherits the first pass's start and the
  // timer reports the sum, so a 20s re-preview reads as minutes.
  if((state==="active"||state==="gatehold") && (!n.dataset.tstart || opts.restart))
    n.dataset.tstart = String(opts.startMs!=null ? t-(Date.now()-opts.startMs) : t);
  // Otto's corner rides the same funnel: whatever stage the rail lights up is the stage he
  // is in. Only active/gatehold - a stage going `done` is not a new mood, the NEXT stage is.
  if(state==="active"||state==="gatehold") mascotTurn(lbl);
  if(state==="failed" && !wasTerminal) mascotReact("error","that didn't work","I stopped at "+lbl);
  if((state==="done"||state==="failed") && !wasTerminal){
    const tm=n.querySelector(".timer");
    if(tm){ const dur = opts.durMs!=null ? opts.durMs : (t-(n.dataset.tstart?+n.dataset.tstart:stageAnchor));
      tm.textContent=fmtDur(dur); tm.classList.add("set"); }
    stageAnchor=t;
  }
}
// live-tick the timer of whatever stage is currently active/holding
setInterval(()=>{ const now=performance.now(); for(const n of pipeEl.children){
  if(n.dataset.tstart && /\b(active|gatehold)\b/.test(n.className)){ const tm=n.querySelector(".timer"); if(tm) tm.textContent=fmtDur(now-+n.dataset.tstart); }
}}, 100);

const led={runs:0,appr:0,cost:0};
function render(){
  document.getElementById("led-runs").textContent=led.runs;
  document.getElementById("led-appr").textContent=led.appr;
  document.getElementById("led-cost").textContent="$"+led.cost.toFixed(2);
  // Tallies are per-chat: stash onto the active chat and save so they survive a switch/reload.
  if(activeChat){ activeChat.stats={runs:led.runs, appr:led.appr, cost:led.cost};
    clearTimeout(chatSaveTimer); chatSaveTimer=setTimeout(persistChat, 600); }
}

const stream=document.getElementById("stream"), input=document.getElementById("input"), sendBtn=document.getElementById("send");
let busy=false, TEMPORAL=false, currentSession=null;
let suppressCarry=false;   // set by "New task" so its next submit starts a clean slate (no context carry)
function scroll(){ stream.scrollTop=stream.scrollHeight; }

/* ---- live-run tracking ----
   A running turn is a Temporal workflow that keeps going server-side regardless of the
   browser. We persist its workflow id on the chat (`run_id`) and drive the on-screen view
   from a `watchLoop`. Only ONE watcher is "live" at a time — `liveWatch` is bumped whenever
   we switch chats / start a new watcher, so any superseded loop quietly parks itself (the
   workflow runs on; reopening the chat or reloading reattaches a fresh watcher). This is
   what lets you leave a running chat and what survives a page refresh. */
let liveWatch=0;
function stopWatch(){ liveWatch++; }                  // park the current watcher (if any)
function setRun(wid){ if(activeChat){ activeChat.run_id=wid; persistChat(); } syncSendBtn(); }
function clearRun(){ if(activeChat){ activeChat.run_id=null; persistChat(); } syncSendBtn(); }
// The composer's single button doubles as Send / Stop: it's only a Stop while a TEMPORAL
// workflow id is actually known (a direct-path run is a blocking HTTP call with nothing
// server-side to terminate, and there's a window right after dispatch, before /api/submit
// returns an id, where nothing exists yet to kill either).
function syncSendBtn(){
  const stoppable = busy && TEMPORAL && !!(activeChat && activeChat.run_id);
  sendBtn.classList.toggle("stop", stoppable);
  sendBtn.disabled = busy && !stoppable;
  sendBtn.innerHTML = stoppable ? "&#9632;" : "&#8593;";
  sendBtn.title = stoppable ? "Stop" : "Send";
}
async function stopRun(){
  if(!activeChat || !activeChat.run_id) return;
  const wid=activeChat.run_id;
  liveWatch++;                              // park whatever watchLoop/gate/clarify is waiting on this run
  const content=[...stream.querySelectorAll(".msg.eno .content")].pop();
  if(content) clearThinking(content);
  const active=[...pipeEl.children].find(c=>/\b(active|gatehold)\b/.test(c.className));
  if(active) setNode(active.dataset.lbl,"failed","stopped by you");
  try { await api("/api/wf/terminate",{id:wid}); setNode("AUDIT","done","recorded: terminated"); }
  catch(e){ /* already finished, or unreachable — nothing more to reconcile client-side */ }
  if(content) content.innerHTML=`<p>⏹ Stopped — I terminated the running task.</p>`;
  clearRun(); led.runs++; render(); finishTurn();
}

/* ---- chat history (persisted server-side; reopen + continue past chats) ---- */
let activeChat=null, recording=true, chatSaveTimer=null;
function newChatId(){ return (self.crypto&&crypto.randomUUID)?crypto.randomUUID():("c"+Date.now()+Math.random().toString(16).slice(2)); }
function recordMsg(role, text, ts){
  if(!recording || text==null) return;
  if(!activeChat) activeChat={id:newChatId(), title:"New chat", messages:[], session_id:null, cap:null};
  activeChat.messages.push({role, text:String(text), ts:ts||new Date().toISOString()});
  if(role==="user" && activeChat.messages.filter(m=>m.role==="user").length===1)
    activeChat.title=String(text).slice(0,80);
  clearTimeout(chatSaveTimer); chatSaveTimer=setTimeout(persistChat, 600);
}
async function persistChat(){
  if(!activeChat) return;
  activeChat.stats={runs:led.runs, appr:led.appr, cost:led.cost};
  if(currentSession){ activeChat.session_id=currentSession.id; activeChat.cap=currentSession.cap;
    activeChat.repo=currentSession.repo||null; activeChat.git_run_id=currentSession.git_run_id||null;
    activeChat.git_branch=currentSession.git_branch||null; }
  try { await fetch("/api/chats/save",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(activeChat)}); loadChatList(); }
  catch(e){}
}
// Shared driver for a running workflow's on-screen view. Reattach-safe: it bails the instant
// a newer watcher starts or the user switches away from `chatId` (the workflow keeps running;
// the chat's persisted run_id lets us reattach later). Used both by a fresh submit and when
// reopening/reloading a chat whose run is still in flight.
async function watchLoop(wid, chatId, content, sess, flags){
  const myWatch = ++liveWatch;
  flags = flags || {};
  let pickShown=!!flags.pickShown, clarDone=!!flags.clarDone, gateDone=!!flags.gateDone, runActive=false;
  // `decomposeDone` tracks the early fan-out CHECK (plan_swarm — is this really several
  // independent tasks?); `previewDone` tracks the separate, later PLAN node — the read-only dry
  // run that shows the human the actual operations at the approval gate. Two different passes
  // that both used to be called "PLAN" in this pipe, which is what made the diagram misrepresent
  // when the real planning work happens (it runs right before GATE, not right after INGRESS).
  let decomposeDone=!!flags.decomposeDone, previewDone=false, swarmShown=false, isSwarm=false, progressTick=0;
  const alive = ()=> myWatch===liveWatch && activeChat && activeChat.id===chatId;
  const capOf = (st)=> st.cap || (sess && sess.cap) || null;
  while(true){
    if(!alive()) return;                          // parked — superseded or switched away
    let st;
    try { st=await (await fetch("/api/wf?id="+encodeURIComponent(wid))).json(); }
    catch(e){ await sleep(500); continue; }
    if(!alive()) return;

    // Swarm: the planner fanned the request out into parallel child workflows. The parent never
    // routes/clarifies/plans/gates a single cap (each child does, independently — writes
    // approved on the Board); DECOMPOSE carries "fanned out", the single-cap nodes collapse, and
    // we wait for the merged result.
    if(st.swarm) isSwarm=true;
    if(isSwarm && !swarmShown){
      swarmShown=true; pickShown=true; clarDone=true; gateDone=true; runActive=true;
      decomposeDone=true; previewDone=true;
      setNode("DECOMPOSE","done","fanned out · "+(st.children||[]).length);
      setNode("ROUTER","done","—"); setNode("CLARIFY","done","—");
      setNode("PLAN","done","per sub-task"); setNode("GATE","done","per sub-task");
      setNode("RUN","active","running sub-tasks…");
      clearThinking(content); showSwarm(content, st.children||[]);
    }

    const times = st.times || {};
    // Pre-route stages: while the cap is still unknown (routing in flight), paint the ACTIVE
    // stage from the workflow's own timestamps — a reattached pipe otherwise leaves DECOMPOSE
    // ticking for the whole ROUTER window and reads as "stuck in plan" (user-reported: a
    // routing retry took 3+ minutes and the pipe blamed the fan-out check the whole time).
    if(!isSwarm && !pickShown){
      if(!decomposeDone && times.DECOMPOSE && times.DECOMPOSE.dur!=null){ setNode("DECOMPOSE","done","single task",{durMs:times.DECOMPOSE.dur}); decomposeDone=true; }
      if(times.ROUTER && times.ROUTER.start && times.ROUTER.dur==null) setNode("ROUTER","active","picking a capability…",{startMs:times.ROUTER.start});
    }
    if(st.cap && !pickShown && !isSwarm){
      if(!decomposeDone){ setNode("DECOMPOSE","done","single task",{durMs:times.DECOMPOSE&&times.DECOMPOSE.dur}); decomposeDone=true; }
      pickShown=true; setNode("ROUTER","done","picked:",{durMs:times.ROUTER&&times.ROUTER.dur}); addPick(st.cap);
      if(!flags.quietPick){ content.innerHTML=`<p>I'll use <code>${esc(st.cap.name)}</code> <span class="badge ${st.cap.risk}">${st.cap.risk}</span></p>`; stampMsg(content); }
      showThinking(content,"Working…");
    }
    // Between CLARIFY and the approval gate the backend runs the real PLAN preview — a full
    // agentic dry-run pass that can take minutes — before `awaiting_approval` flips true, so
    // `st.state` stays "running" the WHOLE time PLAN executes (self._awaiting_approval is only
    // set once plan_capability returns). Gating this on the client-side `clarDone` flag was
    // circular — nothing sets clarDone until the awaiting_approval/done/running-with-gate
    // branches below, all of which fire AFTER PLAN finishes, so PLAN never painted as active at
    // all. Key off the workflow's own `times.CLARIFY`/`times.PLAN` instead, same as the
    // DECOMPOSE/ROUTER pre-route block above.
    if(!isSwarm && !gateDone && !previewDone){
      if(!clarDone && times.CLARIFY && times.CLARIFY.dur!=null){
        setNode("CLARIFY","done","no questions",{durMs:times.CLARIFY.dur}); clarDone=true;
      }
      if(times.PLAN && times.PLAN.dur!=null){ setNode("PLAN","done","previewed",{durMs:times.PLAN.dur}); previewDone=true; }
      else if(times.PLAN && times.PLAN.start) setNode("PLAN","active","previewing what it will do…",{startMs:times.PLAN.start});
    }

    if(st.state==="awaiting_clarification" && !clarDone){
      setNode("CLARIFY","gatehold","waiting for your answer",{startMs:times.CLARIFY&&times.CLARIFY.start}); clearThinking(content);
      const ans=await clarifyTurn(content, st.question);
      if(!alive()) return;
      await api("/api/wf/signal",{id:wid,signal:"clarify",value:ans});
      setNode("CLARIFY","done","answered"); clarDone=true; showThinking(content,"Working…");
    }
    else if(st.state==="awaiting_approval"){
      if(!clarDone){ setNode("CLARIFY","done","no questions",{durMs:times.CLARIFY&&times.CLARIFY.dur}); clarDone=true; }
      if(!previewDone){ setNode("PLAN","done","previewed",{durMs:times.PLAN&&times.PLAN.dur}); previewDone=true; }
      if(!gateDone){
        const cap=capOf(st);
        setNode("GATE","gatehold","waiting for your approval",{startMs:times.GATE&&times.GATE.start}); clearThinking(content);
        // A "request changes" round re-runs PLAN and re-enters GATE, but the loop is parked inside
        // gate() for the whole round, so the diagram would sit on "waiting for your approval" —
        // blaming the human for a wait that is Otto re-planning. gate() drives these two stages
        // back through here; `alive()` stays on THIS side so a chat switch can't repaint another
        // chat's pipe.
        const onReplan=(phase,t)=>{
          if(!alive()) return;
          if(phase==="replanning"){
            setNode("GATE","","");
            setNode("PLAN","active","re-planning with your feedback…",
                    {startMs:t.PLAN&&t.PLAN.start, restart:true});
          } else {
            setNode("PLAN","done","re-previewed",{durMs:t.PLAN&&t.PLAN.dur});
            setNode("GATE","gatehold","waiting for your approval",
                    {startMs:t.GATE&&t.GATE.start, restart:true});
          }
        };
        if(st.replanning) onReplan("replanning", times);   // reload landed mid-round
        const ok=await gate(content, cap, st.plan, st.repo, st.risk_reason, st.plan_concerns, st.plan_revisions, st.max_plan_revisions, st.replanning, wid, onReplan, st.plan_model);
        if(!alive()) return;
        await api("/api/wf/signal",{id:wid,signal:"approve",value:ok});
        setNode("GATE","done", ok?"approved":"denied"); gateDone=true;
        if(ok){ led.appr++; render(); }
        if(ok) showThinking(content,"Running "+(cap?cap.name:"")+"…");
      }
    }
    else if(st.state==="done"){
      const cap=capOf(st);
      if(!decomposeDone){ setNode("DECOMPOSE","done", st.swarm?"fanned out":"single task"); decomposeDone=true; }
      if(!clarDone) setNode("CLARIFY","done","no questions");
      const declined=/^Declined/.test(st.result||"");
      // No gate ever fired (read cap, or an unattended auto-approve) -> the PLAN preview never
      // ran either, since plan_capability only runs inside the approval-gate branch.
      if(!previewDone) setNode("PLAN","done", declined?"skipped"
                       :(st.discussion?"skipped · answering":"no approval needed"));
      if(!gateDone) setNode("GATE","done", declined?"denied"
                     :(st.discussion?"question · nothing to approve"
                       :((cap&&cap.risk==="write")?"approved":"read-only · auto-approved")));
      // A "done" run can still be a needs-human / unverified outcome — flag it instead of
      // rendering it identically to a clean, verified success.
      const flag = declined ? null
        : (st.needs_human || (st.verified===false ? "verify_exhausted" : null));
      setNode("RUN", flag?"failed":"done", declined?"skipped":(flag?(NEEDS_LABEL[flag]||"needs review"):"done"));
      setNode("AUDIT","done", declined?"recorded: declined":"recorded");
      clearThinking(content);
      if(!flag && !declined) mascotReact("dab","all done", "", MASCOT_DAB_MS);
      showResult(enoMsg(), st.result, st.session_id, null, totalDurMs(times), flag);
      if(st.session_id && cap){ currentSession={id:st.session_id, cap, repo:st.repo||null, git_run_id:st.git_run_id||null, git_branch:st.git_branch||null}; updateSessionBar(); }
      clearRun(); led.cost += (st.cost||0); led.runs++; render(); return finishTurn();
    }
    else if(st.state==="unreachable"){
      // The RUN is fine; the SERVER couldn't reach Temporal for this poll (a busy dev server, a
      // restart mid-poll). Treat it as weather, not a verdict: leave every node exactly as it is,
      // say so on screen, and keep polling — a transient blip used to arrive as state "failed"
      // and paint a dead pipeline over a run that then executed happily for another 15 minutes.
      showThinking(content, "\u26a0 lost contact with Temporal \u2014 the run is still going, retrying\u2026");
      await sleep(2000);
      continue;
    }
    else if(st.state==="failed"){
      clearThinking(content); failCurrent("failed");
      // failCurrent only marks whichever node was ACTIVE — AUDIT never activates, so left alone
      // it stays in its pending/never-ran visual forever even on runs the server DID audit.
      // FAILED (OttoWorkflow's own except-and-finalize) and TERMINATED (board Terminate, which
      // now writes its own audit row) both ARE recorded; TIMED_OUT/CANCELED/an unreachable
      // workflow id are NOT — Temporal delivers no exception into the workflow for those, so
      // nothing writes a row (user-reported: a killed run showed no audit trail at all, and the
      // diagram silently implied it never even tried).
      const audited = st.terminal_status==="FAILED" || st.terminal_status==="TERMINATED";
      setNode("AUDIT", audited?"done":"failed", audited?"recorded":"not recorded");
      enoMsg().innerHTML=`<p class="err">Workflow ${esc(st.result||'failed')}.</p>`;
      clearRun(); led.runs++; render(); return finishTurn();
    }
    else if(st.state==="running" && !runActive
            && (gateDone || (times.RUN&&times.RUN.start))){
      // times.RUN.start is the WORKFLOW's own record that execution began — after a page
      // refresh it's the only way to know an already-approved WRITE is executing (gateDone
      // is in-page state and resets to false), so the RUN node + its timer reattach instead
      // of the whole pipe sitting on an unfalsifiable "Working…" forever. It's ALSO why a read
      // cap must wait for it and not shortcut on risk!=="write": the workflow reports state
      // "running" all through the pre-RUN window (CLARIFY, write-intent classify), so a read
      // cap that shortcut used to flip to "Running X…" while actually paused awaiting a
      // clarification — masking the pause so the answer never got surfaced in the chat.
      const cap=capOf(st);
      if(!clarDone){ setNode("CLARIFY","done","no questions",{durMs:times.CLARIFY&&times.CLARIFY.dur}); clarDone=true; }
      if(!previewDone){
        // A reattach after a real gate already has times.PLAN filled in; a run that never
        // gated (read cap / auto-approve) never ran the preview at all.
        setNode("PLAN","done", times.PLAN?"previewed"
               :(st.discussion?"skipped · answering":"no approval needed"),
               {durMs:times.PLAN&&times.PLAN.dur});
        previewDone=true;
      }
      // A DISCUSSION turn is its own reason for skipping the gate, and not the same one as
      // a read cap or a pre-authorized chat: this chat IS write-bound, and the gate is
      // still armed for the next follow-up that asks for a change. Labelling it
      // "auto-approved" would say the opposite of what happened.
      if(!gateDone){ setNode("GATE","done", st.discussion?"question · nothing to approve"
                     :((cap&&cap.risk==="write")?"approved":"read-only · auto-approved"),
                     {durMs:times.GATE&&times.GATE.dur}); gateDone=true; }
      setNode("RUN","active","executing…",{startMs:times.RUN&&times.RUN.start}); runActive=true;
      showThinking(content, cap?("Running "+cap.name+"…"):"Running…");
    }
    // Live RUN progress (issue #97, first cut): while executing, tail the run's streaming
    // transcript (/api/progress) so the chat shows what the agent is DOING — last tool call,
    // event count, verify-retry attempt — and flags a stall, instead of a bare "Working…".
    // Every 4th poll (~1.8s); swarm parents have no transcript of their own, so they skip it.
    if(runActive && st.state==="running" && !isSwarm && (progressTick++ % 4 === 0)){
      try{
        const pg=await (await fetch("/api/progress?id="+encodeURIComponent(wid))).json();
        if(!alive()) return;
        if(pg.found){
          const att=(pg.attempt>1)?(" · attempt "+pg.attempt):"";
          // Which post-PR round is writing, when one is: a review/QA round is the run's OWN work
          // and its transcript is what `idle_s` now measures, so name it rather than let a busy
          // review read as a stalled attempt.
          const part=pg.part?(" · "+pg.part):"";
          setNode("RUN","active","executing…"+att+" · "+pg.events+" events");
          if(pg.idle_s>180){
            showThinking(content,"⚠ no transcript activity for "+Math.round(pg.idle_s/60)+" min"+part+" — verifying, or possibly stuck");
          } else if(pg.last){
            showThinking(content, pg.last+" · "+Math.round(pg.idle_s)+"s ago"+att+part);
          }
          if(pg.supervised) showSupervisor(content, pg.supervisor_last);
        }
      }catch(e){}
    }
    await sleep(450);
  }
}
// Reattach the on-screen view to the active chat's in-flight workflow (after a chat switch or
// page reload). The workflow has been running server-side the whole time. Rebuilds the pipe and
// spins a fresh watchLoop; watchLoop renders whatever the current state is — including a pending
// approval/clarification gate, so a reload mid-approval recovers cleanly.
async function reattachRun(){
  if(!activeChat || !activeChat.run_id) return false;
  const wid=activeChat.run_id;
  const sess=activeChat.cap ? {id:activeChat.session_id, cap:activeChat.cap} : null;
  buildPipe(); setNode("INGRESS","done","resumed");
  const content=enoMsg();
  if(sess){
    // DECOMPOSE/ROUTER are left unpainted here (not pre-set done) so watchLoop's first poll paints
    // them from the workflow's own stage timestamps (st.times) — pre-painting them "done" right now
    // would anchor their timer at this reattach moment instead of when they actually ran (issue #117).
    content.innerHTML=`<p>Resuming <code>${esc(sess.cap.name)}</code> <span class="badge ${sess.cap.risk}">${sess.cap.risk}</span>…</p>`;
  } else {
    setNode("DECOMPOSE","active","planning…");      // cap not yet known — still planning, or a swarm
  }
  showThinking(content,"Resuming…");
  busy=true; syncSendBtn();          // run_id is already on activeChat — this reattaches as stoppable
  // quietPick keeps watchLoop's first poll from overwriting the "Resuming…" line above with its
  // usual "I'll use X" message; it still paints DECOMPOSE/ROUTER/pick itself (see comment above).
  watchLoop(wid, activeChat.id, content, sess, sess?{quietPick:true}:{});
  return true;
}
/* Finished-task notifications (right rail): a chat whose run_id vanished between sidebar
   polls finished a task. We keep the finished chats themselves (id + title), not just a
   count — the chat is in hand when loadChatList detects the finish — so the row can name
   each one and click through to it. The live chat shows its own result inline, so only the
   ones you weren't watching land here. Clear the whole row via "clear"; a title click opens
   that chat and drops it. */
let finishedChats=[], prevRunning=null;
const NOTIF_MAX=4;   // titles shown before the rest collapse into "and N more"
function pushFinished(chat){
  if(!chat||finishedChats.some(c=>c.id===chat.id)) return;
  finishedChats.push({id:chat.id, title:chat.title||"Untitled"});
  renderChatNotif();
}
function dropFinished(id){ finishedChats=finishedChats.filter(c=>c.id!==id); renderChatNotif(); }
function renderChatNotif(){
  const row=document.getElementById("chatnotif"); if(!row) return;
  row.hidden=!finishedChats.length;
  document.getElementById("chatnotif-n").textContent=finishedChats.length>99?"99+":String(finishedChats.length);
  const list=document.getElementById("chatnotif-list");
  const shown=finishedChats.slice(0,NOTIF_MAX), extra=finishedChats.length-shown.length;
  list.innerHTML=shown.map(c=>`<div class="nitem" data-notif="${esc(c.id)}" title="open ${esc(c.title)}">${esc(c.title)}</div>`).join("")
    +(extra>0?`<div class="nmore">and ${extra} more</div>`:"");
  list.querySelectorAll("[data-notif]").forEach(d=>d.addEventListener("click",e=>{
    e.stopPropagation(); const id=d.dataset.notif; dropFinished(id); openChat(id);
  }));
}
document.getElementById("chatnotif").addEventListener("click",e=>{
  if(e.target.closest("#chatnotif-list")) return;   // title clicks handled per-item
  finishedChats=[]; renderChatNotif();
});

/* The sidebar renders one page of chats at a time and grows by the button, never by scrolling
   into hundreds of rows. HIST_PAGE is deliberately the same number as the stream's MSG_PAGE —
   two "show more" controls on one screen that step by different amounts read as two features. */
const HIST_PAGE=20;
let histShown=HIST_PAGE;

/* Collapsed/expanded state of the chat list is per-browser, so it survives a reload but is
   never a server setting. Same idiom as the theme pick and the Events/Memory sections. */
const HIST_COLLAPSED_KEY="ottoHistCollapsed";
function applyHistCollapsed(on){
  const view=document.getElementById("chatview"), btn=document.getElementById("hist-toggle");
  if(view) view.classList.toggle("histcollapsed", !!on);
  if(btn){ btn.setAttribute("aria-expanded", on?"false":"true");
           btn.title = on ? "Show the chat list" : "Collapse the chat list"; }
  try { localStorage.setItem(HIST_COLLAPSED_KEY, on?"1":"0"); } catch(e){}
}
document.getElementById("hist-toggle").addEventListener("click",()=>{
  applyHistCollapsed(!document.getElementById("chatview").classList.contains("histcollapsed"));
});
try { applyHistCollapsed(localStorage.getItem(HIST_COLLAPSED_KEY)==="1"); } catch(e){}

async function loadChatList(){
  const el=document.getElementById("histlist"); if(!el) return;
  let data; try { data=await (await fetch("/api/chats")).json(); } catch(e){ return; }
  const items=data.chats||[];
  const present=new Set(items.map(c=>c.id));
  const running=new Set(items.filter(c=>c.run_id).map(c=>c.id));
  if(prevRunning) for(const id of prevRunning)   // run gone but chat still there = it finished
    if(!running.has(id) && present.has(id) && !(activeChat && activeChat.id===id)) pushFinished(items.find(c=>c.id===id));
  prevRunning=running;
  // Rebuild only when the list (or active selection) actually changed. The background poll
  // calls this on an interval, so this guard keeps it from flickering the sidebar or jumping
  // its scroll position while you're reading — "light poll + preserve selection".
  // `run_id` is in the signature so the spinner appears/disappears as a chat starts/finishes
  // working (the poll re-renders only when something actually changed).
  // histShown rides in the signature: "show more" re-renders through this same guard, and
  // without it the click would be swallowed as an unchanged poll.
  const sig=JSON.stringify([items.map(c=>[c.id,c.title,c.messages,(c.labels||[]).join(","),c.cap,!!c.run_id,!!c.pinned]), activeChat&&activeChat.id, histShown]);
  if(el.dataset.sig===sig) return;
  el.dataset.sig=sig;
  const keepScroll=el.scrollTop;
  const page=items.slice(0, histShown), rest=items.length-page.length;
  el.innerHTML = items.length ? page.map(c=>`
    <div class="histitem ${activeChat&&activeChat.id===c.id?'active':''}" data-chat="${esc(c.id)}">
      <div class="htitle">${esc(c.title||'Untitled')}</div>
      <div class="hmeta">${(c.labels||[]).map(l=>`<span class="hlabel" title="${esc(l)}">${esc(HLABEL_SHORT[l]||l)}</span>`).join("")}${c.cap?`<span class="hcap" title="${esc(c.cap)}">${esc(c.cap)}</span>·`:''}<span class="hcount">${c.messages} msg</span></div>
      ${c.run_id?`<span class="hspin spin" title="working…"></span>`:''}
      <button class="hpin ${c.pinned?'on':''}" data-pinchat="${esc(c.id)}" data-pinned="${c.pinned?1:0}" title="${c.pinned?'unpin':'pin'}"><svg viewBox="0 0 24 24" fill="${c.pinned?'currentColor':'none'}" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="17" x2="12" y2="22"/><path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1v4.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24Z"/></svg></button>
      <button class="hdel" data-delchat="${esc(c.id)}" title="delete">&times;</button>
    </div>`).join("")+(rest>0?`<button class="histmore" id="hist-more" title="${rest} older chat${rest===1?'':'s'} not shown">Show ${Math.min(HIST_PAGE,rest)} more (${rest} older)</button>`:"")
    : `<p class="memempty" style="padding:8px 10px">No past chats yet.</p>`;
  el.scrollTop=keepScroll;
  const more=document.getElementById("hist-more");
  if(more) more.addEventListener("click",()=>{ histShown+=HIST_PAGE; loadChatList(); });
  el.querySelectorAll("[data-chat]").forEach(d=>d.addEventListener("click",e=>{
    if(e.target.closest("[data-delchat]")||e.target.closest("[data-pinchat]")) return; openChat(d.dataset.chat);
  }));
  el.querySelectorAll("[data-pinchat]").forEach(b=>b.addEventListener("click",async e=>{
    e.stopPropagation();
    await fetch("/api/chats/pin",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:b.dataset.pinchat, pinned:b.dataset.pinned!=="1"})});
    loadChatList();
  }));
  el.querySelectorAll("[data-delchat]").forEach(b=>b.addEventListener("click",async e=>{
    e.stopPropagation();
    await fetch("/api/chats/delete",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:b.dataset.delchat})});
    if(activeChat&&activeChat.id===b.dataset.delchat) newChat(); else loadChatList();
  }));
}
/* Completed pipelines persist per chat: leaving a chat snapshots its pipe (stage timers
   included) and reopening restores it — it resets only when a new task runs in that chat
   (every submit path rebuilds the pipe). Mid-run pipes aren't snapshotted; reattachRun
   rebuilds their live view instead. In-memory only — a page reload starts clean. */
const pipeSnaps=new Map();
function snapPipe(){ if(activeChat && !activeChat.run_id) pipeSnaps.set(activeChat.id, pipeEl.innerHTML); }

async function openChat(id){
  let chat; try { chat=await (await fetch("/api/chats/get?id="+encodeURIComponent(id))).json(); } catch(e){ return; }
  if(!chat||!chat.id) return;
  stopWatch();                              // park any watcher on the chat we're leaving (its run continues server-side)
  snapPipe();                               // keep the outgoing chat's finished pipeline for its return
  busy=false; syncSendBtn();
  activateTab("chat");
  recording=false;
  stream.innerHTML=""; buildPipe();
  (chat.messages||[]).forEach(m=>{ m.role==="user" ? userMsg(m.text, m.ts) : showResult(enoMsg(), m.text, null, m.ts); });
  collapseHistory(chat.messages||[]);
  recording=true;
  activeChat={id:chat.id, title:chat.title, messages:(chat.messages||[]).slice(), session_id:chat.session_id||null, cap:chat.cap||null, run_id:chat.run_id||null, repo:chat.repo||null, git_run_id:chat.git_run_id||null, git_branch:chat.git_branch||null, stats:chat.stats||null};
  const st0=chat.stats||{}; led.runs=st0.runs||0; led.appr=st0.appr||0; led.cost=st0.cost||0; render();
  currentSession=(chat.session_id&&chat.cap)?{id:chat.session_id, cap:chat.cap, repo:chat.repo||null, git_run_id:chat.git_run_id||null, git_branch:chat.git_branch||null}:null;
  suppressCarry=false;                      // reopening: a session-less thread carries context on the next follow-up
  // A reopened chat with a bound session shows its id under the last reply (the live-run
  // footer doesn't survive the replay, and the session bar alone was easy to miss).
  if(chat.session_id){
    const last=[...stream.querySelectorAll(".msg.eno .content")].pop();
    if(last) last.insertAdjacentHTML("beforeend", `<div class="sessmeta">session ${sidChip(chat.session_id)}</div>`);
  }
  if(!activeChat.run_id && pipeSnaps.has(activeChat.id)) pipeEl.innerHTML=pipeSnaps.get(activeChat.id);
  updateSessionBar(); loadChatList(); scroll();
  updatePrBar();                            // a PR-review thread gets its post/approve actions
  if(activeChat.run_id) await reattachRun();   // a turn is still in flight — resume its live view
}
function newChat(){
  stopWatch();                              // leaving the current chat parks its watcher (run continues server-side)
  snapPipe();
  busy=false; syncSendBtn();
  activeChat=null; currentSession=null; suppressCarry=false;
  led.runs=0; led.appr=0; led.cost=0; render();
  stream.innerHTML=""; buildPipe(); updateSessionBar(); updatePrBar();
  greeting(); loadChatList(); input.focus();
}

/* A long-lived thread (a daily runbook chat is 130+ turns) replays every turn on open, so the
   newest — the reason you opened it — lands under a wall of history. Only TODAY's turns are
   shown; the rest stay in the DOM but hidden, revealed a page at a time. A chat with nothing
   from today still shows its last page: an empty stream reads as a broken chat, not a quiet one. */
const MSG_PAGE=20;
function collapseHistory(msgs){
  const nodes=[...stream.querySelectorAll(".msg")];
  if(nodes.length!==msgs.length) return;        // replay and DOM disagree — show everything
  const day=new Date(); day.setHours(0,0,0,0);
  let first=msgs.findIndex(m=>{ const d=m.ts?new Date(m.ts):null; return d && !isNaN(d) && d>=day; });
  if(first<0) first=nodes.length-MSG_PAGE;      // nothing today: fall back to the last page
  if(first<=0) return;
  nodes.slice(0,first).forEach(n=>{ n.classList.add("oldmsg"); n.hidden=true; });
  const btn=document.createElement("button");
  btn.className="msgmore"; btn.id="msgmore";
  btn.addEventListener("click",revealEarlier);
  stream.prepend(btn); syncMsgMore();
}
function syncMsgMore(){
  const btn=document.getElementById("msgmore"); if(!btn) return;
  const left=stream.querySelectorAll(".msg.oldmsg[hidden]").length;
  if(!left){ btn.remove(); return; }
  const take=Math.min(MSG_PAGE,left);
  btn.textContent=`Show ${take} earlier message${take===1?"":"s"} (${left} hidden)`;
}
/* Revealing prepends content ABOVE the viewport, so the scroll position has to be pushed down by
   exactly what was added or the reader is thrown back into old turns. `scroll-behavior: smooth`
   on .stream would animate that correction into a visible jump, hence the temporary override. */
function revealEarlier(){
  const hidden=[...stream.querySelectorAll(".msg.oldmsg[hidden]")];
  const h0=stream.scrollHeight, s0=stream.scrollTop;
  hidden.slice(-MSG_PAGE).forEach(n=>{ n.hidden=false; });
  syncMsgMore();
  const prev=stream.style.scrollBehavior; stream.style.scrollBehavior="auto";
  stream.scrollTop=s0+(stream.scrollHeight-h0);
  stream.style.scrollBehavior=prev;
}

/* Sent-time footer for a bubble. Live messages default to "now"; replayed ones use the
   stored ts and show nothing when a pre-timestamp chat has none. */
function stampHTML(ts){
  if(!ts) return "";
  const d=new Date(ts); if(isNaN(d)) return "";
  return `<div class="stamp">${d.toLocaleDateString([],{day:"numeric",month:"short",year:"numeric"})} · ${d.toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"})}</div>`;
}
/* Stamp an Otto bubble's outer .msg (the "I'll use X" router note has no recorded ts of its own,
   so it's the one bubble that otherwise renders stampless — live time is right here). */
function stampMsg(content, ts){ const m=content&&content.closest(".msg"); if(m&&!m.querySelector(":scope > .stamp")) m.insertAdjacentHTML("beforeend", stampHTML(ts||new Date().toISOString())); }
function userMsg(text, ts){ ts=ts||(recording?new Date().toISOString():null);
  const m=document.createElement("div"); m.className="msg user"; m.innerHTML=`<div class="body"></div>`; m.querySelector(".body").textContent=text;
  m.insertAdjacentHTML("beforeend", stampHTML(ts)); stream.appendChild(m); scroll(); recordMsg("user", text, ts); }
function enoMsg(){ const m=document.createElement("div"); m.className="msg eno";
  m.innerHTML=`<div class="row"><div class="avatar"><svg aria-hidden="true"><use href="#mk"/></svg></div><div class="body"><div class="who">Otto</div><div class="content"></div></div></div>`;
  stream.appendChild(m); scroll(); return m.querySelector(".content"); }

// A muted, centered system hint (NOT a conversation turn — never recorded to the chat). Used to
// tell the user when a follow-up couldn't resume the bound session and is running fresh instead.
function sysNote(text){ const d=document.createElement("div"); d.className="sysnote"; d.textContent=text; stream.appendChild(d); scroll(); }

/* Render an agent result into an Eno message body as formatted markdown. */
const NEEDS_BANNER_RE=/^⚠️\s*\*\*Needs human review\*\*[^\n]*\n\n/;
function showResult(node, text, sessionId, ts, durationMs, flag){
  ts=ts||(recording?new Date().toISOString():null);
  node.innerHTML = `<div class="result"></div>`;
  // A needs-human / unverified outcome: render a prominent banner (not just the buried
  // markdown line the workflow prepends) so it can't be mistaken for a clean success. Strip
  // the workflow's own text banner so it isn't shown twice, and keep the recorded text intact.
  let shown = text || "(no output)";
  if(flag){
    const label=NEEDS_LABEL[flag]||"needs review";
    const hint=NEEDS_HINT[flag]||NEEDS_HINT[label]||"This run did not complete cleanly — review it before trusting the result.";
    node.insertAdjacentHTML("afterbegin",
      `<div class="needsflag"><span class="nf-ic">⚠️</span><span><span class="nf-t">Needs review — ${esc(label)}</span><span class="nf-h">${esc(hint)}</span></span></div>`);
    shown = shown.replace(NEEDS_BANNER_RE,"");
  }
  node.querySelector(".result").innerHTML = renderMD(shown);
  // In-card footer: session id + how long the run took (deliberately NOT part of the recorded
  // message text). The sent-time stamp lives outside the card instead, like user bubbles.
  const meta = (sessionId ? `<span class="sessmeta">session ${sidChip(sessionId)}</span>` : "")
    + (durationMs!=null ? `<span class="sessmeta">took ${fmtDur(durationMs)}</span>` : "");
  if(meta) node.insertAdjacentHTML("beforeend", `<div class="resultmeta">${meta}</div>`);
  const msg = node.closest(".msg");
  if(msg) msg.insertAdjacentHTML("beforeend", stampHTML(ts));
  recordMsg("otto", text || "(no output)", ts);
}

// When a reopened chat has prior turns but no bound session, it can't be continued in-session
// (a swarm-decomposed run has N child sessions and no single one to --resume; a session can also
// simply have expired). Rather than silently re-routing a follow-up as a brand-new request and
// dropping all context, build a compact transcript suffix to carry into the fresh route. Returns
// "" for a first message, a normally-resumable chat, or an explicit "New task" (suppressCarry).
// `force` is the model-rebind path: it leaves a live session deliberately, so the guard that
// normally means "resume already carries the history" doesn't apply — nothing else would.
function carryContextForSubmit(force){
  if((currentSession && !force) || suppressCarry) return "";
  const msgs=(activeChat && activeChat.messages) || [];
  if(!msgs.length) return "";                    // the new user message isn't recorded yet
  const CAP=1200, MAX=4;                          // truncate each turn; keep only the recent tail
  const lines=msgs.slice(-MAX).map(m=>{
    const who=m.role==="user"?"user":"assistant";
    let t=String(m.text||"").trim(); if(t.length>CAP) t=t.slice(0,CAP)+" …[truncated]";
    return `[${who}] ${t}`;
  });
  // The framing has to say what to DO with this, not just that it exists. Unstated, a run
  // re-checks current state (correct) and then quietly serves a different figure than the one
  // above it — judged a fabricated "live" pull twice on web-50af486b — or skips the check and
  // asserts nothing has changed. Same rule the memory context states for stored facts.
  return `\n\n--- Earlier in this conversation (background; the request above is what to do now) ---\n`
    + `Treat this as background, not as findings to restate. Anything about CURRENT state — what `
    + `exists, is open, is deployed, is reachable — must be re-checked with tools; the tool result `
    + `wins. If what you find disagrees with anything below, say so and say what changed. Never `
    + `silently replace a figure stated here with a different one.\n\n`
    + lines.join("\n\n");
}

function greeting(){ const c=enoMsg();
  c.innerHTML=`<p class="greeting">I'm Otto. Tell me what you need — I'll route it to the right agent, run it, and check with you before anything changes.</p>
    <div class="chips">
      <div class="chip" data-q="what's open on the board right now">what's on the board <span class="tag">· read</span></div>
      <div class="chip" data-q="give me a deploy status overview for production">deploy status <span class="tag">· read</span></div>
      <div class="chip" data-q="give me a board status overview of open PRs">open PRs <span class="tag">· read</span></div>
    </div>`;
  c.querySelectorAll(".chip").forEach(ch=>ch.addEventListener("click",()=>{ if(!busy) submit(ch.dataset.q); })); }

/* ---- "thinking" indicator: animated dots in the chat while work runs in the background ---- */
function showThinking(content, label){
  if(!content) return;
  let el=content.querySelector(".thinking");
  if(!el){ el=document.createElement("div"); el.className="thinking";
    el.innerHTML=`<span class="tdots"><i></i><i></i><i></i></span><span class="tlabel"></span>`;
    content.appendChild(el); }
  el.querySelector(".tlabel").textContent=label; scroll();
}
function clearThinking(content){
  const el=content&&content.querySelector(".thinking"); if(el) el.remove();
  clearSupervisor(content);
}
/* Shadow-mode AI supervisor indicator (issue #143): a small pill in the message title (.who),
   next to "Otto" — a fixed-width header spot rather than the thinking line, whose live
   progress text can be arbitrarily long and would otherwise squeeze/wrap the badge. Cleared by
   clearThinking() alongside the thinking line on completion/failure. `last` is
   {at_s, verdict, critique} from the newest live checkpoint the backend has appended to the
   transcript, or null before the first one has fired yet. */
function showSupervisor(content, last){
  const who=content&&content.closest(".body") && content.closest(".body").querySelector(".who");
  if(!who) return;
  let el=who.querySelector(".supwatch");
  if(!el){ el=document.createElement("span"); el.className="supwatch";
    el.innerHTML=`<i class="supdot"></i><span class="suptext"></span>`;
    who.appendChild(el); }
  const flagged = last && last.verdict==="retry";
  el.className = "supwatch"+(flagged?" flag":"");
  el.querySelector(".suptext").textContent = last
    ? (flagged?"supervisor flagged (shadow)":"supervisor: on track")
    : "AI supervisor watching";
  el.title = last
    ? `Shadow supervisor checked @${Math.round(last.at_s)}s`+(flagged?` — ${last.critique||"judged off-course"}`:" — looks on track")
      +". Advisory only in shadow mode: it never intervenes."
    : "Otto's shadow-mode supervisor periodically checks this run's live transcript for signs "
      +"it's off-course. Advisory only — it records what it would do but never intervenes.";
}
function clearSupervisor(content){
  const who=content&&content.closest(".body") && content.closest(".body").querySelector(".who");
  const el=who&&who.querySelector(".supwatch"); if(el) el.remove();
}
// Render a swarm's live sub-task list (parallel child workflows). Each child gates its own
// write independently — approve those on the Board; the merged answer lands here when all finish.
function showSwarm(content, children){
  if(!content) return;
  const rows=(children||[]).map(c=>`
    <div class="swrow"><code>${esc(c.cap||'—')}</code><span class="badge ${esc(c.risk||'read')}">${esc(c.risk||'read')}</span>
      <span class="swsub">${esc(c.request||'')}</span></div>`).join("");
  let el=content.querySelector(".swarm");
  if(!el){ el=document.createElement("div"); el.className="swarm"; content.appendChild(el); }
  el.innerHTML=`<div class="swhead">🜂 Fanned out into ${(children||[]).length} parallel sub-tasks</div>${rows}
    <div class="swnote">Each runs as its own workflow; approve any write on the <b>Board</b>. The merged result appears here when they finish.</div>`;
  showThinking(content,"Running sub-tasks in parallel…"); scroll();
}
/* paint the stage that's currently active/holding (or RUN as a fallback) red */
function failCurrent(detail){
  const n=[...pipeEl.children].find(c=>/\b(active|gatehold)\b/.test(c.className));
  setNode(n?n.dataset.lbl:"RUN","failed",detail);
}

async function api(path, body){
  const res = await fetch(path, { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body) });
  const data = await res.json();
  // A refused-because-paused reply is the fastest signal that the pause was engaged elsewhere
  // (CLI, another tab, `touch data/ESTOP`). Paint it now rather than leaving the header claiming
  // Otto is running for up to one 15s poll while the error says otherwise.
  // Re-read rather than synthesising a state here, so the strip shows the real reason instead of
  // blanking it — the reason is often the only clue to who paused it and why.
  if (data && data.paused) fetch("/api/estop").then(r=>r.json()).then(applyEstop).catch(()=>{});
  if (!res.ok || data.error) throw new Error(data.error || ("HTTP "+res.status));
  return data;
}

function sleep(ms){ return new Promise(r=>setTimeout(r,ms)); }

// The bound claude session, made visible + copyable. chats.json stores the id too, but a
// visible chip is the manual recovery handle: if the binding is ever lost (the failure mode
// behind fix/direct-path-continuity), `claude --resume <id>` in a terminal picks the
// conversation back up. Click copies the full resume command.
function sidChip(id){
  if(!id) return "";
  // data-sid + the delegated listener below — never an inline onclick built by string
  // interpolation (esc() is HTML-context only; it doesn't escape ' for a JS-string context).
  return `<code class="sid" data-sid="${esc(id)}" title="claude --resume ${esc(id)} — click to copy">${esc(String(id).slice(0,8))}</code>`;
}
// Chips are inserted dynamically (session bar, result footers), so one delegated listener.
document.addEventListener("click", e=>{
  const el=e.target.closest(".sid"); if(!el || !el.dataset.sid) return;
  try { navigator.clipboard.writeText("claude --resume "+el.dataset.sid); } catch(err){ return; }
  const t=el.textContent; el.textContent="copied ✓"; setTimeout(()=>{ el.textContent=t; },1200);
});

function updateSessionBar(){
  const bar=document.getElementById("sessionbar");
  if(currentSession){
    bar.hidden=false;
    bar.innerHTML=`<span class="sdot"></span>continuing with <b>${esc(currentSession.cap.name)}</b> — replies stay in this session`+
      `${currentSession.id?` · ${sidChip(currentSession.id)}`:""}<span class="nt" id="newtask">New task</span>`;
    document.getElementById("newtask").onclick=()=>{ currentSession=null; suppressCarry=true; updateSessionBar(); input.focus(); };
  } else { bar.hidden=true; bar.innerHTML=""; }
  applyBrainstorm();
}

// Brainstorm mode: pins the built-in `brainstorm` capability instead of routing. Unlike Memory or
// Effort this is NOT a per-turn flag the continue path can forward — it IS the capability, and a
// session is bound to one for life. So it only takes effect on a FRESH chat, and `applyBrainstorm`
// below makes that legible rather than leaving a live checkbox that silently does nothing.
const BRAINSTORM_CAP="brainstorm";
const BRAINSTORM_HINT="Think out loud with Otto instead of handing it a task. Pins the built-in brainstorm capability: it argues a position, offers options and asks the question that decides it, in a few sentences — no TLDR, no plan, no approval gate, and no verify pass. Applies when you START a chat; the chat then stays in this mode.";
function selectedBrainstorm(){ const c=document.getElementById("bscheck"); return !!(c && c.checked); }
// Reflect the BOUND session in the toggle: checked+locked inside a brainstorm chat, unchecked+
// locked inside any other, free only between chats. A toggle that stays clickable while a session
// holds a different cap reads as "next message will brainstorm" and then doesn't.
function applyBrainstorm(){
  const c=document.getElementById("bscheck"), l=document.getElementById("bstoggle");
  if(!c||!l) return;
  if(currentSession && currentSession.cap){
    c.checked=(currentSession.cap.name===BRAINSTORM_CAP); c.disabled=true;
    l.title="This chat is already bound to "+currentSession.cap.name+" — start a new task to change mode.";
  } else { c.disabled=false; l.title=BRAINSTORM_HINT; }
  applyModeExclusions();
}

function selectedRepo(){ const s=document.getElementById("repopick"); return s ? s.value : ""; }
// Post-PR QA loop is only meaningful in repo-mode (it validates the opened PR).
function selectedQA(){ const c=document.getElementById("qacheck"); return !!(c && c.checked && selectedRepo()); }
// Code review runs on every repo-mode PR unconditionally (workflows.py: params.get("review", True))
// — there is no UI control for it because there is nothing to control.
// Plan-then-execute opt-in (design doc 2026-07-16): force a strong model to break the task into
// atomic steps a local executor runs one at a time. Not repo-scoped; the toggle is only shown when
// the backend has plan mode enabled (OTTO_PLAN_MODE != off), so a hidden/unchecked box is false.
function selectedPlan(){ const c=document.getElementById("plancheck"); return !!(c && c.checked); }
// Memory defaults ON — absent box (shouldn't happen) also reads as on, never silently off.
function selectedMemory(){ const c=document.getElementById("memcheck"); return !c || c.checked; }
// Pre-authorize this chat's writes: skips the approval gate AND its plan preview. Defaults OFF —
// an absent box reads as off, never as silently pre-authorized. Named for the approval mode it
// selects (the workflow's `approval:"auto"`), not for the gate it happens to remove.
function selectedAutoApprove(){ const c=document.getElementById("autoapprove"); return !!(c && c.checked); }
// The gate is the only human checkpoint in front of a write, so its state is stated in the
// composer hint rather than living solely in a toggle's colour.
function applyApprovalHint(){
  const c=document.getElementById("autoapprove"), h=document.getElementById("gatehint");
  if(!c) return;
  if(h) h.innerHTML = c.checked ? '<b class="warn">writes are auto-approved</b>'
                                : "writes wait for your approval";
}
// Empty string = "Admin default" (no override); the server treats it the same as an absent key.
function selectedModelOverride(){ const s=document.getElementById("modelpick"); return s ? s.value : ""; }
function selectedEffort(){ const s=document.getElementById("effortpick"); return s ? s.value : ""; }
// Populates the composer's model-override picker from the same pool Admin → Models edits.
// Lives in the "Run mode" bar alongside Memory — both always visible, unlike the plan-mode
// toggle in that same bar which only shows when the backend has plan_mode enabled.
async function loadModelPicker(){
  let pool=[];
  try { pool=(await (await fetch("/api/models")).json()).pool||[]; } catch(e){ return; }
  const sel=document.getElementById("modelpick");
  if(!sel) return;
  const cur=sel.value;
  sel.innerHTML=`<option value="">Admin default</option>`+
    pool.map(m=>`<option value="${esc(m.name)}">${esc(m.name)}</option>`).join("");
  sel.value=cur;   // preserve selection across reloads
}

/* ---- behaviour-rule capture (issue #68): propose → confirm → store, never silent ---- */
async function proposeRule(text, capObj, showMine){
  text=(text||"").trim();
  if(showMine && text) userMsg("/remember-rule "+text);
  if(!text){ ruleNote('Type the correction after the command, e.g. <code>/remember-rule always run the tests before opening a PR</code>.'); return; }
  let sug;
  try { sug=await api("/api/behaviors/suggest",{message:text, cap:capObj?capObj.name:undefined}); }
  catch(e){ ruleNote("Couldn't reach the rule classifier ("+esc(e.message)+")."); return; }
  if(!sug.is_rule){ ruleNote("That doesn't read as a standing behaviour rule, so nothing was saved. You can still add one in the <b>Memory</b> tab."); return; }
  ruleCard(sug.rule, capObj);
}
// Best-effort implicit detection on a follow-up: if it reads as a correction, offer to save it
// (non-blocking; never stored without the user clicking Save).
async function maybeSuggestRule(text, capObj){
  try {
    const sug=await api("/api/behaviors/suggest",{message:text, cap:capObj?capObj.name:undefined});
    if(sug && sug.is_rule) ruleCard(sug.rule, capObj);
  } catch(e){ /* implicit suggestion is best-effort */ }
}
function ruleNote(html){ const c=enoMsg(); c.innerHTML=`<p class="sub" style="margin:0">${html}</p>`; }
function ruleCard(rule, capObj){
  const capScope = capObj ? `${capObj.kind||'agent'}:${capObj.name}` : 'global';
  const c=enoMsg();
  c.innerHTML=`<div class="rulecard">
    <div class="rc-h">Save this as a behaviour rule? It will be injected into future runs.</div>
    <textarea class="rc-rule" rows="2"></textarea>
    <div class="rc-row">
      <select class="rc-scope">${scopeOptions(capScope)}</select>
      <button class="rc-save">Save rule</button>
      <button class="rc-dismiss">Dismiss</button>
    </div></div>`;
  c.querySelector(".rc-rule").value=rule;          // set via .value to avoid HTML-escaping issues
  const card=c.querySelector(".rulecard");
  c.querySelector(".rc-dismiss").addEventListener("click",()=>{ card.innerHTML='<div class="rc-h">Dismissed — not saved.</div>'; });
  c.querySelector(".rc-save").addEventListener("click",async()=>{
    const r=(c.querySelector(".rc-rule").value||"").trim(); if(!r) return;
    const scope=c.querySelector(".rc-scope").value||"global";
    try { await api("/api/behaviors/add",{rule:r, scope}); }
    catch(e){ card.innerHTML='<div class="rc-h">Couldn\'t save the rule.</div>'; return; }
    const label = scope==="global" ? "every run" : scope.split(":").pop();
    card.innerHTML=`<div class="rc-h">✓ Saved — Otto will follow this on ${esc(label)}.</div>`;
  });
}

// --- global pause -----------------------------------------------------------
// ESTOP_BUSY lives OUT here on purpose: the click is a network round trip, and every poller that
// lands mid-flight would otherwise repaint the button back to its old label and make the press
// look like it did nothing (the GC_RUNNING / CONV_BUSY pattern).
let ESTOP_BUSY=false, ESTOP_ON=null;

function applyEstop(st){
  // Single owner of both surfaces — the header button and the strip — so they can never disagree
  // about whether Otto is paused, which is the one thing this control must never be vague about.
  const on=!!(st&&st.engaged);
  ESTOP_ON=on;
  document.body.classList.toggle("paused",on);
  const b=document.getElementById("estop"), l=document.getElementById("estop-label");
  if(b&&!ESTOP_BUSY){ b.classList.toggle("on",on); if(l) l.textContent=on?"Paused — resume":"Pause"; }
  mascotPause(on);
  const why=document.getElementById("estop-why");
  if(why) why.textContent=(st&&st.reason)?`— ${st.reason}`:"";
}

async function toggleEstop(){
  if(ESTOP_BUSY) return;
  const b=document.getElementById("estop"), l=document.getElementById("estop-label");
  // Releasing needs no confirmation (it only ever allows more), but engaging is a real stop —
  // still no prompt, because the whole value of a stop button is that it is instant. It is
  // reversible in one click and kills nothing already running, which is what makes that safe.
  const want=!ESTOP_ON;
  ESTOP_BUSY=true; if(b) b.disabled=true; if(l) l.textContent=want?"pausing…":"resuming…";
  try{
    const r=await fetch("/api/estop",{method:"POST",headers:{"Content-Type":"application/json"},
                                     body:JSON.stringify({engaged:want,reason:want?"paused from the Otto header":""})});
    const st=await r.json();
    ESTOP_BUSY=false; if(b) b.disabled=false;
    applyEstop(st);
  }catch(e){
    // Failed to reach the backend: do NOT assume the toggle took. Re-read the real state, so a
    // button reading "Paused" always means the sentinel is actually there.
    ESTOP_BUSY=false; if(b) b.disabled=false;
    try{ applyEstop(await (await fetch("/api/estop")).json()); }catch(_){ }
  }
}
document.getElementById("estop").addEventListener("click", toggleEstop);
// First paint before any poll lands, so a page loaded while paused never shows a running Otto.
(async()=>{ try{ applyEstop(await (await fetch("/api/estop")).json()); }catch(e){} })();

// The running build, painted from /api/health. The version alone can't identify it — Otto
// runs from a working checkout, so a tag and every commit after it share one number; the sha
// goes in the title so the chip stays short.
function applyVersion(h){
  const el=document.getElementById("ver"); if(!el || !h || !h.version) return;
  el.textContent="v"+h.version;
  el.title=h.revision ? ("version "+h.version+", commit "+h.revision) : ("version "+h.version);
}

async function refreshHealth(){
  // Re-checked before every turn, not just at page load: a tab loaded during a backend
  // restart otherwise latches TEMPORAL=false and silently degrades every later run to the
  // direct path (no durability, no repo-mode, no swarm).
  const was=TEMPORAL;
  try {
    const h=await (await fetch("/api/health")).json();
    TEMPORAL = !!(h.temporal && h.connected);
    applyEstop(h.estop);      // rides the same payload — no extra request per turn
    applyVersion(h);          // likewise: a restart onto a new build repaints on the next turn
    // Plan-then-execute opt-in toggle: shown only when the backend enabled it AND we're on the
    // durable path (the workflow is what reads params.plan_mode). Off → hide + uncheck. Only the
    // toggle itself hides — Memory/Model share this bar and stay visible regardless (#runbar was
    // folded into #planbar as "Run mode" so the composer doesn't grow a lone extra settings row).
    const pt=document.getElementById("plantoggle");
    if(pt){ const avail=TEMPORAL && h.plan_mode && h.plan_mode!=="off";
            pt.hidden=!avail; const c=document.getElementById("plancheck");
            // Default OFF — plan-then-execute is heavy (decompose → step-by-step → replans) and the
            // wrong shape for most reads; it's per-request opt-in. Just uncheck when unavailable.
            if(c && !avail) c.checked=false; }
  } catch(e){ TEMPORAL=false; }
  if(TEMPORAL && !was) loadRepos();    // late upgrade: the repo picker was skipped at load
}

let dispatching=false;   // guards the async gap between Enter and busy=true (the health check)
async function submit(text){
  // "/remember-rule <correction>" → propose a behaviour rule for confirmation (issue #68),
  // never routed as a normal request.
  if(/^\s*\/remember-rule\b/i.test(text||"")){
    const arg=(text||"").replace(/^\s*\/remember-rule\b\s*/i,"").trim();
    input.value=""; autosize(); hideSlash();
    return proposeRule(arg, currentSession?currentSession.cap:null, true);
  }
  // The submit paths set `busy` synchronously at their top, but refreshHealth() awaits a
  // fetch BEFORE dispatch — without this guard a second Enter in that window double-submits.
  if(busy || dispatching) return;
  dispatching=true;
  try { await refreshHealth(); } finally { dispatching=false; }
  if(currentSession) return continueTemporal(text);
  // A follow-up on a reopened, non-resumable thread (no bound session) runs fresh but carries a
  // compact transcript so context isn't silently lost. The submit path surfaces a visible note.
  const carry=carryContextForSubmit();
  const slash=parseSlash(text);                 // explicit "/cap …" → pin the capability
  let pin=slash?slash.cap:null, pinReq=slash?slash.request:null;
  // The Brainstorm toggle is exactly a /brainstorm pin, so express it as one — same server path,
  // same trusted registry resolution, nothing new to forward. An explicit slash always wins: the
  // user naming a capability outranks a toggle they set three messages ago.
  if(!pin && selectedBrainstorm()){
    const bs=CAPS_LIST.find(c=>c.name===BRAINSTORM_CAP && c.enabled!==false);
    if(bs){ pin=bs; pinReq=text; }
  }
  // Temporal is required (issue #278) — without it /api/submit 503s and the error renders here.
  return submitTemporal(text, pin, pinReq, selectedRepo(), selectedQA(), selectedPlan(), selectedMemory(), selectedModelOverride(), carry, selectedEffort());
}

async function continueTemporal(text){
  if(busy) return; text=(text||"").trim(); if(!text) return;
  const sess=currentSession;
  busy=true; sendBtn.disabled=true; input.value=""; autosize();
  userMsg(text); buildPipe();
  setNode("INGRESS","done","continuation");
  setNode("DECOMPOSE","done","—");                     // resume reuses the bound cap — no planning
  setNode("ROUTER","done","continuing: "+sess.cap.name); addPick(sess.cap);
  setNode("CLARIFY","done","—");
  const content=enoMsg(); showThinking(content,"Resuming "+sess.cap.name+"…");

  // The session's last reply, so the server's handoff classifier can resolve references
  // ("that" = the ticket the cap just offered). Without it, plain resume semantics apply.
  const msgs=(activeChat&&activeChat.messages)||[];
  const prevMsg=[...msgs].reverse().find(m=>m.role!=="user");

  let out;
  try { out=await api("/api/continue",{session_id:sess.id, cap:sess.cap, message:text, prev: prevMsg?String(prevMsg.text).slice(-4000):undefined, repo:sess.repo||undefined, git_run_id:sess.git_run_id||undefined, git_branch:sess.git_branch||undefined, model_override: selectedModelOverride()||undefined, effort: selectedEffort()||undefined, auto_approve: selectedAutoApprove()||undefined}); }
  catch(e){ clearThinking(content); content.innerHTML=`<p class="err">Couldn't continue the session (${e.message}).</p>`; return finishTurn(); }

  if(out && out.rebind){
    // The model pick is on the other backend from the one that minted this session, and a
    // session's history can't move backends — so the pick wins and the session ends here. The
    // conversation rides along as context (a resume would have carried it implicitly) and the
    // switch is stated on screen: leaving a session silently is worse than not switching.
    const task=out.rebind.request;
    setNode("DECOMPOSE","done","model switch");
    setNode("ROUTER","active","routing on "+out.rebind.model+"…");
    showThinking(content,"Switching to "+out.rebind.model+"…");
    currentSession=null; updateSessionBar();   // NOT suppressCarry — the carry is the whole point
    let rid;
    try { rid=(await api("/api/submit",{request:task+carryContextForSubmit(true), cap: sess.cap.name,
                                        repo: selectedRepo()||undefined, qa: selectedQA()||undefined,
                                        plan_mode: selectedPlan()||undefined, memory_enabled: selectedMemory(),
                                        auto_approve: selectedAutoApprove()||undefined,
                                        model_override: out.rebind.model,
                                        effort: selectedEffort()||undefined})).id; }
    catch(e){ setNode("ROUTER","failed","failed"); clearThinking(content); content.innerHTML=`<p class="err">Couldn't start the run on ${esc(out.rebind.model)} (${e.message}).</p>`; return finishTurn(); }
    recordMsg("otto", "↪ Switched to "+out.rebind.model+" — that model runs on a different backend, so this starts a fresh run (the earlier conversation is carried as context).");
    setRun(rid);
    watchLoop(rid, activeChat.id, content, null, {});
    return;
  }

  if(out && out.handoff){
    // The follow-up delegates a NEW task (e.g. accepting a ticket the cap offered) — run it
    // as a fresh, normally-routed workflow (repo auto-engage, verify, review loop) instead
    // of inside the bound session, where the wrong cap would keep the work.
    const task=out.handoff.request;
    setNode("DECOMPOSE","done","handoff");
    setNode("ROUTER","active","routing the new task…");
    showThinking(content,"Handed off as a fresh task…");
    let hid;
    // A handoff IS a fresh submit, so it carries the composer exactly like one — the settings
    // are still on screen and a run that silently ignores them reads as the picker being broken.
    try { hid=(await api("/api/submit",{request:task, repo: selectedRepo()||undefined, qa: selectedQA()||undefined,
                                        plan_mode: selectedPlan()||undefined, memory_enabled: selectedMemory(),
                                        auto_approve: selectedAutoApprove()||undefined,
                                        model_override: selectedModelOverride()||undefined,
                                        effort: selectedEffort()||undefined})).id; }
    catch(e){ setNode("ROUTER","failed","failed"); clearThinking(content); content.innerHTML=`<p class="err">Couldn't start the handed-off task (${e.message}).</p>`; return finishTurn(); }
    recordMsg("otto", "↪ Handed off as a fresh task: "+task);
    setRun(hid);
    watchLoop(hid, activeChat.id, content, null, {});
    return;
  }

  const id=out.id;
  setRun(id);
  watchLoop(id, activeChat.id, content, sess, {decomposeDone:true, pickShown:true, clarDone:true, quietPick:true});
  maybeSuggestRule(text, sess.cap);   // implicit: a follow-up that reads as a correction → offer to save it
}

async function submitTemporal(text, pinCap, pinReq, repo, qa, plan, memory, modelOverride, carry, effort){
  if(busy) return; text=(text||"").trim(); if(!text) return;
  busy=true; sendBtn.disabled=true; input.value=""; autosize(); hideSlash();
  userMsg(text);
  if(carry) sysNote("No live session — starting a fresh run with the earlier context carried in.");
  buildPipe();
  setNode("INGRESS","active","received your request");
  setNode("INGRESS","done", repo?("repo: "+repo):("request "+(led.runs+1)));
  // A pinned cap skips the fan-out check (and Router #1); a fresh request is checked for
  // fan-out first — it may run as one capability or split into a parallel swarm.
  if(pinCap){ setNode("DECOMPOSE","done","pinned"); setNode("ROUTER","active","chosen: "+pinCap.name); }
  else { setNode("DECOMPOSE","active","checking for independent sub-tasks…"); }
  const content=enoMsg(); showThinking(content, pinCap?("Starting "+pinCap.name+"…"):"Planning your request…");

  let id;
  const req = (pinCap ? (pinReq||"") : text) + (carry||"");   // pinned: request is the args after /cap; carry appends prior context
  try { id=(await api("/api/submit",{request:req, cap: pinCap?pinCap.name:undefined, repo: repo||undefined, qa: qa||undefined, plan_mode: plan||undefined, memory_enabled: memory, model_override: modelOverride||undefined, effort: effort||undefined, auto_approve: selectedAutoApprove()||undefined})).id; }
  catch(e){ setNode(pinCap?"ROUTER":"DECOMPOSE","failed","failed"); clearThinking(content); content.innerHTML=`<p class="err">Couldn't start workflow (${e.message}).</p>`; return finishTurn(); }

  setRun(id);
  watchLoop(id, activeChat.id, content, pinCap?{cap:pinCap}:null, pinCap?{decomposeDone:true}:{});
}

function clarifyTurn(content, question){
  return new Promise(resolve=>{
    const card=document.createElement("div"); card.className="clarify";
    card.innerHTML=`<div class="clabel">&#10022; needs clarification</div>
      <div class="cq"></div>
      <div class="cfield"><input class="cinput" placeholder="Type your answer…"><button class="btn csend">Answer</button></div>`;
    card.querySelector(".cq").textContent=question;
    content.appendChild(card); scroll();
    const inp=card.querySelector(".cinput"); inp.focus();
    const done=(ans)=>{
      card.classList.add("resolved"); card.querySelector(".cfield").remove();
      const r=document.createElement("div"); r.className="cresolved"; r.textContent="↳ "+ans;
      card.appendChild(r); resolve(ans);
    };
    const go=()=>{ const a=inp.value.trim(); if(a) done(a); };
    card.querySelector(".csend").addEventListener("click",go);
    inp.addEventListener("keydown",e=>{ if(e.key==="Enter"){ e.preventDefault(); go(); }});
  });
}

// `_PLAN_INSTRUCTION` (plans.py) REQUIRES every genuine plan to enumerate numbered steps and
// close with a "Risks & assumptions" section — and that section is where a real plan reasons out
// loud with rhetorical/investigative "?"s ("is X already implemented?", "elapsed time or
// wall-clock?") that the model resolves itself, never a request for the human. A response that
// never reached that structure is a genuine punt instead of a plan. Splitting on raw '?' count
// (a first attempt at this) misfired hard on real plans dense with that reasoning — a plan can be
// riddled with '?' and still have zero questions actually addressed to you.
function hasPlanStructure(text){
  if(/risks\s*&?\s*assumptions/i.test(text)) return true;
  return ((text.match(/^\s*(?:\*\*)?\d+[.)]/gm)||[]).length) >= 2;
}
function splitPlanQuestions(text){
  if(!text) return {questions:[], rest:""};
  if(hasPlanStructure(text)) return {questions:[], rest:text};
  // No plan structure at all — a genuine punt, shown whole rather than line-split (it's
  // ordinarily a short paragraph, not a list).
  if(((text.match(/\?/g)||[]).length) >= 2) return {questions:[text], rest:""};
  return {questions:[], rest:text};
}

// `revisions`/`maxRevisions`/`wid` are only known on the Temporal path (the direct/no-Temporal
// gate calls omit them) — that's also exactly when "Request changes" has a workflow to signal,
// so their absence is what hides the affordance below rather than a separate flag.
function gate(content,cap,plan,repo,reason,concerns,revisions,maxRevisions,replanning,wid,onReplan,planModel){
  return new Promise(resolve=>{
    // The chat feed only ever shows this compact card — name, invocation, a one-line status,
    // and "See plan". Everything that used to be crammed inline (why/repo/concerns/questions/
    // the plan itself/actions/the revise box) now lives in a dedicated modal opened from here,
    // reusing the app's own modal chrome (`.modalOverlay`/`.modalBox`/…) so it still looks and
    // behaves like every other detail view, with the old amber gate styling inside `.modalBody`.
    const card=document.createElement("div"); card.className="gate";
    card.innerHTML=`<div class="glabel">&#9888; write — approval needed</div>
      <div class="cap">${cap.name}</div><div class="inv"></div>
      <div class="gatefoot">
        <div class="gatestatus"></div>
        <button type="button" class="btn accept seeplan">See plan</button>
      </div>`;
    card.querySelector(".inv").textContent = cap.invocation || "";
    const gateStatusEl=card.querySelector(".gatestatus");
    const seeplanBtn=card.querySelector(".seeplan");

    const modal=document.createElement("div"); modal.className="modalOverlay"; modal.hidden=true;
    modal.innerHTML=`<div class="modalBox">
        <div class="modalHead">
          <div class="modalTitle"><b>${cap.name}</b></div>
          <button type="button" class="modalClose" aria-label="Close" title="Close">&times;</button>
        </div>
        <div class="modalBody gate">
          <div class="whygate"></div>
          <div class="repo"></div>
          <div class="concerns" hidden><div class="clabel"></div><ul></ul></div>
          <div class="questions" hidden><div class="qlabel">&#10068; Otto is asking you</div><ul></ul></div>
          <div class="planwrap" hidden><div class="planlabel">Planned operations<span class="planby"></span></div><div class="plan result"></div></div>
          <div class="nopreviewnote" hidden>⚠ The plan preview didn't produce anything (it may have timed out or hit an error) — you're approving the capability itself, not a reviewed plan.</div>
          <div class="revisenote" hidden></div>
          <div class="actions">
            <button class="btn approve">Approve &amp; run</button>
            <button class="btn revise">Request changes</button>
            <button class="btn decline">Decline</button>
          </div>
          <div class="revisebox" hidden>
            <textarea class="revisetext" rows="2" placeholder="What should change before you approve? e.g. &quot;only touch dev, not prod&quot;"></textarea>
            <div class="reviseactions"><button class="btn sm accept send-revise">Send &amp; re-plan</button><button class="btn sm decline cancel-revise">Cancel</button></div>
          </div>
        </div>
      </div>`;
    document.body.appendChild(modal);
    function openGateModal(){ modal.hidden=false; }
    function closeGateModal(){ modal.hidden=true; }
    seeplanBtn.addEventListener("click", openGateModal);
    modal.querySelector(".modalClose").addEventListener("click", closeGateModal);
    modal.addEventListener("click", e=>{ if(e.target===modal) closeGateModal(); });
    function escHandler(e){ if(e.key==="Escape" && !modal.hidden) closeGateModal(); }
    document.addEventListener("keydown", escHandler);

    // WHY the gate fired — a read cap bumped by the write-intent guard was otherwise
    // indistinguishable from a genuinely write-classified capability (user-reported).
    const whyEl=modal.querySelector(".whygate");
    if(reason){ whyEl.textContent = "why: " + reason; }
    else { whyEl.remove(); }
    const repoEl=modal.querySelector(".repo");
    if(repo){ repoEl.textContent = "↪ isolated clone of "+repo+" → draft PR";
      repoEl.title = "your local checkout is never touched"; }
    else { repoEl.remove(); }
    // Plan-first preview + critic findings, factored into one repaint fn — a "Request changes"
    // round replaces both in place (hidden/shown via `hidden`, never removed) instead of the
    // modal being torn down and rebuilt.
    const planWrap=modal.querySelector(".planwrap"), planEl=modal.querySelector(".plan");
    const conWrap=modal.querySelector(".concerns"), noPreviewEl=modal.querySelector(".nopreviewnote");
    const qWrap=modal.querySelector(".questions");
    // WHO wrote the plan you are being asked to approve. Nothing said so before, which is how a
    // preview silently running on the cheapest Claude tier stayed unnoticed — the plan reads as
    // Otto's, not as one model's judgement.
    const planByEl=modal.querySelector(".planby");
    if(planModel) planByEl.textContent = " · written by " + String(planModel).split("/").pop();
    let autoOpened=false;
    function paint(p, cs){
      cs=(cs||[]).filter(c=>c && String(c).trim());
      const trimmed=(p||"").trim();
      // A plan that punted with questions instead of concrete steps, or a real plan that asks
      // one mid-way, renders through the same markdown the model returned — split any question
      // line out so it gets its own box instead of sitting buried inside a wall of steps.
      const {questions, rest} = splitPlanQuestions(trimmed);
      if(questions.length){
        const ul=qWrap.querySelector("ul"); ul.innerHTML="";
        questions.forEach(q=>{ const li=document.createElement("li"); li.innerHTML=renderMD(q); ul.appendChild(li); });
        qWrap.hidden=false;
      } else qWrap.hidden=true;
      if(rest){
        planEl.innerHTML=renderMD(rest);
        planWrap.hidden=false; noPreviewEl.hidden=true;
      } else {
        planWrap.hidden=true;
        // Only the Temporal path (`wid` present) ever runs a preview at all — the direct path
        // has no preview mechanism, so an empty plan there is normal, not a failure to explain.
        // A punt made entirely of questions isn't a failed preview either — it produced exactly
        // what it meant to.
        noPreviewEl.hidden = !wid || !!trimmed;
      }
      if(cs.length){
        conWrap.querySelector(".clabel").textContent =
          "⚠ "+cs.length+" concern"+(cs.length>1?"s":"")+" with this plan";
        const ul=conWrap.querySelector("ul"); ul.innerHTML="";
        cs.forEach(c=>{ const li=document.createElement("li"); li.innerHTML=renderMD(String(c).trim()); ul.appendChild(li); });
        conWrap.hidden=false;
      } else { conWrap.hidden=true; }
      // Blocking only when the ENTIRE response was questions, i.e. there's no concrete plan
      // left to approve — a real plan that also asks something stays approvable, the question
      // is just there for you to weigh in on via "Request changes" if it matters.
      const isQuestions = questions.length>0 && !rest;
      // A question anywhere is easy to miss unless the modal is already open — pop it open the
      // first time one shows up (a full punt or just one line mid-plan), not on every repaint.
      if(questions.length && !autoOpened){ autoOpened=true; openGateModal(); }
      const rt=modal.querySelector(".revisetext");
      if(rt) rt.placeholder = isQuestions
        ? "Type your answers to the questions above…"
        : "What should change before you approve? e.g. \"only touch dev, not prod\"";
      if(isQuestions && (maxRevisions||0)-(revisions||0)>0){
        const rb=modal.querySelector(".revisebox"), rBtn=modal.querySelector(".revise");
        if(rb && rb.hidden){ rb.hidden=false; if(rBtn) rBtn.disabled=true; rt.focus(); }
      }
      // There is no plan to approve yet — only a question — so approving would run blind on
      // whatever the model assumed. Answer (or decline) instead; approving reappears once a
      // real plan comes back from a revision round.
      const approveBtn=modal.querySelector(".approve");
      approveBtn.disabled = isQuestions;
      approveBtn.title = isQuestions
        ? "This plan is only asking a question, not proposing operations — answer below to get a real plan to approve."
        : "";
      // The compact card mirrors the modal's state so the chat feed reads clearly without
      // opening it — only the wording differs.
      gateStatusEl.textContent = isQuestions ? "❓ Otto has a question for you before it can proceed."
        : rest ? (questions.length ? "Plan is ready — it also has a question for you." : "Plan is ready.")
        : wid ? "⚠ Plan preview unavailable." : "Approval needed to run this.";
      gateStatusEl.classList.toggle("isq", isQuestions || (!!rest && questions.length>0));
    }
    paint(plan, concerns);
    content.appendChild(card); scroll();
    const actionBtns=()=>modal.querySelectorAll(".approve, .decline, .revise");
    const finish=(ok)=>{
      card.classList.add("resolved");
      const act=modal.querySelector(".actions"); if(act) act.remove();
      const rb=modal.querySelector(".revisebox"); if(rb) rb.remove();
      const r=document.createElement("div"); r.className="resolution "+(ok?"ok":"no");
      r.textContent= ok ? "✓ approved" : "✕ declined — nothing ran";
      modal.querySelector(".modalBody").appendChild(r);
      gateStatusEl.textContent = r.textContent;
      gateStatusEl.className = "gatestatus resolution "+(ok?"ok":"no");
      seeplanBtn.textContent = "View plan";
      closeGateModal();
      document.removeEventListener("keydown", escHandler);
      if(ok){ led.appr++; render(); } resolve(ok); };
    modal.querySelector(".approve").addEventListener("click",()=>finish(true));
    modal.querySelector(".decline").addEventListener("click",()=>finish(false));

    // Request changes: free-text feedback instead of a decision. The workflow folds it into
    // the request and re-runs the plan preview + critic; THIS modal stays open and repaints
    // with the revised plan rather than resolving, so the outer approve/decline wait is untouched.
    const reviseBtn=modal.querySelector(".revise"), reviseBox=modal.querySelector(".revisebox");
    const reviseText=modal.querySelector(".revisetext");
    if(!wid){ reviseBtn.remove(); reviseBox.remove(); }
    else {
      let left=(maxRevisions||0)-(revisions||0);
      if(left<=0){ reviseBtn.disabled=true; reviseBtn.title="no revision rounds left"; }
      reviseBtn.addEventListener("click",()=>{ reviseBox.hidden=false; reviseBtn.disabled=true; reviseText.focus(); });
      modal.querySelector(".cancel-revise").addEventListener("click",()=>{
        reviseBox.hidden=true; reviseText.value=""; reviseBtn.disabled=left<=0; });

      // Wait out a re-plan round and repaint with the plan it produced. The re-preview is a
      // full agentic pass (minutes), so the ONLY thing on screen until it lands is the note —
      // which is why it must not be cleared early.
      const note=modal.querySelector(".revisenote");
      async function awaitRevision(){
        const base=revisions||0;
        note.hidden=false; note.textContent="Revising the plan…";
        gateStatusEl.textContent="Revising the plan…"; gateStatusEl.classList.remove("isq");
        // `working` is what makes both entry points safe: from the send button the signal may not
        // be processed yet (nothing bumped, nothing replanning), while on a mid-revision reload the
        // counter is ALREADY bumped. Neither reading alone means the new plan is up.
        let working=false;
        for(let tries=0; tries<400; tries++){        // ~10min ceiling, matching the preview's own budget
          await sleep(1500);
          let st;
          try { st=await (await fetch("/api/wf?id="+encodeURIComponent(wid))).json(); }
          catch(e){ continue; }
          if(st.state!=="awaiting_approval"){
            note.textContent="This run has moved on — reload the chat to see its current state.";
            gateStatusEl.textContent="This run has moved on — reload the chat to see its current state.";
            return;
          }
          if(st.replanning || (st.plan_revisions||0) > base) working=true;
          // The pipeline diagram is driven from here too — it is the only surface showing WHICH
          // stage the run is in, and it is stuck behind this same await.
          if(working && onReplan) onReplan(st.replanning ? "replanning" : "gate", st.times||{});
          // plan_revisions bumps the INSTANT the signal lands, before the re-preview runs, so it
          // alone would repaint the previous round's plan ~1.5s in and clear the note — the run
          // looked untouched and the feedback looked dropped. `replanning` is the workflow's own
          // "the new preview is still in flight".
          if(!working || st.replanning) continue;
          revisions=st.plan_revisions||base; left=(maxRevisions||0)-revisions;
          // Re-enable first, THEN paint — paint() may re-disable "Approve" (still just a
          // question) or "Request changes" (auto-opened, no rounds left), and must have the
          // final say over this blanket reset, not get clobbered by it.
          for(const b of actionBtns()) b.disabled=false;
          const sb=modal.querySelector(".send-revise"); if(sb) sb.disabled=false;
          reviseBtn.disabled=left<=0;
          if(left<=0) reviseBtn.title="no revision rounds left";
          paint(st.plan, st.plan_concerns);
          // Cleared LAST, once the new plan is actually on screen. "Revising the plan…" is the
          // only thing standing in for a multi-minute wait, so a card that clears it first has
          // a moment showing neither — which is what a dropped revision looks like.
          note.hidden=true;
          return;
        }
        note.textContent="Still revising — this is taking longer than usual.";
        gateStatusEl.textContent="Still revising — this is taking longer than usual.";
      }

      modal.querySelector(".send-revise").addEventListener("click", async ()=>{
        const fb=reviseText.value.trim(); if(!fb) return;
        const sendBtn2=modal.querySelector(".send-revise");
        sendBtn2.disabled=true; for(const b of actionBtns()) b.disabled=true;
        try { await api("/api/wf/signal",{id:wid, signal:"revise_plan", value:fb}); }
        catch(e){
          sendBtn2.disabled=false; for(const b of actionBtns()) b.disabled=false;
          note.hidden=false; note.textContent="Couldn't send that — try again.";
          return;
        }
        reviseBox.hidden=true; reviseText.value="";
        await awaitRevision();
      });

      // A reload mid-revision rebuilds this card from a state that says awaiting_approval while
      // holding the PREVIOUS plan — approving there approves something the human never saw. Park
      // it on the note until the round lands, same as the sender's own tab, and surface that
      // straight away rather than behind a "See plan" click.
      if(replanning){
        for(const b of actionBtns()) b.disabled=true;
        modal.querySelector(".send-revise").disabled=true;
        openGateModal();
        awaitRevision();
      }
    }
  });
}

function finishTurn(){ busy=false; syncSendBtn(); input.focus(); scroll(); mascotTurn(null); }
function autosize(){ input.style.height="auto"; input.style.height=Math.min(input.scrollHeight,120)+"px"; }

/* ---- slash-command completions: type "/" to pick a capability ---- */
let CAPS_LIST=[], slashItems=[], slashIdx=-1;

/* Set the capability list (powers the "/" slash-command completions) + header counts.
   Called on first load AND after every admin change, so newly added/removed caps (e.g. an
   imported project repo) show up without a manual page refresh. */
function applyCaps(caps){
  CAPS_LIST = caps;
  const on=caps.filter(c=>c.enabled!==false);
  const set=(id,v)=>{ const e=document.getElementById(id); if(e) e.textContent=v; };
  set("n-caps",on.length);
  set("n-read",on.filter(c=>c.risk==="read").length);
  set("n-write",on.filter(c=>c.risk==="write").length);
  // The header counts ENABLED caps, the Admin section lists ALL of them — two different, both
  // correct numbers that read as a bug when they sit on one screen unexplained. The section
  // badge shows both, and is refreshed from here so toggling a cap can't leave it stale.
  const badge=document.querySelector('.asection[data-sect="caps"] .sectcount');
  if(badge) badge.textContent=`${on.length} / ${caps.length}`;
}
// Populate the composer's repo picker (#57). Shown only when ≥1 git project repo is
// registered (Admin → Project repos). Repo-mode is now AUTO-DETECTED for chat writes that edit a
// named repo; this picker is an optional OVERRIDE ("auto-detect" default) to force a specific repo.
async function loadRepos(){
  let repos=[];
  try { repos=(await (await fetch("/api/repos")).json()).repos||[]; } catch(e){ return; }
  const bar=document.getElementById("repobar"), sel=document.getElementById("repopick");
  if(!bar||!sel) return;
  if(!repos.length){ bar.hidden=true; return; }
  const cur=sel.value;
  sel.innerHTML=`<option value="">auto-detect</option>`+
    repos.map(r=>`<option value="${esc(r.name)}">${esc(r.name)} — force isolate</option>`).join("");
  sel.value=cur;                                   // preserve selection across reloads
  bar.hidden=false;
  syncQAToggle();
}
// The "QA the PR" checkbox is only meaningful when a repo is picked (it acts on the opened PR).
// Code review isn't a checkbox at all (it always runs), so it isn't gated here — only dimmed
// the same way, for visual consistency with the row it sits in.
function syncQAToggle(){
  const sel=document.getElementById("repopick");
  if(!sel) return;
  const on=!!sel.value;
  const c=document.getElementById("qacheck");
  if(c){ c.disabled=!on; if(!on) c.checked=false; }
  const tog=document.getElementById("qatoggle"); if(tog) tog.classList.toggle("off",!on);
  const always=document.getElementById("reviewalways"); if(always) always.classList.toggle("off",!on);
  const hint=document.getElementById("prhint"); if(hint) hint.hidden=on;
}
// Three Run-mode controls are MUTUALLY EXCLUSIVE, and each conflict was silent before this:
//   Brainstorm x Repo          — an explicit repo pick forces cap.risk to write, so a read-only
//                                conversation got a plan preview, an approval card and a clone.
//   Brainstorm x Break-into-steps — plan-then-execute wins over the ladder outright, so the
//                                brainstorm turn never ran at all; the musing was decomposed
//                                into atomic steps, each with its own verify ladder.
//   Repo x Break-into-steps    — PRE-EXISTING: `_may_plan_steps` excludes repo-mode, so the box
//                                stayed tickable and did nothing.
// The workflow resolves all three on its own (stale tab, scripted client), always toward the
// narrower mode — but a control that silently does nothing is the bug this exists to prevent,
// so the loser is DISABLED and says why. `title` carries the reason: it is the established home
// for tab copy in this UI, and these rows have no room for prose.
function applyModeExclusions(){
  const repo=document.getElementById("repopick"), bs=document.getElementById("bscheck");
  const plan=document.getElementById("plancheck");
  const bsOn=!!(bs && bs.checked && !bs.disabled);
  if(repo){
    repo.disabled=bsOn;
    if(bsOn) repo.value="";
    repo.title=bsOn ? "Brainstorm changes nothing, so there is no clone or PR to target — it can already read your repos."
                    : "Otto auto-detects when a request edits one of your repos and runs it in an isolated clone → draft PR. Pick a repo here only to force or override that.";
  }
  const repoOn=!!(repo && repo.value);
  if(plan){
    const off=bsOn||repoOn;
    plan.disabled=off; if(off) plan.checked=false;
    const l=document.getElementById("plantoggle");
    if(l) l.title = bsOn ? "Not available while brainstorming — breaking a conversation into atomic steps replaces the conversation."
                  : repoOn ? "Not available in repo mode — a repo run always takes the single-capability path."
                  : "For a big task with several distinct parts: break it into small atomic steps up front and run each one at a time on the local executor, instead of one long single-shot attempt. Different from the read-only plan preview you approve at the write gate — this changes HOW the work is executed, not just what you see before approving it.";
  }
  const bsl=document.getElementById("bstoggle");
  if(bs && bsl && !(currentSession && currentSession.cap)){
    const off=repoOn||!!(plan&&plan.checked);
    bs.disabled=off; if(off) bs.checked=false;
    if(off) bsl.title = repoOn ? "Not available with a repo picked — repo mode clones and opens a PR, which a read-only conversation has nothing to put in."
                               : "Not available with step-by-step execution — that replaces the conversation with a decomposed task.";
  }
  // This function is the one place that UNCHECKS a control on the user's behalf, so the
  // collapsed panel's summary is re-read here rather than only from the change event.
  syncOptSummary();
}
/* Collapsed, the options panel must still say what is set — see the .optsum CSS note. Only
   NON-DEFAULT settings are listed: a summary that always reads the same is ignorable, and the
   whole point is that a stray "Auto approve" stays visible with the panel shut. */
function syncOptSummary(){
  const box=document.getElementById("optsum"); if(!box) return;
  const on=id=>{ const el=document.getElementById(id); return !!(el && el.checked && !el.disabled); };
  const sel=id=>{ const el=document.getElementById(id); return el && el.value ? el.value : ""; };
  const bits=[];
  const repo=sel("repopick"); if(repo) bits.push("repo: "+repo);
  if(on("qacheck")) bits.push("QA in staging");
  if(on("bscheck")) bits.push("brainstorm");
  if(on("plancheck")) bits.push("step-by-step");
  if(!on("memcheck")) bits.push("no memory");
  if(on("autoapprove")) bits.push("auto approve");
  const m=sel("modelpick"); if(m) bits.push("model: "+m);
  const ef=sel("effortpick"); if(ef) bits.push("effort: "+ef);
  box.textContent = bits.length ? "· "+bits.join(" · ") : "· defaults";
  box.title = bits.length ? "Set for this chat: "+bits.join(", ") : "";
}
const OPT_COLLAPSED_KEY="ottoOptCollapsed";
function applyOptCollapsed(on){
  const panel=document.getElementById("optpanel"), btn=document.getElementById("opt-toggle");
  if(panel) panel.classList.toggle("collapsed", !!on);
  if(btn){ btn.setAttribute("aria-expanded", on?"false":"true");
           btn.title = on ? "Show the run options" : "Collapse these run options"; }
  syncOptSummary();
  try { localStorage.setItem(OPT_COLLAPSED_KEY, on?"1":"0"); } catch(e){}
}
document.getElementById("opt-toggle").addEventListener("click",()=>{
  applyOptCollapsed(!document.getElementById("optpanel").classList.contains("collapsed"));
});
try { applyOptCollapsed(localStorage.getItem(OPT_COLLAPSED_KEY)==="1"); } catch(e){}

document.addEventListener("change",e=>{ if(!e.target) return;
  if(e.target.closest("#optpanel")) syncOptSummary();
  if(e.target.id==="repopick") syncQAToggle();
  if(e.target.id==="autoapprove") applyApprovalHint();
  if(["repopick","bscheck","plancheck"].includes(e.target.id)){ applyModeExclusions(); syncQAToggle(); }
});
const slashPop=document.getElementById("slashpop");
function slashFragment(){
  // the command being typed: the whole input is "/<name-so-far>" with no space yet
  const m=/^\s*\/([\w:.\-]*)$/.exec(input.value);
  return m?m[1]:null;
}
function updateSlash(){
  const frag=slashFragment();
  if(frag===null){ hideSlash(); return; }
  const q=frag.toLowerCase();
  slashItems=CAPS_LIST.filter(c=>c.enabled!==false && c.name.toLowerCase().includes(q)).slice(0,8);
  if(!slashItems.length){ hideSlash(); return; }
  if(slashIdx<0||slashIdx>=slashItems.length) slashIdx=0;
  renderSlash();
}
function renderSlash(){
  slashPop.innerHTML=slashItems.map((c,i)=>`<div class="slashitem ${i===slashIdx?'sel':''}" data-i="${i}">
    <span class="slashname">/${esc(c.name)}</span>
    <small>${esc((c.description||'').slice(0,80))}</small>
    <span class="badge ${esc(c.risk)}">${esc(c.risk)}</span>
  </div>`).join("");
  slashPop.hidden=false;
  slashPop.querySelectorAll(".slashitem").forEach(el=>{
    el.addEventListener("mousedown",e=>{ e.preventDefault(); acceptSlash(+el.dataset.i); });   // mousedown keeps focus
    el.addEventListener("mouseover",()=>{ slashIdx=+el.dataset.i; highlightSlash(); });
  });
}
function highlightSlash(){ slashPop.querySelectorAll(".slashitem").forEach((el,i)=>el.classList.toggle("sel",i===slashIdx)); }
function hideSlash(){ slashPop.hidden=true; slashItems=[]; slashIdx=-1; }
function slashOpen(){ return !slashPop.hidden && slashItems.length>0; }
function acceptSlash(i){
  const c=slashItems[i]; if(!c) return;
  input.value="/"+c.name+" "; hideSlash(); input.focus(); autosize();
}
/* If the message is "/<exact enabled cap> [args]", run that capability directly (skip Router #1). */
function parseSlash(text){
  const m=/^\s*\/([\w:.\-]+)(?:\s+([\s\S]*))?$/.exec(text||"");
  if(!m) return null;
  const cap=CAPS_LIST.find(c=>c.name===m[1] && c.enabled!==false);
  return cap ? {cap, request:(m[2]||"").trim()} : null;
}

input.addEventListener("input",()=>{ autosize(); updateSlash(); });
input.addEventListener("blur",()=>setTimeout(hideSlash,150));   // allow click-select to land first
input.addEventListener("keydown",e=>{
  if(slashOpen()){
    if(e.key==="ArrowDown"){ e.preventDefault(); slashIdx=(slashIdx+1)%slashItems.length; highlightSlash(); return; }
    if(e.key==="ArrowUp"){ e.preventDefault(); slashIdx=(slashIdx-1+slashItems.length)%slashItems.length; highlightSlash(); return; }
    if(e.key==="Enter"||e.key==="Tab"){ e.preventDefault(); acceptSlash(slashIdx); return; }
    if(e.key==="Escape"){ e.preventDefault(); hideSlash(); return; }
  }
  if(e.key==="Enter"&&!e.shiftKey){ e.preventDefault(); submit(input.value); }
});
sendBtn.addEventListener("click",()=>{ if(sendBtn.classList.contains("stop")) stopRun(); else submit(input.value); });
