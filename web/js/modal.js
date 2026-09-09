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
  m.hidden=false;
  return body;
}
function closeFormModal(){
  document.getElementById("formModal").hidden=true;
  document.getElementById("formModalBody").innerHTML="";   // drop stale inputs + their listeners
}
document.getElementById("formModalClose").addEventListener("click",closeFormModal);
document.getElementById("formModal").addEventListener("click",e=>{ if(e.target.id==="formModal") closeFormModal(); });
document.addEventListener("keydown",e=>{ if(e.key==="Escape" && !document.getElementById("formModal").hidden) closeFormModal(); });
