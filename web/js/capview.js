"use strict";
// A save re-renders the whole panel, so the capability list's search, open groups and scroll
// are carried across it — else every edit collapses the list and drops the filter.
function capViewState(el){
  const q=el.querySelector("#cap-search");
  if(!q) return null;
  const open={};
  el.querySelectorAll("#cap-list .agroup, #cap-list .asub").forEach(g=>{
    open[capViewKey(g)]=!g.classList.contains("collapsed");
  });
  return {q:q.value, open, scroll:el.scrollTop};
}
function capViewKey(g){
  return g.dataset.grp ? "g:"+g.dataset.grp : "s:"+g.dataset.sub;
}
function restoreCapView(el, view){
  if(!view) return;
  const q=el.querySelector("#cap-search");
  if(q && view.q){ q.value=view.q; q.dispatchEvent(new Event("input")); }
  el.querySelectorAll("#cap-list .agroup, #cap-list .asub").forEach(g=>{
    const k=capViewKey(g);
    if(k in view.open) g.classList.toggle("collapsed",!view.open[k]);
  });
  el.scrollTop=view.scroll;
}
