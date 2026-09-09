"use strict";
/* ---- Otto, the mascot -------------------------------------------------------------------
   The companion in the bottom-left corner. He is not decoration: his state IS the pipeline's
   state, so a glance at the corner answers "is anything happening" from any tab, including
   the six tabs that show no run at all.

   ONE owner. Every source writes a SLOT on MOOD and calls applyMood(); nothing else ever
   touches the element. Sources that each set it directly is how two counters for the same
   noun drift apart (applyCaps), and here it would be worse, because the slots outlive each
   other: a chat turn finishing must not erase "3 runs still going on the board".

   Priority, highest first:
     paused  the global pause - nothing can start, so nothing else is true
     react   a one-off pop when a result lands, ~2s, then it re-resolves
     turn    the run THIS tab is watching - what the human is actually looking at
     fleet   work running or waiting anywhere else (Slack, board, a schedule, another tab)
     idle    nothing; after IDLE_SLEEP_MS he sleeps rather than hovering awake forever

   The stage table is the pipeline's, not a second vocabulary: every stage workflows.py can
   enter has a row here, guarded by test_ingress.MascotStateTests. Tints reuse the board's
   --stg-* chip tokens, so the corner and the Running column say the same thing in the same
   colour. */
const MASCOT_KEY="otto.mascot", MASCOT_POS_KEY="otto.mascot.pos", MASCOT_HOME_KEY="otto.mascot.home", MASCOT_IDLE_SLEEP_MS=240000;
const MASCOT_EDGE=14;      // the gap he keeps from any viewport edge, wherever he is dropped
const MASCOT_STAGE={
  INGRESS:  ["thinking", "let me read that\u2026"],
  DECOMPOSE:["thinking", "seeing if this splits up\u2026"],
  ROUTER:   ["thinking", "finding the right tool\u2026"],
  CLARIFY:  ["thinking", "checking I have everything\u2026"],
  PLAN:     ["planning", "let me think that through\u2026"],
  GATE:     ["thinking", "ready when you are"],
  RUN:      ["working",  "working on it\u2026"],
  PR:       ["working",  "opening a pull request\u2026"],
  REVIEW:   ["working",  "reading back my own diff\u2026"],
  QA:       ["working",  "putting it to the test\u2026"],
  DELIVER:  ["working",  "wrapping up\u2026"],
  AUDIT:    ["working",  "writing it all down\u2026"],
};
const MOOD={paused:false, turn:null, fleet:null, react:null, quietSince:Date.now(), timer:null};
let MASCOT_SIG="";

/* ---- placement ----
   Where he stands is the user's, so he is dragged, not configured. The position is stored as
   a FRACTION of the free area, not as pixels: a pixel pair saved on a 2560px monitor puts him
   off-screen on a laptop, and "off-screen" for a fixed element is unrecoverable without
   clearing storage. Fractions also keep a corner a corner when the window is resized. */
function mascotPos(){
  if(MASCOT_POS_MEM) return MASCOT_POS_MEM;
  try { const v=JSON.parse(localStorage.getItem(MASCOT_POS_KEY)||"null");
        if(v && isFinite(v.fx) && isFinite(v.fy)) return v; } catch(e){}
  return null;                               // never dragged — mascotPlace anchors him to HOME
}
/* His HOME is an ANCHOR, not a fraction: centred at the foot of the workflow rail, standing on
   the ledger. Two reasons it can't be a stored fraction. The rail is a FIXED-width column
   against a variable window, so one fx lands in a different place on every monitor. And the old
   home — the bottom-left corner — was over the chats sidebar, which now collapses to a 34px
   rail he would hang off the side of.
   Returns null when the rail isn't measurable (any tab but Chat, or under 680px); the caller
   then falls back to the last home it resolved, and only failing that to the old corner. */
function mascotHomeXY(r){
  const rail=document.querySelector(".chatview:not([hidden]) .rail");
  const led=rail && rail.querySelector(".ledger");
  if(!rail || !led) return null;
  const rr=rail.getBoundingClientRect(), lr=led.getBoundingClientRect();
  if(rr.width < r.width + 8) return null;    // narrower than he is — nothing to centre him in
  return {x: rr.left + (rr.width - r.width)/2,
          y: Math.max(MASCOT_EDGE, lr.top - r.height - 16)};
}
function mascotClamp(v){ return Math.min(1, Math.max(0, v)); }
/* The resolved home, remembered ACROSS tabs and reloads. "Send him home" is a double-click, and
   a double-click is available from every tab — but the rail it anchors to only exists on Chat,
   so mascotHomeXY returns null everywhere else. Without a persisted answer the reset fell all
   the way through to MASCOT_FALLBACK, i.e. it sent him to the corner he no longer lives in. */
