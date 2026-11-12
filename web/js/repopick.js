"use strict";
// Admin -> Project repos -> + Add. Tick repos from the GitHub login's own list, or paste a URL.
// The list shells out to gh (seconds), so it loads inside the open modal, never with the panel.
let REPO_PICK=[];

function showProjectForm(){
  const c=openFormModal("<b>New project repo</b><br>repositories Otto can read, clone and work in");
  c.innerHTML=`<div class="aform">
    <label>From GitHub</label><input id="pf-filter" placeholder="Filter your repos…">
    <div class="disclist" id="pf-list"><span class="sk-help" style="padding:8px 10px">Loading your repos from GitHub…</span></div>
    <label style="margin-top:10px">Or paste a repo URL</label><input id="pf-url" placeholder="https://github.com/owner/repo">
    <p class="sub" style="margin:2px 0 0">A <code>git@…</code> remote works too. Otto clones it under <code>data/repos/</code> using your <code>gh</code> login.</p>
    <label style="margin-top:10px">Local checkout <span class="sub">(optional, for the pasted URL)</span></label><input id="pf-path" placeholder="/home/you/repositories/repo">
    <p class="sub" style="margin:2px 0 0">Already have it on disk? Point at it and Otto clones from there instead — faster, offline, and the checkout gets write-protected.</p>
    <div class="ferr" id="pf-err"></div>
    <div class="factions"><button class="btn approve" id="pf-save">Add project repo</button><button class="btn decline" id="pf-cancel">Cancel</button></div>
  </div>`;
  const list=document.getElementById("pf-list"), filter=document.getElementById("pf-filter");
  const save=document.getElementById("pf-save"), err=document.getElementById("pf-err");
  const picked=new Set();
  let ready=false;   // until the list lands, a keystroke must not repaint "loading" or the error as "no repos"
  const label=()=>{ save.textContent=picked.size>1 ? "Add "+picked.size+" project repos" : "Add project repo"; };
  const note=t=>{ list.innerHTML=`<span class="sk-help" style="padding:8px 10px">${esc(t)}</span>`; };
  // Re-rendered per keystroke rather than hidden row by row: the picks live in the Set, so a
  // filtered-out tick is still added and a row cannot be shown stale.
  const draw=()=>{
    if(!ready) return;
    const q=filter.value.trim().toLowerCase();
    const rows=REPO_PICK.filter(r=>!q || r.slug.toLowerCase().includes(q));
    if(!rows.length){ note(REPO_PICK.length ? "No repo matches that filter." : "Your GitHub login can see no repos."); return; }
    list.innerHTML=rows.map(r=>`<label class="discrow" title="${esc(r.description)}">
      <input type="checkbox" data-url="${esc(r.url)}"${r.registered?" checked disabled":picked.has(r.url)?" checked":""}>
      <code>${esc(r.slug)}</code>${r.registered?'<span class="dishave">added</span>':""}
      ${r.private?'<span class="disalias">private</span>':""}</label>`).join("");
  };
  list.addEventListener("change",e=>{
    const u=e.target.dataset.url; if(!u) return;
    if(e.target.checked) picked.add(u); else picked.delete(u);
    label();
  });
  filter.addEventListener("input",draw);
  getJSON("/api/repos/available")
    .then(d=>{ REPO_PICK=d.repos||[]; ready=true; if(list.isConnected) draw(); })
    .catch(e=>{ REPO_PICK=[]; if(list.isConnected) note(e.message+" — paste a URL below instead."); });
  document.getElementById("pf-cancel").onclick=closeFormModal;
  save.onclick=async()=>{
    const url=document.getElementById("pf-url").value.trim(), path=document.getElementById("pf-path").value.trim();
    const jobs=[...picked].map(u=>({url:u,path:""}));
    if(url || path) jobs.push({url,path});
    if(!jobs.length){ err.textContent="Tick a repo or paste a URL."; return; }
    // Each clone runs inside its own request, one at a time, so the button counts them — a
    // silent multi-second wait reads as a dead button and gets clicked again.
    save.disabled=true; err.textContent="";
    const failed=[];
    for(let i=0;i<jobs.length;i++){
      // Cancelled or closed mid-run: the form is gone, so stop adding what nobody is watching.
      if(!save.isConnected) break;
      save.textContent=jobs.length>1 ? "Cloning "+(i+1)+"/"+jobs.length+"…" : "Cloning…";
      try {
        await postJSON("/api/project/add",jobs[i]);
        picked.delete(jobs[i].url);
        const row=REPO_PICK.find(r=>r.url===jobs[i].url); if(row) row.registered=true;
      } catch(e){ failed.push((jobs[i].url||jobs[i].path)+": "+e.message); }
    }
    loadAdmin();
    if(!save.isConnected) return;   // never close a modal opened since
    save.disabled=false; label();
    if(!failed.length){ closeFormModal(); return; }
    // What landed is already ticked off; the form stays open on exactly what did not.
    err.textContent=failed.join(" · "); draw();
  };
}
