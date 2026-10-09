"use strict";
/* Factory mode (factory.py) — Admin → Mode, plus the header chip.

   Two modes. MANUAL is everything Otto has always done. FACTORY points ONE registered repo and the
   GitHub board at a self-driving loop: a PM sprint readies tickets, the worker builds them, review
   and QA iterate, and a PR that passes both merges itself. Switching it on is the approval for all
   of that, so the header says so from every tab (chat.js's applyFactory, riding /api/health). */
let FACTORY={state:{}, repos:[], board:"", temporal:false};

async function loadFactory(){
  const host=document.getElementById("factory-section");
  if(!host) return;
  let d;
  try { d=await getJSON("/api/factory"); }
  catch(e){ host.innerHTML=`<p class="err">Couldn't load factory mode (${esc(e.message)}).</p>`; return; }
  FACTORY={state:d.state||{}, repos:d.repos||[], board:d.board||"", temporal:!!d.temporal};
  const s=FACTORY.state, on=!!s.enabled;
  applyFactory(s);
  const spent=(s.spent_usd===null||s.spent_usd===undefined)?"?":`$${(+s.spent_usd).toFixed(2)}`;
  const status=on
    ? `<b>${esc(s.slug||s.repo)}</b> · sprint <b>${esc(""+(s.sprint_n||0))}</b>`
      +` · merged <b>${esc(""+(s.merged||0))}</b> · spent <b>${esc(spent)}</b> of $${esc(""+s.budget_usd)}`
      +` · WIP ${esc(""+s.wip)} · idle sprints ${esc(""+(s.idle_sprints||0))}/${esc(""+s.max_idle_sprints)}`
    : (s.status && s.status!=="manual" ? `last factory: <b>${esc(s.status)}</b>` : `every write waits for a human, as configured per capability`);
  const hist=(s.history||[]).slice(0,6).map(h=>
    `<div class="jstatus"><span title="${esc(h.at)}">${esc(shortWhen(h.at))}</span> · <b>${esc(h.event)}</b> ${esc(h.detail||"")}</div>`).join("");
  host.innerHTML=`<div class="asection" data-sect="mode"><h3><span>Mode<span class="badge ${on?'warn':'off'}">${on?'factory':'manual'}</span></span></h3>
    <div class="asection-body">
      <div class="modepick">
        <button class="btn ${on?'':'approve'}" id="mode-manual" title="Back to Manual: stops the sprint schedule and restores the board settings Factory changed. Runs already going finish.">Manual</button>
        <button class="btn ${on?'approve':''}" id="mode-factory" ${FACTORY.temporal?'':'disabled'} title="${FACTORY.temporal?'Self-driving build loop on one repo: tickets run and PRs merge with no approval':'needs Temporal (run via ./run.sh)'}">Factory${on?' — edit':''}</button>
      </div>
      <div class="jstatus">${status}</div>${hist}
    </div></div>`;
  document.getElementById("mode-factory").addEventListener("click",showFactoryForm);
  document.getElementById("mode-manual").addEventListener("click",async()=>{
    if(!on) return;
    if(await postOr("/api/factory/disable",{},"switching to Manual")) loadFactory();
  });
}

function showFactoryForm(){
  const s=FACTORY.state, on=!!s.enabled;
  const f=openFormModal("<b>Factory mode</b><br>a self-driving build loop on one repo — no approvals, PRs merge themselves");
  const opts=FACTORY.repos.map(r=>`<option value="${esc(r)}">${esc(r)}</option>`).join("");
  f.innerHTML=`<div class="aform">
    <p class="sub">Unsafe by design: every ticket on this repo runs <b>without approval</b>, and a PR that passes review <b>and</b> QA is
      <b>merged automatically</b>. The board${FACTORY.board?` (<a href="${esc(FACTORY.board)}" target="_blank" rel="noopener">open</a>)`:''} must already be configured. The global Pause still stops it.</p>
    <label>Repo &mdash; an EMPTY registered repo the factory owns</label>
    <select id="ff-repo" ${on?'disabled':''}>${opts||'<option value="">(register a repo in Project repos first)</option>'}</select>
    <label>Seed &mdash; the idea; written to <code>GOALS.md</code> with a Definition of Done the PM refines</label>
    <textarea id="ff-seed" class="codearea" placeholder="A browser roguelike where you play a lighthouse keeper…"></textarea>
    <span class="sk-help">Required. An existing <code>GOALS.md</code> is kept as-is — edit that file any time to steer the line.</span>
    <div class="frow">
      <div><label>Budget (USD, whole factory)</label><input id="ff-budget" type="number" min="1" step="1"></div>
      <div><label>Tickets in progress at once</label><input id="ff-wip" type="number" min="1"></div>
    </div>
    <div class="frow">
      <div><label>Sprint check (seconds)</label><input id="ff-every" type="number" min="300"></div>
      <div><label>Stop after N sprints with no merge</label><input id="ff-idle" type="number" min="1"></div>
    </div>
    <label>New tickets per sprint</label><input id="ff-tickets" type="number" min="1">
    <div class="ferr" id="ff-err"></div>
    <div class="factions"><button class="btn approve" id="ff-save">${on?'Save':'Start factory'}</button><button class="btn decline" id="ff-cancel">Cancel</button></div>
  </div>`;
  if(s.repo) document.getElementById("ff-repo").value=s.repo;
  document.getElementById("ff-seed").value=s.seed||"";
  document.getElementById("ff-budget").value=s.budget_usd||"";
  document.getElementById("ff-wip").value=s.wip||1;
  document.getElementById("ff-every").value=s.sprint_every_s||1800;
  document.getElementById("ff-idle").value=s.max_idle_sprints||3;
  document.getElementById("ff-tickets").value=s.max_tickets||5;
  document.getElementById("ff-cancel").onclick=closeFormModal;
  document.getElementById("ff-save").onclick=async()=>{
    const btn=document.getElementById("ff-save");
    if(!document.getElementById("ff-seed").value.trim()){
      document.getElementById("ff-err").textContent="write the seed — the idea the factory builds"; return; }
    btn.disabled=true;
    const body={repo:val("ff-repo")||s.repo, seed:document.getElementById("ff-seed").value,
      budget_usd:+val("ff-budget")||0, wip:+val("ff-wip")||1, sprint_every_s:+val("ff-every")||1800,
      max_idle_sprints:+val("ff-idle")||3, max_tickets:+val("ff-tickets")||5};
    const ok=await postForm("/api/factory/enable", body, document.getElementById("ff-err"));
    btn.disabled=false;
    if(ok){ closeFormModal(); loadFactory(); }
  };
}
