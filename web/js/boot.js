"use strict";
/* Boot and the periodic view refreshes. Loaded LAST on purpose: the startup pass calls into
   chat, tabs and the mascot, so every module above must already have run. */
setInterval(()=>{
  const bv=document.getElementById("boardview");
  if(bv && !bv.hidden) loadBoard(true);
}, 3500);

/* keep the chat sidebar fresh — unattended runs (board/schedule/event) create or append
   threads server-side with no browser to record them, so without this an open Chat tab never
   shows them until reload. loadChatList() no-ops unless the list actually changed and preserves
   scroll/selection, so this poll is invisible while you read. */
setInterval(()=>{
  const cv=document.getElementById("chatview");
  if(cv && !cv.hidden) loadChatList();
}, 5000);

/* keep the schedules list fresh while it's open — the job list itself doesn't
   change from this tab, but last_run / next_run do as jobs fire in the background */
setInterval(()=>{
  const sv=document.getElementById("schedulesview");
  if(!sv || sv.hidden) return;
  const jf=document.getElementById("job-form");
  if(jf && jf.innerHTML.trim()) return; // don't clobber an open add/edit form
  loadJobs(true);
}, 15000);

/* connect + load capabilities */
(async function(){
  try {
    await refreshHealth();   // sets TEMPORAL + loads the repo picker; re-checked on every submit
    const caps = await (await fetch("/api/capabilities")).json();
    applyCaps(caps);
    loadModelPicker();       // composer's model-override select; independent of TEMPORAL/caps
  } catch(e){}
  greeting(); render(); input.focus();
  applyMood();             // first paint: he is on screen before any poll or run lands
  const nc=document.getElementById("new-chat"); if(nc) nc.addEventListener("click", newChat);
  loadChatList();
  // restore the tab the user was last on
  try { const saved=localStorage.getItem("ottoTab"); if(saved && saved!=="chat") activateTab(saved); } catch(e){}
  openDeepLink();   // a #run=<wid> from a push notification wins over the remembered tab
  // If a turn was still running when the page was last open, reattach to it — the workflow
  // kept going server-side. Open the most-recent chat that still has a live run_id.
  if(TEMPORAL) restoreLiveRun();
})();

async function restoreLiveRun(){
  let data; try { data=await (await fetch("/api/chats")).json(); } catch(e){ return; }
  const live=(data.chats||[]).find(c=>c.run_id);   // list is newest-first
  if(live) openChat(live.id);
}
