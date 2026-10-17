"use strict";
/* ---- config-form modal (Schedules + Events "edit"/"configure") ----
   Returns the body element the caller fills, so a form function keeps its existing shape: it wrote
   into an inline <div id="…-form">, now it writes into here. closeFormModal() is what every cancel
   and every successful save calls. */
function openFormModal(title){
  const m=document.getElementById("formModal");
  document.getElementById("formModalTitle").innerHTML=title;
  const body=document.getElementById("formModalBody");
  body.innerHTML="";
  // The box is shared, so reset the widened variant the runbook editor opts into — otherwise the
  // next small form (an event rule, a param prompt) inherits its width and looks broken.
  m.querySelector(".modalBox").classList.remove("wideModalBox");
  FORM_MODAL_THEN=null;   // a close hook belongs to the session it was armed in, never the next one
  FORM_MODAL_SESSION++;
  m.hidden=false;
  return body;
}
function closeFormModal(){
  document.getElementById("formModal").hidden=true;
  document.getElementById("formModalBody").innerHTML="";   // drop stale inputs + their listeners
  const then=FORM_MODAL_THEN, sess=FORM_MODAL_SESSION; FORM_MODAL_THEN=null;
  // Deferred past the closing form's own follow-up (its reload) — and dropped if that follow-up
  // opened another modal meanwhile, so the hook never paints over it.
  if(then) setTimeout(()=>{ if(sess===FORM_MODAL_SESSION) then(); },0);
}
/* One-shot: run `fn` when the form open NOW closes, saved or cancelled — how a setup guide hands
   off to a card's own configure form and comes back to re-check it. Arm it AFTER the form opens:
   any openFormModal clears it, so a form replaced by another never fires a stale hook. */
let FORM_MODAL_THEN=null, FORM_MODAL_SESSION=0;
function afterFormModal(fn){ FORM_MODAL_THEN=fn; }
document.getElementById("formModalClose").addEventListener("click",closeFormModal);
document.getElementById("formModal").addEventListener("click",e=>{ if(e.target.id==="formModal") closeFormModal(); });
document.addEventListener("keydown",e=>{ if(e.key==="Escape" && !document.getElementById("formModal").hidden) closeFormModal(); });
