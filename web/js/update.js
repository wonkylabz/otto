"use strict";
/* Header "Update" control: always shown; a click fetches origin/main (nothing polls git) and opens
   a confirm modal, then watches /api/health's `revision` change to know the restart landed. */
let UPDATE_WATCH=null;

function applyUpdate(u){
  const b=document.getElementById("update"); if(!b || !u) return;
  const running=u.job==="running" || !!UPDATE_WATCH;
  b.disabled=running;
  b.textContent=running ? "Updating…" : u.behind ? `Update · ${u.behind}` : "Update";
}

async function showUpdateForm(){
  const c=openFormModal("<b>Update Otto</b><br>pull origin/main and restart the service");
  c.innerHTML=`<div class="aform"><div class="hint">checking…</div></div>`;
  let st;
  try{ st=await postJSON("/api/update/check",{}); }
  catch(e){ c.innerHTML=`<div class="aform"><div class="ferr">${esc(e.message)}</div></div>`; return; }
  applyUpdate(st);
  const commits=(st.commits||[]).map(x=>`<li><code>${esc(x.sha)}</code> ${esc(x.title)}</li>`).join("");
  const blocks=(st.blockers||[]).map(b=>`<li>${esc(b)}</li>`).join("");
  const last=st.last&&st.last.state&&st.last.state!=="running"
    ? `<label>Last update: ${esc(st.last.state)} ${esc(st.last.from||"")}${st.last.to?" → "+esc(st.last.to):""}${st.last.error?" — "+esc(st.last.error):""}</label>` : "";
  c.innerHTML=`<div class="aform">
    <label>Running <code>${esc(st.revision)}</code> &middot; ${st.fetch_error?"couldn't check origin/main":st.behind?`${st.behind} commit${st.behind===1?"":"s"} behind`:"up to date"}</label>
    ${commits?`<ul>${commits}</ul>`:""}
    ${st.fetch_error?`<div class="ferr">fetch failed: ${esc(st.fetch_error)}</div>`:""}
    ${blocks?`<label>Can't update yet</label><ul class="ferr">${blocks}</ul>`:""}
    ${last}
    <div class="ferr" id="up-err"></div>
    <div class="factions"><button class="btn approve" id="up-go" ${blocks||!st.behind?"disabled":""}>Update &amp; restart</button><button class="btn decline" id="up-cancel">Close</button></div>
  </div>`;
  document.getElementById("up-cancel").addEventListener("click", closeFormModal);
  document.getElementById("up-go").addEventListener("click", async ()=>{
    const go=document.getElementById("up-go"); go.disabled=true; go.textContent="starting…";
    try{ await postJSON("/api/update",{}); }
    catch(e){
      // Only a status is a refusal. A dropped connection is the restart this click started
      // beating the response, so watch for the outcome instead of reporting a failure.
      if(e.status){ document.getElementById("up-err").textContent=e.message;
                    go.disabled=false; go.textContent="Update & restart"; return; }
    }
    closeFormModal();
    watchUpdate(st.revision, (st.last&&st.last.unit)||"");
  });
}

function watchUpdate(from, prevJob){
  toast("Updating Otto — the page reconnects when it's back.","ok");
  const started=Date.now();
  UPDATE_WATCH=poll(async ()=>{
    let h;
    try{ h=await (await fetch("/api/health")).json(); }catch(e){ return false; }   // down mid-restart
    applyUpdate(h.update);
    if(h.revision && h.revision!==from){ UPDATE_WATCH(); UPDATE_WATCH=null; location.reload(); return; }
    // An outcome counts only from THIS update's job, never the last one's "done".
    const u=h.update||{}, done=u.job && u.job!=="running" && u.job_id && u.job_id!==prevJob;
    if(!done && Date.now()-started<300000) return;
    UPDATE_WATCH(); UPDATE_WATCH=null;
    applyUpdate(h.update);
    toast(`Update ${done?u.job:"timed out"} — still on ${from}. Open Update for details.`);
  }, 4000, 8000);
}

document.getElementById("update").addEventListener("click", showUpdateForm);
