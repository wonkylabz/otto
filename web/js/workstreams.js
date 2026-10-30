"use strict";
/* ---- Workstreams: every chat/run about one ticket or PR, grouped ----
   Membership lives on the CHAT (workstreams.py), so the timeline outlives the Swarm board's
   retention window. The board shows a chip per card and filters to one workstream; the timeline
   opens in the shared form modal. BOARD_WS (the board's filter) and BOARD_MODE (which of the
   page's two views is up) live outside the render, so a poll repaint keeps both. */
let BOARD_WS=null, BOARD_MODE="board", _wsViewSig=null;

/* The page's title IS its view switch: two titles in one heading style, the inactive one dimmed. */
function boardHead(sub){
  const tab=(v,label)=>`<button data-view="${v}" class="${BOARD_MODE===v?"on":""}"${BOARD_MODE===v?' aria-current="page"':""}>${label}</button>`;
  return `<div class="phead"><h1 class="viewswitch">${tab("board","Swarm board")}${tab("workstreams","Workstreams")}</h1>
      <p class="sub">${sub}</p>
      <div class="pside" id="board-health"></div></div>`;
}

function wsChip(ws){
  if(!ws) return "";
  return `<a href="#" class="bchip ws" data-wsfilter="${esc(ws.id)}" title="workstream — show only its runs">&#9776; ${esc(ws.title)}</a>`;
}

/* A topic-word MATCH (workstreams.suggest), never a filing: one click files it, × says "not this"
   — which detaches the chat, so neither a suggestion nor a URL match files it again. */
function wsSuggestChip(chatId, sug, dismiss){
  if(!chatId||!sug) return "";
  return `<span class="bchip wssug"><a href="#" data-wsaccept="${esc(chatId)}" data-wsid="${esc(sug.id)}" title="shares a word with this workstream's name — click to file this chat under it">&#9776; ${esc(sug.title)}?</a>${dismiss?`<a href="#" data-wsno="${esc(chatId)}" title="not this workstream — stop suggesting one for this chat">&times;</a>`:""}</span>`;
}

function wireSuggestions(root, reload){
  root.querySelectorAll("[data-wsaccept]").forEach(a=>a.addEventListener("click",async e=>{
    e.preventDefault(); e.stopPropagation();
    if(await postOr("/api/workstreams/assign",{chat_id:a.dataset.wsaccept, workstream_id:a.dataset.wsid},"filing that chat")) reload();
  }));
  root.querySelectorAll("[data-wsno]").forEach(a=>a.addEventListener("click",async e=>{
    e.preventDefault(); e.stopPropagation();
    if(await postOr("/api/workstreams/assign",{chat_id:a.dataset.wsno},"dismissing that suggestion")) reload();
  }));
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
  wireSuggestions(root, reload);
  root.querySelectorAll("[data-wsfilter]").forEach(a=>a.addEventListener("click",e=>{
    e.preventDefault(); e.stopPropagation(); BOARD_WS=a.dataset.wsfilter; reload();
  }));
  root.querySelectorAll("[data-wsclear]").forEach(b=>b.addEventListener("click",()=>{ BOARD_WS=null; reload(); }));
  root.querySelectorAll("[data-wstimeline]").forEach(b=>b.addEventListener("click",()=>openWorkstream(b.dataset.wstimeline)));
  root.querySelectorAll("[data-wsassign]").forEach(a=>a.addEventListener("click",e=>{
    e.preventDefault(); e.stopPropagation(); assignWorkstream(a.dataset.wsassign, a.dataset.wscur||null, reload);
  }));
}

/* Every workstream, newest activity first — the way in to one whose runs the board no longer shows. */
/* The Workstreams view: every workstream, newest activity first — the way in to one whose runs the
   board no longer shows. Rides the board's poll; repaints only when the list changed. */
async function renderWorkstreamsView(el, silent){
  let list;
  try { list=(await getJSON("/api/workstreams")).workstreams||[]; }
  catch(e){ if(!silent) el.innerHTML=boardHead("")+`<p class="err">Couldn't load workstreams (${esc(e.message)}).</p>`; return false; }
  if(BOARD_MODE!=="workstreams") return;          // switched away while the list was loading
  const sig=JSON.stringify(list);
  if(silent && sig===_wsViewSig && el.querySelector(".viewswitch")){ loadBoardHealth(); return; }
  _wsViewSig=sig;
  const row=w=>`<li class="wsrow" data-wstl="${esc(w.id)}" tabindex="0" role="button">
    <div class="wshead"><b>${esc(w.title)}</b>${w.running?`<span class="bchip stage">${w.running} running</span>`:""}</div>
    <div class="wsmeta">${w.chats} chat${w.chats===1?"":"s"} · last activity ${esc(shortWhen(w.updated))}${(w.refs||[]).map(r=>`<span class="bchip">${esc(r)}</span>`).join("")}</div>
  </li>`;
  el.innerHTML=boardHead("Every chat about one ticket or PR, grouped — including runs the board no longer shows.")+
    (list.length?`<ul class="wsrows">${list.map(row).join("")}</ul>`
    :`<p class="memempty">No workstreams yet. File a run's chat with <b>+ Workstream</b> on its card in Swarm board, or link the same issue or PR from two chats and they group by themselves.</p>`);
  loadBoardHealth();
  wireBoardHead(el);
  el.querySelectorAll("[data-wstl]").forEach(r=>{
    const go=()=>openWorkstream(r.dataset.wstl);
    r.addEventListener("click",go);
    r.addEventListener("keydown",e=>{ if(e.key==="Enter"||e.key===" "){ e.preventDefault(); go(); } });
  });
}

