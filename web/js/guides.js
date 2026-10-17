"use strict";
/* ---- Setup guides: on-demand, step-by-step walkthroughs for the Events integrations ----
   Separate from onboarding: opened from the "guide" button on the Slack and GitHub sections.
   A guide never stores a token or a setting of its own. Each step reads the SAME config endpoints
   the Events cards render from, says whether it is already done, and its action opens the card's
   existing configure form. When that form closes (saved or cancelled) the guide reopens and
   re-checks, so it always resumes at the first step still undone — running it again is safe.
   Each guide's branch choice (Slack identity, GitHub features) is remembered per browser. */
let GUIDE=null;   // {key, ctx, choice, step}

const GUIDE_PICK="otto.guides.choice";
function guideChoiceRead(k){ try{ return (JSON.parse(localStorage.getItem(GUIDE_PICK))||{})[k]||null; }catch(e){ return null; } }
function guideChoiceWrite(k,v){ try{ const st=JSON.parse(localStorage.getItem(GUIDE_PICK))||{}; st[k]=v; localStorage.setItem(GUIDE_PICK,JSON.stringify(st)); }catch(e){} }

const gCode=s=>`<code>${esc(s)}</code>`;
const gList=rows=>`<ul class="gdlist">${rows.map(r=>`<li>${r}</li>`).join("")}</ul>`;
const gInline=rows=>`<p class="gdcodes">${rows.join(" ")}</p>`;
// The closing check can only pass once everything before it has: a live poll left over from an
// earlier setup must not read as "done" beside a missing token.
const gGated=steps=>{ const last=steps[steps.length-1]; last.done=last.done && steps.slice(0,-1).every(s=>s.done); return steps; };
const gMissing=sc=>(sc && sc.known && (sc.missing||[]).length)
  ? `<div class="obnote">The token is missing ${sc.missing.map(([s])=>gCode(s)).join(", ")}. Add the scope(s), <b>reinstall the app</b> (the old token never gains them), update the token and restart Otto.</div>` : "";

// The scope list comes from the server (slack._SCOPES), the same table scope_gaps checks.
const gScopes=(d,k)=>gInline(((d.scope_catalog||{})[k]||[]).map(([s,why,opt])=>`<code title="${esc(why)}${opt?" (optional)":""}">${esc(s)}${opt?" <i>optional</i>":""}</code>`));

