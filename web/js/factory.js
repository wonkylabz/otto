"use strict";
/* Factory mode (factory.py) — Admin → Mode, plus the header chip.

   Two modes. MANUAL is everything Otto has always done. FACTORY points ONE registered repo and the
   GitHub board at a self-driving loop: a PM sprint readies tickets, the worker builds them, review
   and QA iterate, and a PR that passes both merges itself. Switching it on is the approval for all
   of that, so the header says so from every tab (chat.js's applyFactory, riding /api/health). */
let FACTORY={state:{}, repos:[], board:"", temporal:false, mcps:[]};

async function loadFactory(){
  const host=document.getElementById("factory-section");
  if(!host) return;
  let d;
  try { d=await getJSON("/api/factory"); }
  catch(e){ host.innerHTML=`<p class="err">Couldn't load factory mode (${esc(e.message)}).</p>`; return; }
  FACTORY={state:d.state||{}, repos:d.repos||[], board:d.board||"", temporal:!!d.temporal, mcps:d.mcps||[]};
  const s=FACTORY.state, on=!!s.enabled;
  applyFactory(s);
  const spent=(s.spent_usd===null||s.spent_usd===undefined)?"?":`$${(+s.spent_usd).toFixed(2)}`;
  const status=on
    ? `<b>${esc(s.slug||s.repo)}</b> · sprint ${esc(""+(s.sprint_n||0))} · merged ${esc(""+(s.merged||0))}`
      +` · spent ${esc(spent)} of $${esc(""+s.budget_usd)} · WIP ${esc(""+s.wip)}`
      +` · idle ${esc(""+(s.idle_sprints||0))}/${esc(""+s.max_idle_sprints)}`
      +(s.held?` · <b>on hold</b> — press Start to run`:"")
      +(s.paused?` · <b>paused</b> (${esc(s.paused.reason||"")}) — resumes by itself`:"")
    : (s.status && s.status!=="manual" ? `last factory: <b>${esc(s.status)}</b>` : `every write waits for a human, as configured per capability`);
  const hist=(s.history||[]).length;
  const tip=FACTORY.temporal?'Self-driving build loop on one repo: tickets run and PRs merge with no approval':'needs Temporal (run via ./run.sh)';
  const btns=on
    ? (s.held
        ? `<button class="addbtn" id="mode-start" title="Start running sprints and tickets now">Start</button>`
        : `<button class="addbtn" id="mode-hold" title="Stop starting new sprints and tickets. Runs already going finish; settings, budget and counters are kept.">Pause</button>`)
      +`<button class="addbtn" id="mode-log" title="what the factory did, newest first">Log</button>
       <button class="addbtn" id="mode-edit" title="change the budget, WIP and cadence">Edit</button>
       <button class="addbtn addnew" id="mode-manual" title="Stops the sprint schedule and restores the board settings Factory changed. Runs already going finish.">Back to Manual</button>`
    : (hist?`<button class="addbtn" id="mode-log" title="what the last factory did, newest first">Log</button>`:'')
      +`<button class="addbtn addnew" id="mode-factory" ${FACTORY.temporal?'':'disabled'} title="${tip}">Start factory</button>`;
  host.innerHTML=`<div class="asection coll collapsed" data-sect="mode"><h3><span class="secttoggle" title="collapse / expand">
      <span class="gcaret">&#9662;</span>Mode<span class="sectcount">${on?'factory':'manual'}</span></span>
    <span class="h3btns">${btns}</span></h3>
    <div class="asection-body"><p class="sub" style="margin:0">${status}</p></div></div>`;
  wireSections(host);
  const bind=(id,fn)=>{ const b=document.getElementById(id); if(b) b.addEventListener("click",fn); };
  bind("mode-factory",showFactoryForm);
  bind("mode-edit",showFactoryForm);
  bind("mode-log",showFactoryLog);
  bind("mode-start",async()=>{
    let r;
    try { r=await postJSON("/api/factory/start",{}); }
    catch(e){ toast("starting the factory failed: "+e.message); return; }
    if(r&&r.state&&r.state.warning) toast(r.state.warning);
    loadFactory();
  });
  bind("mode-hold",async()=>{
    if(await postOr("/api/factory/hold",{},"pausing the factory")) loadFactory();
  });
  bind("mode-manual",async()=>{
    if(await postOr("/api/factory/disable",{},"switching to Manual")) loadFactory();
  });
}

function showFactoryLog(){
  const f=openFormModal("<b>Factory log</b>");
  const rows=(FACTORY.state.history||[]).map(h=>
    `<tr><td><b>${esc(h.event)}</b><br><span class="sub" title="${esc(h.at)}">${esc(shortWhen(h.at))}</span></td>`
    +`<td>${esc(h.detail||"")}</td></tr>`).join("");
  f.innerHTML=`<table class="ctable"><thead><tr><th>Event</th><th>Detail</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function showFactoryForm(){
  const s=FACTORY.state, on=!!s.enabled;
  const f=openFormModal("<b>Factory mode</b><br>a self-driving build loop on one repo — no approvals, PRs merge themselves");
  const opts=FACTORY.repos.map(r=>`<option value="${esc(r)}">${esc(r)}</option>`).join("");
  f.innerHTML=`<div class="aform">
    <p class="sub">Unsafe by design: every ticket on this repo runs <b>without approval</b>, and a PR that passes review <b>and</b> QA is
      <b>merged automatically</b>. Saving leaves it <b>on hold</b> &mdash; press Start when the rest is wired up. The board${FACTORY.board?` (<a href="${esc(FACTORY.board)}" target="_blank" rel="noopener">open</a>)`:''} must already be configured. The global Pause still stops it.</p>
    <label>Repo &mdash; a registered repo the factory owns: empty to start a project, or with code to finish one</label>
    <select id="ff-repo" ${on?'disabled':''}>${opts||'<option value="">(register a repo in Project repos first)</option>'}</select>
    <label>Seed &mdash; the idea, or for a repo with code what to finish or add; written to <code>GOALS.md</code> with a Definition of Done the PM refines</label>
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
    <label>MCP servers the factory may use</label>
    <span class="sk-help">Its runs are unattended and pre-approved, so every server is OFF unless ticked &mdash; keep mail, chat and home servers out. Manual runs are unaffected.</span>
    <div style="display:flex;flex-wrap:wrap;gap:6px 16px">${FACTORY.mcps.map(m=>`<label class="mtoggle"><input type="checkbox" class="ff-mcp" value="${esc(m.name)}"${(s.mcp_allow||[]).includes(m.name)?' checked':''}> ${esc(m.display)} <span class="sub">${esc(m.source)}</span></label>`).join("")||'<span class="sub">no MCP servers enabled</span>'}</div>
    <div class="ferr" id="ff-err"></div>
    <div class="factions"><button class="btn approve" id="ff-save">${on?'Save':'Save (on hold)'}</button><button class="btn decline" id="ff-cancel">Cancel</button></div>
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
      max_idle_sprints:+val("ff-idle")||3, max_tickets:+val("ff-tickets")||5,
      mcp_allow:[...f.querySelectorAll(".ff-mcp:checked")].map(c=>c.value)};
    const ok=await postForm("/api/factory/enable", body, document.getElementById("ff-err"));
    btn.disabled=false;
    if(ok){ closeFormModal(); loadFactory(); }
  };
}
