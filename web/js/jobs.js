"use strict";
/* ---- jobs tab (runbooks) ----
   ONE list for both triggers, because a runbook and a schedule are the same object: a saved,
   parameterized unit of work, where a cron is just one more way to start it. Splitting them into
   two tabs would mean adding a cron MOVES a job between tabs instead of editing a field. */
let _jobs=[], JOB_CAPS=[];
let _triggering={};   // job id -> true while a manual run is in flight (for the spinner)

/* Both groups default OPEN — the key records what the user explicitly CLOSED, same contract as
   the Events tab's integration sections. */
const JOBSECTS="otto.jobs.sections";
function jobCollapsed(){ try{ return JSON.parse(localStorage.getItem(JOBSECTS))||{}; } catch(e){ return {}; } }
function jobSetCollapsed(id,closed){
  const st=jobCollapsed(); if(closed) st[id]=1; else delete st[id];
  try{ localStorage.setItem(JOBSECTS,JSON.stringify(st)); }catch(e){}
}

function jobSection(id, title, hint, rows, empty){
  const closed=jobCollapsed()[id];
  return `<div class="asection coll${closed?' collapsed':''}" data-sect="${id}">
    <h3><span class="secttoggle" title="collapse / expand"><span class="gcaret">&#9662;</span>${title}<span class="sectcount">${rows.length}</span></span></h3>
    <div class="asection-body">
      <p class="sub" style="margin:10px 0 12px">${hint}</p>
      ${rows.map(jobRow).join("") || `<p class="memempty">${empty}</p>`}
    </div>
  </div>`;
}

async function loadJobs(silent){
  const el=document.getElementById("schedulesview");
  if(!silent) el.innerHTML=`<p class="sub">loading…</p>`;
  let data;
  try { data=await (await fetch("/api/runbooks")).json(); }
  catch(e){ if(!silent) el.innerHTML=`<p class="err">Couldn't load jobs (${e.message}).</p>`; return; }
  _jobs=data.jobs||[]; JOB_CAPS=data.caps||[];
  const onDemand=_jobs.filter(j=>j.on_demand).sort((a,b)=>(a.name||'').localeCompare(b.name||''));
  // soonest first — the API returns store order, so the next thing to fire could be anywhere in
  // the list. A disabled job has no next_run and sorts to the end rather than to the top.
  const scheduled=_jobs.filter(j=>!j.on_demand).sort((a,b)=>
    (a.next_run?Date.parse(a.next_run):Infinity)-(b.next_run?Date.parse(b.next_run):Infinity));
  // Temporal down is DEGRADED, not fatal: on-demand runbooks are a plain store, so the list still
  // renders and stays editable — only cron firing and "run now" are unavailable.
  const warn = data.temporal===false
    ? `<p class="modalHint">Temporal isn't running — you can still write runbooks, but nothing can
       fire or be run until you start Otto with <code>./run.sh</code>.</p>` : "";
  el.innerHTML=`
    <div class="phead"><h1>Jobs</h1>
      <p class="sub">A runbook is a saved unit of work: a request, optional parameters, an optional
        ordered graph of steps, and the prose that explains it. Run it on demand, or give it a cron.
        Cron runs with nobody present, so writes need the job's own auto-approve. Times in
        <b>${esc(data.tz||'UTC')}</b>.</p></div>
    ${warn}
    <div class="memhead"><span class="memstat"><b>${_jobs.length}</b> runbook${_jobs.length===1?'':'s'}</span>
      <button class="clearbtn" id="add-job" style="margin-left:auto;color:var(--accent)">+ runbook</button></div>
    ${_jobs.length ? `
      ${jobSection("rb-ondemand", "On demand",
        "Run these yourself. A runbook with parameters asks for them at the moment you click Run.",
        onDemand, "Nothing on demand yet.")}
      ${jobSection("rb-scheduled", "Scheduled",
        "Fired by Temporal on a cron, with nobody present — so a write only runs if the job auto-approves it, and every required parameter must have a default.",
        scheduled, "Nothing scheduled yet.")}`
      : '<p class="memempty">No runbooks yet.</p>'}`;
  wireJobs(el);
}

