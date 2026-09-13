"use strict";
/* ---- events tab: Integrations (Slack, GitHub board, Webhooks) ---- */
let EVENT_RULES=[], EVENT_CAPS=[];
/* Collapsible integration sections default OPEN (unlike Admin's asections, this tab only ever
   holds 3 of them) — the key stores what the user explicitly CLOSED, not what they opened.
   bindEventSections binds via property assignment (idempotent) since loadEvents() fully
   re-renders #eventsview on every tab visit and after every rule edit; addEventListener here
   would stack a new listener on the persistent element each time. */
const EVSECTS="otto.events.sections";
function evCollapsed(){ try{ return JSON.parse(localStorage.getItem(EVSECTS))||{}; } catch(e){ return {}; } }
function evSetCollapsed(id,closed){
  const st=evCollapsed(); if(closed) st[id]=1; else delete st[id];
  try{ localStorage.setItem(EVSECTS,JSON.stringify(st)); }catch(e){}
}
/* Whether the per-model token breakdown is expanded. Collapsed by default and remembered:
   the totals above it always show, so nothing is hidden that the header did not already say. */
let TOK_OPEN=false;
try { TOK_OPEN = localStorage.getItem("ottoTokOpen")==="1"; } catch(e){}

/* An integration's header badge summarises the feature inside it — the child sections load
   async, so each one writes its parent's badge as it renders (one writer per badge). */
/* GitHub holds two independent features (board queue, PR reviews), so its section badge is a
   ROLL-UP of both: a warning on either wins (it names the thing that needs attention), otherwise
   "on" if either is polling. Without this the second feature to render would clobber the first's
   badge and the header would report whichever one happened to load last. */
let GH_BADGE={board:{cls:"off",label:"off"}, pr:{cls:"off",label:"off"}};
function setGithubBadge(which,cls,label){
  GH_BADGE[which]={cls,label};
  const rank={off:0,on:1,warn:2}, b=GH_BADGE.board, p=GH_BADGE.pr;
  const worst=(rank[b.cls]||0)>=(rank[p.cls]||0)?b:p;
  const anyOn=b.cls==="on"||p.cls==="on";
  setIntegBadge("github-badge", worst.cls, worst.cls==="warn"?worst.label:(anyOn?"on":"off"));
}
function setIntegBadge(id,cls,label){
  const b=document.getElementById(id); if(!b) return;
  b.className="badge "+cls; b.textContent=label;
}
function bindEventSections(el){
  el.onclick=e=>{
    const t=e.target.closest(".secttoggle");
    if(t){ const sec=t.closest(".asection"); evSetCollapsed(sec.dataset.sect, sec.classList.toggle("collapsed")); return; }
    const b=e.target.closest(".asection.coll h3 button");
    if(b){ const sec=b.closest(".asection");
      if(sec && sec.classList.contains("collapsed")){ sec.classList.remove("collapsed"); evSetCollapsed(sec.dataset.sect,false); } }
  };
}
async function loadEvents(){
  const el=document.getElementById("eventsview");
  el.innerHTML=`<p class="sub">loading…</p>`;
  let data;
  try { data=await (await fetch("/api/event-rules")).json(); }
  catch(e){ el.innerHTML=`<p class="err">Couldn't load event rules (${esc(e.message)}).</p>`; return; }
  EVENT_RULES=data.rules||[]; EVENT_CAPS=data.caps||[];
  const activeRules=EVENT_RULES.filter(r=>r.enabled!==false).length;
  const whBadge = !data.enabled ? '<span class="badge warn">no secret</span>'
    : (activeRules ? '<span class="badge on">on</span>' : '<span class="badge off">off</span>');
  const cards = EVENT_RULES.map((r,i)=>`<div class="job ${r.enabled===false?'off':''}">
    <div class="jtop"><span class="switch ${r.enabled===false?'':'on'}" data-togglerule="${i}" title="enable / disable"></span>
      <span class="jcron">/api/events/${esc(r.source||'?')}</span>
      <div class="jactions"><button class="addbtn" data-editrule="${i}">edit</button>
        <button class="remove" data-delrule="${i}" title="remove">&times;</button></div></div>
    <div class="jreq">${esc(r.template||'')}</div>
    <div class="jstatus">${r.cap?`&rarr; <b>${esc(r.cap)}</b>`:'auto-route'} ·
      ${({auto:'<span class="warn">auto-approves writes</span>',ask:'writes need Board approval',skip:'writes skipped'})[r.approval||(r.auto_approve?'auto':'skip')]}
      ${r.when&&Object.keys(r.when).length?` · when ${esc(Object.entries(r.when).map(([k,v])=>k+'='+v).join(', '))}`:''}
      ${r.reply_to&&r.reply_to.url?` · reply &rarr; ${esc(r.reply_to.url)}`:''}</div>
  </div>`).join("");
  const closed=evCollapsed();
  el.innerHTML=`<div class="phead"><h1>Events</h1>
      <p class="sub">Ingresses that bring work in without you.</p></div>
    <div class="phead sec"><h2>Integrations</h2>
      <p class="sub">Slack, GitHub, and inbound webhooks &mdash; each connects, configures, and toggles independently below.</p></div>
    <div class="asection coll${closed['ev-slack']?' collapsed':''}" data-sect="ev-slack">
      <h3><span class="secttoggle" title="collapse / expand"><span class="gcaret">&#9662;</span>Slack<span class="badge off" id="slack-badge">off</span></span></h3>
      <div class="asection-body"><div id="slack-section"></div></div>
    </div>
    <div class="asection coll${closed['ev-github']?' collapsed':''}" data-sect="ev-github">
      <h3><span class="secttoggle" title="collapse / expand"><span class="gcaret">&#9662;</span>GitHub<span class="badge off" id="github-badge">off</span></span></h3>
      <div class="asection-body"><div id="board-queue-section"></div><div id="pr-review-section"></div></div>
    </div>
    <div class="asection coll${closed['ev-webhooks']?' collapsed':''}" data-sect="ev-webhooks">
      <h3><span class="secttoggle" title="collapse / expand"><span class="gcaret">&#9662;</span>Webhooks${whBadge}<span class="sectcount">${EVENT_RULES.length} rule${EVENT_RULES.length===1?'':'s'}</span></span>
        <button class="addbtn addnew" id="add-rule">+ Add event rule</button></h3>
      <div class="asection-body">
        <p class="sub" style="margin:10px 0 10px">Turn any inbound webhook into an unattended run &mdash; the first matching enabled rule runs it. <b>POST</b> to <code title="Send X-Otto-Timestamp (unix seconds) and X-Otto-Signature = hex HMAC-SHA256 of &lt;timestamp&gt;.&lt;raw body&gt; under OTTO_EVENT_SECRET. Both headers are required; the timestamp must be within 5 minutes.">${esc(location.origin)}/api/events/&lt;source&gt;</code>. Both <code>X-Otto-Timestamp</code> and <code>X-Otto-Signature</code> are required &mdash; sign <code>&lt;timestamp&gt;.&lt;raw body&gt;</code>.</p>
        ${!data.enabled?'<p class="err">Disabled — set <code>OTTO_EVENT_SECRET</code> and restart. Rules can still be authored.</p>':''}
        ${cards || '<p class="memempty">No rules yet.</p>'}
      </div>
    </div>`;
  el.querySelectorAll("[data-togglerule]").forEach(s=>s.addEventListener("click",async()=>{
    const r=EVENT_RULES[+s.dataset.togglerule];
    r.enabled = r.enabled===false;          // absent/true -> false, false -> true
    await saveRules(); loadEvents();
  }));
  el.querySelectorAll("[data-editrule]").forEach(b=>b.addEventListener("click",()=>showRuleForm(+b.dataset.editrule)));
  el.querySelectorAll("[data-delrule]").forEach(b=>b.addEventListener("click",async()=>{
    if(!confirm("Remove this rule?")) return;
    EVENT_RULES.splice(+b.dataset.delrule,1); await saveRules(); loadEvents();
  }));
  document.getElementById("add-rule").addEventListener("click",()=>showRuleForm(-1));
  bindEventSections(el);
  loadSlackConfig();
  loadBoardQueue();
  loadPrReviews();
}