function slackSteps(d, ident){
  const c=d.config||{}, poll=d.poll||{};
  const out=[];
  const both=ident==="both", user=ident!=="bot", bot=ident!=="user";
  out.push({t:"Create a Slack app", done:(!user||d.token_set)&&(!bot||d.bot_token_set),
    b:`At <a href="https://api.slack.com/apps" target="_blank" rel="noopener">api.slack.com/apps</a> choose <b>Create New App → From scratch</b> and pick your workspace${both?" — one app carries both tokens":""}. Under <b>OAuth &amp; Permissions → Scopes</b> add:`+
      (user?`<p class="obsub"><b>User Token Scopes</b> (answering as you — a bot can never read your own DMs):</p>${gScopes(d,"user")}`:"")+
      (bot?`<p class="obsub"><b>Bot Token Scopes</b> (answering as Otto). Slack splits listing a conversation from reading it, so both halves are needed:</p>${gScopes(d,"bot")}<p class="obsub">Under <b>App Home</b>, turn on the <b>Messages Tab</b> — otherwise nobody can DM the bot.</p>`:"")+
      `<p class="obsub">Then <b>Install to Workspace</b>.</p>`});
  const toks=[user&&["OTTO_SLACK_USER_TOKEN","xoxp-…","User OAuth Token",d.token_set,d.scopes],
              bot&&["OTTO_SLACK_BOT_TOKEN","xoxb-…","Bot User OAuth Token",d.bot_token_set,d.bot_scopes]].filter(Boolean);
  out.push({t:"Give Otto the token", done:toks.every(([,,,set,sc])=>set && !(sc&&sc.rejected) && !(sc&&sc.known&&(sc.missing||[]).length)),
    b:`Copy the ${toks.map(([,,label])=>`<b>${label}</b>`).join(" and the ")} from <b>OAuth &amp; Permissions</b> into Otto's ${gCode(".env")}, never into anything under ${gCode("data/")}:`+
      gList(toks.map(([env,shape,,set])=>`${gCode(env+"="+shape)} ${set?'<span class="gdok">set</span>':'<span class="gdno">not set</span>'}`))+
      `<p class="obsub">${gCode("./.venv/bin/python setup_wizard.py")} can write the user token for you without echoing it. Tokens are read at startup, so <b>restart Otto</b> afterwards, then press Re-check.</p>`+
      (user?gMissing(d.scopes):"")+(bot?gMissing(d.bot_scopes):"")+
      toks.filter(([,,,,sc])=>sc&&sc.rejected).map(([env,,,,sc])=>`<div class="obnote">Slack refused ${gCode(env)} (${esc(String(sc.rejected))}) — copy a fresh token from <b>OAuth &amp; Permissions</b> and restart Otto.</div>`).join("")});
  if(user) out.push({t:"Choose who Otto answers as you", done:(d.answering||{}).user==="on",
    b:((d.answering||{}).user==="test"?`<div class="obnote">Test mode is on with no allowlist: Otto answers only your own self-DM. Fine for trying it — add people or channels when you're ready.</div>`:"")+
      `Otto only answers people and channels on the allowlist — <b>both empty means nobody</b>. Add Slack member IDs (profile → ⋮ → <i>Copy member ID</i>) and/or channel IDs (the ${gCode("C…")} part of a channel link), tick <b>Enabled</b> and save. Writes still wait for your approval on the Board unless you change that.`+
      `<p class="obsub">Trying it alone? <b>Test mode</b> lets your own self-DM trigger a run — turn it off on a real workspace.</p>`,
    act:{label:"Open Auto-answer settings", run:()=>{ gSlackSync(d); showSlackForm(); }}});
  if(bot) out.push({t:"Choose where the bot answers", done:(d.answering||{}).bot==="on",
    b:`List the channels the bot may answer in and the people who may DM it, tick <b>Enabled</b> and save. A listed channel still needs the bot <b>invited</b> to it: type ${gCode("/invite @Otto")} there.`+
      `<p class="obsub"><b>Approvers</b> are a separate list on purpose — who may ask is not who may approve a write.</p>`,
    act:{label:"Open Bot user settings", run:()=>{ gSlackSync(d); showSlackBotForm(); }}});
  out.push({t:"Check it's listening", done:!!d.temporal && !!poll.exists && !poll.failing && !poll.paused,
    b:(!d.temporal?`<div class="obnote">Temporal isn't running — start Otto with ${gCode("./run.sh")}.</div>`:"")+
      (d.temporal && !poll.exists?`<div class="obnote">No poll schedule yet — restart Otto to create it.</div>`:"")+
      (poll.failing?`<div class="obnote">Every poll is failing — check the worker log for the traceback.</div>`:"")+
      (poll.paused?`<div class="obnote">The poll schedule is paused — nothing is being read until it resumes.</div>`:"")+
      `<p class="obsub">Send a message from an allowlisted account${bot?" (or @-mention the bot in a listed channel)":""}. Otto reacts with 👀, then replies in the thread within one poll (${esc(""+Math.max(20,+c.poll_seconds||60))}s — the schedule never polls faster than 20s).</p>`+
      (bot?`<p class="obsub">Optional: an app-level token in ${gCode("OTTO_SLACK_APP_TOKEN")} turns on Socket Mode, so the bot answers instantly instead of on the poll.</p>`:"")});
  return out;
}

/* The card forms save {...<their global>, ...their own fields}. Hand them the config THIS guide
   just read, or a card that failed or is still loading posts {} and wipes the other identity's keys. */