function jobRow(j){
  const busy = j.running || (j.id in _triggering);
  // A migrated schedule has no name of its own — it got request[:80], so the row would otherwise
  // print a truncated title above the full text it was truncated from. Where the name is just a
  // prefix of the request, the request IS the title and the name line is dropped.
  const derived = j.request && j.name && j.request.startsWith(j.name);
  const title = derived ? j.request : (j.name || j.request);
  const sub = derived ? "" : (j.request && j.request !== j.name ? j.request : "");
  const bits=[
    j.cap?`&rarr; <b>${esc(j.cap)}</b> (pinned)`:'auto-route',
    j.steps.length?`<b>${j.steps.length}</b> steps`:null,
    j.params.length?`<b>${j.params.length}</b> param${j.params.length===1?'':'s'}`:null,
    j.has_doc?'has notes':null,
  ].filter(Boolean);
  const approve = j.on_demand ? ''
    : (j.auto_approve ? '<span class="jauto" title="a cron fire runs writes with nobody present">auto-approves writes</span>'
                      : '<span class="jreads" title="an unattended write is skipped, so this only ever reads">reads only</span>');
  return `<div class="job ${j.enabled?'':'off'}">
    <div class="jtop">
      ${j.on_demand?'<span></span>':`<span class="switch ${j.enabled?'on':''}" data-togglejob="${j.id}" title="enable / disable"></span>`}
      <span class="jname" title="${esc(title)}">${esc(title)}</span>
      <span class="jslot jcronslot">${j.on_demand?'<span class="jtag">on demand</span>':`<span class="jcron">${esc(j.cron)}</span>`}</span>
      <span class="jslot jnextslot">${j.on_demand?'':`<span class="jnext" title="${esc(j.next_run||'')}">next ${esc(j.next_run?shortWhen(j.next_run):'—')}</span>`}</span>
      <div class="jactions">
        <button class="addbtn" data-editjob="${j.id}">edit</button>
        ${busy ? '<span class="runspin"><span class="spin"></span>running…</span>'
               : `<button class="addbtn" data-runjob="${j.id}">run now</button>`}
        <button class="remove" data-deljob="${j.id}" title="remove">&times;</button>
      </div>
    </div>
    ${sub?`<div class="jreq">${esc(sub)}</div>`:''}
    <div class="jstatus">${approve}${approve?' ':''}${bits.join(" · ")}
      ${j.on_demand?'':` · last <span title="${esc(j.last_run||'')}">${j.last_run?esc(shortWhen(j.last_run)):'never'}</span>`}</div>
  </div>`;
}

function wireJobs(el){
  // Delegated + assigned as a PROPERTY (not addEventListener): loadJobs() replaces this view's
  // innerHTML on every refresh but #schedulesview itself persists, so a stacking listener would
  // toggle the section twice per click after the first poll.
  el.onclick=e=>{
    const t=e.target.closest(".secttoggle");
    if(!t) return;
    const sec=t.closest(".asection");
    jobSetCollapsed(sec.dataset.sect, sec.classList.toggle("collapsed"));
  };
  el.querySelectorAll("[data-togglejob]").forEach(s=>s.addEventListener("click",async()=>{
    await fetch("/api/runbooks/toggle",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id:s.dataset.togglejob,enabled:!s.classList.contains("on")})});
    loadJobs();
  }));
  el.querySelectorAll("[data-deljob]").forEach(b=>b.addEventListener("click",async()=>{
    if(!confirm("Remove this runbook?")) return;
    await fetch("/api/runbooks/remove",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:b.dataset.deljob})});
    loadJobs();
  }));
  el.querySelectorAll("[data-runjob]").forEach(b=>b.addEventListener("click",()=>{
    const job=_jobs.find(j=>j.id===b.dataset.runjob);
    if(!job) return;
    // A parameterized runbook asks BEFORE starting — the whole point of parameters is that the
    // value is chosen per run, so firing with the defaults on a click would be the wrong answer
    // exactly when it matters ("which env?").
    if(job.params.length) showParamForm(job);
    else startJob(job.id, null, b);
  }));
  el.querySelectorAll("[data-editjob]").forEach(b=>b.addEventListener("click",()=>openJobForm(b.dataset.editjob)));
  const add=document.getElementById("add-job");
  if(add) add.addEventListener("click",()=>showJobForm());
}