/* ---- Slack auto-answer listener (a pull ingress; polls Slack as you) ---- */
let SLACK_CFG={}, SLACK_CAPS=[];
/* An allowlist entry may carry a trailing label ("U01ABCDE2FG #alex") — ids are opaque, so the
   label is stored verbatim and stripped wherever the id is used. Mirrors slack.entry_id(). */
const slackIds=a=>(a||[]).map(s=>String(s).replace(/[#;].*$/,"").trim().split(/\s+/)[0]).filter(Boolean);
async function loadSlackConfig(){
  const host=document.getElementById("slack-section");
  if(!host) return;
  let d;
  try { d=await (await fetch("/api/slack-config")).json(); }
  catch(e){ host.innerHTML=`<p class="err">Couldn't load Slack config (${esc(e.message)}).</p>`; return; }
  SLACK_CFG=d.config||{}; SLACK_CAPS=d.caps||[];
  const c=SLACK_CFG, poll=d.poll||{};
  let status, badgeCls, badgeLabel;
  if(!d.token_set){ status=`<span class="warn">no token</span> — set <code>OTTO_SLACK_USER_TOKEN</code> (a Slack user token, <code>xoxp-…</code>) and restart`; badgeCls='off'; badgeLabel='no token'; }
  else if(!d.temporal){ status=`<span class="warn">needs Temporal (run via ./run.sh)</span>`; badgeCls='warn'; badgeLabel='no Temporal'; }
  else if(!c.enabled){ status=`<span class="warn">disabled</span> — enable to start listening`; badgeCls='off'; badgeLabel='off'; }
  else if(!poll.exists){ status=`enabled, but no poll schedule yet — restart the server to create it`; badgeCls='warn'; badgeLabel='starting…'; }
  else if(poll.failing){ status=`enabled, but every poll is failing &mdash; see below`; badgeCls='warn'; badgeLabel='failing'; }
  else if(d.scopes && d.scopes.known && (d.scopes.missing||[]).length){
    status=`enabled, but the token can't read anything &mdash; see below`; badgeCls='warn'; badgeLabel='no scopes'; }
  else {
    status=`listening as <b>${esc(d.self||'you')}</b>, polling every <b>${esc(""+(c.poll_seconds||60))}s</b>`
        +(poll.paused?` · <span class="warn">paused</span>`:``)
        +(poll.next_run?` · next <b title="${esc(poll.next_run)}">${esc(shortWhen(poll.next_run))}</b>`:``)
        +(poll.last_run?` · last <span title="${esc(poll.last_run)}">${esc(shortWhen(poll.last_run))}</span>`:` · not run yet`);
    badgeCls=poll.paused?'warn':'on'; badgeLabel=poll.paused?'paused':'on';
  }
  const nUsers=slackIds(c.allow_users).length, nChans=slackIds(c.allow_channels).length;
  const appr=({ask:'writes need Board approval',auto:'<span class="warn">auto-approves writes</span>',skip:'writes skipped'})[c.approval_default||'ask'];
  const closed=evCollapsed()['ev-slack-answer'];
  /* The parent "Slack" badge covers BOTH identities: one is enough to say the integration is on,
     and a badge reading "off" while the bot is answering in a channel is worse than no badge.
     Its own variables, NOT badgeCls/badgeLabel: those belong to the Auto-answer subsection two
     lines below, and promoting the parent by reassigning them made Auto-answer claim "on" while
     its own card said "no token" and its Enabled box was unchecked (reported with a screenshot).
     A summary badge may READ its parts; it must never write to them. */
  const botOn = !!(c.bot_enabled && d.bot_token_set);
  const intCls = (badgeCls==='on' || botOn) ? 'on' : badgeCls;
  const intLabel = (badgeCls==='on' || botOn) ? 'on' : badgeLabel;
  setIntegBadge("slack-badge",intCls,intLabel);
  host.innerHTML=`<div class="asection coll subsection${closed?' collapsed':''}" data-sect="ev-slack-answer">
    <h3><span class="secttoggle" title="collapse / expand"><span class="gcaret">&#9662;</span>Auto-answer<span class="badge ${badgeCls}">${esc(badgeLabel)}</span></span>
      <button class="addbtn" id="slack-edit">configure</button></h3>
    <div class="asection-body">
      <p class="sub" style="margin:10px 0 10px">Answers allowlisted DMs and @-mentions on your behalf — acks, runs unattended, replies in-thread. Reads auto-answer; ${appr}.</p>
      <div class="job ${c.enabled?'':'off'}"><div class="jtop"><span class="switch ${c.enabled?'on':''} ${d.token_set?'':'locked'}" ${d.token_set?'data-toggleslack="1"':''}
          title="${d.token_set?'enable / disable':'needs OTTO_SLACK_USER_TOKEN before it can be enabled'}"></span>
        <span class="jcron">Slack listener</span></div>
        <div class="jstatus">${status}</div>
        ${scopeWarning(d.scopes)}${scopeNote(d.scopes)}${pollFailureNote(poll)}
        <div class="jstatus">allowlist: <b>${nUsers}</b> user(s), <b>${nChans}</b> channel(s)
          · watch: ${c.watch_dms!==false?'DMs':''}${(c.watch_dms!==false&&c.watch_mentions!==false)?' + ':''}${c.watch_mentions!==false?'mentions':''}${(c.watch_dms===false&&c.watch_mentions===false)?'nothing':''}
          ${c.cap?` · pinned &rarr; <b>${esc(c.cap)}</b>`:''}</div>
      </div>
    </div>
  </div>`+slackBotCard(d,c,poll);
  document.getElementById("slack-edit").addEventListener("click",showSlackForm);
  const sw=host.querySelector("[data-toggleslack]");
  if(sw) sw.addEventListener("click",async()=>{
    // enabling with no allowlisted user/channel would listen to nobody — same guard the form applies
    if(!c.enabled && !nUsers && !nChans && !c.allow_self){
      alert("Add at least one allowed user or channel (or turn on test mode) in configure first."); return; }
    await fetch("/api/slack-config",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({config:{...c, enabled:!c.enabled}})});
    loadSlackConfig();
  });
  wireSlackBotCard(host,d,c);
}

/* A token that is missing a scope polls happily and answers nobody: Slack refuses each call with
   `missing_scope` and the poll still "succeeds", so the card would say "listening" while both
   inbound paths are dead. Found on the first real install. Rendered ABOVE the status line, because
   it is the reason the status line is lying. */
function scopeWarning(g){
  if(!g || !g.known || !g.missing || !g.missing.length) return "";
  const rows=g.missing.map(([s,why])=>`<code>${esc(s)}</code> &mdash; ${esc(why)}`).join("<br>");
  return `<div class="jstatus"><span class="warn">missing Slack scope(s)</span> &mdash; it polls but
    can read nothing, so nothing is ever answered. Add these to the token in <b>OAuth &amp;
    Permissions &rarr; Scopes</b>, then <b>reinstall the app</b> and restart Otto:<br>${rows}</div>`;
}
/* A poll that FIRES but fails every time looks exactly like a quiet Slack — "listening, next run
   in 40s" while nothing is ever answered. Shown on both identity cards: one activity serves both,
   so when it breaks, both are down. */
function pollFailureNote(poll){
  if(!poll || !poll.failing) return "";
  return `<div class="jstatus"><span class="warn">the poll is failing</span> &mdash; the last
    ${esc(""+(poll.recent_checked||0))} runs all ended ${esc(String(poll.last_failure||"badly").toLowerCase())},
    so nothing is being answered even though the schedule is live. Check the worker log
    (<code>/tmp/otto-worker.log</code>) for the traceback.</div>`;
}
function scopeNote(g){
  if(!g || !g.known || !g.optional || !g.optional.length) return "";
  return `<div class="jstatus sub">not granted: ${g.optional.map(([s])=>`<code>${esc(s)}</code>`).join(", ")}
    &mdash; private channels / group DMs stay invisible; everything else works.</div>`;
}

/* Socket Mode is an OPTIONAL accelerator, so its absence is a one-line offer, not a warning: the
   bot works without it. Its presence is only worth a line when something is wrong with it. */
function socketNote(sk){
  if(!sk) return "";
  /* A connected socket that has never been sent an event: the green light that means nothing.
     Shown as a warning with the exact fix, because it is indistinguishable from working. */
  if(sk.idle_warning)
    return `<div class="jstatus"><span class="warn">socket is missing events</span> &mdash; ${esc(sk.idle_warning)}</div>`;
  if(sk.enabled && sk.connected) return "";
  if(sk.enabled && sk.error)
    return `<div class="jstatus"><span class="warn">socket</span> ${esc(sk.error)} &mdash; still answering on the poll.</div>`;
  if(!sk.enabled && sk.why)
    return `<div class="jstatus sub">replies arrive on the poll. For instant delivery: ${esc(sk.why)}.</div>`;
  return "";
}

/* The BOT identity — Otto answering under its own name, in channels it's been invited to and in
   DMs sent to it. A SEPARATE card from Auto-answer, not a checkbox inside it: the two have
   different tokens, different allowlists and different failure modes ("nobody can see the bot" is
   almost always "it hasn't been invited to the channel"), and one card mixing them made it
   ambiguous which account a reply would come from. */
function slackBotCard(d,c,poll){
  const nU=slackIds(c.bot_allow_users).length, nC=slackIds(c.bot_allow_channels).length;
  const nA=slackIds(c.bot_approvers).length;
  let st,bc,bl;
  if(!d.bot_token_set){ st=`<span class="warn">no token</span> &mdash; set <code>OTTO_SLACK_BOT_TOKEN</code> (a Slack <b>bot</b> token, <code>xoxb-&hellip;</code>) and restart`; bc='off'; bl='no token'; }
  else if(!d.temporal){ st=`<span class="warn">needs Temporal (run via ./run.sh)</span>`; bc='warn'; bl='no Temporal'; }
  else if(!c.bot_enabled){ st=`<span class="warn">disabled</span> &mdash; enable to start listening as the bot`; bc='off'; bl='off'; }
  else if(!poll.exists){ st=`enabled, but no poll schedule yet &mdash; restart the server to create it`; bc='warn'; bl='starting…'; }
  else if(poll.failing){ st=`enabled, but every poll is failing &mdash; see below`; bc='warn'; bl='failing'; }
  else if(d.bot_scopes && d.bot_scopes.known && (d.bot_scopes.missing||[]).length){
    st=`enabled, but the token can't read anything &mdash; see below`; bc='warn'; bl='no scopes'; }
  else {
    /* Socket Mode changes only WHEN the poll runs, so it is reported as the delivery speed of an
       already-working listener — never as a second thing that can be broken. Without it the bot
       still answers, just up to poll_seconds late, which is what the fallback text says. */
    const sk=d.socket||{};
    const speed = sk.enabled
      ? (sk.idle_warning ? `<span class="warn">socket missing events</span> &mdash; answering on the ${esc(""+(c.poll_seconds||60))}s poll`
         : sk.connected ? `<b>instant</b> <span class="sub">(socket connected)</span>`
         : `<span class="warn">socket reconnecting</span> &mdash; answering on the ${esc(""+(c.poll_seconds||60))}s poll meanwhile`)
      : `on the ${esc(""+(c.poll_seconds||60))}s poll`;
    st=`answering as <b>${esc(d.bot_self||'the bot')}</b>, ${speed}`
        +(poll.paused?` · <span class="warn">paused</span>`:``);
    bc='on'; bl=(sk.connected && !sk.idle_warning)?'instant':'on'; }
  const closed=evCollapsed()['ev-slack-bot'];
  return `<div class="asection coll subsection${closed?' collapsed':''}" data-sect="ev-slack-bot">
    <h3><span class="secttoggle" title="collapse / expand"><span class="gcaret">&#9662;</span>Bot user<span class="badge ${bc}">${esc(bl)}</span></span>
      <button class="addbtn" id="slackbot-edit">configure</button></h3>
    <div class="asection-body">
      <p class="sub" style="margin:10px 0 10px">Otto answers under its <b>own name</b> here &mdash; @-mentions in channels it's been invited to, and DMs sent to the bot. It never reads your DMs, and never claims to speak for you. Runs alongside Auto-answer on one poll.</p>
      <div class="job ${c.bot_enabled?'':'off'}"><div class="jtop"><span class="switch ${c.bot_enabled?'on':''} ${d.bot_token_set?'':'locked'}" ${d.bot_token_set?'data-toggleslackbot="1"':''}
          title="${d.bot_token_set?'enable / disable':'needs OTTO_SLACK_BOT_TOKEN before it can be enabled'}"></span>
        <span class="jcron">Slack bot</span></div>
        <div class="jstatus">${st}</div>
        ${scopeWarning(d.bot_scopes)}${scopeNote(d.bot_scopes)}${socketNote(d.socket)}${pollFailureNote(poll)}
        <div class="jstatus">allowlist: <b>${nU}</b> user(s) for DMs, <b>${nC}</b> channel(s)${nA?` · <b>${nA}</b> approver(s)`:``}
          · watch: ${c.bot_watch_dms!==false?'DMs to the bot':''}${(c.bot_watch_dms!==false&&c.bot_watch_mentions!==false)?' + ':''}${c.bot_watch_mentions!==false?'@-mentions':''}${(c.bot_watch_dms===false&&c.bot_watch_mentions===false)?'nothing':''}</div>
      </div>
    </div>
  </div>`;
}
function wireSlackBotCard(host,d,c){
  const e=document.getElementById("slackbot-edit");
  if(e) e.addEventListener("click",showSlackBotForm);
  const sw=host.querySelector("[data-toggleslackbot]");
  if(sw) sw.addEventListener("click",async()=>{
    const nU=slackIds(c.bot_allow_users).length, nC=slackIds(c.bot_allow_channels).length;
  const nA=slackIds(c.bot_approvers).length;
    if(!c.bot_enabled && !nU && !nC){
      alert("Add at least one allowed channel (or a user allowed to DM the bot) in configure first."); return; }
    await fetch("/api/slack-config",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({config:{...c, bot_enabled:!c.bot_enabled}})});
    loadSlackConfig();
  });
}
function showSlackBotForm(){
  const c=SLACK_CFG, f=openFormModal("<b>Slack bot user</b><br>Otto answering under its own name");
  f.innerHTML=`<div class="aform slackform">
    <label class="sk-toggle sk-master"><input type="checkbox" id="sb-enabled">
      <span class="sk-tg-txt"><b>Enabled</b><small>Answer as the Otto bot. Needs <code>OTTO_SLACK_BOT_TOKEN</code> and at least one allowlisted channel or DM user below.</small></span></label>

    <div class="sk-sec">
      <div class="sk-sectitle">What to answer</div>
      <div class="frow">
        <label class="sk-toggle"><input type="checkbox" id="sb-dms">
          <span class="sk-tg-txt"><b>Direct messages to the bot</b><small>DMs people send to Otto itself &mdash; never your own DMs.</small></span></label>
        <label class="sk-toggle"><input type="checkbox" id="sb-mentions">
          <span class="sk-tg-txt"><b>Channel @-mentions</b><small>Messages that @-mention the bot in an allowed channel. It must be invited to that channel.</small></span></label>
      </div>
    </div>

    <div class="sk-sec">
      <div class="sk-sectitle">Where the bot may answer</div>
      <p class="sk-help">Separate from the Auto-answer allowlist on purpose &mdash; inviting the bot somewhere must not change what your own account replies to. <b>Channels are the bound for @-mentions</b>: the bot only reads the channels listed here (and that it's a member of). The user list gates who may DM it.</p>
      <div class="frow">
        <div>
          <label>Allowed channels</label>
          <span class="sk-help">one channel ID per line &mdash; <code>C</code> / <code>G</code>, add <code>#name</code> to label it</span>
          <textarea id="sb-channels" class="sk-mono" placeholder="C01ABCDEF #eng-help"></textarea>
        </div>
        <div>
          <label>Allowed DM users</label>
          <span class="sk-help">one user ID per line &mdash; starts with <code>U</code>, add <code>#who</code> to label it</span>
          <textarea id="sb-users" class="sk-mono" placeholder="U01ABCDEF #alice"></textarea>
        </div>
      </div>
    </div>

    <div class="sk-sec">
      <div class="sk-sectitle">Who can approve a write from Slack <span class="sk-opt">&mdash; optional</span></div>
      <p class="sk-help">When a request needs approval, Otto says so in the thread. Anyone listed here can clear it by replying <code>approve</code> or <code>no</code>; everyone else's reply is treated as an ordinary message. <b>Deliberately separate from the lists above</b> &mdash; being allowed to ask Otto for something is not the same as being allowed to authorise it. Leave empty (the default) and approvals happen only on the Needs-you board.</p>
      <textarea id="sb-approvers" class="sk-mono" placeholder="U01ABCDEF #you"></textarea>
    </div>

    <div class="sk-sec">
      <div class="sk-sectitle">Interim reply</div>
      <span class="sk-help">posted the moment a message arrives, before Otto works on the real answer. The bot speaks for itself &mdash; it isn't standing in for you.</span>
      <textarea id="sb-ack" class="sk-ack"></textarea>
      <label>Greeting <span class="sk-opt">&mdash; when someone only says hello</span></label>
      <textarea id="sb-hello" class="sk-ack"></textarea>
    </div>

    <p class="sk-help">Poll interval, write approval and the pinned capability are shared with Auto-answer &mdash; set them there.</p>

    <div class="ferr" id="sb-err"></div>
    <div class="factions"><button class="btn approve" id="sb-save">Save</button><button class="btn decline" id="sb-cancel">Cancel</button></div>
  </div>`;
  document.getElementById("sb-enabled").checked=!!c.bot_enabled;
  document.getElementById("sb-dms").checked=c.bot_watch_dms!==false;
  document.getElementById("sb-mentions").checked=c.bot_watch_mentions!==false;
  document.getElementById("sb-channels").value=(c.bot_allow_channels||[]).join("\n");
  document.getElementById("sb-users").value=(c.bot_allow_users||[]).join("\n");
  document.getElementById("sb-approvers").value=(c.bot_approvers||[]).join("\n");
  document.getElementById("sb-ack").value=c.bot_ack_template||"";
  document.getElementById("sb-hello").value=c.bot_greeting_template||"";
  document.getElementById("sb-cancel").onclick=closeFormModal;
  document.getElementById("sb-save").onclick=async()=>{
    const lines=id=>document.getElementById(id).value.split("\n").map(s=>s.trim()).filter(Boolean);
    const bot_allow_users=lines("sb-users"), bot_allow_channels=lines("sb-channels");
    if(document.getElementById("sb-enabled").checked && !slackIds(bot_allow_users).length
       && !slackIds(bot_allow_channels).length){
      document.getElementById("sb-err").textContent="add at least one allowed channel or DM user ID (a #comment alone isn't one) before enabling"; return; }
    const config={...SLACK_CFG,
      bot_enabled:document.getElementById("sb-enabled").checked,
      bot_watch_dms:document.getElementById("sb-dms").checked,
      bot_watch_mentions:document.getElementById("sb-mentions").checked,
      bot_allow_users, bot_allow_channels,
      bot_approvers:lines("sb-approvers"),
      bot_ack_template:document.getElementById("sb-ack").value,
      bot_greeting_template:document.getElementById("sb-hello").value};
    await fetch("/api/slack-config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({config})});
    closeFormModal(); loadSlackConfig();
  };
}
function showSlackForm(){
  const c=SLACK_CFG, f=openFormModal("<b>Slack auto-answer</b><br>who Otto answers on your behalf");
  const capOpts=['<option value="">(auto-route)</option>'].concat(
    SLACK_CAPS.map(n=>`<option value="${esc(n)}"${c.cap===n?' selected':''}>${esc(n)}</option>`)).concat(
    (c.cap && !SLACK_CAPS.includes(c.cap))
      ? [`<option value="${esc(c.cap)}" selected>${esc(c.cap)} (disabled)</option>`] : []).join("");
  f.innerHTML=`<div class="aform slackform">
    <label class="sk-toggle sk-master"><input type="checkbox" id="sf-enabled">
      <span class="sk-tg-txt"><b>Enabled</b><small>Listen on Slack and answer on your behalf. Needs a user token and at least one allowlisted person below.</small></span></label>

    <div class="sk-sec">
      <div class="sk-sectitle">What to answer</div>
      <div class="frow">
        <label class="sk-toggle"><input type="checkbox" id="sf-dms">
          <span class="sk-tg-txt"><b>Direct messages</b><small>DMs people send you.</small></span></label>
        <label class="sk-toggle"><input type="checkbox" id="sf-mentions">
          <span class="sk-tg-txt"><b>Channel @-mentions</b><small>When you're @-mentioned in a channel you're in (best-effort).</small></span></label>
      </div>
    </div>

    <div class="sk-sec">
      <div class="sk-sectitle">Who's allowed to trigger a reply</div>
      <p class="sk-help">Only these people or channels can make Otto answer &mdash; everyone else is ignored. Leave both empty and nothing runs. In Slack: open a profile or channel &rarr; <b>⋯ More</b> &rarr; <b>Copy member ID</b> / <b>Copy channel ID</b>.</p>
      <div class="frow">
        <div>
          <label>Allowed users</label>
          <span class="sk-help">one user ID per line &mdash; starts with <code>U</code>, add <code>#who</code> to label it</span>
          <textarea id="sf-users" class="sk-mono" placeholder="U01ABCDEF #alice&#10;U02GHIJKL #bob"></textarea>
        </div>
        <div>
          <label>Allowed channels</label>
          <span class="sk-help">one channel/DM ID per line &mdash; <code>C</code> / <code>D</code> / <code>G</code>, add <code>#name</code> to label it</span>
          <textarea id="sf-channels" class="sk-mono" placeholder="C01ABCDEF #sre-alerts"></textarea>
        </div>
      </div>
      <label class="sk-toggle sk-test"><input type="checkbox" id="sf-allowself">
        <span class="sk-tg-txt"><b>Answer my own messages <span class="sk-opt">— test mode</span></b><small>Also reply to messages you send yourself (your Slack self-DM), so you can test solo without a second account. Loop-safe. Leave this OFF in normal use.</small></span></label>
    </div>

    <div class="sk-sec">
      <div class="sk-sectitle">Behaviour</div>
      <div class="frow">
        <div><label>Poll interval</label><span class="sk-help">how often to check Slack, in seconds (min 20)</span>
          <input id="sf-poll" type="number" min="20" placeholder="60"></div>
        <div><label>When a request is a write</label><span class="sk-help">questions always auto-answer; this is only for writes</span>
          <select id="sf-approval">
            <option value="ask">Pause for my approval on the Board</option>
            <option value="auto">Auto-approve (risky &mdash; runs writes unattended)</option>
            <option value="skip">Skip &mdash; never do writes</option></select></div>
      </div>
      <label>Pin a capability <span class="sk-opt">— optional</span></label>
      <span class="sk-help">skip routing and always use this capability for Slack requests</span>
      <select id="sf-cap">${capOpts}</select>
    </div>

    <div class="sk-sec">
      <div class="sk-sectitle">Interim reply</div>
      <span class="sk-help">posted in-thread the moment a message arrives, before Otto works on the real answer</span>
      <textarea id="sf-ack" class="sk-ack"></textarea>
    </div>

    <div class="ferr" id="sf-err"></div>
    <div class="factions"><button class="btn approve" id="sf-save">Save</button><button class="btn decline" id="sf-cancel">Cancel</button></div>
  </div>`;
  document.getElementById("sf-enabled").checked=!!c.enabled;
  document.getElementById("sf-dms").checked=c.watch_dms!==false;
  document.getElementById("sf-mentions").checked=c.watch_mentions!==false;
  document.getElementById("sf-allowself").checked=!!c.allow_self;
  document.getElementById("sf-users").value=(c.allow_users||[]).join("\n");
  document.getElementById("sf-channels").value=(c.allow_channels||[]).join("\n");
  document.getElementById("sf-poll").value=c.poll_seconds||60;
  document.getElementById("sf-approval").value=c.approval_default||"ask";
  document.getElementById("sf-ack").value=c.ack_template||"";
  document.getElementById("sf-cancel").onclick=closeFormModal;
  document.getElementById("sf-save").onclick=async()=>{
    const lines=id=>document.getElementById(id).value.split("\n").map(s=>s.trim()).filter(Boolean);
    const allow_users=lines("sf-users"), allow_channels=lines("sf-channels");
    const allow_self=document.getElementById("sf-allowself").checked;
    if(document.getElementById("sf-enabled").checked && !slackIds(allow_users).length
       && !slackIds(allow_channels).length && !allow_self){
      document.getElementById("sf-err").textContent="add at least one allowed user or channel ID (a #comment alone isn't one) before enabling, or turn on test mode"; return; }
    /* Spread the loaded config first: this form owns only the user-identity fields, and the
       server fills anything absent from defaults — so a bare object here would silently reset the
       bot's allowlist and templates every time Auto-answer was saved. */
    const config={...SLACK_CFG,
      enabled:document.getElementById("sf-enabled").checked,
      watch_dms:document.getElementById("sf-dms").checked,
      watch_mentions:document.getElementById("sf-mentions").checked,
      allow_self, allow_users, allow_channels,
      poll_seconds:Math.max(20,+document.getElementById("sf-poll").value||60),
      approval_default:document.getElementById("sf-approval").value||"ask",
      cap:document.getElementById("sf-cap").value||"",
      ack_template:document.getElementById("sf-ack").value};
    await fetch("/api/slack-config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({config})});
    closeFormModal(); loadSlackConfig();
  };
}

/* ---- GitHub board work queue (a pull ingress; config + poll-schedule status) ---- */
let BOARD_CFG={}, BOARD_CAPS=[], BOARD_URL="";
/* Mirrors board.project_spec() — the server normalizes on save; this only spares the operator
   a round-trip to be told a pasted URL was unreadable. */
function boardSpec(v){
  const s=String(v||"").trim().replace(/\/+$/,"");
  if(!s) return "";
  const m=s.match(/github\.com\/(?:orgs|users)\/([^/\s]+)\/projects\/(\d+)/);
  if(m) return m[1]+"/"+m[2];
  if(s.includes("github.com")) return "";
  const parts=s.split("/");
  return (parts.length===2 && parts[0] && /^\d+$/.test(parts[1])) ? s : "";
}
async function loadBoardQueue(){
  const host=document.getElementById("board-queue-section");
  if(!host) return;
  let d;
  try { d=await (await fetch("/api/board-config")).json(); }
  catch(e){ host.innerHTML=`<p class="err">Couldn't load board config (${esc(e.message)}).</p>`; return; }
  BOARD_CFG=d.config||{}; BOARD_CAPS=d.caps||[]; BOARD_URL=d.url||"";
  const c=BOARD_CFG, poll=d.poll||{}, cols=c.columns||{};
  // The card's title IS the board, so the status line below it says only the cadence.
  const boardLink = d.url ? `<a href="${esc(d.url)}" target="_blank" rel="noopener" title="open the board on GitHub"><code>${esc(c.project)}</code></a>`
                          : (c.project ? `<code>${esc(c.project)}</code>` : `schedule`);
  let status, badgeCls, badgeLabel;
  if(!d.temporal){ status=`<span class="warn">needs Temporal (run via ./run.sh)</span>`; badgeCls='warn'; badgeLabel='no Temporal'; }
  else if(!c.enabled||!c.project){ status=`<span class="warn">disabled</span> — set a board URL + enable to start polling`; badgeCls='off'; badgeLabel='off'; }
  else if(!poll.exists){ status=`enabled, but no poll schedule yet — restart the server to create it`; badgeCls='warn'; badgeLabel='starting…'; }
  else {
    status=`polling every <b>${esc(""+(c.poll_seconds||120))}s</b>`
        +(poll.paused?` · <span class="warn">paused</span>`:``)
        +(poll.next_run?` · next <b title="${esc(poll.next_run)}">${esc(shortWhen(poll.next_run))}</b>`:``)
        +(poll.last_run?` · last <span title="${esc(poll.last_run)}">${esc(shortWhen(poll.last_run))}</span>`:` · not run yet`);
    badgeCls=poll.paused?'warn':'on'; badgeLabel=poll.paused?'paused':'on';
  }
  const labelMap=Object.entries(c.label_cap||{}).map(([k,v])=>`${esc(k)}&rarr;${esc(v)}`).join(", ")||"none";
  const closed=evCollapsed()['ev-board'];
  setGithubBadge("board",badgeCls,badgeLabel);
  host.innerHTML=`<div class="asection coll subsection${closed?' collapsed':''}" data-sect="ev-board">
    <h3><span class="secttoggle" title="collapse / expand"><span class="gcaret">&#9662;</span>Board queue<span class="badge ${badgeCls}">${esc(badgeLabel)}</span></span>
      <button class="addbtn" id="board-edit">configure</button></h3>
    <div class="asection-body">
      <p class="sub" style="margin:10px 0 10px">Cards in <b>${esc(cols.ready||'Ready')}</b> run unattended — moving one there <i>is</i> the approval.
        Results comment back, then the card moves to <b>${esc(cols.review||'Review')}</b> or <b>${esc(cols.done||'Done')}</b>.</p>
      <div class="job ${c.enabled&&c.project?'':'off'}"><div class="jtop"><span class="switch ${c.enabled?'on':''} ${c.project?'':'locked'}" ${c.project?'data-toggleboard="1"':''}
          title="${c.project?'enable / disable polling this board':'set a board URL in configure before enabling'}"></span>
        <span class="jcron">poll ${boardLink}</span></div>
        <div class="jstatus">${status}</div>
        <div class="jstatus">columns: ${esc(cols.ready||'Ready')} &rarr; ${esc(cols.active||'In Progress')} &rarr; ${esc(cols.review||'Review')}/${esc(cols.done||'Done')}
          · labels&rarr;cap: ${labelMap}
          · repo-edit: <code>${esc(c.repo_edit_label||'repo-edit')}</code> · hold: <code>${esc(c.hold_label||'hold')}</code></div>
      </div>
    </div>
  </div>`;
  document.getElementById("board-edit").addEventListener("click",showBoardForm);
  const bsw=host.querySelector("[data-toggleboard]");
  if(bsw) bsw.addEventListener("click",async()=>{
    await fetch("/api/board-config",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({config:{...c, enabled:!c.enabled}})});
    loadBoardQueue();
  });
}
/* ---- PR auto-review (the GitHub ingress's PULL half: a pending review request on you) ----
   GitHub drops you from `review-requested` the moment you submit a review and puts you back on a
   re-request, so the poller needs no extra bookkeeping to review once per request. Nothing here
   writes to GitHub without a click: the review waits in an Otto chat thread until "post". */
let PRREV_CFG={}, PRREV_CAPS=[], PRREV_REPOS=[];
async function loadPrReviews(){
  const host=document.getElementById("pr-review-section");
  if(!host) return;
  let d;
  try { d=await (await fetch("/api/pr-review-config")).json(); }
  catch(e){ host.innerHTML=`<p class="err">Couldn't load PR-review config (${esc(e.message)}).</p>`; return; }
  PRREV_CFG=d.config||{}; PRREV_CAPS=d.caps||[]; PRREV_REPOS=d.known_repos||[];
  const c=PRREV_CFG, poll=d.poll||{};
  let status, badgeCls, badgeLabel;
  if(!d.temporal){ status=`<span class="warn">needs Temporal (run via ./run.sh)</span>`; badgeCls='warn'; badgeLabel='no Temporal'; }
  else if(!d.viewer){ status=`<span class="warn">gh is not logged in</span> — run <code>gh auth login</code>, Otto reviews as that account`; badgeCls='warn'; badgeLabel='no gh login'; }
  else if(!c.enabled){ status=`<span class="warn">disabled</span> — enable to start watching for review requests`; badgeCls='off'; badgeLabel='off'; }
  else if(!poll.exists){ status=`enabled, but no poll schedule yet — restart the server to create it`; badgeCls='warn'; badgeLabel='starting…'; }
  else {
    status=`polling every <b>${esc(""+(c.poll_seconds||900))}s</b>`
        +(poll.paused?` · <span class="warn">paused</span>`:``)
        +(poll.next_run?` · next <b title="${esc(poll.next_run)}">${esc(shortWhen(poll.next_run))}</b>`:``)
        +(poll.last_run?` · last <span title="${esc(poll.last_run)}">${esc(shortWhen(poll.last_run))}</span>`:` · not run yet`);
    badgeCls=poll.paused?'warn':'on'; badgeLabel=poll.paused?'paused':'on';
  }
  const skips=[c.skip_drafts?"drafts":null, c.skip_own?"your own PRs":null].filter(Boolean);
  const scope=(c.repos&&c.repos.length)
    ? (c.repos.length<=3 ? c.repos.map(r=>r.split("/").pop()).join(", ")
                         : `${c.repos.length} repos`)
    : "any repo";     // no allowlist = every repo the gh login can see
  const rows=(d.reviews||[]).filter(r=>!r.posted_at&&!r.dismissed);
  const ready=rows.filter(r=>r.ready).length;
  const done=(d.reviews||[]).length-rows.length;
  const closed=evCollapsed()['ev-prreview'];
  setGithubBadge("pr",badgeCls,badgeLabel);
  host.innerHTML=`<div class="asection coll subsection${closed?' collapsed':''}" data-sect="ev-prreview">
    <h3><span class="secttoggle" title="collapse / expand"><span class="gcaret">&#9662;</span>PR reviews<span class="badge ${badgeCls}">${esc(badgeLabel)}</span>${rows.length?`<span class="sectcount">${rows.length} waiting</span>`:''}</span>
      <button class="addbtn" id="prrev-edit">configure</button></h3>
    <div class="asection-body">
      <p class="sub" style="margin:10px 0 10px">A pull request that asks <b>you</b> for review is picked up automatically and reviewed read-only by
        <code>${esc(d.cap||'code-reviewer')}</code>. Every review lands in <b>its own chat thread</b> to read.
        ${c.auto_post
          ? `It is then <b>submitted to GitHub as soon as it finishes, with no click</b> &mdash; under your name, as an ${c.approve_on_pass!==false?`<b>approval</b> when the verdict says approve, otherwise a review comment`:`review comment`}.`
          : `<b>Nothing reaches GitHub until you press the button there</b>. It submits under your name: an <b>approve</b> verdict submits an approval${c.approve_on_pass===false?' (currently off &mdash; a comment instead)':''}, anything else a review comment.`}</p>
      <div class="job ${c.enabled?'':'off'}"><div class="jtop"><span class="switch ${c.enabled?'on':''}" data-toggleprrev="1"
          title="enable / disable watching for review requests"></span>
        <span class="jcron">watch ${d.viewer?`<code>review-requested:${esc(d.viewer)}</code>`:`review requests`}</span></div>
        <div class="jstatus">${status}</div>
        <div class="jstatus">scope: ${esc(scope)}${skips.length?` · skips ${esc(skips.join(" and "))}`:''} · at most <b>${esc(""+(c.max_per_poll||3))}</b> new review${(c.max_per_poll||3)===1?'':'s'} per poll
          · ${c.post_nitpicks?`posts nitpicks`:`nitpicks stay in the chat`}
          · ${c.auto_post?`<b class="warn">posts automatically</b>`:`posts on your click`}</div>
      </div>
      ${rows.length
        ? `<p class="sub" style="margin:8px 0 0"><b>${rows.length}</b> waiting${ready?` · <b>${ready}</b> ready to post`:''} &mdash; each one is a chat thread; open it to read the review and post it.</p>`
        : `<p class="sub" style="margin:8px 0 0">Nothing waiting.${done?` ${done} posted or dismissed.`:''}</p>`}
    </div>
  </div>`;
  document.getElementById("prrev-edit").addEventListener("click",showPrReviewForm);
  const sw=host.querySelector("[data-toggleprrev]");
  if(sw) sw.addEventListener("click",async()=>{
    await fetch("/api/pr-review-config",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({config:{...c, enabled:!c.enabled}})});
    loadPrReviews();
  });
}
/* ---- PR-review actions, in the chat that holds the review ----
   The review IS the chat, so the decision belongs beside the text it is about: judging a review
   from a one-line preview in a config panel is how you approve something you did not read. The
   Events tab states configuration; this bar acts. */
let PRBAR=null;                        // the row for the open chat, or null
/* Tabs are hidden flags on the view divs, not a variable — repainting the Events panel from the
   Chat tab would rebuild a DOM nobody is looking at (and `loadPrReviews` re-fetches). */
const eventsVisible=()=>{ const e=document.getElementById("eventsview"); return !!e && !e.hidden; };
async function updatePrBar(){
  const bar=document.getElementById("prbar");
  if(!bar) return;
  PRBAR=null; bar.hidden=true; bar.innerHTML="";
  const id=activeChat&&activeChat.id;
  if(!id || !/^gh-pr-/.test(id)) return;   // cheap pre-filter; the server is the authority
  let d; try { d=await (await fetch("/api/pr-review/chat?key="+encodeURIComponent(id))).json(); }
  catch(e){ return; }
  const r=d.review;
  if(!r || !activeChat || activeChat.id!==id) return;   // chat switched while we were asking
  PRBAR=r;
  const link=`<a href="${esc(r.url||'')}" target="_blank" rel="noopener"><b>${esc(r.repo||'')}#${esc(""+r.number)}</b></a>`;
  if(r.posted_at){
    bar.innerHTML=`<span class="sdot"></span>${r.approved?'approved':'posted'} on ${link}`;
  } else if(r.dismissed){
    bar.innerHTML=`<span class="sdot"></span>dismissed &mdash; ${link} was left alone`
      +`<span class="nt" id="pr-undismiss">Undo</span>`;
  } else if(!r.ready){
    bar.innerHTML=`<span class="sdot"></span>reviewing ${link}&hellip;`;
  } else {
    const willApprove = r.verdict==="approve" && d.approve_on_pass!==false;
    if(d.auto_post){
      // Auto-post is on: the buttons would race the sweep, and "post" on a review already
      // queued for posting reads as if nothing happened.
      bar.innerHTML=`<span class="sdot"></span>review of ${link} ready &mdash; submitted automatically`
        +(willApprove?` as an <b>approval</b>`:` as a comment`)
        +`<span class="nt" id="pr-dismiss">Hold it back</span>`;
      document.getElementById("pr-dismiss").onclick=()=>prReviewDismiss(r.key, true);
      bar.hidden=false; return;
    }
    bar.innerHTML=`<span class="sdot"></span>review of ${link} ready`
      +(willApprove?` &mdash; the reviewer says <b>approve</b>`:``)
      +`<span class="nt" id="pr-post">${willApprove?'Approve &amp; post':'Post to PR'}</span>`
      +`<span class="nt" id="pr-dismiss">Dismiss</span>`;
    document.getElementById("pr-post").onclick=()=>prReviewPost(r.key, willApprove);
    document.getElementById("pr-dismiss").onclick=()=>prReviewDismiss(r.key, true);
  }
  const undo=document.getElementById("pr-undismiss");
  if(undo) undo.onclick=()=>prReviewDismiss(r.key, false);
  bar.hidden=false;
}
async function prReviewDismiss(key, dismissed){
  await fetch("/api/pr-review/dismiss",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({key, dismissed})});
  await updatePrBar();
  if(eventsVisible()) loadPrReviews();
}
async function prReviewPost(key, willApprove){
  if(!confirm(willApprove
      ? "APPROVE this PR and post the review, as you?"
      : "Post this review to the PR as your review comment?")) return;
  const bar=document.getElementById("prbar");
  if(bar) bar.innerHTML=`<span class="sdot"></span>${willApprove?'approving':'posting'}&hellip;`;
  let out={};
  try { out=await (await fetch("/api/pr-review/post",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({key})})).json(); }
  catch(e){ out={ok:false, detail:e.message}; }
  if(!out.ok) alert("Couldn't post the review: "+(out.detail||out.error||"unknown error"));
  await updatePrBar();
  if(eventsVisible()) loadPrReviews();
}
function showPrReviewForm(){
  const c=PRREV_CFG, f=openFormModal("<b>PR reviews</b><br>which pull requests Otto reviews for you");
  const capOpts=['<option value="">(default — the review capability)</option>'].concat(
    (PRREV_CAPS||[]).map(n=>`<option value="${esc(n)}">${esc(n)}</option>`));
  f.innerHTML=`<div class="aform">
    <label><input type="checkbox" id="pf2-enabled"> Enabled (watch for review requests)</label>
    <div class="frow">
      <div><label>Poll interval (seconds)</label><input id="pf2-poll" type="number" min="60" placeholder="900"></div>
      <div><label>Max new reviews per poll</label><input id="pf2-max" type="number" min="1" placeholder="3"></div>
    </div>
    <label>Reviewing capability</label><select id="pf2-cap">${capOpts}</select>
    <span class="sk-help">Must be read-only — it reads the diff through <code>gh</code> and never touches the PR.</span>
    <label><input type="checkbox" id="pf2-autopost"> Post the review automatically once it is done</label>
    <span class="sk-help">Off: every review waits in its chat until you press the button. On: Otto submits it the moment the run finishes, with no click &mdash; a reply that ends on no verdict at all (a crashed or off-format run) is still held back.</span>
    <label><input type="checkbox" id="pf2-nits"> Include nitpicks in what gets posted</label>
    <span class="sk-help">Off: nit-level findings stay in your chat and never reach the PR. Blocking findings, suggestions, praise and the verdict always go.</span>
    <label><input type="checkbox" id="pf2-approve"> Approve the PR when the review says approve</label>
    <span class="sk-help">Off: every post is a plain review comment. This decides what gets submitted, whether you press the button or Otto does.</span>
    <label><input type="checkbox" id="pf2-drafts"> Skip draft PRs</label>
    <label><input type="checkbox" id="pf2-own"> Skip PRs I authored</label>
    <label>Scope</label>
    <div class="modepick">
      <label class="moderow"><input type="radio" name="pf2-scope" id="pf2-scope-any" value="any"> Any repo I can see</label>
      <label class="moderow"><input type="radio" name="pf2-scope" id="pf2-scope-only" value="only"> Only these:</label>
    </div>
    <div class="disclist" id="pf2-repolist"></div>
    <span class="sk-help">Every repo registered in Admin &rarr; Project repos, listed automatically.</span>
    <label>Other repos &mdash; one <code>owner/repo</code> per line</label>
    <textarea id="pf2-repos" placeholder="acme-corp/example-service"></textarea>
    <div class="ferr" id="pf2-err"></div>
    <div class="factions"><button class="btn approve" id="pf2-save">Save</button><button class="btn decline" id="pf2-cancel">Cancel</button></div>
  </div>`;
  document.getElementById("pf2-enabled").checked=!!c.enabled;
  document.getElementById("pf2-poll").value=c.poll_seconds||900;
  document.getElementById("pf2-max").value=c.max_per_poll||3;
  document.getElementById("pf2-cap").value=c.cap||"";
  document.getElementById("pf2-autopost").checked=!!c.auto_post;
  document.getElementById("pf2-nits").checked=!!c.post_nitpicks;
  document.getElementById("pf2-approve").checked=c.approve_on_pass!==false;
  document.getElementById("pf2-drafts").checked=c.skip_drafts!==false;
  document.getElementById("pf2-own").checked=c.skip_own!==false;
  /* A configured slug that is NOT a registered repo still has to be visible and editable, or
     merely opening the form and saving would silently narrow the allowlist to the registered
     subset. Known ones become ticks, the rest stay as text, and both are unioned on save. */
  const known=new Set(PRREV_REPOS.map(r=>r.slug)), picked=new Set(c.repos||[]);
  document.getElementById("pf2-repolist").innerHTML = PRREV_REPOS.length
    ? PRREV_REPOS.map(r=>`<label class="discrow"><input type="checkbox" data-prrepo="${esc(r.slug)}"${picked.has(r.slug)?" checked":""}>
        <code>${esc(r.slug)}</code><span class="disalias">${esc(r.name)}</span></label>`).join("")
    : `<span class="sk-help" style="padding:8px 10px">No repos registered yet &mdash; add them in Admin &rarr; Project repos.</span>`;
  document.getElementById("pf2-repos").value=(c.repos||[]).filter(r=>!known.has(r)).join("\n");
  /* An empty allowlist means EVERY repo, which a list of unticked boxes reads as "none" — the
     mode says which one it is out loud. Stored config is unchanged (`repos: []` is still "any"):
     this only stops the two from being told apart by counting checkboxes. */
  const anyBtn=document.getElementById("pf2-scope-any"), onlyBtn=document.getElementById("pf2-scope-only");
  const started=(c.repos||[]).length>0;
  anyBtn.checked=!started; onlyBtn.checked=started;
  const ticks=()=>[...document.querySelectorAll("[data-prrepo]")];
  const extraLines=()=>document.getElementById("pf2-repos").value.split("\n").map(x=>x.trim()).filter(Boolean);
  function applyScope(){
    const only=onlyBtn.checked;
    document.getElementById("pf2-repolist").classList.toggle("dim", !only);
    ticks().forEach(b=>{ b.disabled=!only; });
    document.getElementById("pf2-repos").disabled=!only;
  }
  anyBtn.onchange=onlyBtn.onchange=applyScope;
  // Ticking a repo IS choosing "only these" — leaving the mode on "any" while boxes are ticked
  // is the same ambiguity in reverse.
  ticks().forEach(b=>b.addEventListener("change",()=>{
    if(b.checked && !onlyBtn.checked){ onlyBtn.checked=true; applyScope(); }
  }));
  applyScope();
  document.getElementById("pf2-cancel").onclick=closeFormModal;
  document.getElementById("pf2-save").onclick=async()=>{
    const extra=document.getElementById("pf2-repos").value.split("\n").map(s=>s.trim()).filter(Boolean);
    const bad=extra.filter(r=>!/^[^/\s]+\/[^/\s]+$/.test(r.replace(/^https?:\/\/github\.com\//,"").replace(/\.git$/,"")));
    if(bad.length){ document.getElementById("pf2-err").textContent="not an owner/repo: "+bad[0]; return; }
    const ticked=[...document.querySelectorAll("[data-prrepo]")].filter(b=>b.checked).map(b=>b.dataset.prrepo);
    // The MODE decides, not the tick count: "only these" with nothing chosen would store the
    // empty allowlist that means EVERY repo — the exact reversal this control exists to stop.
    let repos=[];
    if(document.getElementById("pf2-scope-only").checked){
      repos=[...new Set(ticked.concat(extra))];
      if(!repos.length){
        document.getElementById("pf2-err").textContent=
          "pick at least one repo, or choose \u201cAny repo I can see\u201d \u2014 an empty list would review every repo";
        return;
      }
    }
    const config={enabled:document.getElementById("pf2-enabled").checked,
      poll_seconds:Math.max(60,+document.getElementById("pf2-poll").value||900),
      max_per_poll:Math.max(1,+document.getElementById("pf2-max").value||3),
      cap:val("pf2-cap"),
      auto_post:document.getElementById("pf2-autopost").checked,
      post_nitpicks:document.getElementById("pf2-nits").checked,
      approve_on_pass:document.getElementById("pf2-approve").checked,
      skip_drafts:document.getElementById("pf2-drafts").checked,
      skip_own:document.getElementById("pf2-own").checked,
      repos};
    await fetch("/api/pr-review-config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({config})});
    closeFormModal(); loadPrReviews();
  };
}
function showBoardForm(){
  const c=BOARD_CFG, cols=c.columns||{}, f=openFormModal("<b>GitHub board</b><br>which board Otto polls, and how cards map to runs");
  const labelLines=Object.entries(c.label_cap||{}).map(([k,v])=>`${k}=${v}`).join("\n");
  f.innerHTML=`<div class="aform">
    <label><input type="checkbox" id="bf-enabled"> Enabled (poll this board)</label>
    <label>Board URL &mdash; paste the GitHub Projects v2 board from your browser</label>
    <input id="bf-project" placeholder="https://github.com/orgs/my-org/projects/7">
    <span class="sk-help">The bare <code>&lt;owner&gt;/&lt;number&gt;</code> slug works too. Stored as the slug.</span>
    <div class="frow">
      <div><label>Poll interval (seconds)</label><input id="bf-poll" type="number" min="30" placeholder="120"></div>
      <div><label>Status field name</label><input id="bf-field" placeholder="Status"></div>
    </div>
    <div class="frow">
      <div><label>Ready column</label><input id="bf-ready" placeholder="Ready"></div>
      <div><label>In-progress column</label><input id="bf-active" placeholder="In Progress"></div>
    </div>
    <div class="frow">
      <div><label>Review column</label><input id="bf-review" placeholder="Review"></div>
      <div><label>Done column</label><input id="bf-done" placeholder="Done"></div>
    </div>
    <label>Label &rarr; capability (optional) &mdash; one <code>label=capability</code> per line</label>
    <textarea id="bf-labelcap" placeholder="incident=incident"></textarea>
    <div class="frow">
      <div><label>Repo-edit label (clone + draft PR)</label><input id="bf-repoedit" placeholder="repo-edit"></div>
      <div><label>Hold label (defer write to Board)</label><input id="bf-hold" placeholder="hold"></div>
    </div>
    <div class="ferr" id="bf-err"></div>
    <div class="factions"><button class="btn approve" id="bf-save">Save</button><button class="btn decline" id="bf-cancel">Cancel</button></div>
  </div>`;
  document.getElementById("bf-enabled").checked=!!c.enabled;
  document.getElementById("bf-project").value=BOARD_URL||c.project||"";
  document.getElementById("bf-poll").value=c.poll_seconds||120;
  document.getElementById("bf-field").value=c.status_field||"Status";
  document.getElementById("bf-ready").value=cols.ready||"Ready";
  document.getElementById("bf-active").value=cols.active||"In Progress";
  document.getElementById("bf-review").value=cols.review||"Review";
  document.getElementById("bf-done").value=cols.done||"Done";
  document.getElementById("bf-labelcap").value=labelLines;
  document.getElementById("bf-repoedit").value=c.repo_edit_label||"repo-edit";
  document.getElementById("bf-hold").value=c.hold_label||"hold";
  document.getElementById("bf-cancel").onclick=closeFormModal;
  document.getElementById("bf-save").onclick=async()=>{
    const raw=val("bf-project"), project=boardSpec(raw);
    if(raw && !project){
      document.getElementById("bf-err").textContent="couldn't read a board from that — paste the project URL, e.g. https://github.com/orgs/my-org/projects/7"; return; }
    if(document.getElementById("bf-enabled").checked && !project){
      document.getElementById("bf-err").textContent="a board URL is required to enable polling"; return; }
    const label_cap={};
    document.getElementById("bf-labelcap").value.split("\n").map(s=>s.trim()).filter(Boolean).forEach(line=>{
      const eq=line.indexOf("="); if(eq>0) label_cap[line.slice(0,eq).trim()]=line.slice(eq+1).trim();
    });
    const config={enabled:document.getElementById("bf-enabled").checked, project,
      poll_seconds:Math.max(30,+document.getElementById("bf-poll").value||120),
      status_field:val("bf-field")||"Status",
      columns:{ready:val("bf-ready")||"Ready", active:val("bf-active")||"In Progress",
        review:val("bf-review")||"Review", done:val("bf-done")||"Done"},
      label_cap, repo_edit_label:val("bf-repoedit")||"repo-edit", hold_label:val("bf-hold")||"hold"};
    await fetch("/api/board-config",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({config})});
    closeFormModal(); loadBoardQueue();
  };
}
async function saveRules(){
  await fetch("/api/event-rules",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({rules:EVENT_RULES})});
}
function showRuleForm(idx){
  const editing=idx>=0, r=editing?EVENT_RULES[idx]:{};
  const c=openFormModal(editing?"<b>Edit event rule</b><br>"+esc(r.source||""):"<b>New event rule</b><br>turn an inbound webhook into a run");
  const capOpts=['<option value="">(auto-route)</option>'].concat((EVENT_CAPS||[]).map(n=>`<option value="${esc(n)}">${esc(n)}</option>`)).concat(
    (r.cap && !(EVENT_CAPS||[]).includes(r.cap))
      ? [`<option value="${esc(r.cap)}">${esc(r.cap)} (disabled)</option>`] : []);
  c.innerHTML=`<div class="aform">
    <label>Source &mdash; the <code>/api/events/&lt;source&gt;</code> path segment</label><input id="rf-source" placeholder="newrelic">
    <label>Request template &mdash; use {dotted.path} tokens from the payload</label>
    <textarea id="rf-template" placeholder="Investigate this alert: {condition_name} on {targets.0.name}"></textarea>
    <label>Match filters (optional) &mdash; one <code>path=value</code> per line; all must match</label>
    <textarea id="rf-when" placeholder="event_type=INCIDENT"></textarea>
    <div class="frow">
      <div><label>Capability</label><select id="rf-cap">${capOpts.join("")}</select></div>
      <div><label>On a write</label><select id="rf-approval">
        <option value="skip">skip (don't run writes)</option>
        <option value="ask">ask — approve on the Board</option>
        <option value="auto">auto-approve</option>
      </select></div>
    </div>
    <label>Reply webhook URL (optional) &mdash; POST the result here when done</label><input id="rf-reply" placeholder="https://…">
    <div class="ferr" id="rf-err"></div>
    <div class="factions"><button class="btn approve" id="rf-save">${editing?'Save changes':'Add rule'}</button><button class="btn decline" id="rf-cancel">Cancel</button></div>
  </div>`;
  const curApproval=r.approval || (r.auto_approve?"auto":"skip");
  if(editing){
    document.getElementById("rf-source").value=r.source||"";
    document.getElementById("rf-template").value=r.template||"";
    document.getElementById("rf-when").value=r.when?Object.entries(r.when).map(([k,v])=>`${k}=${v}`).join("\n"):"";
    document.getElementById("rf-cap").value=r.cap||"";
    document.getElementById("rf-reply").value=(r.reply_to&&r.reply_to.url)||"";
  }
  document.getElementById("rf-approval").value=curApproval;
  document.getElementById("rf-cancel").onclick=closeFormModal;
  document.getElementById("rf-save").onclick=async()=>{
    const source=val("rf-source"), template=document.getElementById("rf-template").value.trim();
    if(!source||!template){ document.getElementById("rf-err").textContent="source and template are required"; return; }
    const when={};
    document.getElementById("rf-when").value.split("\n").map(s=>s.trim()).filter(Boolean).forEach(line=>{
      const eq=line.indexOf("="); if(eq>0) when[line.slice(0,eq).trim()]=line.slice(eq+1).trim();
    });
    const rule={source, template, approval:document.getElementById("rf-approval").value};
    // the form replaces the whole rule, so carry the toggle over — an edit must not silently re-enable
    if(editing && r.enabled===false) rule.enabled=false;
    const cap=val("rf-cap"); if(cap) rule.cap=cap;
    if(Object.keys(when).length) rule.when=when;
    const replyUrl=val("rf-reply"); if(replyUrl) rule.reply_to={kind:"webhook", url:replyUrl};
    if(editing) EVENT_RULES[idx]=rule; else EVENT_RULES.push(rule);
    await saveRules(); closeFormModal(); loadEvents();
  };
}