function gSlackSync(d){ SLACK_CFG=d.config||{}; SLACK_CAPS=d.caps||[]; }

function githubSteps(ctx, feats){
  const {pr, board, repos}=ctx, bc=(board.config||{}), pc=(pr.config||{});
  const out=[{t:"Sign in the GitHub CLI", done:!!pr.viewer,
    b:`Otto talks to GitHub through ${gCode("gh")}, as whoever it is logged in as. Install it and run ${gCode("gh auth login")} on the machine Otto runs on.`+
      (pr.viewer?`<p class="obsub">Signed in as <b>${esc(pr.viewer)}</b>.</p>`:`<p class="obsub">After signing in, press Re-check — no restart needed. A failed check is retried after two minutes, so give it a moment if it still says to do.</p>`)}];
  if(feats.includes("board")){
    out.push({t:"Prepare a project board", done:!!bc.project,
      b:`Create a GitHub <b>Project</b> (the new Projects, not classic) with a Status field holding the columns ${["Ready","In Progress","Review","Done","Blocked"].map(gCode).join(", ")}. Moving a card to <b>Ready</b> is what approves it to run, so only people you trust should be able to move cards there.`+
        `<p class="obsub">Reading a board needs the ${gCode("project")} scope, which ${gCode("gh auth login")} doesn't grant: run ${gCode("gh auth refresh -h github.com -s project")}. Otto can't see the scope without calling GitHub, so this step can't check it — if the Board queue card says the board is unreachable, this is why.</p>`});
    out.push({t:"Connect the board", done:!!bc.project && !!bc.enabled,
      b:`Paste the board's URL (${gCode("https://github.com/orgs/<org>/projects/<n>")}), check the column names match yours, tick <b>Enabled</b> and save.`+
        `<p class="obsub">Labels can pin a card to a capability; the <b>repo-edit</b> label makes it run in a clone and open a PR.</p>`,
      act:{label:"Open Board queue settings", run:()=>{ BOARD_CFG=bc; BOARD_CAPS=board.caps||[]; BOARD_URL=board.url||""; showBoardForm(); }}});
  }
  if(feats.includes("pr")) out.push({t:"Review the PRs that ask for you", done:!!pc.enabled,
    b:`When a pull request requests <b>your</b> review, Otto reviews it read-only and parks the review in a chat thread for you to read. Nothing reaches GitHub until you press post${pc.auto_post?" — <b>auto-post is ON</b>, so reviews currently publish without a click":""}. Pick which repos it watches, tick <b>Enabled</b> and save.`,
    act:{label:"Open PR review settings", run:()=>{ PRREV_CFG=pc; PRREV_CAPS=pr.caps||[]; PRREV_REPOS=pr.known_repos||[]; showPrReviewForm(); }}});
  if(feats.includes("repos")) out.push({t:"Register the repos Otto may change", done:(ctx.nrepos||0)>0,
    b:`Board tickets and chat requests that edit code run in an isolated clone of a <b>registered</b> repo and come back as a draft PR. Register each repo in <b>Admin → Project repos</b>.`+
      ((repos||[]).length?`<p class="obsub">Registered: ${repos.map(r=>gCode(r.name||r.slug)).join(", ")}.</p>`:""),
    act:{label:"Open Project repos", run:()=>obGoto("admin","projects"), leaves:true}});
  const polls=[feats.includes("board")&&bc.enabled&&board.poll, feats.includes("pr")&&pc.enabled&&pr.poll].filter(Boolean);
  // Board and PR polls report existence and pause only — neither tracks failures the way Slack's
  // does, so a fire that errors is something this step cannot see.
  out.push({t:"Check it's running", done:!!pr.temporal && polls.every(p=>p.exists && !p.paused),
    b:(!pr.temporal?`<div class="obnote">Temporal isn't running — start Otto with ${gCode("./run.sh")}.</div>`:"")+
      (polls.some(p=>!p.exists)?`<div class="obnote">A poll schedule hasn't been created yet — restart Otto.</div>`:"")+
      (polls.some(p=>p.paused)?`<div class="obnote">A poll schedule is paused — nothing runs until it resumes.</div>`:"")+
      `<p class="obsub">${feats.includes("board")?"Move a small ticket to <b>Ready</b>: Otto picks it up on the next poll, comments the result and moves the card. ":""}${feats.includes("pr")?"Ask Otto's account for review on a PR and watch the Events card's waiting count.":""}</p>`});
  return out;
}

