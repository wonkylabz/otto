"use strict";
/* Shared primitives, loaded before every view module below: three or more of them use each
   of these, so a view never has to depend on another view for a formatting helper. */
function esc(s){ return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

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
