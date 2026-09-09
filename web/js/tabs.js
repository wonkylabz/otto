"use strict";
/* ---- tabs ---- */
let adminLoaded=false, POLICY_STATE=null, MODEL_STATE=null, saveTimer=null, GATEWAY_STATS=null, SCORECARD={};
let MODEL_HEALTH={};   // {model name: {ok, detail, at, via}} — last known outcome per pool entry
let MCP_ISSUES=0;      // broken enabled MCP servers, kept so the badge can be recomputed without a re-render
let SECRETS=null;      // config.secret_status() — presence + source per secret, NEVER a value
let AUDIT_FILTER={wid:"",cap:"",verified:""};
function activateTab(v){
  const views={chat:"chatview",memory:"memoryview",knowledge:"knowledgeview",audit:"auditview",board:"boardview",schedules:"schedulesview",events:"eventsview",admin:"adminview"};
  if(!views[v]) v="chat";
  document.querySelectorAll(".tab").forEach(x=>x.classList.toggle("active", x.dataset.view===v));
  for(const [view,id] of Object.entries(views)) document.getElementById(id).hidden = view!==v;
  try { localStorage.setItem("ottoTab", v); } catch(e){}
  if(v==="admin" && !adminLoaded) loadAdmin();
  if(v==="memory") loadMemory();   // always refresh — it changes as you use chat
  if(v==="knowledge") loadKnowledge();
  if(v==="audit") loadAudit();
  if(v==="board") loadBoard();
  if(v==="schedules") loadJobs();
  if(v==="events") loadEvents();
}
document.querySelectorAll(".tab").forEach(t=>t.addEventListener("click",()=>activateTab(t.dataset.view)));

/* ---- deep link (#run=<wid>) ----
   Where a push notification's tap lands (delivery.notify sets click=<url>#run=<wid>). Without
   this the tap opened the home tab and the reader had to hunt for the run they were just told
   about — which is the only thing the push was for.
   The run may be LIVE (a Board card, which is where its Approve/Deny buttons are) or long
   finished (no card at all), so try the board first and fall back to the run-detail modal, which
   reads the audit trail and still works after the workflow has aged out of Temporal.
   PENDING_RUN lives OUTSIDE the render: loadBoard rebuilds its DOM on a 3.5s poll, so the target
   card does not exist yet when the hash is read. */
let PENDING_RUN=null;
function openDeepLink(){
  const m=/^#run=(.+)$/.exec(location.hash||"");
  if(!m) return;
  PENDING_RUN=decodeURIComponent(m[1]);
  activateTab("board");           // -> loadBoard -> renderBoard -> focusPendingRun
}
function focusPendingRun(){
  if(!PENDING_RUN) return;
  const wid=PENDING_RUN;
  // Matched by comparing dataset values, NOT `[data-run="${CSS.escape(wid)}"]` — CSS.escape is
  // for identifiers and injects backslashes the literal attribute value does not have.
  let card=null;
  document.querySelectorAll(".bcard[data-run]").forEach(c=>{ if(c.dataset.run===wid) card=c; });
  PENDING_RUN=null;
  if(card){
    card.scrollIntoView({block:"center",behavior:"smooth"});
    card.classList.add("focus");
    setTimeout(()=>card.classList.remove("focus"),4000);
  } else {
    openRunDebug(wid,null,null);
  }
}
window.addEventListener("hashchange",openDeepLink);
