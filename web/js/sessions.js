"use strict";
/* Admin -> Browser sessions (#217). A browser holds a revocable SESSION, never the API token:
   one row per logged-in browser, the cookie itself never listed. Rendered into renderAdmin's
   template, so these are only called at render time, never at load. */
function sessionsSection(){
  return `    <div class="asection coll collapsed" data-sect="sessions"><h3><span class="secttoggle" title="collapse / expand">
        <span class="gcaret">&#9662;</span>Browser sessions<span class="sectcount" id="sess-count"></span></span>
        <button class="addbtn" id="logout-btn" title="End this browser's session">Log out</button></h3>
      <div class="asection-body">
      <p class="sub" style="margin:0 0 8px">Every browser logged in to this Otto. Revoke one you no longer use; deleting <code>data/.api/token</code> ends them all.</p>
      <table class="ctable sesstable">
        <colgroup><col><col class="c-when"><col class="c-act"></colgroup>
        <thead><tr><th>Browser</th><th class="c-when">Since</th><th class="c-act"></th></tr></thead>
        <tbody id="sess-rows"></tbody></table>
      </div>
    </div>`;
}

function wireSessions(el){
  el.querySelector("#logout-btn").addEventListener("click", async e=>{
    e.stopPropagation();
    if(await postOr("/api/logout", {}, "logging out")) location.reload();
  });
  loadSessions();
}

async function loadSessions(){
  const body=document.getElementById("sess-rows"); if(!body) return;
  let d; try { d=await getJSON("/api/sessions"); }
  catch(e){ body.innerHTML=`<tr><td colspan="3" class="err">${esc(e.message)}</td></tr>`; return; }
  const rows=d.sessions||[];
  document.getElementById("sess-count").textContent=rows.length;
  body.innerHTML=rows.map(r=>`<tr><td class="wrap" title="${esc(r.label)}">${esc(r.label||"unknown browser")}${r.current?' <span class="sub">(this browser)</span>':""}</td>
    <td class="c-when" title="${esc(new Date(r.created*1000).toISOString())}">${esc(shortWhen(r.created*1000))}</td>
    <td class="c-act">${r.current?"":`<button class="btn decline" data-sess="${esc(r.id)}">Revoke</button>`}</td></tr>`).join("")
    || `<tr><td colspan="3" class="sub">No browser sessions.</td></tr>`;
  body.querySelectorAll("[data-sess]").forEach(b=>b.addEventListener("click", async ()=>{
    if(await postOr("/api/sessions/revoke", {id:b.dataset.sess}, "revoking that session")) loadSessions();
  }));
}

