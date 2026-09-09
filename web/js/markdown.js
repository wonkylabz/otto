"use strict";
/* Markdown rendering for agent output. Its own file because both the chat surface and the
   board/audit run views render agent text, and neither owns the other. */
/* Minimal, safe markdown -> HTML for agent output. Escapes first (output is untrusted),
   then applies a small subset: fenced/inline code, headings, bold/italic, links, lists,
   blockquotes, hr. Code blocks are pulled out before escaping so their contents render
   verbatim. */
function renderMD(src){
  src = String(src==null ? "" : src);
  const blocks = [];
  src = src.replace(/```[ \t]*([\w.-]*)\r?\n?([\s\S]*?)```/g, (_,lang,code)=>{
    blocks.push(`<pre><code>${esc(code.replace(/\n$/,""))}</code></pre>`);
    // The placeholder must be a sequence no model output can contain and no HTML escape
    // rewrites. U+E000 is a private-use code point, written as an escape so this file stays
    // plain ASCII - it used to be a literal NUL byte, which defeated every string-matching
    // editor and forced `grep -a` on the whole page.
    return `\uE000B${blocks.length-1}\uE000`;
  });
  src = esc(src);
  const inline = s => s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|\s)(https?:\/\/[^\s<]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
  let html = "", list = null;
  const closeList = () => { if(list){ html += `</${list}>`; list = null; } };
  const splitRow = r => r.trim().replace(/^\|/,"").replace(/\|$/,"").split("|").map(c=>c.trim());
  const isTableSep = r => r!=null && r.includes("|") && r.includes("-") && /^[\s:|-]+$/.test(r.trim());
  const lines = src.split("\n");
  for(let i=0; i<lines.length; i++){
    const raw = lines[i];
    const ph = raw.match(/^\uE000B(\d+)\uE000$/);
    if(ph){ closeList(); html += blocks[+ph[1]]; continue; }
    const line = raw.replace(/\s+$/,"");
    let m;
    // A blank line does NOT end a list. Every plan step is a paragraph with a blank line after
    // it, so closing here made each step its own <ol> and all eleven rendered as "1." Every
    // other branch already closes the list when the content stops being list items, which is
    // what actually ends one.
    if(!line.trim()){ continue; }
    // GFM table: a header row of |-separated cells, then a |---|---| divider, then body rows
    if(line.includes("|") && isTableSep(lines[i+1])){
      const aligns = splitRow(lines[i+1]).map(c=>{ const l=c.startsWith(":"), rt=c.endsWith(":");
        return l&&rt?"center":rt?"right":l?"left":""; });
      const headers = splitRow(line);
      const cs = ci => aligns[ci] ? ` style="text-align:${aligns[ci]}"` : "";
      let t = "<table><thead><tr>";
      headers.forEach((h,ci)=>{ t += `<th${cs(ci)}>${inline(h)}</th>`; });
      t += "</tr></thead><tbody>";
      i += 2;
      for(; i<lines.length; i++){
        const r = lines[i];
        if(!r.trim() || !r.includes("|")){ i--; break; }
        const cells = splitRow(r);
        t += "<tr>" + headers.map((_,ci)=>`<td${cs(ci)}>${inline(cells[ci]||"")}</td>`).join("") + "</tr>";
      }
      t += "</tbody></table>";
      closeList(); html += t; continue;
    }
    if(/^\s*(---|\*\*\*|___)\s*$/.test(line)){ closeList(); html += "<hr>"; continue; }
    if(m = line.match(/^(#{1,6})\s+(.*)$/)){ closeList(); const lv=m[1].length; html += `<h${lv}>${inline(m[2])}</h${lv}>`; continue; }
    if(m = line.match(/^\s*&gt;\s?(.*)$/)){ closeList(); html += `<blockquote>${inline(m[1])}</blockquote>`; continue; }   // '>' is &gt; post-escape
    if(m = line.match(/^\s*[-*+]\s+(.*)$/)){ if(list!=="ul"){ closeList(); html+="<ul>"; list="ul"; } html += `<li>${inline(m[1])}</li>`; continue; }
    // `start` from the first item's own number: a plan that resumes at "4." after a paragraph
    // would otherwise restart the count at 1 and silently renumber the steps a human approves.
    if(m = line.match(/^\s*(\d+)[.)]\s+(.*)$/)){ if(list!=="ol"){ closeList(); html += m[1]==="1" ? "<ol>" : `<ol start="${+m[1]}">`; list="ol"; } html += `<li>${inline(m[2])}</li>`; continue; }
    closeList(); html += `<p>${inline(line)}</p>`;
  }
  closeList();
  return html;
}
