"use strict";
/* ---- Workstreams: every chat/run about one ticket or PR, grouped ----
   Membership lives on the CHAT (workstreams.py), so the timeline outlives the Swarm board's
   retention window. The board shows a chip per card and filters to one workstream; the timeline
   opens in the shared form modal. BOARD_WS is the board's filter — outside the render, so a poll
   repaint keeps it. */
let BOARD_WS=null;

function wsChip(ws){
  if(!ws) return "";
  return `<a href="#" class="bchip ws" data-wsfilter="${esc(ws.id)}" title="workstream — show only its runs">&#9776; ${esc(ws.title)}</a>`;
}

function wsFilterBar(items){
  if(!BOARD_WS) return "";
  const hit=items.find(it=>it.workstream&&it.workstream.id===BOARD_WS);
  const title=hit?hit.workstream.title:"this workstream";
  return `<div class="wsbar"><span>Workstream: <b>${esc(title)}</b></span>
    <button class="addbtn" data-wstimeline="${esc(BOARD_WS)}" title="every chat in it, oldest first — including runs the board no longer shows">Timeline</button>
    <button class="addbtn" data-wsclear="1" title="show every run again">Clear</button></div>`;
}

function wireWorkstreams(root, reload){
  root.querySelectorAll("[data-wsfilter]").forEach(a=>a.addEventListener("click",e=>{
    e.preventDefault(); e.stopPropagation(); BOARD_WS=a.dataset.wsfilter; reload();
  }));
  root.querySelectorAll("[data-wsclear]").forEach(b=>b.addEventListener("click",()=>{ BOARD_WS=null; reload(); }));
  root.querySelectorAll("[data-wstimeline]").forEach(b=>b.addEventListener("click",()=>openWorkstream(b.dataset.wstimeline)));
  root.querySelectorAll("[data-wsassign]").forEach(a=>a.addEventListener("click",e=>{
    e.preventDefault(); e.stopPropagation(); assignWorkstream(a.dataset.wsassign, a.dataset.wscur||null, reload);
  }));
}

async function openWorkstream(id){
  const body=openFormModal("Workstream");
  body.closest(".modalBox").classList.add("wideModalBox");
  body.innerHTML=`<p class="sub">loading…</p>`;
  let ws;
  try { ws=await getJSON("/api/workstreams/get?id="+encodeURIComponent(id)); }
  catch(e){ body.innerHTML=`<p class="err">Couldn't load it (${esc(e.message)}).</p>`; return; }
  document.getElementById("formModalTitle").textContent=ws.title;
  const refs=(ws.refs||[]).map(r=>`<span class="bchip">${esc(r)}</span>`).join("");
  const row=it=>`<li class="wsitem">
    <div class="wshead"><span class="bwhen">${esc(shortWhen(it.created))}</span><b>${esc(it.title||"Untitled")}</b>
      ${it.cap?`<span class="bchip">${esc(it.cap)}</span>`:""}${(it.labels||[]).map(l=>`<span class="bchip">${esc(l)}</span>`).join("")}
      ${it.running?`<span class="bchip stage">running</span>`:""}${it.cost?`<span class="bchip">$${Number(it.cost).toFixed(2)}</span>`:""}</div>
    ${it.prs.length?`<div class="wsprs">${it.prs.map(u=>`<a href="${esc(u)}" target="_blank" rel="noopener">${esc(u.replace(/^https:\/\/github\.com\//,""))} &#8599;</a>`).join(" ")}</div>`:""}
    ${it.last?`<div class="bout clip">${renderMD(it.last)}</div>`:""}
    <div class="wsacts"><a href="#" data-wsopen="${esc(it.id)}">Open chat</a><a href="#" data-wsout="${esc(it.id)}" title="take this chat out of the workstream">Take out</a></div>
  </li>`;
  body.innerHTML=`<div class="wsrefs">${refs||'<span class="sub">no linked tickets or PRs</span>'}</div>
    <ol class="wslist">${(ws.items||[]).map(row).join("")}</ol>
    <div class="askActions"><button class="btn sm" id="ws-rename">Rename</button><button class="btn danger sm" id="ws-ungroup" title="the chats are kept — only the grouping goes">Ungroup</button></div>`;
  body.querySelectorAll("[data-wsopen]").forEach(a=>a.addEventListener("click",e=>{
    e.preventDefault(); closeFormModal(); openChat(a.dataset.wsopen);
  }));
  body.querySelectorAll("[data-wsout]").forEach(a=>a.addEventListener("click",async e=>{
    e.preventDefault();
    if(await postOr("/api/workstreams/assign",{chat_id:a.dataset.wsout},"taking that chat out")) openWorkstream(id);
  }));
  body.querySelector("#ws-rename").addEventListener("click",async()=>{
    const t=await askText("Rename this workstream", ws.title, {ok:"Rename"});
    if(t&&t.trim()&&await postOr("/api/workstreams/rename",{id, title:t.trim()},"renaming it")) openWorkstream(id);
  });
  body.querySelector("#ws-ungroup").addEventListener("click",async()=>{
    if(!await askConfirm("Ungroup these chats? They stay, and won't be regrouped automatically.",{ok:"Ungroup",danger:true})) return;
    if(await postOr("/api/workstreams/delete",{id},"ungrouping it")){ if(BOARD_WS===id) BOARD_WS=null; closeFormModal(); loadBoard(); }
  });
}

async function assignWorkstream(chatId, current, then){
  const body=openFormModal("File under a workstream");
  let list=[];
  try { list=(await getJSON("/api/workstreams")).workstreams||[]; } catch(e){}
  body.innerHTML=`<div class="aform">
    <label>Existing<select id="ws-pick"><option value="">— new workstream —</option>${list.map(w=>`<option value="${esc(w.id)}"${w.id===current?" selected":""}>${esc(w.title)} (${w.chats})</option>`).join("")}</select></label>
    <label>New workstream name<input type="text" id="ws-new" placeholder="e.g. #480 — retry on Slack" /></label>
    <p class="err" id="ws-err"></p>
    <div class="askActions">${current?`<button class="btn decline sm" id="ws-out">Take out</button>`:""}<button class="btn approve sm" id="ws-save">Save</button></div></div>`;
  const pick=body.querySelector("#ws-pick"), name=body.querySelector("#ws-new");
  const sync=()=>{ name.disabled=!!pick.value; };
  pick.addEventListener("change",sync); sync();
  const send=async payload=>{
    if(await postForm("/api/workstreams/assign", Object.assign({chat_id:chatId},payload), body.querySelector("#ws-err"))){ closeFormModal(); if(then) then(); }
  };
  body.querySelector("#ws-save").addEventListener("click",()=>{
    if(pick.value) return send({workstream_id:pick.value});
    if(!name.value.trim()){ body.querySelector("#ws-err").textContent="Pick one, or name a new one."; return; }
    send({title:name.value.trim()});
  });
  const out=body.querySelector("#ws-out");
  if(out) out.addEventListener("click",()=>send({}));
}
