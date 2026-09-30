"use strict";
/* Admin → Share extensions: the caps+MCP bundle, and the whole-install config snapshot (#166).
   A snapshot import is PREVIEW then APPLY: the apply carries the preview's fingerprint, and the
   server refuses (409) if the plan changed since — so it changes exactly what was shown. */
async function exportBundle(){
  const data=await (await fetch("/api/bundle/export")).json();
  const blob=new Blob([JSON.stringify(data,null,2)],{type:"application/json"});
  const a=document.createElement("a");
  a.href=URL.createObjectURL(blob); a.download="otto-bundle.json";
  document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(a.href);
  const n=(data.capabilities||[]).length, m=Object.keys(data.mcp_servers||{}).length;
  setBundleMsg(`exported ${n} capability(ies) + ${m} MCP server(s) → otto-bundle.json`);
}

function wireBundle(el){
  const file=el.querySelector("#bundle-file");
  el.querySelector("#export-bundle").addEventListener("click",exportBundle);
  el.querySelector("#import-bundle").addEventListener("click",()=>{ file.value=""; file.click(); });
  file.addEventListener("change",async()=>{
    const f=file.files[0]; if(!f) return;
    let bundle;
    try { bundle=JSON.parse(await f.text()); }
    catch(e){ setBundleMsg("not valid JSON", true); return; }
    let data;
    try { data=await postJSON("/api/bundle/import", bundle); }
    catch(e){ setBundleMsg("import failed: "+e.message, true); return; }
    const caps=(data.capabilities_added||[]).length, mcps=(data.mcps_added||[]).length;
    const renamed=[...(data.capabilities_renamed||[]),...(data.mcps_renamed||[])];
    let msg=`imported ${caps} capability(ies) + ${mcps} MCP server(s)`;
    if(renamed.length) msg+=` · renamed: ${renamed.map(r=>`${r.from}→${r.to}`).join(", ")}`;
    if((data.needs_env||[]).length) msg+=` · set env for: ${data.needs_env.join(", ")}`;
    await loadAdmin();        // re-render the lists first (rebuilds the panel)…
    setBundleMsg(msg);        // …then post the summary into the fresh element
  });
}

function setBundleMsg(text, bad){
  const m=document.getElementById("bundle-msg");
  if(!m) return;
  m.textContent=text; m.className="bundlemsg"+(bad?" bad":" ok");
}

function downloadJSON(data, name){
  const blob=new Blob([JSON.stringify(data,null,2)],{type:"application/json"});
  const a=document.createElement("a");
  a.href=URL.createObjectURL(blob); a.download=name;
  document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(a.href);
}

function setSnapMsg(text, bad){
  const m=document.getElementById("snapshot-msg");
  if(!m) return;
  m.textContent=text; m.className="bundlemsg"+(bad?" bad":" ok");
}

async function exportSnapshot(){
  let data;
  try { data=await (await fetch("/api/profile/export")).json(); }
  catch(e){ setSnapMsg("export failed: "+e.message, true); return; }
  downloadJSON(data, "otto-snapshot.json");
  const n=Object.values(data.sections||{}).reduce((t,s)=>t+Object.keys(s||{}).length,0);
  setSnapMsg(`exported ${n} item(s) across ${Object.keys(data.sections||{}).length} stores → otto-snapshot.json`);
}

function snapValue(v){
  if(v===null || v===undefined) return "—";
  const s=typeof v==="string" ? v : JSON.stringify(v);
  return s.length>80 ? s.slice(0,80)+"…" : s;
}

