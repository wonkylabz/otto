"use strict";
/* Chat attachments (#161). Each file uploads the moment it is picked, dropped or pasted, so a
   send only names ids the server already holds; the chips are the composer's pending set. */
const ATT_CLIP=`<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 9.5l-6.1 6.1a3.9 3.9 0 01-5.5-5.5l6.4-6.4a2.6 2.6 0 013.7 3.7L8.1 13.8a1.3 1.3 0 01-1.9-1.9l5.7-5.7"/></svg>`;
const ATT_INLINE=new Set(["image/png","image/jpeg","image/gif","image/webp"]);
let pendingAtts=[];   // [{key, name, status:"up"|"ok"|"err", meta, error}]
const attachBar=document.getElementById("attachbar");
const attachInput=document.getElementById("attachinput");

function attSize(n){ if(n==null) return ""; if(n<1024) return n+" B"; if(n<1048576) return (n/1024).toFixed(1)+" KB"; return (n/1048576).toFixed(1)+" MB"; }

function attachFiles(files){
  [...(files||[])].forEach(file=>{
    const a={key:Math.random().toString(36).slice(2), name:file.name||"pasted-image.png", status:"up"};
    pendingAtts.push(a); renderAttachBar();
    postFile("/api/attachments", file, a.name)
      .then(meta=>{ a.status="ok"; a.meta=meta; })
      .catch(e=>{ a.status="err"; a.error=e.message; toast("Couldn't attach "+a.name+": "+e.message); })
      .finally(renderAttachBar);
  });
}

function renderAttachBar(){
  attachBar.hidden=!pendingAtts.length;
  attachBar.innerHTML=pendingAtts.map(a=>`<span class="attchip ${a.status==="ok"?"":a.status}" data-key="${esc(a.key)}" title="${esc(a.error||a.name)}">`
    +(a.status==="up"?"⏳":ATT_CLIP)+`<span>${esc(a.name)}${a.meta?" · "+esc(attSize(a.meta.size)):""}</span>`
    +`<button type="button" aria-label="Remove ${esc(a.name)}">×</button></span>`).join("");
}

/* The ids to send with this message, clearing the set. null = not ready (still uploading). */
function takeAttachments(){
  if(pendingAtts.some(a=>a.status==="up")){ toast("Still uploading an attachment — send again in a moment."); return null; }
  const metas=pendingAtts.filter(a=>a.status==="ok").map(a=>a.meta);
  pendingAtts=[]; renderAttachBar();
  return metas;
}

/* A sent message's files: raster images as thumbnails, everything else as a download chip.
   An expired upload 404s, so its thumbnail falls back to a plain chip. */
function attsHTML(atts){
  if(!atts||!atts.length) return "";
  return `<div class="atts">`+atts.map(a=>{
    const href="/api/attachments/"+encodeURIComponent(a.id);
    const chip=`<a class="attchip" href="${esc(href)}" download="${esc(a.name)}">${ATT_CLIP}<span>${esc(a.name)}${a.size!=null?" · "+esc(attSize(a.size)):""}</span></a>`;
    return ATT_INLINE.has(a.type)
      ? `<a href="${esc(href)}" target="_blank" rel="noopener" title="${esc(a.name)}"><img src="${esc(href)}" alt="${esc(a.name)}" loading="lazy" data-chip="${esc(chip)}"></a>`
      : chip;
  }).join("")+`</div>`;
}

document.getElementById("attachbtn").addEventListener("click",()=>attachInput.click());
attachInput.addEventListener("change",()=>{ attachFiles(attachInput.files); attachInput.value=""; });
attachBar.addEventListener("click",e=>{
  const chip=e.target.closest("button") && e.target.closest(".attchip"); if(!chip) return;
  pendingAtts=pendingAtts.filter(a=>a.key!==chip.dataset.key); renderAttachBar();
});
document.getElementById("input").addEventListener("paste",e=>{
  const files=e.clipboardData && e.clipboardData.files;
  if(files && files.length){ e.preventDefault(); attachFiles(files); }
});
(()=>{
  const convo=document.querySelector(".convo"), composer=document.querySelector(".composer");
  const hasFiles=e=>e.dataTransfer && [...e.dataTransfer.types].includes("Files");
  convo.addEventListener("dragover",e=>{ if(!hasFiles(e)) return; e.preventDefault(); composer.classList.add("dropping"); });
  convo.addEventListener("dragleave",e=>{ if(!convo.contains(e.relatedTarget)) composer.classList.remove("dropping"); });
  convo.addEventListener("drop",e=>{ if(!hasFiles(e)) return; e.preventDefault(); composer.classList.remove("dropping"); attachFiles(e.dataTransfer.files); });
  // An expired thumbnail becomes the chip it would have been, so the bubble still says what was sent.
  document.getElementById("stream").addEventListener("error",e=>{
    const img=e.target; if(img.tagName!=="IMG"||!img.dataset.chip) return;
    const link=img.parentElement; link.outerHTML=img.dataset.chip.replace("</span>"," (expired)</span>");
  }, true);
})();
