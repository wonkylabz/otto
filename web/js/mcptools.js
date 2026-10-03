"use strict";
/* Admin -> MCP servers -> Tools: which of one server's tools are SAFE (issue #193).

   A safe tool may run with no plan preview and no approval gate, for the owner and Slack
   approvers, when a request needs nothing else — and that run holds ONLY the safe tools. Unticked
   is gated, which is also what every new or unknown tool is. The list comes from the cached
   catalogue (`GET /api/mcp/tools`, or `POST /api/mcp/tools/list` to start the server once);
   `/api/mcp/safe-tools` is the one writer, and refuses a name
   the server does not list. Its own file because admin.js sits at the asset ratchet; bound by one
   delegated listener, so nothing here runs at load beyond registering it. */

async function showMcpToolsForm(name, display, refresh){
  const c=openFormModal("<b>Safe tools</b><br>"+esc(display||name));
  c.innerHTML=`<div class="aform"><span class="sk-help">Loading the tool list&hellip;</span></div>`;
  let d;
  // Re-list STARTS the server, so it is a POST (CSRF-checked); the plain open reads the cache.
  try { d=refresh ? await postJSON("/api/mcp/tools/list",{name:name})
                  : await getJSON("/api/mcp/tools?name="+encodeURIComponent(name)); }
  catch(e){ c.innerHTML=`<div class="aform"><div class="ferr">${esc(e.message)}</div></div>`; return; }
  const tools=d.tools||[];
  c.innerHTML=`<div class="aform">
    <span class="sk-help">A <b>safe</b> tool can run without a plan or an approval, when you or a
      Slack approver ask for something that needs only safe tools &mdash; and that run gets only
      the safe tools, nothing else. Unticked tools, and any tool added later, stay gated. Tick
      only what you would approve without reading, like switching a light.</span>
    ${d.note?`<div class="ferr">${esc(d.note)}</div>`:''}
    <div class="disclist" id="mt-list"></div>
    <div class="ferr" id="mt-err"></div>
    <div class="factions"><button class="btn approve" id="mt-save" ${tools.length?'':'disabled'}>Save</button>
      <button class="btn" id="mt-refresh" title="start the server once and re-read its tools">Re-list tools</button>
      <button class="btn decline" id="mt-cancel">Cancel</button></div>
  </div>`;
  const list=document.getElementById("mt-list");
  if(!tools.length && !d.note) list.textContent="This server lists no tools.";
  tools.forEach(t=>{
    const row=document.createElement("label");
    row.className="discrow";
    row.title=t.description||"";
    const box=document.createElement("input");
    box.type="checkbox"; box.className="mt-box"; box.value=t.name; box.checked=!!t.safe;
    const txt=document.createElement("span");
    txt.textContent=" "+t.name;
    row.appendChild(box); row.appendChild(txt); list.appendChild(row);
  });
  document.getElementById("mt-cancel").onclick=closeFormModal;
  document.getElementById("mt-refresh").onclick=()=>showMcpToolsForm(name, display, true);
  document.getElementById("mt-save").onclick=async()=>{
    const want=[...document.querySelectorAll(".mt-box")].filter(b=>b.checked).map(b=>b.value);
    try { await postJSON("/api/mcp/safe-tools",{name:name,tools:want}); }
    catch(e){ document.getElementById("mt-err").textContent=e.message; return; }
    closeFormModal(); loadAdmin();
  };
}

document.addEventListener("click",e=>{
  const b=e.target.closest&&e.target.closest("[data-mcptools]");
  if(b) showMcpToolsForm(b.dataset.mcptools, b.dataset.mcpdisplay);
});