function mascotHomeMem(){
  if(MASCOT_HOME_MEM) return MASCOT_HOME_MEM;
  try { const v=JSON.parse(localStorage.getItem(MASCOT_HOME_KEY)||"null");
        if(v && isFinite(v.fx) && isFinite(v.fy)) return (MASCOT_HOME_MEM=v); } catch(e){}
  return null;
}
/* Last resort: a browser that has never rendered the Chat tab, so nothing has ever measured the
   rail. The rail is the RIGHT-hand column (.chatview is 232px | 1fr | 322px), so the bottom-right
   corner is the nearest thing to standing at the foot of it. It used to be {fx:0} — the
   bottom-left corner he lived in BEFORE the rail, on the opposite side of the window. */
const MASCOT_FALLBACK={fx:1, fy:1};
/* Applies the stored fraction as real pixels. Called on show, after a drag and on resize -
   the free area changes with the window, and a fraction only means something against it. */
function mascotPlace(){
  const d=document.getElementById("mascot");
  if(!d || d.hidden) return;
  const r=d.getBoundingClientRect(), p=mascotPos();
  const freeX=Math.max(0, window.innerWidth - r.width - MASCOT_EDGE*2);
  const freeY=Math.max(0, window.innerHeight - r.height - MASCOT_EDGE*2);
  const home=p ? null : mascotHomeXY(r);
  let x, y;
  if(home){
    x=home.x; y=home.y;
    // Cached as a fraction: the rail can't be measured from any other tab, so a resize over
    // there would otherwise fling an undragged Otto into a corner he has never stood in.
    MASCOT_HOME_MEM={fx: freeX ? mascotClamp((x-MASCOT_EDGE)/freeX) : 0,
                     fy: freeY ? mascotClamp((y-MASCOT_EDGE)/freeY) : 1};
    try { localStorage.setItem(MASCOT_HOME_KEY, JSON.stringify(MASCOT_HOME_MEM)); } catch(e){}
  } else {
    const f=p || mascotHomeMem() || MASCOT_FALLBACK;
    x=MASCOT_EDGE + freeX*mascotClamp(f.fx); y=MASCOT_EDGE + freeY*mascotClamp(f.fy);
  }
  d.style.left=Math.round(x)+"px"; d.style.top=Math.round(y)+"px"; d.style.bottom="auto";
  // No room for a bubble above his head up there - put it under him and flip the tail.
  d.classList.toggle("below", y < 120);
  mascotLayout();
}
/* Whether the two surfaces that reach his DEFAULT corner should give up space for him. Read
   off his real rect, not off a stored flag: he is draggable, so "is he there" is a question
   only the geometry can answer. */
function mascotLayout(){
  const d=document.getElementById("mascot");
  const r=d&&!d.hidden ? d.getBoundingClientRect() : null;
  const inCorner = !!r && r.left < 260 && r.bottom > window.innerHeight - 260;
  document.body.classList.toggle("mascot-reserve", inCorner);
  // His FIGURE, not the dock: the bubble comes and goes, and an inset that resized every time
  // he spoke would shunt the chat list up and down under the cursor.
  const fig=document.getElementById("mascot-fig");
  const fh=fig ? Math.round(fig.getBoundingClientRect().height) : 0;
  if(fh) document.body.style.setProperty("--m-h", fh+"px");
}
let MASCOT_DRAG=null, MASCOT_DRAGGED=false;
function mascotDragStart(e){
  if(e.button||e.target.closest(".mhide")) return;
  const d=document.getElementById("mascot"), r=d.getBoundingClientRect();
  MASCOT_DRAG={dx:e.clientX-r.left, dy:e.clientY-r.top, x0:e.clientX, y0:e.clientY};
  MASCOT_DRAGGED=false;
  try { e.currentTarget.setPointerCapture(e.pointerId); } catch(err){}
}
function mascotDragMove(e){
  if(!MASCOT_DRAG) return;
  // A 5px threshold, so a plain click on him is still a click and not a 1px drag that
  // swallows it - his bubble is also a link to the board.
  if(!MASCOT_DRAGGED && Math.abs(e.clientX-MASCOT_DRAG.x0)+Math.abs(e.clientY-MASCOT_DRAG.y0) < 5) return;
  const d=document.getElementById("mascot"), r=d.getBoundingClientRect();
  MASCOT_DRAGGED=true; d.classList.add("dragging");
  const freeX=Math.max(1, window.innerWidth - r.width - MASCOT_EDGE*2);
  const freeY=Math.max(1, window.innerHeight - r.height - MASCOT_EDGE*2);
  const pos={fx:mascotClamp((e.clientX-MASCOT_DRAG.dx-MASCOT_EDGE)/freeX),
             fy:mascotClamp((e.clientY-MASCOT_DRAG.dy-MASCOT_EDGE)/freeY)};
  try { localStorage.setItem(MASCOT_POS_KEY, JSON.stringify(pos)); } catch(err){}
  MASCOT_POS_MEM=pos;
  mascotPlace();
}
function mascotDragEnd(){
  if(!MASCOT_DRAG) return;
  MASCOT_DRAG=null;
  document.getElementById("mascot").classList.remove("dragging");
}
/* Sends him home. The one recovery from a drop the user regrets, and the only way back if a
   window shrinks around an awkward spot. */
