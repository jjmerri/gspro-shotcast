// The demo site's walkthrough (tools/demo/build.py adds it to the demo's copy of the page; the stats
// site people install never has it). It offers itself on a first visit to the home page, steps through
// the page's views, and ends with ×, "End tour" or Esc at any point. The "Tour" button brings it back.
(() => {
  const KEY = "shotcast.demo.tour";
  const ME = "player=Josh+Mercer";
  const VOD = "20261104-133911-67149a";   // Premonition: the round with a recording
  const HOLE = 8;                         // its replay and birdie putt
  const PUTT = 4041;                      // the hole 8 birdie putt
  let roundId = null;

  const STEPS = [
    { title: "Welcome to the Shotcast demo",
      text: "Shotcast is an AI commentary booth for GSPro golf sims: two commentators call every shot live, and "
          + "everything played lands on a stats site like this one. These are real rounds (names changed) "
          + "in a made-up league. Take a one-minute tour, or look around on your own." },
    { hash: "#home?player=", el: "#tabs", title: "The site",
      text: "Home, Rounds, Players and Watch. It's built from the sim's own shot data, so there's nothing to enter by hand." },
    { hash: "#home?player=", el: ".league", title: "League night",
      text: "League weeks score themselves: each team's net, the week's results and the season standings." },
    { hash: "#home?player=", el: "[data-recap]", title: "The week's recap",
      text: "When a week ends, the booth writes and narrates a recap video over the round's best moments." },
    { hash: `#rounds?${ME}`, el: "tr[data-round]", title: "Every round",
      text: "Every round played at the sim, with the score and how it went. Next opens Josh's latest.",
      leave: () => { const r = document.querySelector("tr[data-round]"); roundId = r && r.dataset.round; } },
    { hash: () => roundId ? `#round?${ME}&id=${roundId}` : `#rounds?${ME}`, el: ".scorecard", title: "The card",
      text: "Hole by hole, with the out and in nines. Every hole number opens a replay of that hole: press 8 (or Next).",
      click: `.scorecard [data-replay="${HOLE}"]` },
    { hash: () => roundId ? `#round?${ME}&id=${roundId}&hole=${HOLE}` : `#rounds?${ME}`, el: ".replay svg", title: "Hole replays",
      text: "The hole redrawn from the sim's shot data, every player's shots traced from tee to cup as they played it. "
          + "Above it, ↻ plays it again and ▶ jumps to the hole in the broadcast." },
    { hash: `#shots?${ME}&course=Premonition&shot=${PUTT}`, el: `tr[data-shot="${PUTT}"]`, title: "Every shot",
      text: "Each shot's launch numbers, where it finished, and what the booth said about it. Here's a birdie putt on 8." },
    { hash: `#shots?${ME}&course=Premonition&shot=${PUTT}`, el: `tr[data-shot="${PUTT}"] [data-vod]`, title: "Jump to the video",
      text: "Rounds are recorded, and every shot links to its moment in the broadcast. Press ▶ (or Next) to watch this putt.",
      click: `tr[data-shot="${PUTT}"] [data-vod]` },
    { hash: `#vods?${ME}&vod=${VOD}&at=1504`, el: "#liveslot", title: "Right to the moment",
      text: "The round's replay, starting at the putt, with the booth's call. Every replay can be scrubbed and shared." },
    { hash: "#live", el: "#liveslot", title: "Live",
      text: "While a round is on, it streams here live with the commentary. In the demo, hole 8 of that round plays on a loop." },
    { hash: "#live", el: "#chatslot", title: "Chat",
      text: "Viewers chat along, and the booth can read it out. This demo's chat is scripted: what you type is only kept in your browser." },
    { hash: `#player?${ME}&name=Josh+Mercer`, el: "#profile", title: "Players",
      text: "Each player's page: a handicap index worked out from their rounds, scoring trends, clubs and records." },
    { title: "That's the tour",
      text: "Click around: try ▶ on any shot in Rounds → Shots. The Tour button at the bottom left runs this again." },
  ];

  const css = document.createElement("style");
  css.textContent = `
    #tour-spot { position:fixed; z-index:1000; pointer-events:none; border-radius:8px; border:2px solid var(--accent);
                 box-shadow:0 0 0 9999px rgba(0,0,0,.55); transition:all .25s ease; }
    #tour-card { position:fixed; z-index:1001; width:min(360px, calc(100vw - 24px)); background:var(--panel);
                 border:1px solid var(--accent); border-radius:10px; padding:14px 16px 12px; color:var(--text);
                 box-shadow:0 8px 28px rgba(0,0,0,.6); font-size:14px; line-height:1.45; }
    #tour-card h3 { margin:0 28px 6px 0; font-size:16px; }
    #tour-card p { margin:0 0 12px; color:var(--text); }
    #tour-card .row { display:flex; align-items:center; gap:8px; }
    #tour-card .n { color:var(--dim); font-size:12px; margin-right:auto; }
    #tour-card button { font:inherit; border-radius:6px; padding:5px 12px; cursor:pointer;
                        border:1px solid var(--line); background:transparent; color:var(--text); }
    #tour-card button.go { background:var(--accent); border-color:var(--accent); color:#0f1715; font-weight:600; }
    #tour-card .x { position:absolute; top:6px; right:8px; border:0; padding:2px 8px; font-size:18px; color:var(--dim); }
    #tour-open { position:fixed; left:14px; bottom:14px; z-index:49; font:inherit; font-size:13px; cursor:pointer;
                 background:var(--panel); color:var(--text); border:1px solid var(--accent); border-radius:999px;
                 padding:6px 14px; box-shadow:0 4px 14px rgba(0,0,0,.4); }
    @media (max-width: 600px) { #tour-card { left:12px !important; right:12px; bottom:12px; top:auto !important; width:auto; } }`;
  document.head.appendChild(css);

  const open = document.createElement("button");
  open.id = "tour-open";
  open.textContent = "🧭 Tour";
  open.title = "A walk through the demo";
  open.onclick = () => start(1);
  document.body.appendChild(open);

  let at = -1, spot = null, card = null, target = null, timer = null;
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

  function start(i = 0) {
    open.hidden = true;
    if (!card) {
      card = document.createElement("div");
      card.id = "tour-card";
      card.setAttribute("role", "dialog");
      card.setAttribute("aria-label", "Demo tour");
      document.body.appendChild(card);
      card.addEventListener("click", e => {
        const b = e.target.closest("button");
        if (!b) return;
        if (b.dataset.t === "next") next();
        else if (b.dataset.t === "back") show(at - 1);
        else end();
      });
    }
    show(i);
  }

  function end() {
    try { localStorage.setItem(KEY, "seen"); } catch (e) {}
    clearInterval(timer);
    at = -1; target = null;
    if (spot) spot.remove();
    if (card) card.remove();
    spot = card = null;
    open.hidden = false;
  }

  function next() {
    const s = STEPS[at];
    if (s.leave) s.leave();
    if (at === STEPS.length - 1) return end();
    if (s.click) {
      const b = document.querySelector(s.click);
      if (b) return b.click();   // the click listener below moves on
    }
    show(at + 1);
  }

  function show(i, navigated = false) {
    at = Math.max(0, Math.min(i, STEPS.length - 1));
    const s = STEPS[at], last = at === STEPS.length - 1;
    const hash = typeof s.hash === "function" ? s.hash() : s.hash;
    if (hash && !navigated && location.hash !== hash) location.hash = hash;
    const intro = at === 0;
    card.innerHTML = `<button class="x" data-t="end" title="End the tour" aria-label="End the tour">×</button>
      <h3>${esc(s.title)}</h3><p>${esc(s.text)}</p><div class="row">
      ${intro ? "" : `<span class="n">${at} of ${STEPS.length - 1}</span>`}
      ${intro ? `<button data-t="end">Look around myself</button>` : `<button data-t="end">End tour</button>`}
      ${at > 1 ? `<button data-t="back">Back</button>` : ""}
      <button class="go" data-t="next">${intro ? "Take the tour" : last ? "Done" : "Next"}</button></div>`;
    card.querySelector(".go").focus({ preventScroll: true });
    target = null;
    if (spot) spot.hidden = true;
    place();
    clearInterval(timer);
    if (!s.el) return;
    const want = at, since = Date.now();
    let lastY = -1, aligns = 0;
    // The views draw after their data loads: wait for the step's element, then keep up with the layout.
    timer = setInterval(() => {
      if (at !== want) return;
      if (!target || !target.isConnected) {
        const el = document.querySelector(s.el);
        if (el && el.getClientRects().length) {
          target = el;
          el.scrollIntoView({ block: "center", behavior: "smooth" });
        } else if (Date.now() - since > 12000) { clearInterval(timer); return; }
      }
      // A tall target (the hole replay, once its map loads) goes to the top, under the sticky header, to leave room
      // for the card. The page scrolls a hole link's replay to the middle itself, so this waits for scrolling to
      // settle and checks again, for the step's first few seconds only (after that the scroll is the visitor's).
      if (target && target.getBoundingClientRect().height > innerHeight / 2 && Date.now() - since < 5000 && aligns < 3) {
        const head = document.querySelector("header");
        const off = target.getBoundingClientRect().top - (head ? Math.max(0, head.getBoundingClientRect().bottom) : 0) - 12;
        if (Math.abs(off) > 16 && scrollY === lastY) { aligns += 1; scrollTo({ top: scrollY + off, behavior: "smooth" }); }
      }
      lastY = scrollY;
      place();
    }, 200);
  }

  function place() {
    if (!card) return;
    const r = target && target.isConnected ? target.getBoundingClientRect() : null;
    if (!r || !r.width) {
      if (spot) spot.hidden = true;
      Object.assign(card.style, { left: `${(innerWidth - card.offsetWidth) / 2}px`, top: `${Math.max(12, (innerHeight - card.offsetHeight) / 2)}px` });
      return;
    }
    if (!spot) { spot = document.createElement("div"); spot.id = "tour-spot"; document.body.appendChild(spot); }
    spot.hidden = false;
    const pad = 6;
    Object.assign(spot.style, { left: `${r.left - pad}px`, top: `${r.top - pad}px`, width: `${r.width + 2 * pad}px`, height: `${r.height + 2 * pad}px` });
    const h = card.offsetHeight, w = card.offsetWidth;
    const below = r.bottom + pad + 12, above = r.top - pad - 12 - h, right = r.right + pad + 12;
    const fitsRight = right + w < innerWidth - 12, tall = r.height > innerHeight / 2;
    let top, left = Math.min(Math.max(12, r.left), innerWidth - w - 12);
    if (below + h < innerHeight) top = below;
    else if (fitsRight && tall) { left = right; top = Math.min(Math.max(12, r.top), innerHeight - h - 12); }
    else if (above > 0) top = above;
    else if (fitsRight) { left = right; top = Math.min(Math.max(12, r.top), innerHeight - h - 12); }
    else top = innerHeight - h - 12;
    Object.assign(card.style, { left: `${left}px`, top: `${top}px` });
  }

  addEventListener("resize", place);
  addEventListener("scroll", place, { passive: true });
  addEventListener("keydown", e => { if (e.key === "Escape" && card) end(); });
  // Pressing the highlighted ▶ itself goes on to the next step, as Next would.
  document.addEventListener("click", e => {
    const s = STEPS[at];
    if (s && s.click && e.target.closest(s.click)) { at += 1; show(at, true); }
  }, true);

  let seen = null;
  try { seen = localStorage.getItem(KEY); } catch (e) {}
  if (!seen && (!location.hash || location.hash.startsWith("#home"))) start(0);
})();
