"use strict";
/* Shared primitives, loaded before every view module below: three or more of them use each
   of these, so a view never has to depend on another view for a formatting helper. */
/* `'` is escaped too: the tree happens to quote every attribute with `"`, so a single quote was
   safe by coincidence rather than by rule — one single-quoted attribute would have broken out. */
function esc(s){ return (s||"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }

function val(id){ return document.getElementById(id).value.trim(); }

/* Find one element by a data-* VALUE, comparing the attribute rather than building a selector.
   Interpolating CSS.escape into a quoted attribute selector is the forbidden form (ui.md, and
   a grep guard in UiAssetLayoutTests): CSS.escape is for IDENTIFIERS
   and injects backslashes a quoted attribute value does not have, so the match silently fails
   the moment a value stops being [a-z0-9-]. Same shape as tabs.js's focusPendingRun.
   `sel` narrows WHICH element type to match — several rows key a textarea and the button that
   saves it on the same attribute, so without it the answer is whichever comes first in the DOM. */
function byData(root, attr, value, sel){
  const els=(root||document).querySelectorAll((sel||"")+"["+attr+"]");
  for(const e of els){ if(e.getAttribute(attr)===value) return e; }
  return null;
}

/* Make click-only toggles keyboard-operable, in ONE place rather than in the ~20 templates
   that emit them. A <span>/<div> with a click handler is unreachable by keyboard and announces
   as nothing, which is how every collapsible section header on Admin/Events/Jobs/Audit and the
   board's attempt headers were built. Rewriting each template to a <button> would fight the
   flex layouts they sit in, so the affordance is added at bind time instead: role + tabindex
   make it focusable and announced, and Enter/Space forward to the element's existing click
   listeners (delegated ones included, since .click() bubbles).

   `aria-expanded` is synced after the handlers run, not during: several of these toggles are
   bound by delegation on an ancestor, so the class flip happens on the way up and reading it
   inline would report the state the section just left. */
function enhanceToggles(root, sel, box, openClass){
  // `openClass` is the class on `box` meaning EXPANDED. The two conventions in the tree are
  // opposite spellings of one state — `.asection.collapsed` and `.dbgatt.open` — so the
  // inversion lives here rather than at each call site.
  const inverted = openClass==="collapsed";
  (root||document).querySelectorAll(sel).forEach(t=>{
    if(t.dataset.kbd) return;                  // idempotent: renders re-run over live nodes
    t.dataset.kbd="1";
    t.setAttribute("role","button");
    t.setAttribute("tabindex","0");
    const sync=()=>{
      const b=t.closest(box);
      if(!b) return;
      const has=b.classList.contains(openClass);
      t.setAttribute("aria-expanded", String(inverted ? !has : has));
    };
    sync();
    t.addEventListener("click",()=>setTimeout(sync,0));
    t.addEventListener("keydown",e=>{
      if(e.key==="Enter"||e.key===" "){ e.preventDefault(); t.click(); }
    });
  });
}

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