async function startJob(id, values, btn, errEl){
  _triggering[id]=true;
  if(btn) btn.outerHTML=`<span class="runspin"><span class="spin"></span>running…</span>`;  // instant feedback
  let d={};
  try {
    const r=await fetch("/api/runbooks/run",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({id, values})});
    d=await r.json();
    if(!r.ok||d.error){
      delete _triggering[id];
      if(errEl){ errEl.textContent=d.error||"failed"; return false; }
      alert(d.error||"failed to start"); loadJobs(true); return false;
    }
  } catch(e){ delete _triggering[id]; loadJobs(true); return false; }
  pollTrigger(id, 0);
  return true;
}

// Poll after a manual run: keep the spinner until the run actually FINISHES (Temporal's
// running_actions clears), not just until it started. The optimistic `_triggering` flag covers
// the gap before the backend reports it running. An on-demand runbook has no Schedule object to
// report `running`, so its spinner rides the optimism window alone and then clears.
function pollTrigger(id, n){
  setTimeout(async ()=>{
    if(document.getElementById("schedulesview").hidden){ delete _triggering[id]; return; }
    await loadJobs(true);                       // re-fetch + re-render (spinner = running || _triggering)
    const job=_jobs.find(j=>j.id===id);
    if(job && job.running) delete _triggering[id];   // backend confirms it's running — hand off to job.running
    else if(n>=5) delete _triggering[id];            // optimism window (~15s) over; trust job.running
    const busy=(job && job.running) || (id in _triggering);
    if(busy && n<120) pollTrigger(id, n+1);          // keep spinning until it finishes (cap ~6min)
    else delete _triggering[id];
  }, 3000);
}

/* ---- run a parameterized runbook ---- */
function showParamForm(job){
  const c=openFormModal(`<b>Run ${esc(job.name||job.id)}</b><br>fill in this run's parameters`);
  c.innerHTML=`<div class="aform">
    ${job.params.map((p,i)=>`
      <label>${esc(p.label||p.name)}${p.required?'':' <span class="sub">(optional)</span>'}</label>
      ${p.choices && p.choices.length
        ? `<select id="pv-${i}">${p.choices.map(o=>`<option value="${esc(o)}"${o===p.default?' selected':''}>${esc(o)}</option>`).join("")}</select>`
        : `<input id="pv-${i}" value="${esc(p.default||'')}" placeholder="${esc(p.name)}">`}`).join("")}
    <div class="ferr" id="pv-err"></div>
    <div class="factions"><button class="btn approve" id="pv-run">Run</button><button class="btn decline" id="pv-cancel">Cancel</button></div>
  </div>`;
  document.getElementById("pv-cancel").onclick=closeFormModal;
  document.getElementById("pv-run").onclick=async()=>{
    const values={};
    job.params.forEach((p,i)=>{ values[p.name]=document.getElementById("pv-"+i).value; });
    const err=document.getElementById("pv-err");
    err.textContent="";
    if(await startJob(job.id, values, null, err)){ closeFormModal(); loadJobs(true); }
  };
}

/* ---- the runbook editor ----
   Uses the shared config-form modal like every other config form in the app (see the UI
   conventions in CLAUDE.md) — widened, because steps and notes need the room. */
async function openJobForm(id){
  let full=null;
  try { full=(await (await fetch("/api/runbook/"+encodeURIComponent(id))).json()).runbook; }
  catch(e){ alert("Couldn't load that runbook."); return; }
  showJobForm(Object.assign({id}, full));
}