function mascotHome(){
  MASCOT_POS_MEM=null;
  try { localStorage.removeItem(MASCOT_POS_KEY); } catch(e){}
  mascotPlace();
}

/* Hiding him is a property of who is looking, not of this Otto - same reasoning (and same
   storage) as the theme, never a server setting. */
/* MASCOT_ON is the live answer and localStorage only SEEDS it: a browser that refuses
   storage (private window, site data blocked) would otherwise re-read "shown" on the very
   next tick and undo the click, leaving his own dismiss button visibly dead. */
let MASCOT_ON=true, MASCOT_POS_MEM, MASCOT_HOME_MEM;
try { MASCOT_ON = localStorage.getItem(MASCOT_KEY)!=="off"; } catch(e){}
function mascotShown(){ return MASCOT_ON; }
function showMascot(on){
  MASCOT_ON=!!on;
  try { localStorage.setItem(MASCOT_KEY, on?"on":"off"); } catch(e){}
  MASCOT_SIG="";
  applyMood();
}
function stageMood(stage, sub){
  const m=MASCOT_STAGE[stage]||["thinking","working on it\u2026"];
  return {state:m[0], say:m[1], sub:sub||""};
}
function resolveMood(){
  if(MOOD.paused) return {state:"sleeping", say:"I\u2019m paused", sub:"nothing new will start"};
  if(MOOD.react) return MOOD.react;
  // No sub-line on his own turn: "in this chat" is the one thing the reader can already see.
  if(MOOD.turn) return stageMood(MOOD.turn.stage);
  const f=MOOD.fleet||{};
  if(f.stage) return stageMood(f.stage, f.sub);      // this one earns a sub - it says WHERE
  if(f.waiting) return {state:"thinking", say:"waiting on you", sub:f.sub};
  if(f.needs) return {state:"error", say:"something needs a look", sub:f.sub};
  return (Date.now()-MOOD.quietSince > MASCOT_IDLE_SLEEP_MS)
    ? {state:"sleeping", say:"", sub:"", quiet:true}
    : {state:"idle", say:"", sub:"", quiet:true};
}
/* The single writer. Idempotent on purpose - every poller re-resolves on its own tick, and
   re-setting `state` would restart a CSS animation mid-swing (a permanently stuttering robot
   is how you notice). */
function applyMood(){
  const d=document.getElementById("mascot");
  if(!d) return;
  if(!mascotShown()){ d.hidden=true; document.body.classList.remove("mascot-on");
                     document.body.classList.remove("mascot-reserve"); return; }
  d.hidden=false; document.body.classList.add("mascot-on");
  if(!d.style.left) mascotPlace();      // first paint: fractions -> pixels
  const m=resolveMood(), sig=[m.state,m.say,m.sub,m.quiet].join("|");
  if(sig===MASCOT_SIG) return;
  MASCOT_SIG=sig;
  const fig=document.getElementById("mascot-fig");
  // STATE only. His ink is a fixed `color="var(--accent)"` on the element and moves only with
  // the theme: repainting the character on every stage made him read as a status light rather
  // than as Otto, and the stage colour is already carried by the bubble - the part that is
  // actually reporting.
  if(fig) fig.setAttribute("state", m.state);
  // Nothing is happening: he stays, the panel goes. A bubble reading "ready" over the chat
  // list is a permanent obstruction carrying no information.
  const bub=document.getElementById("mascot-bubble"); if(bub) bub.hidden=!!m.quiet;
  const say=document.getElementById("mascot-say"); if(say) say.textContent=m.say;
  const sub=document.getElementById("mascot-sub");
  if(sub){ sub.textContent=m.sub||""; sub.hidden=!m.sub; }
}
function mascotPause(on){ if(MOOD.paused===!!on) return; MOOD.paused=!!on; MOOD.quietSince=Date.now(); applyMood(); }
/* The stage THIS tab's turn is in. Only active/gatehold stages call it - a stage going `done`
   must not blank him, or the corner strobes empty between every two stages. */
