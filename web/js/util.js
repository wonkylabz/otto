"use strict";
/* Shared primitives, loaded before every view module below: three or more of them use each
   of these, so a view never has to depend on another view for a formatting helper. */
/* `'` is escaped too: the tree happens to quote every attribute with `"`, so a single quote was
   safe by coincidence rather than by rule — one single-quoted attribute would have broken out. */
function esc(s){ return (s||"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }

function val(id){ return document.getElementById(id).value.trim(); }

/* Card timestamps: "14:06" today, "29 Jul 14:06" otherwise — the full ISO stays in the title. */
function shortWhen(iso){
  if(!iso) return "";
  const d=new Date(iso); if(isNaN(d)) return iso;
  const t=d.toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"});
  return d.toDateString()===new Date().toDateString() ? t
    : d.toLocaleDateString([],{day:"numeric",month:"short"})+" "+t;
}
function fmtDur(ms){ ms=Math.max(0,ms); if(ms<1000) return Math.round(ms)+" ms"; const s=ms/1000; if(s<60) return s.toFixed(1)+" s"; const m=Math.floor(s/60),r=Math.round(s%60); return m+" m "+r+" s"; }

// Size a textarea to its content, bounded. For the one-per-line code fields: an argv of ten
// lines in a three-line box is a scrollbar the operator has to fight to read what they are
// approving. `scrollHeight` is 0 under `display:none`, so this must run while the field is
// VISIBLE — inside an open modal, never before it is shown (ui.md).
function growArea(el, maxPx){
  if(!el) return;
  const cap=maxPx||320;
  const fit=()=>{ el.style.height="auto"; el.style.height=Math.min(Math.max(el.scrollHeight+2, 96), cap)+"px"; };
  el.addEventListener("input", fit);
  fit();
}