function paramRow(p){
  p=p||{};
  return `<div class="erow" data-prow>
    <input class="ep-name" placeholder="name" value="${esc(p.name||'')}" title="used as {{name}}">
    <input class="ep-label" placeholder="label" value="${esc(p.label||'')}">
    <input class="ep-default" placeholder="default" value="${esc(p.default||'')}">
    <input class="ep-choices" placeholder="choices, comma separated" value="${esc((p.choices||[]).join(', '))}">
    <label class="echk" title="required"><input type="checkbox" class="ep-req" ${p.required===false?'':'checked'}> req</label>
    <button class="remove" data-delrow title="remove">&times;</button>
  </div>`;
}

function stepRow(s, caps){
  s=s||{};
  const opts=['<option value="">(runbook\'s capability)</option>'].concat(
    caps.map(n=>`<option value="${esc(n)}"${n===s.cap?' selected':''}>${esc(n)}</option>`)).join("");
  return `<div class="erow erow-step" data-srow>
    <input class="es-id" placeholder="id" value="${esc(s.id||'')}" title="referenced by other steps' 'needs'">
    <input class="es-goal" placeholder="what this step does" value="${esc(s.goal||'')}">
    <select class="es-cap" title="capability for this step">${opts}</select>
    <input class="es-needs" placeholder="needs (ids)" value="${esc((s.needs||[]).join(', '))}" title="step ids that must finish first">
    <button class="remove" data-delrow title="remove">&times;</button>
  </div>`;
}

