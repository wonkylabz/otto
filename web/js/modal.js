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

/* ---- in-app confirm / prompt ----
   Native confirm()/alert()/prompt() are banned: Chrome can return false in 0ms without ever
   painting the dialog, so a destructive button silently does nothing. Resolves true/false (or the
   text / null). Its own overlay, above formModal, so asking never wipes an open form. */
let ASK_DONE=null, ASK_FROM=null;
document.body.insertAdjacentHTML("beforeend",`<div class="modalOverlay askOverlay" id="askModal" hidden>
  <div class="modalBox askBox" role="dialog" aria-modal="true"><div class="modalBody">
    <p class="askMsg" id="askMsg"></p>
    <textarea class="askInput" id="askInput" rows="4" hidden></textarea>
    <div class="askActions"><button class="btn decline sm" id="askCancel">Cancel</button><button class="btn approve sm" id="askOk">OK</button></div>
  </div></div></div>`);
function askModal(msg, {ok="OK", danger=false, text=null}={}){
  if(ASK_DONE) ASK_DONE(null);   // a second ask supersedes an unanswered one
  const m=document.getElementById("askModal"), okBtn=document.getElementById("askOk");
  const input=document.getElementById("askInput");
  document.getElementById("askMsg").textContent=msg;
  okBtn.textContent=ok;
  okBtn.className="btn sm "+(danger?"danger":"approve");
  input.hidden=text===null; input.value=text??"";
  ASK_FROM=ASK_FROM||document.activeElement;
  m.hidden=false;
  (text===null?okBtn:input).focus();
  return new Promise(resolve=>{
    ASK_DONE=v=>{ ASK_DONE=null; m.hidden=true; resolve(v);
      const from=ASK_FROM; ASK_FROM=null; if(from && from.isConnected) from.focus(); };
  });
}
function askAnswer(yes){
  if(!ASK_DONE) return;
  const input=document.getElementById("askInput");
  ASK_DONE(yes ? (input.hidden ? true : input.value) : (input.hidden ? false : null));
}
function askConfirm(msg, opts){ return askModal(msg, opts); }
function askText(msg, value, opts){ return askModal(msg, {...opts, text:value||""}); }
document.getElementById("askOk").addEventListener("click",()=>askAnswer(true));
document.getElementById("askCancel").addEventListener("click",()=>askAnswer(false));
document.getElementById("askModal").addEventListener("click",e=>{ if(e.target.id==="askModal") askAnswer(false); });
document.addEventListener("keydown",e=>{
  if(!ASK_DONE) return;
  if(e.key==="Escape"){ e.stopImmediatePropagation(); askAnswer(false); }
  else if(e.key==="Tab"){   // trap focus: Space on a control under the scrim would still fire it
    const f=[...document.querySelectorAll("#askModal textarea:not([hidden]), #askModal button")];
    const i=f.indexOf(document.activeElement);
    e.preventDefault(); f[(i+(e.shiftKey?-1:1)+f.length)%f.length].focus(); }
  else if(e.key==="Enter" && document.getElementById("askInput").hidden){ e.preventDefault(); askAnswer(true); }
}, true);