const GUIDES={
  slack:{title:"Slack setup guide",
    choices:{q:"Who should Otto answer as?", opts:[
      ["user","As you","Reads your DMs and @-mentions of you, and replies under your name."],
      ["bot","As a bot","Its own Slack user: DMs to the bot and @-mentions in channels it's invited to."],
      ["both","Both","Two identities, separately switchable, on one poll."]]},
    load:async()=>getJSON("/api/slack-config"),
    steps:(ctx,choice)=>gGated(slackSteps(ctx,choice))},
  github:{title:"GitHub setup guide", multi:true,
    choices:{q:"What should Otto do with GitHub?", opts:[
      ["board","Work from a project board","Cards moved to Ready run unattended; results come back as comments."],
      ["pr","Review PRs that ask for you","Read-only reviews, posted when you say so."],
      ["repos","Open PRs in my repos","Code changes run in an isolated clone and return as draft PRs."]]},
    // Repos from the PR config's known_repos — URL-first, so a repo registered by URL whose
    // clone hasn't landed still counts (workspace.git_repos would miss it, and spawns git).
    load:async()=>{ const [pr,board]=await Promise.all([getJSON("/api/pr-review-config"),getJSON("/api/board-config")]);
                    return {pr,board,repos:pr.known_repos||[],nrepos:pr.registered_repos||0}; },
    steps:(ctx,choice)=>gGated(githubSteps(ctx,choice))},
};

async function openGuide(key, keepStep){
  const g=GUIDES[key];
  // The GitHub checks can wait on `gh` the first time; say so instead of a dead click.
  openFormModal(`<b>${esc(g.title)}</b>`).innerHTML=`<p class="obsub">Checking this install…</p>`;
  const sess=FORM_MODAL_SESSION;
  // Closed, or replaced by another form, while loading: the user moved on — never paint over it.
  const gone=()=>sess!==FORM_MODAL_SESSION || document.getElementById("formModal").hidden;
  let ctx; try { ctx=await g.load(); } catch(e){ if(!gone()){ closeFormModal(); toast("couldn't load the "+key+" guide: "+e.message); } return; }
  if(gone()) return;
  let choice=(GUIDE&&GUIDE.key===key&&GUIDE.choice)||guideChoiceRead(key);
  if(Array.isArray(choice) && !choice.length) choice=null;   // nothing ticked is no choice yet
  GUIDE={key, ctx, choice, step:0};
  if(choice){
    // Resume where the work is: the first undone step (or stay put on a re-check).
    const steps=g.steps(ctx,choice), first=steps.findIndex(s=>!s.done);
    GUIDE.step = keepStep!=null ? Math.min(keepStep,steps.length) : 1+(first<0?steps.length-1:first);
  }
  guideRender();
}

