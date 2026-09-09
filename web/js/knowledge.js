"use strict";
/* ---- knowledge tab (issue #67): imported reference docs, RAG-injected on fresh runs ---- */
async function loadKnowledge(){
  const el=document.getElementById("knowledgeview");
  el.innerHTML=`<p class="sub">loading…</p>`;
  let d;
  try { d=await (await fetch("/api/knowledge")).json(); }
  catch(e){ el.innerHTML=`<p class="err">Couldn't load the knowledge base (${esc(e.message)}).</p>`; return; }
  const st=d.settings||{};
  const embedOpts=['<option value="">keyword match (no embedding model)</option>']
    .concat((d.embed_models||[]).map(n=>`<option value="${esc(n)}"${st.embed_model===n?' selected':''}>${esc(n)}</option>`)).join("");
  // When an embedding model is configured but a doc has un-embedded chunks, retrieval silently
  // degrades to keyword match for it (the embed call failed at add-time) — flag it, don't hide it.
  const modelSet=!!st.embed_model;
  const stale=modelSet?(d.docs||[]).filter(doc=>doc.chunks>doc.embedded).length:0;
  const docs=(d.docs||[]).map(doc=>{
    const under=modelSet&&doc.chunks>doc.embedded;
    return `<div class="event">
      <div class="kdoc"><b>${esc(doc.title)}</b> <span class="rscope">${doc.chunks} chunk${doc.chunks===1?'':'s'}${doc.embedded?` · ${doc.embedded} embedded`:''}</span>${under?` <span class="kwarn">⚠ ${doc.embedded?'partly ':'not '}embedded — keyword-only</span>`:''}</div>
      <div class="esrc">${esc(doc.source||'')} · <span title="${esc(doc.at||'')}">${esc(shortWhen(doc.at))}</span>
        <button class="ruledel" data-id="${esc(doc.id)}">delete</button></div>
    </div>`;}).join("");
  const staleBanner=stale?`<p class="err">⚠ ${stale} document${stale===1?'':'s'} couldn't be embedded with <b>${esc(st.embed_model)}</b> — retrieval is keyword-only until you fix the model and <b>Re-embed all</b>.</p>`:'';
  const thr=(st.threshold!=null?st.threshold:0.18);
  const empty=!(d.count||0);
  el.innerHTML=`
    <div class="phead"><h1>Knowledge base</h1>
      <p class="sub">Docs retrieved and injected on every fresh run. <b>Memory</b> is what Otto learned; this is what you told it.</p></div>
    <div class="kbar">
      <span class="memstat"><b>${d.count||0}</b> document${d.count===1?'':'s'}</span>
      <button class="clearbtn kaddbtn" id="k-toggle">${empty?'':'+ Add document'}</button>
    </div>
    ${staleBanner}
    ${docs || `<div class="kempty"><b>No documents yet</b>Paste a runbook, a doc or any reference text below — Otto retrieves the relevant snippets and injects them into every fresh run.</div>`}
    <div class="kadd" id="k-form"${empty?'':' hidden'}>
      <input id="k-title" placeholder="Document title (e.g. AWS VPN renewal runbook)">
      <textarea id="k-text" rows="6" placeholder="Paste the document text…"></textarea>
      <div class="rulerow"><button class="clearbtn" id="k-add" style="margin-left:0">Add document</button></div>
    </div>
    <div class="phead sec"><h2>Retrieval settings</h2>
      <p class="sub">Ranked by meaning with an embedding model, else keyword overlap. Higher threshold = fewer, closer snippets.</p></div>
    <div class="ksettings">
      <label>Embedding model <select id="k-embed">${embedOpts}</select></label>
      <label>Threshold <input type="range" id="k-thr" min="0" max="0.9" step="0.02" value="${thr}"> <span id="k-thrv">${(+thr).toFixed(2)}</span></label>
    </div>
    ${modelSet&&!empty?`<div class="rulerow"><button class="clearbtn" id="k-reembed" style="margin-left:0">Re-embed all documents</button> <span class="sub" id="k-reembed-msg">Recompute every doc's vectors after changing the model.</span></div>`:''}
    <div class="phead sec"><h2>Preview retrieval</h2>
      <p class="sub">See which snippets a request would pull in.</p></div>
    <div class="kprev"><input id="k-q" placeholder="e.g. how do I renew the client VPN certificate?"><button class="clearbtn" id="k-prev" style="margin-left:0">Preview</button></div>
    <div id="k-hits"></div>`;
  const kToggle=document.getElementById("k-toggle"), kForm=document.getElementById("k-form");
  if(kToggle&&kForm){
    // hidden on an empty store: the empty panel is already the invitation, so a second
    // "+ Add document" beside it would be two calls to action for one job
    kToggle.hidden = !kToggle.textContent.trim();
    kToggle.addEventListener("click",()=>{
      const show=kForm.hasAttribute("hidden");
      kForm.toggleAttribute("hidden",!show);
      kToggle.textContent = show?"Cancel":"+ Add document";
      if(show) document.getElementById("k-title").focus();
    });
  }
  const addBtn=document.getElementById("k-add");
  if(addBtn) addBtn.addEventListener("click",async()=>{
    const title=(document.getElementById("k-title").value||"").trim();
    const text=(document.getElementById("k-text").value||"").trim();
    if(!text) return;
    addBtn.disabled=true; addBtn.textContent="Adding…";
    try { await fetch("/api/knowledge/add",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({title,text})}); }
    finally { loadKnowledge(); }
  });
  el.querySelectorAll(".ruledel").forEach(b=>b.addEventListener("click",async()=>{
    await fetch("/api/knowledge/delete",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:b.dataset.id})});
    loadKnowledge();
  }));
  const embedSel=document.getElementById("k-embed");
  if(embedSel) embedSel.addEventListener("change",async()=>{
    await fetch("/api/knowledge/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({embed_model:embedSel.value})});
    loadKnowledge();   // reflect the model change (re-embed prompt / stale badges)
  });
  const reBtn=document.getElementById("k-reembed"), reMsg=document.getElementById("k-reembed-msg");
  if(reBtn) reBtn.addEventListener("click",async()=>{
    reBtn.disabled=true; reBtn.textContent="Re-embedding…";
    try {
      const r=await (await fetch("/api/knowledge/reembed",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"})).json();
      const res=r.result||{};
      if(reMsg) reMsg.textContent=`Embedded ${res.embedded||0}/${res.chunks||0} chunk${res.chunks===1?'':'s'}${res.embedded<res.chunks?' — some still failed; check the model is reachable.':' ✓'}`;
    } catch(e){ if(reMsg) reMsg.textContent="Re-embed failed: "+e.message; }
    finally { setTimeout(loadKnowledge,1200); }
  });
  const thrEl=document.getElementById("k-thr"), thrV=document.getElementById("k-thrv");
  if(thrEl){
    thrEl.addEventListener("input",()=>{ thrV.textContent=(+thrEl.value).toFixed(2); });
    thrEl.addEventListener("change",async()=>{
      await fetch("/api/knowledge/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({threshold:+thrEl.value})});
    });
  }
  const prevBtn=document.getElementById("k-prev");
  if(prevBtn) prevBtn.addEventListener("click",async()=>{
    const q=(document.getElementById("k-q").value||"").trim(); if(!q) return;
    let r; try { r=await (await fetch("/api/knowledge/preview",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({query:q})})).json(); }
    catch(e){ return; }
    const hits=(r.hits||[]).map(h=>`<div class="event"><div class="approach">${esc(h.text)}</div><div class="esrc">${esc(h.title)} · score ${h.score}</div></div>`).join("");
    document.getElementById("k-hits").innerHTML=hits || '<p class="memempty">No snippet clears the threshold for that query.</p>';
  });
}
