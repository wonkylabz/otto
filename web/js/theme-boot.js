"use strict";
/* Theme boot. Loaded in <head> and BEFORE the first paint on purpose: applied from the body
   script instead, every reload would flash the default cream ground before repainting dark.
   The choice is per-browser (localStorage), not a server setting — it is a property of who is
   looking, not of this Otto. An unknown or unreadable value simply leaves the attribute off,
   which is the default Chocolate Truffle palette in :root. */
window.OTTO_THEME_KEY = "otto.theme";
window.applyTheme = function(name){
  const el = document.documentElement;
  if(name && /^[a-z0-9-]+$/.test(name)) el.setAttribute("data-theme", name);
  else el.removeAttribute("data-theme");
};
window.currentTheme = function(){
  try { return localStorage.getItem(window.OTTO_THEME_KEY) || "chocolate-truffle"; }
  catch(e){ return "chocolate-truffle"; }
};
/* The favicon is the one copy of the mark that CANNOT use the palette tokens: a data: URI is
   its own document, with no access to this page's custom properties. So it is repainted from
   the resolved values whenever the theme changes - same geometry as the #mk <symbol>, same
   three roles. The static href in <head> is chocolate-truffle's own resolved values, so the
   icon is already right before this runs and stays right if it never does. */
window.paintFavicon = function(){
  const cs = getComputedStyle(document.documentElement);
  const tok = n => (cs.getPropertyValue(n) || "").trim();
  const accent = tok("--accent"), face = tok("--on-accent"), warn = tok("--warn");
  if(!accent || !face || !warn) return;          // stylesheet not parsed yet - keep the default
  const svg = "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>"
    + "<rect width='100' height='100' rx='26' fill='" + accent + "'/>"
    + "<rect x='26' y='30' width='48' height='52' rx='12' fill='" + face + "'/>"
    + "<rect x='36' y='42' width='10' height='26' rx='5' fill='" + accent + "'/>"
    + "<rect x='54' y='42' width='10' height='26' rx='5' fill='" + accent + "'/>"
    + "<circle cx='50' cy='16' r='8' fill='" + warn + "'/></svg>";
  const link = document.querySelector('link[rel="icon"]');
  if(link) link.setAttribute("href", "data:image/svg+xml," + encodeURIComponent(svg));
};
window.applyTheme(window.currentTheme());
window.paintFavicon();