function guideRender(){
  const g=GUIDES[GUIDE.key], body=openFormModal(`<b>${esc(g.title)}</b>`);
  body.closest(".modalBox").classList.add("wideModalBox");
  if(GUIDE.step===0 || !GUIDE.choice || (g.multi && !GUIDE.choice.length)){
    const sel=v=>g.multi ? (GUIDE.choice||[]).includes(v) : GUIDE.choice===v;
    body.innerHTML=`<p class="obq">${esc(g.choices.q)}</p><p class="obsub">The guide checks each step against this install, so steps you've already done show as done.</p>
      <div class="obopts">${g.choices.opts.map(([v,l,s])=>`<label class="obopt${sel(v)?' sel':''}"><input type="${g.multi?'checkbox':'radio'}" name="gd-pick" value="${esc(v)}"${sel(v)?' checked':''}><b>${esc(l)}</b><span>${esc(s)}</span></label>`).join("")}</div>
      <div class="obnav"><button class="btn approve" data-gd="next"${(g.multi?(GUIDE.choice||[]).length:GUIDE.choice)?'':' disabled'}>Next</button></div>`;
    body.querySelectorAll('input[name="gd-pick"]').forEach(i=>i.addEventListener("change",()=>{
      GUIDE.choice = g.multi ? [...body.querySelectorAll('input[name="gd-pick"]:checked')].map(x=>x.value) : i.value;
      guideChoiceWrite(GUIDE.key, GUIDE.choice); guideRender(); refocusOption(i);
    }));
  } else {
    const steps=g.steps(GUIDE.ctx, GUIDE.choice), i=GUIDE.step-1, s=steps[i], last=i===steps.length-1;
    const dots=steps.map((x,j)=>`<i class="${x.done?'on':''}${j===i?' gdcur':''}" title="${esc(x.t)}${x.done?' — done':''}"></i>`).join("");
    body.innerHTML=`<div class="obsteps">${dots}</div>
      <p class="obq">${i+1}. ${esc(s.t)} ${s.done?'<span class="gdok">done</span>':'<span class="gdno">to do</span>'}</p>
      <div class="gdbody">${s.b}</div>
      ${s.act?`<p class="obsub">${s.act.leaves?"":"The guide comes back when you save or close the form."}</p>`:""}
      <div class="obnav"><button class="btn decline obleft" data-gd="back">Back</button>
        <button class="btn revise" data-gd="recheck" title="Read the configuration again">Re-check</button>
        ${s.act?`<button class="btn revise" data-gd="act">${esc(s.act.label)}</button>`:""}
        <button class="btn approve" data-gd="${last?'done':'next'}">${last?'Finish':'Next'}</button></div>`;
  }
  body.querySelectorAll("[data-gd]").forEach(b=>b.addEventListener("click",()=>guideAction(b.dataset.gd)));
}

function guideAction(a){
  const g=GUIDES[GUIDE.key];
  if(a==="next"){ GUIDE.step++; guideRender(); return; }
  if(a==="back"){ GUIDE.step=Math.max(0,GUIDE.step-1); guideRender(); return; }
  if(a==="done"){ closeFormModal(); return; }
  if(a==="recheck"){
    // Re-ask the gh login and Slack's scopes first (no connector list — no MCP spawn): the
    // card endpoints the guide reads only ever serve the cached answer.
    const key=GUIDE.key, step=GUIDE.step;
    openFormModal(`<b>${esc(g.title)}</b>`).innerHTML=`<p class="obsub">Checking again…</p>`;
    const sess=FORM_MODAL_SESSION;
    postJSON("/api/onboarding/recheck",{connectors:false}).catch(()=>{}).finally(()=>{
      // Closed or replaced while it ran: the user moved on — never paint over it.
      if(sess===FORM_MODAL_SESSION && !document.getElementById("formModal").hidden) openGuide(key, step);
    });
    return;
  }
  if(a==="act"){
    const s=g.steps(GUIDE.ctx,GUIDE.choice)[GUIDE.step-1], key=GUIDE.key;
    closeFormModal();
    try { s.act.run(); }
    catch(e){ toast("couldn't open that form: "+e.message); closeFormModal(); openGuide(key); return; }
    if(!s.act.leaves) afterFormModal(()=>openGuide(key));   // armed after the form opened (modal.js)
  }
}

/* The guide buttons live in the Events section headers, which loadEvents rebuilds on every visit,
   so they are bound by delegation once rather than per render. */
document.addEventListener("click",e=>{
  const b=e.target.closest("[data-guide]");
  if(b) openGuide(b.dataset.guide);
});