function wireBoardHead(el){
  el.querySelectorAll("[data-view]").forEach(b=>b.addEventListener("click",()=>{
    if(BOARD_MODE===b.dataset.view) return;
    BOARD_MODE=b.dataset.view; _wsViewSig=null; loadBoard();
  }));
}

async function openWorkstream(id){
  const body=openFormModal("Workstream");
  body.closest(".modalBox").classList.add("wideModalBox");
  body.innerHTML=`<p class="sub">loading…</p>`;
  let ws;
  try { ws=await getJSON("/api/workstreams/get?id="+encodeURIComponent(id)); }
  catch(e){ body.innerHTML=`<p class="err">Couldn't load it (${esc(e.message)}).</p>`; return; }
  const n=(ws.items||[]).length;
  document.getElementById("formModalTitle").innerHTML=`<b>${esc(ws.title)}</b><br>${n} chat${n===1?"":"s"}${n?` since ${esc(shortWhen(ws.items[0].created))}`:""}`;
  const refs=(ws.refs||[]).map(r=>`<span class="bchip">${esc(r)}</span>`).join("");
  const row=it=>`<li class="wsitem">
    <div class="wshead"><span class="bwhen">${esc(shortWhen(it.created))}</span><b>${esc(it.title||"Untitled")}</b>
      ${it.cap?`<span class="bchip">${esc(it.cap)}</span>`:""}${(it.labels||[]).map(l=>`<span class="bchip">${esc(l)}</span>`).join("")}
      ${it.running?`<span class="bchip stage">running</span>`:""}${it.cost?`<span class="bchip">$${Number(it.cost).toFixed(2)}</span>`:""}</div>
    ${it.prs.length?`<div class="wsprs">${it.prs.map(u=>`<a href="${esc(u)}" target="_blank" rel="noopener">${esc(u.replace(/^https:\/\/github\.com\//,""))} &#8599;</a>`).join(" ")}</div>`:""}
    ${it.last?`<div class="bout clip">${renderMD(it.last)}</div>`:""}
    <div class="wsacts"><a href="#" data-wsopen="${esc(it.id)}">Open chat</a><a href="#" data-wsout="${esc(it.id)}" title="take this chat out of the workstream">Take out</a></div>
  </li>`;
  body.innerHTML=`<div class="aform wstl"><div class="wsrefs">${refs||'<span class="sub">no linked tickets or PRs</span>'}</div>
    <ol class="wslist">${(ws.items||[]).map(row).join("")}</ol>
    <div class="factions"><button class="btn approve" id="ws-board" title="filter the Swarm board to this workstream's runs">Show on board</button><button class="btn decline" id="ws-rename">Rename</button><button class="btn danger" id="ws-ungroup" title="the chats are kept — only the grouping goes">Ungroup</button></div></div>`;
  body.querySelector("#ws-board").addEventListener("click",()=>{
    BOARD_WS=id; BOARD_MODE="board"; closeFormModal(); activateTab("board"); loadBoard();
  });
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
    if(await postOr("/api/workstreams/delete",{id},"ungrouping it")){ if(BOARD_WS===id) BOARD_WS=null; closeFormModal(); _wsViewSig=null; loadBoard(); }
  });
}

async function assignWorkstream(chatId, current, then){
  const body=openFormModal("<b>File under a workstream</b><br>group this run's chat with the rest of the same work");
  let list=[];
  try { list=(await getJSON("/api/workstreams")).workstreams||[]; } catch(e){}
  const row=w=>`<label class="moderow"><input type="radio" name="ws-pick" value="${esc(w.id)}"${w.id===current?" checked":""}>
    <span class="wsname">${esc(w.title)}</span><span class="wsn">${w.chats} chat${w.chats===1?"":"s"}</span></label>`;
  body.innerHTML=`<div class="aform">
    ${list.length?`<label>Add to</label><div class="modepick wspick">${list.map(row).join("")}
      <label class="moderow"><input type="radio" name="ws-pick" value=""${current?"":" checked"}> <span class="wsname">A new workstream</span></label></div>`:""}
    <label for="ws-new">${list.length?"New workstream name":"Name"}</label>
    <input type="text" id="ws-new" placeholder="e.g. #480 — retry on Slack">
    <div class="ferr" id="ws-err"></div>
    <div class="factions"><button class="btn approve" id="ws-save">Save</button>
      ${current?`<button class="btn decline" id="ws-out" title="take this chat out of its workstream">Take out</button>`:""}
      <button class="btn decline" id="ws-cancel">Cancel</button></div>
  </div>`;
  const name=body.querySelector("#ws-new"), err=body.querySelector("#ws-err");
  const picked=()=>{ const r=body.querySelector('input[name="ws-pick"]:checked'); return r?r.value:""; };
  const sync=()=>{ name.disabled=!!picked(); };
  body.querySelectorAll('input[name="ws-pick"]').forEach(r=>r.addEventListener("change",()=>{ sync(); if(!r.value) name.focus(); }));
  sync(); if(!name.disabled) name.focus();
  const send=async payload=>{
    if(await postForm("/api/workstreams/assign", Object.assign({chat_id:chatId},payload), err)){ closeFormModal(); if(then) then(); }
  };
  const save=()=>{
    if(picked()) return send({workstream_id:picked()});
    if(!name.value.trim()){ err.textContent="Name the new workstream."; name.focus(); return; }
    send({title:name.value.trim()});
  };
  body.querySelector("#ws-save").addEventListener("click",save);
  name.addEventListener("keydown",e=>{ if(e.key==="Enter"){ e.preventDefault(); save(); } });
  body.querySelector("#ws-cancel").addEventListener("click",closeFormModal);
  const out=body.querySelector("#ws-out");
  if(out) out.addEventListener("click",()=>send({}));
}