function mascotTurn(stage, sub){
  MOOD.turn = stage ? {stage, sub:sub||"in this chat"} : null;
  MOOD.quietSince=Date.now();
  applyMood();
}
function mascotReact(state, say, sub, ms){
  ms=ms||2200;
  MOOD.react={state, say, sub:sub||""};
  MOOD.quietSince=Date.now();
  applyMood();
  clearTimeout(MOOD.timer);
  MOOD.timer=setTimeout(()=>{ MOOD.react=null; applyMood(); }, ms);
}
/* Forgetting something is the one memory action with a before and an after, so it gets a
   pose of its own: he opens his head, lifts the fact out and drops it in the bin. It rides
   the REACT slot like every other one-off, so a run in flight simply takes the corner back
   when the cycle ends - and the duration is one full cycle of the animation, because half of
   one is a robot freezing with its own head open. */
/* A dab is a pose he has to ARRIVE at and then hold: the throw alone is .85s, and cutting it
   at the default react window shows a robot with its arms halfway up rather than a dab. */
const MASCOT_DAB_MS=3000;
const MASCOT_EVICT_MS=4700;
function mascotEvict(n){
  mascotReact("evicting", "taking out the trash",
              n>1 ? n+" memories gone" : "one memory gone", MASCOT_EVICT_MS);
}
/* Which ingress a run came in through, read off its workflow id - the same prefixes the
   audit trail and the board are keyed on. Never render the raw id: a slack-* id holds a
   channel id (privacy.source_line makes the same distinction). */
function runOrigin(id){
  id=id||"";
  if(id.indexOf("slack-")===0) return "Slack";
  if(id.indexOf("sched-")===0) return "a schedule";
  if(id.indexOf("gh-")===0) return "the board";
  if(id.indexOf("evt-")===0) return "a webhook";
  return "another chat";
}
/* Work happening anywhere but this tab's own turn. Both callers hand over the same shape so
   there is one summariser: the 15s /api/needs-you badge poll (every tab but Board) and
   loadBoard's own 3.5s data (Board tab). No third poller - see the estop bar's note. */
function mascotFleet(sum){
  const mine=activeChat&&activeChat.run_id;
  const run=(sum.run||[]).filter(it=>it.id!==mine);
  const f={};
  if(run.length){
    const it=run[0];
    f.stage=it.stage||"RUN";
    f.sub="for "+runOrigin(it.id)+(it.cap?" · "+it.cap:"")+(run.length>1?" (+"+(run.length-1)+" more)":"");
  } else if(sum.waiting){
    f.waiting=sum.waiting;
    f.sub=sum.waiting+(sum.waiting>1?" runs need":" run needs")+" a decision";
  } else if(sum.needs){
    f.needs=sum.needs;
    f.sub=sum.needs+" run"+(sum.needs>1?"s":"")+" ended needing review";
  }
  const sig=JSON.stringify(f);
  if(sig===JSON.stringify(MOOD.fleet||{})) return;
  MOOD.fleet=f; MOOD.quietSince=Date.now(); applyMood();
}
// Null-guarded: these are top-level statements in a 6,000-line script, so a missing element
// here would throw and take the boot IIFE below down with it - the whole app, over a mascot.
const mBubble=document.getElementById("mascot-bubble"), mFig=document.getElementById("mascot-fig");
if(mBubble) mBubble.addEventListener("click", e=>{
  if(MASCOT_DRAGGED){ MASCOT_DRAGGED=false; return; }   // that click was the end of a drag
  if(e.target.closest(".mhide")){ showMascot(false); return; }
  // He is reporting on something that isn't on screen - take the human to it.
  if(!MOOD.turn && (MOOD.fleet||{}).sub) activateTab("board");
});
if(mFig) mFig.addEventListener("click", ()=>{ if(mFig.blink) mFig.blink(); });
// Drag him anywhere; double-click sends him home. Both grips move the same dock.
[mBubble, mFig].forEach(el=>{
  if(!el) return;
  el.addEventListener("pointerdown", mascotDragStart);
  el.addEventListener("pointermove", mascotDragMove);
  el.addEventListener("pointerup", mascotDragEnd);
  el.addEventListener("pointercancel", mascotDragEnd);
  el.addEventListener("dblclick", mascotHome);
});
// A resize changes the free area the stored fraction is measured against, and can leave a
// fixed element off-screen - which for a dock with no scroll of its own is unrecoverable.
window.addEventListener("resize", mascotPlace);
/* A local ticker, not a poller: nothing on the network changes when a quiet Otto falls
   asleep, and the 15s badge polls stop being a reliable clock the moment a tab is
   backgrounded. Cheap - applyMood is a no-op unless the resolved mood actually changed. */
setInterval(applyMood, 20000);
