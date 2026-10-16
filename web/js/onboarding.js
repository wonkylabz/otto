"use strict";
/* ---- Onboarding: how do you plan to use Otto? → settings diff + integration checklist → tour ----
   Opens by itself until finished or skipped (`/api/onboarding`'s auto_open), and from the header's
   Setup button any time after. Re-running is safe by construction: a profile is a full assignment,
   so the second apply of the same answers has nothing to change. Integrations are DETECTED and
   pointed at, never configured here — their tokens and connectors keep their own forms.
   Nothing here runs at load except wiring its own button; boot.js calls initOnboarding(). */
let OB=null;   // {meta, answers, step}

async function initOnboarding(){
  const status=document.querySelector("header .status"), upd=document.getElementById("update");
  if(status && !document.getElementById("ob-start")){
    const b=document.createElement("button");
    b.className="estop obbtn"; b.id="ob-start"; b.textContent="Setup";
    b.title="Re-run onboarding: how you use Otto, a cost profile, what to connect, and a tour";
    b.addEventListener("click",()=>openOnboarding());
    status.insertBefore(b, upd);
  }
  let meta; try { meta=await getJSON("/api/onboarding"); } catch(e){ return; }
  // A push's deep link (#run=<wid>) lands on the run it is about; the wizard must not cover it.
  if(meta.auto_open && !/^#run=/.test(location.hash)) openOnboarding(meta);
}

async function openOnboarding(meta){
  if(!meta){ try { meta=await getJSON("/api/onboarding"); } catch(e){ toast("onboarding failed to load: "+e.message); return; } }
  const prev=meta.answers||{};
  OB={meta, step:0, seq:0, done:false, answers:{usage:prev.usage||"", profile:prev.profile||"balanced",
                                                 integrations:prev.integrations||null}};
  obRender();
  // Any way out that isn't Apply — X, Esc, "Just the tour", an integration's Open — counts as
  // dismissed, keeping the draft answers: otherwise every page load reopens it on someone who
  // has already dealt with it. Setup in the header still runs it again.
  afterFormModal(()=>{ if(OB && !OB.done) postOr("/api/onboarding/skip",{answers:OB.answers},"saving onboarding"); });
}

const OB_STEPS=["welcome","usage","profile","integrations","review"];

function obBody(){
  const body=openFormModal("<b>Set up Otto</b>");
  body.closest(".modalBox").classList.add("wideModalBox");
  return body;
}

function obRender(){
  const body=obBody(), step=OB_STEPS[OB.step], a=OB.answers, m=OB.meta;
  const bar=`<div class="obsteps">${OB_STEPS.map((_,i)=>`<i class="${i<=OB.step?'on':''}"></i>`).join("")}</div>`;
  const opt=(type,name,val,label,sub,checked)=>`<label class="obopt${checked?' sel':''}"><input type="${type}" name="${name}" value="${esc(val)}"${checked?' checked':''}><b>${esc(label)}</b>${sub?`<span>${esc(sub)}</span>`:""}</label>`;
  let html="";
  if(step==="welcome"){
    html=`<p class="obq">Welcome to Otto</p>
      <p class="obsub">Three quick questions tune Otto to how you'll use it: who it works for, how much each run may cost, and what it should be able to reach. You'll see every change before it's made, and you can run this again any time from the <b>Setup</b> button.</p>
      <div class="obnav"><button class="btn decline obleft" data-ob="skip" title="Don't open this by itself again — the Setup button still runs it">Skip for now</button>
      <button class="btn revise" data-ob="tour">Just the tour</button><button class="btn approve" data-ob="next">Start</button></div>`;
  } else if(step==="usage"){
    const sub={personal:"Your own projects, inbox, calendar, errands.",work:"Your team's repos, tickets, Slack and on-call.",both:"One Otto for both."};
    html=`<p class="obq">What will you use Otto for?</p><p class="obsub">This picks which integrations are suggested next. It never changes how a run behaves.</p>
      <div class="obopts">${m.usages.map(u=>opt("radio","ob-usage",u,u[0].toUpperCase()+u.slice(1),sub[u],a.usage===u)).join("")}</div>`;
  } else if(step==="profile"){
    html=`<p class="obq">How should Otto trade cost against quality?</p><p class="obsub">Each profile sets the retry, second-opinion and effort knobs in Admin → Runtime settings. You can fine-tune them there later.</p>
      <div class="obopts">${m.profiles.map(p=>opt("radio","ob-profile",p.key,p.label,p.blurb,a.profile===p.key)).join("")}</div>`;
  } else if(step==="integrations"){
    if(!a.integrations) a.integrations=m.integrations.filter(i=>i.default_for.includes(a.usage)).map(i=>i.key);
    html=`<p class="obq">What should Otto be able to reach?</p><p class="obsub">Next you'll see which of these are already connected and where to connect the rest.</p>
      <div class="obopts">${m.integrations.map(i=>opt("checkbox","ob-int",i.key,i.label,"",a.integrations.includes(i.key))).join("")}</div>`;
  } else {
    html=`<p class="obq">Review</p><p class="obsub" id="ob-loading">Checking this install…</p><div id="ob-review"></div>`;
  }
  if(step!=="welcome"){
    const canNext=step==="usage"?!!a.usage:true;
    html+=`<div class="oberr" id="ob-err"></div><div class="obnav"><button class="btn decline obleft" data-ob="back">Back</button>`+
      (step==="review" ? `<button class="btn revise" data-ob="apply" disabled>Apply</button><button class="btn approve" data-ob="apply-tour" disabled>Apply &amp; take the tour</button>`
                       : `<button class="btn approve" data-ob="next"${canNext?'':' disabled'}>Next</button>`)+`</div>`;
  }
  body.innerHTML=bar+html;
  body.querySelectorAll(".obopt input").forEach(inp=>inp.addEventListener("change",()=>{
    if(inp.name==="ob-usage"){ a.usage=inp.value; a.integrations=null; }
    if(inp.name==="ob-profile") a.profile=inp.value;
    if(inp.name==="ob-int") a.integrations=[...body.querySelectorAll('input[name="ob-int"]:checked')].map(x=>x.value);
    obRender();
  }));
  body.querySelectorAll("[data-ob]").forEach(b=>b.addEventListener("click",()=>obAction(b.dataset.ob)));
  if(step==="review") obReview();
}

async function obAction(act){
  if(act==="next"){ OB.step++; obRender(); return; }
  if(act==="back"){ OB.step--; obRender(); return; }
  if(act==="tour"){ closeFormModal(); startTour(); return; }
  if(act==="skip"){ closeFormModal(); return; }   // the close hook records the skip
  if(act==="apply"||act==="apply-tour"){
    const err=document.getElementById("ob-err");
    document.querySelectorAll('[data-ob^="apply"]').forEach(b=>b.disabled=true);
    let out;
    try { out=await postJSON("/api/onboarding/apply",{answers:OB.answers, expect:OB.fingerprint}); }
    catch(e){ err.textContent=e.message; obReview(); return; }   // 409: re-preview what changed
    OB.done=true;
    const n=(out.applied||[]).length;
    toast(n?`Onboarding applied ${n} setting change${n>1?"s":""}`:"Onboarding saved — settings already matched","ok");
    closeFormModal();
    if(typeof adminLoaded!=="undefined" && adminLoaded) loadAdmin();   // the settings it shows just moved
    if(act==="apply-tour") startTour();
  }
}

function obFmt(v){ return v===true?"on":v===false?"off":v===0?"0 (off)":String(v); }

async function obReview(){
  const box=document.getElementById("ob-review"), err=document.getElementById("ob-err"), seq=++OB.seq;
  let p;
  try { p=await postJSON("/api/onboarding/preview",{answers:OB.answers}); }
  catch(e){ if(err && err.isConnected) err.textContent=e.message; return; }
  // Superseded (Back → changed an answer → Next) or closed while in flight: a detached node is
  // still truthy, so test the sequence and isConnected, never the reference.
  if(seq!==OB.seq || !box.isConnected) return;
  OB.fingerprint=p.fingerprint;
  document.getElementById("ob-loading").textContent="Nothing is written until you apply. Applying the same answers again changes nothing.";
  const label={set:'<span class="obset">will change</span>',keep:'<span class="obkeep">already set</span>',
               pinned:'<span class="obpin">pinned by env</span>'};
  const rows=p.settings.map(s=>`<tr><td><code>${esc(s.name)}</code></td><td>${esc(obFmt(s.before))}</td><td>${esc(obFmt(s.after))}</td><td>${label[s.action]}${s.action==="pinned"?` <code>${esc(s.env)}</code>`:""}</td></tr>`).join("");
  const ints=p.integrations.map(i=>`<div class="obint"><span class="obmark ${i.ready?'ok':'no'}">${i.ready?"&#10003;":i.ready===null?"?":"!"}</span>
      <div class="obtext"><b>${esc(i.label)}</b> — ${esc(i.detail)}${i.hint?`<br>${esc(i.hint)}`:""}</div>
      <button class="btn sm revise" data-goto="${esc(i.where.tab)}" data-sect="${esc(i.where.sect)}">Open</button></div>`).join("");
  box.innerHTML=`<table class="obtable"><tr><th>Setting</th><th>Now</th><th>${esc(p.answers.profile)}</th><th></th></tr>${rows}</table>`+
    (ints?`<p class="obq">Integrations</p>${ints}`:`<p class="obsub">No integrations picked — Otto still works from chat with its built-in capabilities.</p>`)+
    p.notes.map(n=>`<div class="obnote">${esc(n)}</div>`).join("");
  box.querySelectorAll("[data-goto]").forEach(b=>b.addEventListener("click",()=>obGoto(b.dataset.goto,b.dataset.sect)));
  document.querySelectorAll('[data-ob^="apply"]').forEach(b=>b.disabled=false);
}

/* Deep link into the section that sets an integration. Admin/Events render asynchronously after
   activateTab, so wait for the section to exist; then open it (remembered, like a click) and show it. */
function obGoto(tab, sect){
  closeFormModal(); endTour();
  activateTab(tab);
  const t0=Date.now();
  const find=()=>{
    const sec=[...document.querySelectorAll(".asection[data-sect]")].find(s=>s.dataset.sect===sect && s.offsetParent);
    if(!sec){ if(Date.now()-t0<5000) setTimeout(find,150); return; }
    sec.classList.remove("collapsed");
    // Events and Admin remember sections in opposite stores (closed-set vs open-set).
    if(tab==="events") evSetCollapsed(sect,false);
    else { const st=sectsRead(); st[sect]=1; sectsWrite(st); }
    sec.scrollIntoView({behavior:"smooth", block:"start"});
    sec.classList.add("obflash"); setTimeout(()=>sec.classList.remove("obflash"),2400);
  };
  find();
}

/* ---- the tour: a spotlight over each thing worth knowing, in reading order ---- */
const TOUR=[
  {sel:'.tab[data-view="chat"]', t:"Chat", b:"Ask for anything here. Otto picks the right capability, asks if something is unclear, and shows a plan for your approval before it changes anything."},
  {sel:"#optpanel", t:"Run options", b:"Pin a repo, a model or a mode for the next message. Repo changes always run in an isolated clone and come back as a draft PR."},
  {sel:'.tab[data-view="schedules"]', t:"Jobs", b:"Saved requests and runbooks — run them on demand or on a schedule."},
  {sel:'.tab[data-view="events"]', t:"Events", b:"Where Otto listens: Slack, the GitHub board, PR review requests and webhooks. Each switches on from its own card."},
  {sel:'.tab[data-view="board"]', t:"Board", b:"Every run as a card. Approvals waiting on you and runs that got stuck show up here — check it when the badge lights up."},
  {sel:'.tab[data-view="memory"]', t:"Memory", b:"What Otto learned: facts, approaches that worked and your standing rules. Forget anything that's wrong."},
  {sel:'.tab[data-view="knowledge"]', t:"Knowledge", b:"Paste docs Otto should consult when answering."},
  {sel:'.tab[data-view="audit"]', t:"Audit", b:"The permanent record of every run: what ran, who approved it, and what it cost."},
  {sel:'.tab[data-view="admin"]', t:"Admin", b:"Capabilities, MCP servers, which model runs each phase, runtime settings and your project repos. A badge here means something needs attention."},
  {sel:"#estop", t:"Pause", b:"Stops every new run from starting, from any source. Work already running is never killed."},
  {sel:"#mascot", t:"Otto", b:"Shows what Otto is doing right now. Drag him anywhere."},
  {sel:"#ob-start", t:"Setup", b:"Run this onboarding again whenever how you use Otto changes."},
];
let TOUR_I=-1;

function startTour(){
  activateTab("chat");          // the run options live in the chat view
  TOUR_I=-1; tourStep(1);
  window.addEventListener("resize", tourPlace);
  document.addEventListener("keydown", tourKey);
}
function endTour(){
  TOUR_I=-1;
  document.querySelectorAll(".obspot,.obtip").forEach(e=>e.remove());
  window.removeEventListener("resize", tourPlace);
  document.removeEventListener("keydown", tourKey);
}
function tourKey(e){
  if(e.key==="Escape") endTour();
  else if(e.key==="ArrowRight") tourStep(1);
  else if(e.key==="ArrowLeft") tourStep(-1);
}
// Skip a target that isn't on screen (a hidden mascot, a collapsed header at phone width).
function tourVisible(s){ const el=document.querySelector(s.sel); return el && el.getClientRects().length && !el.closest("[hidden]") ? el : null; }
function tourStep(dir){
  let i=TOUR_I+dir;
  while(i>=0 && i<TOUR.length && !tourVisible(TOUR[i])) i+=dir;
  if(i<0) return;
  if(i>=TOUR.length){ endTour(); return; }
  TOUR_I=i; tourPlace();
}
function tourPlace(){
  if(TOUR_I<0) return;
  const s=TOUR[TOUR_I], el=tourVisible(s);
  if(!el){ tourStep(1); return; }
  let spot=document.querySelector(".obspot"), tip=document.querySelector(".obtip");
  if(!spot){ spot=document.createElement("div"); spot.className="obspot"; document.body.appendChild(spot); }
  if(!tip){ tip=document.createElement("div"); tip.className="obtip"; tip.setAttribute("role","dialog"); document.body.appendChild(tip); }
  const r=el.getBoundingClientRect(), pad=6;
  Object.assign(spot.style,{left:(r.left-pad)+"px", top:(r.top-pad)+"px", width:(r.width+2*pad)+"px", height:(r.height+2*pad)+"px"});
  const last=!TOUR.slice(TOUR_I+1).some(tourVisible);
  tip.innerHTML=`<h4>${esc(s.t)}</h4>${esc(s.b)}<div class="obnav"><span class="obcount">${TOUR_I+1} / ${TOUR.length}</span>
    <button class="btn sm decline" data-t="end">${last?"Close":"Skip tour"}</button>${TOUR_I>0?'<button class="btn sm revise" data-t="back">Back</button>':""}
    ${last?"":'<button class="btn sm approve" data-t="next">Next</button>'}</div>`;
  tip.querySelectorAll("[data-t]").forEach(b=>b.addEventListener("click",()=>{
    const t=b.dataset.t; if(t==="end") endTour(); else tourStep(t==="next"?1:-1);
  }));
  // Below the target when it sits in the top half, above it otherwise; clamped to the viewport.
  const tw=tip.offsetWidth, th=tip.offsetHeight, vw=window.innerWidth, vh=window.innerHeight;
  const top = r.bottom+pad+12+th < vh ? r.bottom+pad+12 : Math.max(12, r.top-pad-12-th);
  tip.style.top=top+"px";
  tip.style.left=Math.min(Math.max(12, r.left), vw-tw-12)+"px";
  const nx=tip.querySelector('[data-t="next"]')||tip.querySelector('[data-t="end"]'); if(nx) nx.focus();
}