function renderSnapPlan(box, plan){
  const rows=plan.changes.map(c=>`<tr><td class="snapsec">${esc(c.section)}</td><td class="snapkey">${esc(c.key)}</td>
    <td><span class="snapact snapact-${esc(c.action)}">${c.action==="keep"?"kept local":esc(c.action)}</span></td>
    <td class="snapval">${c.reason?esc(c.reason):(c.action==="keep"||c.action==="update"
      ?`${esc(snapValue(c.before))} → ${esc(snapValue(c.after))}`
      :esc(snapValue(c.action==="remove"?c.before:c.after)))}</td></tr>`).join("");
  const secrets=plan.secrets.map(s=>`<li>${esc(s.for)} — ${esc(s.what)}${s.name?` <code>${esc(s.name)}</code>`:""}</li>`).join("");
  const warns=plan.warnings.map(w=>`<li>${esc(w)}</li>`).join("");
  const n=plan.changes.filter(c=>c.action!=="keep").length;
  box.innerHTML=(rows?`<table class="ctable snaptable"><colgroup><col class="c-sstore"><col class="c-sitem"><col class="c-sact"><col></colgroup><thead><tr><th>Store</th><th>Item</th><th>Change</th><th>Value</th></tr></thead><tbody>${rows}</tbody></table>`
      :`<p class="sub">Nothing to change — this install already matches.</p>`)
    +(secrets?`<p class="sub"><b>Secrets to set afterwards</b></p><ul class="snaplist">${secrets}</ul>`:"")
    +(warns?`<p class="sub"><b>Warnings</b></p><ul class="snaplist">${warns}</ul>`:"");
  return n;
}

function openSnapshotImport(profile){
  const body=openFormModal("Import snapshot");
  body.closest(".modalBox").classList.add("wideModalBox");
  body.innerHTML=`<div class="snapmodes">
      <label title="Add what's missing; anything already configured here is kept"><input type="radio" name="snapmode" value="merge" checked> Merge</label>
      <label title="Make this install match the snapshot: update and REMOVE to match"><input type="radio" name="snapmode" value="replace"> Replace</label></div>
    <div class="snapplan"><p class="sub">previewing…</p></div>
    <div class="factions"><button class="addbtn" id="snap-apply" disabled>Apply</button>
      <span class="bundlemsg" id="snap-modal-msg"></span></div>`;
  const box=body.querySelector(".snapplan"), btn=body.querySelector("#snap-apply");
  const msg=(t,bad)=>{ const m=body.querySelector("#snap-modal-msg"); m.textContent=t; m.className="bundlemsg"+(bad?" bad":""); };
  let plan=null;
  const mode=()=>body.querySelector("input[name=snapmode]:checked").value;
  async function preview(){
    btn.disabled=true; plan=null; msg("");
    box.innerHTML=`<p class="sub">previewing…</p>`;
    try { plan=await postJSON("/api/profile/preview", {profile, mode:mode()}); }
    catch(e){ box.innerHTML=""; msg("preview failed: "+e.message, true); return; }
    const n=renderSnapPlan(box, plan);
    btn.disabled=!n;
    btn.textContent=n?`Apply ${n} change(s)`:"Apply";
  }
  body.querySelectorAll("input[name=snapmode]").forEach(r=>r.addEventListener("change",preview));
  btn.addEventListener("click",async()=>{
    if(!plan) return;
    btn.disabled=true; msg("applying…");
    let data;
    try { data=await postJSON("/api/profile/import", {profile, mode:plan.mode, expect:plan.fingerprint}); }
    catch(e){ await preview(); msg(e.message, true); return; }
    closeFormModal();
    await loadAdmin();
    const bad=(data.status||[]).filter(s=>/fail|not imported/.test(s));
    setSnapMsg(`applied ${data.applied.length} change(s)`+(data.secrets.length?` · ${data.secrets.length} secret(s) to set`:"")
      +(bad.length?` · ${bad.join("; ")}`:""), !!bad.length);
  });
  preview();
}

function wireShare(el){
  wireBundle(el);
  const file=el.querySelector("#snapshot-file");
  el.querySelector("#export-snapshot").addEventListener("click",exportSnapshot);
  el.querySelector("#import-snapshot").addEventListener("click",()=>{ file.value=""; file.click(); });
  file.addEventListener("change",async()=>{
    const f=file.files[0]; if(!f) return;
    let profile;
    try { profile=JSON.parse(await f.text()); }
    catch(e){ setSnapMsg("not valid JSON", true); return; }
    openSnapshotImport(profile);
  });
}
