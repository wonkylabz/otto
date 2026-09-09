"use strict";
/* <otto-mascot> — Otto the mascot as a framework-agnostic custom element.
 *
 * Inlined rather than served as its own file, like every other line of this UI: a separate
 * asset needs a route in server.py, and a route needs a SERVICE RESTART to ship — which
 * restarts the worker too and costs whatever run is in flight its current attempt. A mascot
 * is not worth that. index.html is re-read per request, so an edit here ships on a refresh.
 *
 *   <otto-mascot state="idle" size="180"></otto-mascot>
 *
 * Attributes
 *   state  idle | thinking | planning | working | evicting | success | dab | error | sleeping
 *          (default idle)
 *   size   px width of the mascot (height follows the 4:5 ratio)    (default 200)
 *   color  ink color (any CSS color)                               (default #2b2b2b)
 *   speed  animation speed multiplier, 0.5 = half speed            (default 1)
 *   shadow "off" to hide the hover shadow
 *
 * JS:  otto.state = 'thinking'      // or otto.setAttribute('state', 'thinking')
 *      otto.blink()                 // one-off blink
 *      otto.react('success')        // play a state for 1.6s, then return to previous
 *
 * Respects prefers-reduced-motion: motion is reduced to a slow float.
 */
(function () {
  const STATES = ['idle', 'thinking', 'planning', 'working', 'evicting', 'success', 'dab', 'error', 'sleeping'];

  const SVG = `
<svg class="otto" viewBox="0 0 240 300" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <g class="shadow">
    <ellipse cx="121" cy="272" rx="30" ry="5"></ellipse>
    <ellipse cx="121" cy="272" rx="17" ry="2.8" opacity="0.7"></ellipse>
  </g>
  <g class="board">
    <rect x="126" y="48" width="108" height="164" rx="4"></rect>
    <path class="ink i1" d="M 140 82 L 208 82"></path>
    <path class="ink i2" d="M 140 100 L 192 100"></path>
    <path class="ink i3" d="M 140 118 L 204 118"></path>
    <rect class="ink i4" x="140" y="136" width="36" height="22" rx="2"></rect>
    <path class="ink i5" d="M 182 147 L 212 147"></path>
    <path class="ink i5" d="M 206 142 L 212 147 L 206 152"></path>
    <path class="ink i6" d="M 140 176 L 196 176"></path>
  </g>
  <g class="figure">
    <g class="noggin">
      <g class="face">
        <g class="antenna">
          <path class="stem" d="M 120 55 L 121 30"></path>
          <circle class="bulb" cx="121" cy="25" r="6"></circle>
          <path class="glint" d="M 117 22 L 120 20"></path>
        </g>
        <path class="head" d="M 75 72 C 75 62, 81 56, 91 56 L 151 55 C 161 55, 167 61, 168 71 L 168 112 C 168 122, 162 128, 152 128 L 90 128 C 80 128, 74 122, 74 112 Z"></path>
        <path class="hatch head-hatch" d="M 84 122 C 82 106, 82 84, 85 62"></path>
        <g class="skull">
          <path class="skull-rim" d="M 78 63 C 99 70, 145 70, 166 63 C 145 56, 99 56, 78 63 Z"></path>
          <path class="skull-void" d="M 88 63 C 105 68, 139 68, 156 62 C 139 57, 105 57, 88 63 Z"></path>
          <path class="skull-cap" d="M 78 63 C 79 58, 84 56, 91 56 L 151 55 C 160 55, 165 58, 166 63 C 145 70, 99 70, 78 63 Z"></path>
        </g>
        <path class="ear ear-l" d="M 74 88 C 68 86, 64 88, 63 94 C 62 100, 66 104, 72 103"></path>
        <path class="ear ear-r" d="M 168 88 C 174 86, 178 88, 179 94 C 180 100, 176 104, 170 103"></path>
        <g class="eye eye-l"><rect x="99" y="76" width="9" height="26" rx="4.5"></rect></g>
        <g class="eye eye-r"><rect x="134" y="76" width="9" height="26" rx="4.5"></rect></g>
      </g>
    </g>
    <path class="neck" d="M 114 128 L 113 146"></path>
    <path class="neck" d="M 129 130 L 130 146"></path>
    <path class="torso" d="M 84 158 C 84 150, 90 145, 100 144 L 142 144 C 152 145, 158 150, 158 158 L 156 216 C 155 224, 149 228, 140 228 L 102 228 C 93 228, 87 224, 86 216 Z"></path>
    <ellipse class="core" cx="121" cy="188" rx="26" ry="26"></ellipse>
    <ellipse class="core-inner" cx="121" cy="188" rx="21" ry="21"></ellipse>
    <path class="hatch" d="M 121 144 L 121 156"></path>
    <g class="back">
      <path d="M 88 70 L 154 70"></path>
      <rect x="100" y="166" width="42" height="46" rx="4"></rect>
      <circle cx="107" cy="173" r="2"></circle>
      <circle cx="135" cy="173" r="2"></circle>
      <circle cx="107" cy="205" r="2"></circle>
      <circle cx="135" cy="205" r="2"></circle>
    </g>
    <g class="hatch shading">
      <path d="M 79 108 C 83 115, 88 121, 93 125"></path>
      <path d="M 143 202 C 148 196, 150 190, 150 184"></path>
      <path d="M 147 198 C 151 192, 152 186, 152 180"></path>
      <path d="M 100 218 L 108 210"></path>
      <path d="M 106 220 L 114 212"></path>
      <path d="M 132 222 L 140 214"></path>
      <path d="M 138 220 L 146 212"></path>
    </g>
    <g class="draft">
      <path d="M 96 244 C 105 250, 137 250, 146 244"></path>
      <path d="M 106 254 C 112 258, 130 258, 136 254"></path>
    </g>
    <g class="tarms">
      <g class="tarm tarm-l">
        <circle class="joint" cx="76" cy="156" r="10"></circle>
        <path d="M 70 165 L 50 186"></path>
        <path d="M 79 172 L 58 192"></path>
        <circle class="joint" cx="49" cy="196" r="7"></circle>
        <path d="M 48 203 L 60 214"></path>
        <path d="M 55 200 L 67 211"></path>
      </g>
      <g class="tarm tarm-r">
        <circle class="joint" cx="166" cy="156" r="10"></circle>
        <path d="M 172 165 L 192 186"></path>
        <path d="M 163 172 L 184 192"></path>
        <circle class="joint" cx="193" cy="196" r="7"></circle>
        <path d="M 194 203 L 182 214"></path>
        <path d="M 187 200 L 175 211"></path>
      </g>
    </g>
    <g class="arm arm-l">
      <circle class="joint" cx="76" cy="156" r="10"></circle>
      <path d="M 72 165 L 70 190"></path>
      <path d="M 81 165 L 79 190"></path>
      <circle class="joint" cx="75" cy="196" r="8"></circle>
      <path d="M 71 203 L 68 232"></path>
      <path d="M 79 203 L 77 232"></path>
      <path d="M 68 232 C 62 240, 60 248, 63 254"></path>
      <path d="M 77 234 C 78 242, 74 250, 68 254"></path>
    </g>
    <g class="arm arm-r">
      <circle class="joint" cx="166" cy="156" r="10"></circle>
      <path d="M 170 165 L 172 190"></path>
      <path d="M 161 165 L 163 190"></path>
      <circle class="joint" cx="167" cy="196" r="8"></circle>
      <path d="M 171 203 L 174 232"></path>
      <path d="M 163 203 L 165 232"></path>
      <path d="M 174 232 C 180 240, 182 248, 179 254"></path>
      <path d="M 165 234 C 164 242, 168 250, 174 254"></path>
    </g>
    <g class="xarms">
      <g class="xarm xarm-r">
        <circle class="joint" cx="166" cy="156" r="10"></circle>
        <path d="M 170 165 L 173 181"></path>
        <path d="M 161 165 L 164 181"></path>
        <path class="xfore" d="M 169 179 L 96 171 C 88 170, 82 174, 83 180 C 83 185, 89 188, 95 188 L 170 188 Z"></path>
        <circle class="joint" cx="171" cy="184" r="7"></circle>
        <path class="xhand" d="M 90 179 C 93 179, 96 181, 96 185"></path>
      </g>
      <g class="xarm xarm-l">
        <circle class="joint" cx="76" cy="156" r="10"></circle>
        <path d="M 72 165 L 69 182"></path>
        <path d="M 81 165 L 78 182"></path>
        <path class="xfore" d="M 76 187 L 145 194 C 154 195, 158 199, 157 203 C 156 208, 150 209, 144 204 L 75 196 Z"></path>
        <circle class="joint" cx="73" cy="191" r="7"></circle>
        <path class="xhand" d="M 150 197 C 153 199, 153 202, 150 205"></path>
      </g>
    </g>
    <g class="dabarms">
      <g class="dabarm dabarm-l">
        <circle class="joint" cx="76" cy="156" r="10"></circle>
        <path d="M 73 151 L 183 87"></path>
        <path d="M 79 161 L 189 97"></path>
        <circle class="joint" cx="186" cy="92" r="8"></circle>
        <path d="M 184 87 L 110 121"></path>
        <path d="M 188 97 L 114 131"></path>
        <path class="dabhand" d="M 110 121 C 104 121, 100 125, 102 131"></path>
        <path class="dabhand" d="M 114 131 C 108 132, 104 130, 103 125"></path>
      </g>
      <g class="dabarm dabarm-r">
        <circle class="joint" cx="166" cy="156" r="10"></circle>
        <path d="M 173 150 L 205 105"></path>
        <path d="M 162 161 L 194 116"></path>
        <circle class="joint" cx="200" cy="111" r="7"></circle>
        <path d="M 204 106 L 231 68"></path>
        <path d="M 195 116 L 222 78"></path>
        <path class="dabhand" d="M 231 68 C 234 61, 231 56, 225 57"></path>
        <path class="dabhand" d="M 222 78 C 217 74, 217 68, 221 64"></path>
      </g>
    </g>
    <g class="egrab">
      <circle class="joint" cx="166" cy="156" r="10"></circle>
      <path d="M 170 156 L 189 102"></path>
      <path d="M 162 154 L 181 100"></path>
      <g class="eforearm">
        <circle class="joint" cx="185" cy="100" r="7"></circle>
        <path d="M 188 96 L 138 60"></path>
        <path d="M 182 104 L 132 68"></path>
        <path class="claw" d="M 131 58 C 126 61, 126 67, 131 70"></path>
        <path class="claw" d="M 139 57 C 135 62, 135 68, 139 72"></path>
      </g>
    </g>
    <g class="lid">
      <path class="lid-shell" d="M 64 210 L 56 128 C 56 124, 59 122, 63 122 L 177 122 C 181 122, 184 124, 184 128 L 176 210 Z"></path>
      <path class="lid-inset" d="M 70 203 L 63 130 L 177 130 L 170 203 Z"></path>
      <circle class="lid-mark" cx="120" cy="168" r="7"></circle>
      <path class="lid-mark" d="M 120 161 L 120 150"></path>
    </g>
    <g class="base">
      <path class="base-wall" d="M 64 210 L 176 210 L 174 222 C 174 225, 172 226, 169 226 L 71 226 C 68 226, 66 225, 66 222 Z"></path>
      <path class="hinge" d="M 67 213 L 173 213"></path>
      <path class="port" d="M 96 220 L 144 220"></path>
      <path class="port" d="M 78 220 L 88 220"></path>
    </g>
  </g>
  <g class="binset">
    <g class="bin">
      <path class="bin-body" d="M 185 224 L 233 224 L 225 270 C 225 272, 223 273, 220 273 L 198 273 C 195 273, 193 272, 193 270 Z"></path>
      <path class="bin-rib" d="M 199 234 L 197 264"></path>
      <path class="bin-rib" d="M 209 234 L 209 264"></path>
      <path class="bin-rib" d="M 219 234 L 221 264"></path>
      <g class="binflap">
        <path class="bin-hood" d="M 181 224 L 237 224 L 232 215 L 186 215 Z"></path>
        <path class="bin-knob" d="M 209 215 L 209 207"></path>
      </g>
    </g>
    <g class="mote">
      <circle cx="146" cy="76" r="5"></circle>
      <path d="M 143 74 L 149 74"></path>
      <path d="M 143 79 L 148 79"></path>
    </g>
    <g class="fly fly-a"><circle cx="198" cy="200" r="2.4"></circle><path d="M 195 197 L 197 199"></path><path d="M 201 197 L 199 199"></path></g>
    <g class="fly fly-b"><circle cx="218" cy="192" r="2.2"></circle><path d="M 215 189 L 217 191"></path><path d="M 221 189 L 219 191"></path></g>
    <g class="fly fly-c"><circle cx="188" cy="186" r="2"></circle><path d="M 186 184 L 187 185"></path><path d="M 190 184 L 189 185"></path></g>
  </g>
  <g class="think-dots">
    <circle cx="186" cy="60" r="4"></circle>
    <circle cx="200" cy="42" r="5.5"></circle>
    <circle cx="216" cy="24" r="7"></circle>
  </g>
  <g class="zzz">
    <path d="M 182 56 L 196 56 L 182 70 L 196 70"></path>
    <path d="M 200 32 L 212 32 L 200 44 L 212 44"></path>
  </g>
  <g class="sweat">
    <path d="M 178 66 C 183 73, 183 80, 178 83 C 173 80, 173 73, 178 66 Z"></path>
  </g>
  <g class="spark">
    <path d="M 186 62 L 186 50"></path>
    <path d="M 198 70 L 208 62"></path>
    <path d="M 172 54 L 166 44"></path>
  </g>
</svg>`;

  const CSS = `
:host {
  display: inline-block;
  line-height: 0;
  --otto-ink: #2b2b2b;
  --otto-paper: transparent;
  /* Second role, so the mascot can speak the same three-colour language as the mark: line,
     paper, and one warm accent on the antenna. Defaults to the ink, so the element dropped on
     a bare page still renders as the single-colour drawing it was designed as. */
  --otto-accent: var(--otto-ink);
  --otto-speed: 1;
}
.otto { width: 100%; height: auto; overflow: visible; display: block; }
.otto * { fill: none; stroke: var(--otto-ink); stroke-linecap: round; stroke-linejoin: round; }
.head, .torso { stroke-width: 2.4; fill: var(--otto-paper); }
.ear, .antenna .stem, .bulb, .arm path, .core { stroke-width: 2.2; }
.bulb, .joint { fill: var(--otto-paper); stroke-width: 2.2; }
.bulb, .antenna .stem, .glint { stroke: var(--otto-accent); }
.eye rect { fill: var(--otto-ink); stroke: none; }
.neck { stroke-width: 1.6; }
.core-inner, .glint { stroke-width: 1; }
.hatch, .hatch path { stroke-width: 1; opacity: .55; }
.head-hatch { stroke-width: 1.2; }
.draft path { stroke-width: 1.5; opacity: .55; }
.shadow ellipse { stroke-width: 1.2; opacity: .45; }
.think-dots circle, .zzz path, .sweat path, .spark path, .impact path { stroke-width: 1.8; opacity: 0; }
.think-dots circle { fill: var(--otto-paper); }

/* --- shared motion ------------------------------------------------- */
@keyframes otto-float { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-6px); } }
@keyframes otto-shadow { 0%, 100% { transform: scale(1); opacity: .45; } 50% { transform: scale(1.06); opacity: .3; } }
@keyframes otto-blink { 0%, 91%, 100% { transform: scaleY(1); } 94% { transform: scaleY(.08); } }
@keyframes otto-antenna { 0%, 100% { transform: rotate(-4deg); } 50% { transform: rotate(5deg); } }
@keyframes otto-look { 0%, 100% { transform: translateX(0); } 30% { transform: translateX(2px); } 70% { transform: translateX(-2px); } }
@keyframes otto-swing-l { 0%, 100% { transform: rotate(-5deg); } 50% { transform: rotate(6deg); } }
@keyframes otto-swing-r { 0%, 100% { transform: rotate(5deg); } 50% { transform: rotate(-6deg); } }
@keyframes otto-pulse { 0%, 100% { transform: scale(1); opacity: 1; } 50% { transform: scale(1.35); opacity: .5; } }
@keyframes otto-dots { 0% { opacity: 0; transform: translateY(4px); } 30% { opacity: .9; transform: translateY(0); } 70% { opacity: .9; } 100% { opacity: 0; } }
@keyframes otto-shake { 0%, 100% { transform: translateX(0) rotate(0); } 20% { transform: translateX(-4px) rotate(-2deg); } 40% { transform: translateX(4px) rotate(2deg); } 60% { transform: translateX(-3px) rotate(-1.5deg); } 80% { transform: translateX(3px) rotate(1deg); } }
@keyframes otto-pop { 0% { transform: scale(1) translateY(0); } 35% { transform: scale(1.06) translateY(-12px); } 100% { transform: scale(1) translateY(0); } }
@keyframes otto-drip { 0% { opacity: 0; transform: translateY(-6px); } 25% { opacity: .9; } 100% { opacity: 0; transform: translateY(14px); } }
@keyframes otto-flash { 0%, 100% { opacity: 0; } 20%, 60% { opacity: .9; } }
/* WORK: he types. The hands hold their inward angle over the keyboard and TAP - alternating,
   so it reads as typing rather than as two hands bobbing together - and the tap is mostly
   vertical, because a rotation big enough to see at this size swings the whole forearm.
   Every keyframe writes the same transform list, translate() then rotate(): a list that
   changes shape between stops drops CSS into matrix interpolation, which takes its own path
   and reads as the arm bending. */
/* PLANNING: he turns his back to us and works a whiteboard. The turn is done by SWAPPING
   detail, not by redrawing him - a robot's silhouette is the same from behind, so the front
   face (eyes, chest core, the skirt and hatching) hides and a head seam plus a back panel
   take their place. Cheaper than a second figure, and it cannot drift out of proportion
   with the front one.
   He also steps aside: the board needs the right half of the canvas, and standing him in
   front of his own diagram would hide the thing he is drawing. The offset lives INSIDE the
   float keyframes because .figure already animates transform - a second animation on the
   same property does not compose, it replaces. */
@keyframes otto-float-aside {
  0%, 100% { transform: translate(-44px, 0); }
  50%      { transform: translate(-44px, -6px); } }
@keyframes otto-shadow-aside {
  0%, 100% { transform: translate(-44px, 0) scale(1); opacity: .45; }
  50%      { transform: translate(-44px, 0) scale(1.06); opacity: .3; } }
/* The writing hand sweeps a short arc across the board, pausing at each end - a marker
   travelling at a constant speed reads as wiping, not writing. */
@keyframes otto-write {
  0%       { transform: translate(0, 0) rotate(-152deg); }
  14%      { transform: translate(0, 0) rotate(-138deg); }
  22%      { transform: translate(0, 0) rotate(-150deg); }
  36%      { transform: translate(0, 0) rotate(-136deg); }
  44%      { transform: translate(0, 0) rotate(-151deg); }
  58%      { transform: translate(0, 0) rotate(-139deg); }
  70%, 100%{ transform: translate(0, 0) rotate(-152deg); } }
/* The other arm hangs, with a little counter-sway so he is not a statue. */
@keyframes otto-idle-arm {
  0%, 100% { transform: translate(0, 0) rotate(3deg); }
  50%      { transform: translate(0, 0) rotate(-3deg); } }
/* Each mark appears where he has just been, then the board clears and he starts again. */
@keyframes otto-ink {
  0%, 6%   { opacity: 0; }
  12%, 88% { opacity: .85; }
  94%, 100%{ opacity: 0; } }
@keyframes otto-type-l {
  0%,  10%  { transform: translate(0, -5px) rotate(-9deg); }
  16%       { transform: translate(0, 3px)  rotate(-11deg); }
  24%, 54%  { transform: translate(0, -5px) rotate(-9deg); }
  60%       { transform: translate(0, 2px)  rotate(-8deg); }
  68%, 100% { transform: translate(0, -5px) rotate(-9deg); } }
@keyframes otto-type-r {
  0%,  32%  { transform: translate(0, -5px) rotate(9deg); }
  38%       { transform: translate(0, 3px)  rotate(11deg); }
  46%, 80%  { transform: translate(0, -5px) rotate(9deg); }
  86%       { transform: translate(0, 2px)  rotate(8deg); }
  94%, 100% { transform: translate(0, -5px) rotate(9deg); } }
.antenna, .eye, .arm, .figure, .shadow, .think-dots circle { transform-box: fill-box; transform-origin: center; }
.antenna { transform-origin: 50% 100%; }
/* The shoulder joints, in USER UNITS. These replace a fill-box percentage, which is a
   fraction of the group's own bounding box, so it moves the moment anything is added to the
   hand - quietly bending every arm animation with it. Both numbers are exactly what the old
   50% 8% resolved to, so nothing else changes.
   NOTE: no backticks anywhere in this stylesheet. It lives in a JS template literal, and one
   backtick in a comment ends the string and takes the whole component with it. */
.arm-l, .arm-r, .tarm { transform-box: view-box; }
.arm-l { transform-origin: 70.5px 154.6px; }
.arm-r { transform-origin: 169px 154.6px; }
.tarm-l { transform-origin: 76px 156px; }
.tarm-r { transform-origin: 166px 156px; }
/* Everything above the neck. Inert until a state asks for it, and the pivot is the NECK in
   user units: a fill-box centre sits mid-face and swings the head sideways off the body. */
.noggin { transform-box: view-box; transform-origin: 121px 134px; }
.face { transform-box: view-box; transform-origin: 121px 130px; }
.base { opacity: 0; }
.base-wall { fill: var(--otto-paper); stroke-width: 2.2; }
.base .hinge { stroke-width: 1.2; opacity: .5; }
.base .port { stroke-width: 1.4; opacity: .5; }
/* The laptop, drawn from BEHIND it - because he is behind it, facing us.
   The whole thing follows from where the machine points. The screen faces HIM, so what we
   see is the lid's back; the base extends AWAY from us, under and behind the lid, so the
   keyboard is not visible AT ALL and the only part of the base on our side is its rear
   wall - the thin slab under the hinge. Drawing a keyboard deck in front of the lid (the
   first cut did) puts the keys between us and the screen, which is a laptop facing US with
   him behind it typing on the far side of his own display: the perspective reads broken even
   to someone who cannot say why. Hence .base is a wall, drawn AFTER .lid because it is the
   nearest part of the machine, and entirely below the lid's bottom edge.
   The lid is LANDSCAPE and widens toward the top: a screen is wider than tall, and it tilts
   back toward the viewer, so its top edge is the near one. A panel as narrow as his torso
   and tall enough to reach his chin read as a lectern, not a laptop.
   Opaque paper is what puts him behind it, and the face is cropped exactly as a real one
   crops it: the lid tops out between his eyes and his chin.
   With his hands hidden behind the lid, the typing is carried by .tarms - a working-only
   pair of arms with the ELBOWS OUT past the lid's edges and the forearms disappearing behind
   its lower corners, which is what a person typing behind a laptop actually shows. The
   everyday arms are hidden for the duration (they hang to the hem, well below the machine,
   and read as legs sticking out under it). Same otto-type-* keyframes either way, so there
   is still one pair of tap timings to keep out of phase. */
.lid, .tarms { opacity: 0; }
/* The dab arms. Same reason .tarms and .egrab exist: the everyday arm is a rigid pair of
   bones pivoting at the shoulder, and a dab is defined by the FOLD - one elbow thrown out
   sideways with the forearm coming back across the face. A straight arm swung to the same
   angle reads as pointing at his own head. This pair is drawn in the finished pose, so the
   throw is one rotation per arm about its own shoulder, ending at zero. */
.dabarms { opacity: 0; }
/* The folded arms. Same reason .tarms, .dabarms and .egrab exist: the everyday arm is a rigid
   pair of bones pivoting at the shoulder, and a fold is two bends per arm - swinging the
   straight pair inward reads as reaching for his own chest. Drawn already folded, one forearm
   stacked above the other with each hand landing on the opposite arm, so the pose needs no
   animation of its own and the shake on .figure carries all the motion. */
.xarms { opacity: 0; }
.xarm path { stroke-width: 2.2; }
.xarm .xfore { fill: var(--otto-paper); stroke-width: 2.2; }
.xarm .xhand { stroke-width: 1.6; opacity: .8; }
.dabarm path { stroke-width: 2.2; }
.dabarm .dabhand { stroke-width: 2; }
.dabarm { transform-box: view-box; }
.dabarm-l { transform-origin: 76px 156px; }
.dabarm-r { transform-origin: 166px 156px; }
.tarm path { stroke-width: 2.2; }
.lid-shell { fill: var(--otto-paper); stroke-width: 2.2; }
.lid-inset { stroke-width: 1; opacity: .4; }
.lid .lid-mark { stroke: var(--otto-accent); stroke-width: 1.8; }
.board, .back { opacity: 0; }
.board rect:first-child { fill: var(--otto-paper); stroke-width: 2.2; }
.board .ink { stroke-width: 1.8; opacity: 0; }
.back rect { stroke-width: 1.6; }
.back circle { stroke-width: 1.2; }
.back path { stroke-width: 1.2; opacity: .8; }
/* The eviction props. Three groups, hidden until the state asks for them - same rule as the
   laptop: a bin left standing next to a sleeping Otto is a prop nobody put away.
   .skull is the top of his head made openable. It is NOT a second head: the cap traces the
   head path's own top edge exactly, so while it is shut the drawing is unchanged and the
   seam is invisible; the rim and the shaded void under it only exist once the cap swings up.
   .egrab is a working-only arm, for the same reason .tarms exists - the everyday arm is a
   rigid pair of bones pivoting at the shoulder, and reaching the top of his own head is a
   170-degree swing that passes through his torso. This one is already bent up and over.
   .binset stands OUTSIDE .figure so the bin keeps the ground while he floats, and so the
   memory lands in the same place every cycle rather than wherever his bob had reached. */
.skull, .egrab, .binset { opacity: 0; }
.skull-rim { fill: var(--otto-paper); stroke-width: 2.2; }
.skull-void { fill: var(--otto-ink); stroke: none; opacity: .18; }
.skull-cap { fill: var(--otto-paper); stroke-width: 2.4; }
.egrab path { stroke-width: 2.2; }
.egrab .claw { stroke-width: 2; }
.bin-body, .bin-hood { fill: var(--otto-paper); stroke-width: 2.2; }
.bin-rib { stroke-width: 1; opacity: .5; }
.bin-knob { stroke-width: 2; }
/* The memory itself: a small bead in the warm accent, the one colour on him that means
   "this is the thing" - the same role the antenna bulb plays in every other pose. */
.mote circle { fill: var(--otto-paper); stroke: var(--otto-accent); stroke-width: 2; }
.mote path { stroke-width: 1; opacity: .6; }
.mote { opacity: 0; }
.fly circle { fill: var(--otto-ink); stroke: none; }
.fly path { stroke-width: 1.2; opacity: .7; }
/* User units again, never a fill-box percentage: the cap hinges on the head's own left
   corner and the bin hood on its own right corner, and both are fixed points of the drawing
   rather than fractions of a bounding box that moves whenever anything is added. */
.skull-cap, .egrab, .eforearm, .binflap, .mote, .fly { transform-box: view-box; }
.skull-cap { transform-origin: 166px 63px; }
.egrab { transform-origin: 166px 156px; }
.eforearm { transform-origin: 185px 100px; }
/* The memory scales as it falls, and a scale needs an origin ON THE THING: transform-box
   view-box centres it on the whole drawing, so shrinking dragged the bead back toward the
   middle of the canvas and it fell down Otto's side instead of into the bin. */
.mote { transform-origin: 146px 76px; }
.binflap { transform-origin: 237px 224px; }

/* --- idle ---------------------------------------------------------- */
.figure { animation: otto-float calc(3.6s / var(--otto-speed)) ease-in-out infinite; }
.shadow { animation: otto-shadow calc(3.6s / var(--otto-speed)) ease-in-out infinite; }
.antenna { animation: otto-antenna calc(4s / var(--otto-speed)) ease-in-out infinite; }
.eye { animation: otto-blink calc(5.2s / var(--otto-speed)) ease-in-out infinite; }

/* --- thinking ------------------------------------------------------ */
:host([state="thinking"]) .figure { animation-duration: calc(5s / var(--otto-speed)); }
:host([state="thinking"]) .eye-l { transform: rotate(-10deg) scaleY(.75); }
:host([state="thinking"]) .eye-r { transform: rotate(-10deg) scaleY(.55) translateY(-3px); }
:host([state="thinking"]) .eye { animation: none; }
:host([state="thinking"]) .bulb { animation: otto-pulse calc(1.4s / var(--otto-speed)) ease-in-out infinite; transform-box: fill-box; transform-origin: center; }
:host([state="thinking"]) .think-dots circle { animation: otto-dots calc(1.8s / var(--otto-speed)) ease-in-out infinite; }
:host([state="thinking"]) .think-dots circle:nth-child(2) { animation-delay: calc(.25s / var(--otto-speed)); }
:host([state="thinking"]) .think-dots circle:nth-child(3) { animation-delay: calc(.5s / var(--otto-speed)); }

/* --- working ------------------------------------------------------- */
:host([state="working"]) .figure { animation-duration: calc(1.5s / var(--otto-speed)); }
:host([state="working"]) .shadow { animation-duration: calc(1.5s / var(--otto-speed)); }
:host([state="working"]) .eye { animation: none; transform: translateY(3px) scaleY(.3) scaleX(2.1); }
:host([state="working"]) .tarm-l { animation: otto-type-l calc(.9s / var(--otto-speed)) linear infinite; }
:host([state="working"]) .tarm-r { animation: otto-type-r calc(.9s / var(--otto-speed)) linear infinite; }
:host([state="working"]) .arm { opacity: 0; }
:host([state="working"]) .lid, :host([state="working"]) .base, :host([state="working"]) .tarms { opacity: 1; }

/* --- planning ------------------------------------------------------------------------ */
:host([state="planning"]) .figure { animation: otto-float-aside calc(4.4s / var(--otto-speed)) ease-in-out infinite; }
:host([state="planning"]) .shadow { animation: otto-shadow-aside calc(4.4s / var(--otto-speed)) ease-in-out infinite; }
:host([state="planning"]) .board, :host([state="planning"]) .back { opacity: 1; }
:host([state="planning"]) .eye, :host([state="planning"]) .core, :host([state="planning"]) .core-inner,
:host([state="planning"]) .draft, :host([state="planning"]) .shading, :host([state="planning"]) .head-hatch,
:host([state="planning"]) .glint { opacity: 0; }
:host([state="planning"]) .arm-r { animation: otto-write calc(3.2s / var(--otto-speed)) ease-in-out infinite; }
:host([state="planning"]) .arm-l { animation: otto-idle-arm calc(4.4s / var(--otto-speed)) ease-in-out infinite; }
:host([state="planning"]) .antenna { animation-duration: calc(3.4s / var(--otto-speed)); }
:host([state="planning"]) .board .ink { animation: otto-ink calc(3.2s / var(--otto-speed)) linear infinite; }
:host([state="planning"]) .i2 { animation-delay: calc(.42s / var(--otto-speed)); }
:host([state="planning"]) .i3 { animation-delay: calc(.84s / var(--otto-speed)); }
:host([state="planning"]) .i4 { animation-delay: calc(1.26s / var(--otto-speed)); }
:host([state="planning"]) .i5 { animation-delay: calc(1.68s / var(--otto-speed)); }
:host([state="planning"]) .i6 { animation-delay: calc(2.1s / var(--otto-speed)); }
:host([state="working"]) .antenna { animation-duration: calc(1.1s / var(--otto-speed)); }
:host([state="working"]) .sweat path { animation: otto-drip calc(1.6s / var(--otto-speed)) ease-in infinite; }

/* EVICTING: memory garbage collection, played out. He flips the top of his head open like a
   bin lid, lifts one memory out with a service arm, tosses it into the trash and shuts up
   again. The bin has flies, because the joke only lands if the bin is obviously a bin.
   Four things move on ONE clock, so the timing is written as percentages of a single 4.6s
   cycle rather than as four durations that would drift apart the moment one is tuned:
     6-16%   the head opens         (and stays open while the hand is inside)
     20-28%  the hand reaches in and grips
     36-48%  it lifts, winds back, and throws
     48-78%  the memory arcs across and drops through the bin mouth
     58-68%  the head shuts again   (after the hand is clear, never onto his own wrist)
   The antenna is mounted on the cap, so it rides the SAME keyframes: hinged anywhere off
   centre the cap would otherwise swing straight through the stem, and an opaque cap would
   swallow it. The flies are on their own loops - they are scenery, not part of the beat. */
@keyframes otto-headlid {
  0%, 6%    { transform: rotate(0deg); }
  16%, 58%  { transform: rotate(30deg); }
  68%, 100% { transform: rotate(0deg); } }
/* The arm carries a translate as well as a rotation: pivoting a rigid arm at the shoulder
   moves the hand almost horizontally at the top of its arc, so rotation alone cannot dip
   into the head and lift back out of it. Every stop writes translate() then rotate(), for
   the same reason the typing keyframes do - a list that changes shape between stops drops
   CSS into matrix interpolation and the arm appears to bend. */
@keyframes otto-grab {
  0%, 10%   { transform: rotate(0deg); }
  24%       { transform: rotate(-3deg); }
  48%       { transform: rotate(4deg); }
  64%, 100% { transform: rotate(0deg); } }
@keyframes otto-reach {
  0%, 10%   { transform: rotate(25deg); }
  20%, 28%  { transform: rotate(-4deg); }
  36%       { transform: rotate(25deg); }
  42%       { transform: rotate(15deg); }
  48%       { transform: rotate(48deg); }
  56%       { transform: rotate(36deg); }
  64%, 100% { transform: rotate(25deg); } }
/* The memory tracks the HAND until the throw (the offsets are the hand positions those arm
   stops resolve to), then leaves it and arcs on its own. It fades a frame after it passes
   the rim rather than at it: vanishing exactly on the edge reads as deleted in mid-air. */
@keyframes otto-mote {
  0%, 16%   { opacity: 0; transform: translate(-22px, -9px) scale(.5); }
  22%, 28%  { opacity: 1; transform: translate(-22px, -9px) scale(1); }
  36%       { opacity: 1; transform: translate(5px, -36px) scale(1); }
  42%       { opacity: 1; transform: translate(2px, -32px) scale(1); }
  48%       { opacity: 1; transform: translate(37px, -45px) scale(1); }
  56%       { opacity: 1; transform: translate(54px, -46px) scale(1); }
  64%       { opacity: 1; transform: translate(63px, -10px) scale(.95); }
  71%       { opacity: 1; transform: translate(63px, 60px) scale(.85); }
  76%       { opacity: 1; transform: translate(63px, 138px) scale(.75); }
  79%, 100% { opacity: 0; transform: translate(63px, 156px) scale(.7); } }
@keyframes otto-binflap {
  0%, 66%   { transform: rotate(0deg); }
  72%, 78%  { transform: rotate(24deg); }
  86%, 100% { transform: rotate(0deg); } }
@keyframes otto-fly-a {
  0%, 100% { transform: translate(0, 0); }
  25%      { transform: translate(9px, -7px); }
  50%      { transform: translate(3px, -15px); }
  75%      { transform: translate(-8px, -6px); } }
@keyframes otto-fly-b {
  0%, 100% { transform: translate(0, 0); }
  30%      { transform: translate(-11px, 6px); }
  60%      { transform: translate(-4px, 13px); }
  85%      { transform: translate(6px, 4px); } }
@keyframes otto-fly-c {
  0%, 100% { transform: translate(0, 0); }
  35%      { transform: translate(7px, 9px); }
  70%      { transform: translate(13px, -4px); } }

/* --- evicting ------------------------------------------------------ */
:host([state="evicting"]) .figure { animation-duration: calc(3.2s / var(--otto-speed)); }
:host([state="evicting"]) .shadow { animation-duration: calc(3.2s / var(--otto-speed)); }
:host([state="evicting"]) .skull { opacity: 1; }
:host([state="evicting"]) .egrab { opacity: 1; }
:host([state="evicting"]) .binset { opacity: 1; }
:host([state="evicting"]) .arm-r { opacity: 0; }
:host([state="evicting"]) .eye { animation: none; transform: translateY(1px) scaleY(.6) scaleX(1.3); }
:host([state="evicting"]) .skull-cap { animation: otto-headlid calc(4.6s / var(--otto-speed)) ease-in-out infinite; }
:host([state="evicting"]) .antenna { animation: otto-headlid calc(4.6s / var(--otto-speed)) ease-in-out infinite;
  transform-box: view-box; transform-origin: 166px 63px; }
:host([state="evicting"]) .egrab { animation: otto-grab calc(4.6s / var(--otto-speed)) ease-in-out infinite; }
:host([state="evicting"]) .eforearm { animation: otto-reach calc(4.6s / var(--otto-speed)) ease-in-out infinite; }
:host([state="evicting"]) .mote { animation: otto-mote calc(4.6s / var(--otto-speed)) linear infinite; }
:host([state="evicting"]) .binflap { animation: otto-binflap calc(4.6s / var(--otto-speed)) ease-in-out infinite; }
:host([state="evicting"]) .fly-a { animation: otto-fly-a calc(1.5s / var(--otto-speed)) linear infinite; }
:host([state="evicting"]) .fly-b { animation: otto-fly-b calc(1.9s / var(--otto-speed)) linear infinite; }
:host([state="evicting"]) .fly-c { animation: otto-fly-c calc(1.2s / var(--otto-speed)) linear infinite; }

/* --- success ------------------------------------------------------- */
:host([state="success"]) .figure { animation: otto-pop calc(.9s / var(--otto-speed)) cubic-bezier(.3, 1.6, .5, 1) 1 both, otto-float calc(3.6s / var(--otto-speed)) ease-in-out infinite calc(.9s / var(--otto-speed)); }
:host([state="success"]) .eye { animation: none; transform: scaleY(.5) scaleX(1.5) translateY(-2px); }
:host([state="success"]) .arm-l { transform: rotate(42deg); transition: transform .3s cubic-bezier(.3, 1.6, .5, 1); }
:host([state="success"]) .arm-r { transform: rotate(-42deg); transition: transform .3s cubic-bezier(.3, 1.6, .5, 1); }
:host([state="success"]) .spark path { animation: otto-flash calc(.9s / var(--otto-speed)) ease-out 1 both; }

/* --- dab ------------------------------------------------------------
   The celebration when a run lands clean. A dab is a POSE, not a wiggle: the read comes
   entirely from the two arms ending up parallel on the same diagonal - one folded across the
   face, one thrown out past it - so both are animated to a held angle with fill-mode both,
   never to a transition that could be interrupted mid-swing and leave him half-posed.
   The lean rides the float keyframes for the same reason planning steps aside inside its own:
   .figure already animates transform, and a second animation on one property replaces it
   rather than composing, so the tilt has to be written into every keyframe that moves him.
   The dip before the throw is what makes it read as a dab rather than as arms snapping to a
   new position - a body drops before it hits the pose. */
@keyframes otto-dab-hit {
  0%        { transform: translateY(0) rotate(0deg); }
  22%       { transform: translateY(7px) rotate(-2deg); }
  46%       { transform: translateY(-11px) rotate(11deg); }
  70%       { transform: translateY(-1px) rotate(6deg); }
  100%      { transform: translateY(-3px) rotate(8deg); } }
@keyframes otto-dab-hold {
  0%, 100%  { transform: translateY(-3px) rotate(8deg); }
  50%       { transform: translateY(-9px) rotate(8deg); } }
/* The bow. He does not turn to look at the throw - he drops his face into the crook of the
   folded arm, which is the whole tell of a dab: what the viewer sees is the top of the head
   and nothing of the face. So the head pitches hard forward on the NECK and sinks into the
   shoulders, and the folded forearm crosses what is left of the face.
   Tried and rejected: squashing the head flat to fake looking straight down. A box head at
   40% height stops being a head and reads as a pill, and it lands exactly where the forearm
   already is - two parallel bars and no character. A pitch keeps his proportions. */
@keyframes otto-dab-head {
  0%        { transform: rotate(0deg); }
  22%       { transform: rotate(-6deg); }
  52%       { transform: rotate(32deg); }
  70%, 100% { transform: rotate(28deg); } }
/* A little foreshortening on top of the pitch, and he settles down into his shoulders. */
@keyframes otto-dab-face {
  0%        { transform: translateY(0) scaleY(1); }
  22%       { transform: translateY(-2px) scaleY(1.02); }
  52%       { transform: translateY(7px) scaleY(.78); }
  70%, 100% { transform: translateY(6px) scaleY(.8); } }
/* The stalk is on top of a head that is now half as tall, so it rides the squash and undoes
   it: without the counter-scale the antenna foreshortens into a stub, and mounted outside the
   bow it simply floats above him. Kept near-upright on purpose - a rotated stalk inside a
   non-uniform scale comes out skewed, not tilted. */
@keyframes otto-dab-antenna {
  0%        { transform: scaleY(1) rotate(0deg); }
  22%       { transform: scaleY(1) rotate(5deg); }
  52%       { transform: scaleY(1.28) rotate(-4deg); }
  70%, 100% { transform: scaleY(1.25) rotate(-3deg); } }
/* The detail swap rides the SAME clock as the turn. Switched by a static rule instead, the
   far eye and the near ear are already gone on frame one - a full-width face missing an eye,
   which reads as a rendering bug rather than as a head about to move. */
@keyframes otto-dab-swap {
  0%, 26%   { opacity: 1; }
  38%, 100% { opacity: 0; } }
/* Both arms wind back a few degrees first, then swing past the pose and settle into it. */
@keyframes otto-dab-arm-l {
  0%        { transform: rotate(118deg); }
  22%       { transform: rotate(126deg); }
  52%       { transform: rotate(-8deg); }
  70%, 100% { transform: rotate(0deg); } }
@keyframes otto-dab-arm-r {
  0%        { transform: rotate(140deg); }
  22%       { transform: rotate(148deg); }
  52%       { transform: rotate(-10deg); }
  70%, 100% { transform: rotate(0deg); } }
:host([state="dab"]) .figure { transform-box: view-box; transform-origin: 121px 226px;
  animation: otto-dab-hit calc(.85s / var(--otto-speed)) cubic-bezier(.3, 1.5, .5, 1) 1 both, otto-dab-hold calc(3.6s / var(--otto-speed)) ease-in-out infinite calc(.85s / var(--otto-speed)); }
:host([state="dab"]) .arm { opacity: 0; }
:host([state="dab"]) .dabarms { opacity: 1; }
:host([state="dab"]) .noggin { animation: otto-dab-head calc(.85s / var(--otto-speed)) cubic-bezier(.3, 1.5, .5, 1) 1 both; }
:host([state="dab"]) .face { animation: otto-dab-face calc(.85s / var(--otto-speed)) cubic-bezier(.3, 1.5, .5, 1) 1 both; }
:host([state="dab"]) .dabarm-l { animation: otto-dab-arm-l calc(.85s / var(--otto-speed)) cubic-bezier(.3, 1.5, .5, 1) 1 both; }
:host([state="dab"]) .dabarm-r { animation: otto-dab-arm-r calc(.85s / var(--otto-speed)) cubic-bezier(.3, 1.5, .5, 1) 1 both; }
/* The eyes go with the bow - they are pointing at the floor, behind his own forearm - and
   they fade on the SAME clock as the pitch: dropped by a static rule they are gone on frame
   one, an upright face with no eyes, which reads as a rendering bug. The strokes are told not
   to squash with the foreshortening, or the horizontal lines come out thinner. */
:host([state="dab"]) .noggin * { vector-effect: non-scaling-stroke; }
:host([state="dab"]) .eye { animation: otto-dab-swap calc(.85s / var(--otto-speed)) linear 1 both; }
:host([state="dab"]) .antenna { animation: otto-dab-antenna calc(.85s / var(--otto-speed)) cubic-bezier(.3, 1.5, .5, 1) 1 both; }
:host([state="dab"]) .spark path { animation: otto-flash calc(.85s / var(--otto-speed)) ease-out 1 both; animation-delay: calc(.3s / var(--otto-speed)); }

/* --- error --------------------------------------------------------- */
:host([state="error"]) .figure { animation: otto-shake calc(.5s / var(--otto-speed)) ease-in-out 2 both, otto-float calc(3.6s / var(--otto-speed)) ease-in-out infinite calc(1s / var(--otto-speed)); }
:host([state="error"]) .eye { animation: none; transform: scaleY(.28) scaleX(1.8); }
:host([state="error"]) .eye-l { transform: rotate(14deg) scaleY(.28) scaleX(1.8); }
:host([state="error"]) .eye-r { transform: rotate(-14deg) scaleY(.28) scaleX(1.8); }
:host([state="error"]) .antenna { animation: none; transform: rotate(24deg); transition: transform .35s ease; }
:host([state="error"]) .arm { opacity: 0; }
:host([state="error"]) .xarms { opacity: 1; }

/* --- sleeping ------------------------------------------------------ */
:host([state="sleeping"]) .figure { animation-duration: calc(7s / var(--otto-speed)); }
:host([state="sleeping"]) .shadow { animation-duration: calc(7s / var(--otto-speed)); }
:host([state="sleeping"]) .eye { animation: none; transform: scaleY(.16) scaleX(1.6); }
:host([state="sleeping"]) .antenna { animation: none; transform: rotate(18deg); }
:host([state="sleeping"]) .zzz path { animation: otto-dots calc(3s / var(--otto-speed)) ease-in-out infinite; }
:host([state="sleeping"]) .zzz path:nth-child(2) { animation-delay: calc(.6s / var(--otto-speed)); }

:host([shadow="off"]) .shadow { display: none; }

@media (prefers-reduced-motion: reduce) {
  .otto *, .otto { animation: none !important; }
  .figure { animation: otto-float 6s ease-in-out infinite !important; }
}`;

  class OttoMascot extends HTMLElement {
    static get observedAttributes() { return ['size', 'color', 'speed']; }

    constructor() {
      super();
      const root = this.attachShadow({ mode: 'open' });
      const style = document.createElement('style');
      style.textContent = CSS;
      root.append(style);
      root.innerHTML += SVG;
      this._blinkTimer = null;
    }

    connectedCallback() {
      if (!this.hasAttribute('state')) this.setAttribute('state', 'idle');
      this._sync();
    }

    attributeChangedCallback() { this._sync(); }

    _sync() {
      const size = this.getAttribute('size');
      if (size) this.style.width = /^\d+$/.test(size) ? size + 'px' : size;
      const color = this.getAttribute('color');
      this.style.setProperty('--otto-ink', color || '#2b2b2b');
      const speed = parseFloat(this.getAttribute('speed'));
      this.style.setProperty('--otto-speed', speed > 0 ? speed : 1);
    }

    get state() { return this.getAttribute('state') || 'idle'; }
    set state(v) { this.setAttribute('state', STATES.includes(v) ? v : 'idle'); }

    /** One-off blink, whatever the state. */
    blink() {
      const eyes = this.shadowRoot.querySelectorAll('.eye');
      eyes.forEach((e) => {
        e.style.transition = 'none';
        e.animate(
          [{ transform: getComputedStyle(e).transform + ' scaleY(1)' },
           { transform: getComputedStyle(e).transform + ' scaleY(0.08)' },
           { transform: getComputedStyle(e).transform + ' scaleY(1)' }],
          { duration: 180, easing: 'ease-in-out' }
        );
      });
    }

    /** Play a state for `ms`, then go back to where it was. */
    react(state, ms = 1600) {
      const prev = this.state;
      this.state = state;
      clearTimeout(this._reactTimer);
      this._reactTimer = setTimeout(() => { this.state = prev === state ? 'idle' : prev; }, ms);
    }
  }

  if (!customElements.get('otto-mascot')) customElements.define('otto-mascot', OttoMascot);
  if (typeof window !== 'undefined') window.OttoMascot = OttoMascot;
})();