function showJobForm(job){
  const editing=!!(job && job.id);
  const c=openFormModal(editing?"<b>Edit runbook</b>":"<b>New runbook</b><br>a saved unit of work you can run on demand or on a cron");
  document.getElementById("formModal").querySelector(".modalBox").classList.add("wideModalBox");
  const caps=JOB_CAPS.slice();
  if(job && job.cap && !caps.includes(job.cap)) caps.push(job.cap);
  const capOpts=['<option value="">(auto-route — let Router #1 pick)</option>'].concat(
    caps.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`)).join("");
  c.innerHTML=`<div class="aform">
    <label>Name</label><input id="jf-name" placeholder="Rotate VPN certs">
    <label>Request</label>
    <span class="sub" style="margin:-2px 0 4px;font-size:12px">The overall task. With steps below, this is the orientation they all share.</span>
    <input id="jf-req" placeholder="Rotate the client VPN certs in {{env}}">
    <label>Capability</label>
    <span class="sub" style="margin:-2px 0 4px;font-size:12px">Pin one to skip routing — required for a <b>project</b> skill. Steps can override it individually.</span>
    <select id="jf-cap">${capOpts}</select>

    <label>Parameters</label>
    <span class="sub" style="margin:-2px 0 4px;font-size:12px">Use as <code>{{name}}</code> in the request, steps and notes. A <b>scheduled</b> runbook needs a default for every required parameter — nobody is there to be asked.</span>
    <div id="jf-params"></div>
    <button class="addbtn" id="jf-addparam" type="button">+ parameter</button>

    <label style="margin-top:12px">Steps</label>
    <span class="sub" style="margin:-2px 0 4px;font-size:12px">Leave empty for a single-turn run. With steps, each runs in dependency order (independent ones run together) and its output is passed to whatever <code>needs</code> it. Otto will <b>not</b> rewrite your steps if one fails — it stops for you.</span>
    <div id="jf-steps"></div>
    <button class="addbtn" id="jf-addstep" type="button">+ step</button>

    <label style="margin-top:12px">Notes / the runbook itself</label>
    <span class="sub" style="margin:-2px 0 4px;font-size:12px">Preconditions, rollback, who to escalate to. Treated as the <b>approved plan</b>: it goes to the executor and to the verifier, and it replaces the plan preview at the approval gate.</span>
    <textarea id="jf-doc" rows="6" placeholder="## Preconditions&#10;- VPN connected&#10;&#10;## Rollback&#10;- Re-import the previous cert"></textarea>

    <label style="margin-top:12px">Cron — minute hour day-of-month month day-of-week</label>
    <span class="sub" style="margin:-2px 0 4px;font-size:12px">Leave empty for an on-demand-only runbook.</span>
    <input id="jf-cron" placeholder="0 9 * * 1-5">
    <div class="toolchips">
      <span class="cronpreset" data-cron="">on demand only</span>
      <span class="cronpreset" data-cron="0 9 * * 1-5">weekdays 9am</span>
      <span class="cronpreset" data-cron="0 * * * *">hourly</span>
      <span class="cronpreset" data-cron="*/15 * * * *">every 15 min</span>
      <span class="cronpreset" data-cron="0 18 * * *">daily 6pm</span>
    </div>
    <label style="flex-direction:row;align-items:center;gap:9px;margin-top:4px">
      <input type="checkbox" id="jf-auto"> <span class="warn">auto-approve writes (run mutating capabilities unattended)</span></label>
    <div class="ferr" id="jf-err"></div>
    <div class="factions"><button class="btn approve" id="jf-save">${editing?'Save changes':'Add runbook'}</button><button class="btn decline" id="jf-cancel">Cancel</button></div>
  </div>`;

  const pbox=document.getElementById("jf-params"), sbox=document.getElementById("jf-steps");
  if(editing){
    document.getElementById("jf-name").value=job.name||"";
    document.getElementById("jf-req").value=job.request||"";
    document.getElementById("jf-cron").value=job.cron||"";
    document.getElementById("jf-doc").value=job.doc||"";
    document.getElementById("jf-auto").checked=!!job.auto_approve;
    if(job.cap) document.getElementById("jf-cap").value=job.cap;
    (job.params||[]).forEach(p=>pbox.insertAdjacentHTML("beforeend", paramRow(p)));
    (job.steps||[]).forEach(s=>sbox.insertAdjacentHTML("beforeend", stepRow(s, caps)));
  }
  document.getElementById("jf-addparam").onclick=()=>pbox.insertAdjacentHTML("beforeend", paramRow());
  document.getElementById("jf-addstep").onclick=()=>{
    // Default the id to s1, s2, … so `needs` has something stable to point at without the author
    // having to invent ids.
    sbox.insertAdjacentHTML("beforeend", stepRow({id:"s"+(sbox.querySelectorAll("[data-srow]").length+1)}, caps));
  };
  c.addEventListener("click",e=>{
    const b=e.target.closest("[data-delrow]");
    if(b) b.closest(".erow").remove();
  });
  c.querySelectorAll(".cronpreset").forEach(p=>p.addEventListener("click",()=>{ document.getElementById("jf-cron").value=p.dataset.cron; }));
  document.getElementById("jf-cancel").onclick=closeFormModal;
  document.getElementById("jf-save").onclick=async()=>{
    const csv=v=>v.split(",").map(x=>x.trim()).filter(Boolean);
    const params=[...pbox.querySelectorAll("[data-prow]")].map(r=>({
      name:r.querySelector(".ep-name").value.trim(),
      label:r.querySelector(".ep-label").value.trim(),
      default:r.querySelector(".ep-default").value.trim(),
      choices:csv(r.querySelector(".ep-choices").value),
      required:r.querySelector(".ep-req").checked,
    })).filter(p=>p.name);
    const steps=[...sbox.querySelectorAll("[data-srow]")].map(r=>({
      id:r.querySelector(".es-id").value.trim(),
      goal:r.querySelector(".es-goal").value.trim(),
      cap:r.querySelector(".es-cap").value,
      needs:csv(r.querySelector(".es-needs").value),
    })).filter(s=>s.goal);
    const payload={
      name:val("jf-name"), request:val("jf-req"), cap:document.getElementById("jf-cap").value,
      cron:val("jf-cron"), auto_approve:document.getElementById("jf-auto").checked,
      doc:document.getElementById("jf-doc").value, params, steps,
    };
    if(editing) payload.id=job.id;
    const err=document.getElementById("jf-err");
    if(!payload.name){ err.textContent="name is required"; return; }
    const r=await fetch(editing?"/api/runbooks/edit":"/api/runbooks/add",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify(payload)});
    const d=await r.json(); if(!r.ok||d.error){ err.textContent=d.error||"failed"; return; }
    closeFormModal(); loadJobs();
  };
}
