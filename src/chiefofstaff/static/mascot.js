// The captain: Chief of Staff's duck, in a captain's cap. Five moods, as Home stands: idle
// (sways, blinks), working (at the ship's wheel), needs (salutes, an amber "!"), watching (a
// spyglass, looking about) and done (hops, wings up, sparkles). Pure SVG and CSS: nothing to
// load, no script once drawn, no text of its own, and still with reduced motion.

const SPARK = "l1.6 5.4 5.4 1.6-5.4 1.6-1.6 5.4-1.6-5.4-5.4-1.6 5.4-1.6z";
const KNOBS = [[80, 107], [80, 153], [57, 130], [103, 130], [63.7, 113.7], [96.3, 146.3], [96.3, 113.7], [63.7, 146.3]];

export const DUCK_SVG = `<svg class="duck-art" viewBox="0 0 160 170" aria-hidden="true" focusable="false">
  <ellipse class="d-shadow" cx="81" cy="160" rx="34" ry="5"/>
  <g class="d-bubble">
    <path d="M118 10h22a8 8 0 0 1 8 8v12a8 8 0 0 1-8 8h-12l-7 7v-7h-3a8 8 0 0 1-8-8V18a8 8 0 0 1 8-8z"/>
    <rect class="d-bang" x="127" y="15" width="4.4" height="12" rx="2.2"/>
    <circle class="d-bang" cx="129.2" cy="32" r="2.5"/>
  </g>
  <g class="d-sparks">
    <path class="s1" d="M28 33${SPARK}"/>
    <path class="s2" d="M134 22${SPARK}"/>
    <path class="s3" d="M138 98${SPARK}"/>
  </g>
  <g class="d-all">
    <ellipse class="d-foot" cx="68" cy="154" rx="10" ry="4.5"/>
    <ellipse class="d-foot" cx="94" cy="154" rx="10" ry="4.5"/>
    <path class="d-body" d="M44 124q0-30 36-30t38 26q8-4 10-12q6 20-10 32q-10 14-38 14t-36-30z"/>
    <g class="d-wings">
      <ellipse class="d-wing" cx="50" cy="126" rx="8" ry="14" transform="rotate(20 50 126)"/>
      <ellipse class="d-wing" cx="108" cy="126" rx="9" ry="15" transform="rotate(-20 108 126)"/>
    </g>
    <g class="d-helm">
      <g class="d-wheel">
        <path class="d-spoke" d="M80 107v46M57 130h46M63.7 113.7l32.6 32.6M96.3 113.7l-32.6 32.6"/>
        <circle class="d-rim" cx="80" cy="130" r="16"/>
        <circle class="d-hub" cx="80" cy="130" r="4.5"/>
        ${KNOBS.map(([x, y]) => `<circle class="d-knob" cx="${x}" cy="${y}" r="2.8"/>`).join("")}
      </g>
      <ellipse class="d-wing" cx="59" cy="121" rx="7" ry="12" transform="rotate(55 59 121)"/>
      <ellipse class="d-wing" cx="101" cy="121" rx="7" ry="12" transform="rotate(-55 101 121)"/>
    </g>
    <g class="d-head">
      <circle class="d-body" cx="80" cy="78" r="30"/>
      <g class="d-eyes"><g class="d-look">
        <ellipse class="d-eye" cx="69" cy="83" rx="4.8" ry="6.2"/>
        <ellipse class="d-eye d-right" cx="91" cy="83" rx="4.8" ry="6.2"/>
        <circle class="d-glint" cx="70.7" cy="80.7" r="1.7"/>
        <circle class="d-glint d-right" cx="92.7" cy="80.7" r="1.7"/>
      </g></g>
      <path class="d-happy" d="M64 85q5-7 10 0M86 85q5-7 10 0"/>
      <ellipse class="d-cheek" cx="59" cy="95" rx="6" ry="3.5"/>
      <ellipse class="d-cheek" cx="101" cy="95" rx="6" ry="3.5"/>
      <ellipse class="d-open" cx="80" cy="99" rx="7" ry="4.5"/>
      <ellipse class="d-beak" cx="80" cy="95" rx="11" ry="5.5"/>
      <path class="d-smile" d="M72 95q8 3 16 0"/>
      <path class="d-cap" d="M50 64q2-26 30-26t30 26z"/>
      <rect class="d-band" x="50" y="60" width="60" height="8" rx="2"/>
      <path class="d-visor" d="M50 66q30 9 60 0q-2 6-30 8q-28-2-30-8z"/>
      <circle class="d-badge" cx="80" cy="52" r="5"/>
      <circle class="d-badge-in" cx="80" cy="52" r="2"/>
      <g class="d-scope">
        <g transform="rotate(-10 91 83)">
          <rect class="d-tube" x="96" y="76.5" width="36" height="13" rx="2"/>
          <rect class="d-tube-band" x="112" y="76.5" width="4" height="13"/>
          <rect class="d-eyepiece" x="85" y="77" width="13" height="12" rx="3"/>
          <ellipse class="d-lens" cx="132" cy="83" rx="3" ry="6.5"/>
        </g>
        <ellipse class="d-wing" cx="121" cy="99" rx="7" ry="13" transform="rotate(-35 121 99)"/>
      </g>
    </g>
    <g class="d-salute"><ellipse class="d-wing" cx="117" cy="87" rx="7" ry="15" transform="rotate(-20 117 87)"/></g>
    <g class="d-cheer-l"><ellipse class="d-wing" cx="42" cy="106" rx="7" ry="14" transform="rotate(-35 42 106)"/></g>
    <g class="d-cheer-r"><ellipse class="d-wing" cx="118" cy="106" rx="7" ry="14" transform="rotate(35 118 106)"/></g>
  </g>
</svg>`.replace(/>\s+</g, "><");

export const MOODS = ["idle", "working", "needs", "watching", "done"];
// How long it moves after its mood changes (or it is drawn); then it stands still in that
// mood and only blinks, which costs nothing between blinks. A pointer over it wakes it.
export const MOVE_MS = 8000;
const rests = new WeakMap();

// A duck in a mood. Its loops follow the clock, not when it was drawn: one drawn again (a line
// rebuilt on each update) carries on where the last one was, without a jump.
export function duck(mood = "idle") {
  const holder = document.createElement("span");
  holder.innerHTML = DUCK_SVG;
  holder.style.setProperty("--sync", `${-(Date.now() % 60000)}ms`);
  setMood(holder, mood);
  return holder;
}
export function setMood(node, mood) {
  const name = MOODS.includes(mood) ? mood : "idle";
  if (node.dataset.mood === name) return;
  node.dataset.mood = name;
  node.className = `duck is-${name} moving`;
  clearTimeout(rests.get(node));
  rests.set(node, setTimeout(() => node.classList.remove("moving"), MOVE_MS));
}
