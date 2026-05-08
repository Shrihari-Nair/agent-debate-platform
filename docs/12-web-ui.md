# Web UI — The Browser Observer

The browser UI lives in [web/index.html](../web/index.html). It is a single HTML file — no build step, no bundler, no framework. It imports the LiveKit client SDK directly from a CDN via ES modules.

---

## Architecture at a Glance

```
index.html
├── CSS (variables, layout, participant cards, transcript, badges)
├── HTML structure
│   ├── aside (left panel) — form + participants
│   └── section (right panel) — live transcript
└── <script type="module">
    ├── Imports: Room, RoomEvent, Track from livekit-client@2.9.0
    ├── State: room, participants map, turns array
    ├── Debater form (render + add/remove)
    ├── LiveKit room event handlers
    ├── Data packet handlers (6 event types)
    └── startDebate() → POST /debate → connectToRoom()
```

---

## What Is a LiveKit Client?

The LiveKit client SDK (`livekit-client`) is a JavaScript library that handles WebRTC connections to LiveKit rooms. It abstracts:
- WebSocket signalling with the LiveKit server
- ICE negotiation (the protocol that establishes peer-to-peer paths)
- Audio/video track subscription
- Data channel messages

You create a `Room` object, attach event handlers, and call `room.connect(wsUrl, token)`. After that, the SDK emits events as things happen: participants joining, audio tracks becoming available, data being received.

---

## SDK Import

```javascript
import {
    Room,
    RoomEvent,
    Track,
} from "https://esm.sh/livekit-client@2.9.0";
```

`esm.sh` is a CDN that serves npm packages as ES modules. No npm install, no package.json — the browser fetches this directly. Pin the version (`@2.9.0`) to avoid breaking changes from newer releases.

---

## State Object

```javascript
const state = {
    room: null,
    participants: new Map(), // identity -> { role, name, stance, removed, active }
    turns: [],
    activeTurns: new Map(), // "slug:phase" -> DOM card element (while streaming)
};
```

- `room` — the active `Room` instance, or `null` if not connected.
- `participants` — a `Map` keyed by participant identity (e.g. `"judge"`, `"debater-pro"`, `"debater-con"`). Each entry stores the metadata needed to render a participant card.
- `turns` — an array of rendered DOM elements (used to clear the placeholder on first turn).
- `activeTurns` — a `Map` keyed by `"slug:phase"` (e.g. `"pro:rebuttal_1"`). Holds a reference to the DOM card for a debater's turn **while that turn is actively streaming**. The streaming event handlers (`turn_status`, `research_chunk`, `turn_text_chunk`) use this map to find the right card without searching the DOM. Entries are deleted when `turnSpoken` or `turn_text_end` finalises the card.

---

## The Debater Form

### Initial Render

```javascript
const DEFAULT_DEBATERS = [
    { slug: "pro", name: "Alex (Pro)", stance: "Yes, AI agents should have voting rights..." },
    { slug: "con", name: "Morgan (Con)", stance: "No, voting rights must remain exclusive to humans..." },
];
const debaters = [...DEFAULT_DEBATERS];
renderDebaters(debaters);
```

On page load, two debater cards are pre-filled with the default pro/con stances. Users can edit all fields, add debaters (up to 4), or remove them (down to 2).

### `renderDebaters()` — Reactive Re-rendering

```javascript
function renderDebaters(list) {
    const root = $("debaters");
    root.innerHTML = "";      // clear existing cards
    list.forEach((d, i) => {
        // build card HTML with data-i and data-k attributes
        const card = document.createElement("div");
        card.className = "debater-card";
        card.innerHTML = `
          <div class="row">
            <input data-i="${i}" data-k="name" ... value="${d.name}" />
            <input data-i="${i}" data-k="slug" ... value="${d.slug}" />
          </div>
          ...
        `;
        root.appendChild(card);
    });
    // attach input handlers that update the `list` array in-place
    root.querySelectorAll("input,textarea").forEach((el) => {
        el.addEventListener("input", (e) => {
            const i = Number(e.target.dataset.i);
            const k = e.target.dataset.k;
            list[i][k] = e.target.value;
        });
    });
    // attach remove buttons
    root.querySelectorAll("button[data-remove]").forEach((b) => {
        b.addEventListener("click", (e) => {
            const i = Number(e.target.dataset.remove);
            if (list.length > 2) {
                list.splice(i, 1);
                renderDebaters(list);  // re-render after removal
            }
        });
    });
}
```

Each input has `data-i` (index into the `debaters` array) and `data-k` (which property to update). The event handlers use these to update the `debaters` array in place. When a debater is removed, the array is mutated and the form is fully re-rendered.

---

## Starting a Debate: `startDebate()`

```javascript
async function startDebate() {
    $("start-btn").disabled = true;
    $("ctrl-status").textContent = "Creating room and dispatching agents...";
    try {
        const payload = {
            topic: $("topic").value.trim(),
            debaters: debaters.map((d) => ({
                slug: d.slug.trim(),
                name: d.name.trim(),
                stance: d.stance.trim(),
            })),
        };
        const res = await fetch(`${ORCHESTRATOR_URL}/debate`, {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify(payload),
        });
        if (!res.ok) {
            const err = await res.text();
            throw new Error(err || `HTTP ${res.status}`);
        }
        const data = await res.json();
        $("room-tag").textContent = data.room;
        $("room-tag").style.display = "inline-block";
        $("ctrl-status").textContent = `Connected to ${data.room}.`;
        await connectToRoom(data.ws_url, data.observer_token);
    } catch (e) {
        $("ctrl-status").innerHTML = `<span class="danger">Error: ${escapeHtml(e.message)}</span>`;
        $("start-btn").disabled = false;
    }
}
```

### Steps

1. Disable the start button to prevent double-submission.
2. Build the `POST /debate` payload from the form.
3. `fetch()` the orchestrator — `await` makes this async.
4. On success, extract `ws_url` and `observer_token` from the response.
5. Call `connectToRoom()` with those credentials.
6. On error, show the error message and re-enable the button.

---

## Connecting to the Room: `connectToRoom()`

```javascript
async function connectToRoom(wsUrl, token) {
    const room = new Room({ adaptiveStream: true, dynacast: true });
    state.room = room;

    room
        .on(RoomEvent.Connected, () => setStatus("connected", "live"))
        .on(RoomEvent.Disconnected, () => setStatus("disconnected"))
        .on(RoomEvent.ParticipantConnected, (p) => { ... })
        .on(RoomEvent.ParticipantDisconnected, (p) => { ... })
        .on(RoomEvent.TrackSubscribed, (track, _pub, participant) => { ... })
        .on(RoomEvent.TrackUnsubscribed, (track) => { ... })
        .on(RoomEvent.ActiveSpeakersChanged, (speakers) => { ... })
        .on(RoomEvent.DataReceived, (payload, _participant, _kind, topic) => { ... });

    await room.connect(wsUrl, token);

    // Seed participants already in the room
    for (const [id, p] of room.remoteParticipants) {
        const attrs = p.attributes || {};
        state.participants.set(id, { ... });
    }
    renderParticipants();
}
```

### `Room` Options

- `adaptiveStream: true` — adjusts audio quality based on network conditions.
- `dynacast: true` — only subscribes to tracks that are needed (optimisation for larger rooms).

### Room Event Handlers

#### `RoomEvent.ParticipantConnected`

```javascript
.on(RoomEvent.ParticipantConnected, (p) => {
    const attrs = p.attributes || {};
    const role = attrs.role || "participant";
    const existing = state.participants.get(p.identity) || {};
    state.participants.set(p.identity, {
        ...existing,
        role,
        name: p.name || existing.name || p.identity,
        slug: attrs.slug || existing.slug,
        stance: attrs.stance || existing.stance,
    });
    renderParticipants();
})
```

When an agent connects, reads its `attributes` (set by `req.accept(attributes=...)` in the agent code) to get `role`, `slug`, and `stance`. The `...existing` spread preserves any data already seeded from the `debate_started` event (debaters' stances are pre-seeded before they physically connect).

#### `RoomEvent.TrackSubscribed`

```javascript
.on(RoomEvent.TrackSubscribed, (track, _pub, participant) => {
    if (track.kind === Track.Kind.Audio) {
        const el = track.attach();
        el.dataset.identity = participant.identity;
        el.autoplay = true;
        $("audio-mount").appendChild(el);
    }
})
```

When an audio track becomes available (i.e., an agent starts TTS), `track.attach()` creates an HTML `<audio>` element bound to that track. Appending it to `#audio-mount` (a hidden `div`) causes the browser to play the audio automatically.

`el.autoplay = true` — audio autoplay requires a prior user gesture. Clicking "Start debate" is that gesture, which is why autoplay works here.

#### `RoomEvent.ActiveSpeakersChanged`

```javascript
.on(RoomEvent.ActiveSpeakersChanged, (speakers) => {
    const active = new Set(speakers.map((s) => s.identity));
    for (const [id, info] of state.participants) {
        info.active = active.has(id);
    }
    renderParticipants();
})
```

LiveKit emits this event whenever the set of currently-speaking participants changes (based on audio level detection). The `active` flag is used to add a blue glow border (`.pcard.active { border-color: var(--live); }`) to the participant card of whoever is currently speaking.

#### `RoomEvent.DataReceived`

```javascript
.on(RoomEvent.DataReceived, (payload, _participant, _kind, topic) => {
    if (topic && topic !== "debate.event") return;
    try {
        const ev = JSON.parse(new TextDecoder().decode(payload));
        handleEvent(ev);
    } catch (e) {
        console.warn("bad data packet", e);
    }
})
```

Data packets arrive as `Uint8Array` (raw bytes). `new TextDecoder().decode(payload)` converts bytes to a string. `JSON.parse()` converts the string to an object. The filter `topic !== "debate.event"` ignores packets on other topics.

---

## Data Event Handlers

### `handleEvent()` — The Dispatcher

```javascript
function handleEvent(ev) {
    if (!ev || !ev.type) return;
    switch (ev.type) {
        case "debate_started":    /* pre-seed stances */; break;
        case "phase_started":     phase(ev); break;
        case "turn_spoken":       turnSpoken(ev.entry); break;
        case "fact_check":        factCheck(ev); break;
        case "debater_removed":   removedNote(ev); break;
        case "verdict":           verdictNote(ev); break;
        // Real-time streaming events from debater agents:
        case "turn_status":       turnStatus(ev); break;
        case "research_chunk":    researchChunk(ev); break;
        case "research_done":     researchDone(ev); break;
        case "turn_text_start":   turnTextStart(ev); break;
        case "turn_text_chunk":   turnTextChunk(ev); break;
        case "turn_text_end":     turnTextEnd(ev); break;
    }
}
```

Routes events to specific rendering functions based on the `type` field. The six new streaming event types (bottom group) all come from debater agents via `_publish_to_room()` rather than the judge.

---

### `turnTextStart()` — Create the Card Immediately

```javascript
function turnTextStart(ev) {
    const div = document.createElement("div");
    div.className = "turn typing-cursor";  // blinking cursor via CSS
    div.innerHTML = `
        <div class="meta">
            <strong>${escapeHtml(ev.name || ev.slug)}</strong>
            <span>· ${escapeHtml(ev.phase)}</span>
        </div>
        <div class="body"></div>
    `;
    appendTurn(div);
    state.activeTurns.set(_activeKey(ev.slug, ev.phase), div);
}
```

Published by the debater **before** `build_turn()` starts. Creates an empty card with a blinking cursor and registers it in `state.activeTurns`. All subsequent streaming events for this `slug:phase` pair update this card.

### `turnStatus()` — Pipeline Step Badge

```javascript
function turnStatus(ev) {
    const card = state.activeTurns.get(_activeKey(ev.slug, ev.phase));
    if (!card) return;
    let badge = card.querySelector(".step-badge") || document.createElement("span");
    badge.className = "step-badge";
    badge.textContent = STATUS_LABELS[ev.status] || ev.status;
    card.querySelector(".meta").appendChild(badge);
}
```

Updates a small badge in the card's meta row with a human-readable label. `STATUS_LABELS` maps internal status strings to display text:

```javascript
const STATUS_LABELS = {
    retrieving_memory:  "🧠 recalling…",
    planning_strategy:  "🗺 planning…",
    researching:        "🔍 researching…",
    composing_argument: "✍ composing…",
    verifying_claims:   "🔬 verifying…",
    reflecting:         "🔄 reflecting…",
    speaking:           "🎙 speaking…",
};
```

### `researchChunk()` — Live Evidence Stream

```javascript
function researchChunk(ev) {
    const card = state.activeTurns.get(_activeKey(ev.slug, ev.phase));
    if (!card) return;
    const panel = _getOrCreateResearchPanel(card);
    panel.querySelector(".research-text").textContent += ev.text;
}
```

Appends Phase-1 research tokens to a collapsible `<details>` panel inside the card. The panel is collapsed by default so it does not overwhelm the transcript, but users can expand it to read the raw evidence the debater is gathering. Tokens arrive in real time as Gemini streams them.

### `researchDone()` — Finalise the Research Panel

```javascript
function researchDone(ev) {
    const panel = _getOrCreateResearchPanel(card);
    panel.querySelector("summary").textContent =
        `🔍 research notes (${srcs.length} source(s))`;
    // appends clickable source links inside the panel
}
```

Published after `build_turn` returns. Updates the panel's `<summary>` with the source count and appends clickable citation links inside the panel.

### `turnTextChunk()` — Append Sentence

```javascript
function turnTextChunk(ev) {
    const card = state.activeTurns.get(_activeKey(ev.slug, ev.phase));
    if (!card) return;
    card.querySelector(".body").textContent += ev.text;
    root.scrollTop = root.scrollHeight;
}
```

Appends a sentence to the card body. The debater sends one sentence every 50ms, so the text appears with a typewriter effect. Auto-scroll keeps the latest sentence visible.

### `turnTextEnd()` — Remove Typing Cursor

```javascript
function turnTextEnd(ev) {
    const card = state.activeTurns.get(_activeKey(ev.slug, ev.phase));
    if (!card) return;
    card.classList.remove("typing-cursor");
    card.querySelector(".step-badge")?.remove();
}
```

Removes the blinking cursor CSS class and the step badge. The card is now static text.

### `turnSpoken()` — A Debater's Turn (Judge's Canonical Event)

```javascript
function turnSpoken(entry) {
    const key = _activeKey(entry.slug, entry.phase);
    const existing = state.activeTurns.get(key);
    if (existing) {
        // Card was created by turn_text_start. Enrich it with citations.
        existing.classList.remove("typing-cursor");
        existing.querySelector(".step-badge")?.remove();
        // append .cites div if not already there
        state.activeTurns.delete(key);
        return;
    }
    // Fallback: streaming events were not received — build full card.
    const div = document.createElement("div");
    div.className = "turn";
    div.innerHTML = `
        <div class="meta"><strong>${escapeHtml(entry.name)}</strong> <span>· ${escapeHtml(entry.phase)}</span></div>
        <div class="body">${escapeHtml(entry.text)}</div>
        ${cites ? `<div class="cites">${cites}</div>` : ""}
    `;
    appendTurn(div);
}
```

`turn_spoken` comes from the **judge** after the debater has finished speaking. It is the canonical, authoritative record of the turn.

**If streaming worked** (the normal case): the card was already created by `turn_text_start` and text was filled in by `turn_text_chunk`. `turnSpoken` finds the existing card, removes the cursor and step badge, and appends the citation links. The text body is not replaced.

**If streaming didn't happen** (no observer when the debater spoke, network issues): `turnSpoken` falls back to creating the complete card all at once, identical to the old behaviour.

This design means `turn_spoken` is always the last-resort guarantee that the turn appears in the transcript, even if all streaming events were lost.

### `factCheck()` — Verdict Badge

```javascript
function factCheck(ev) {
    const div = document.createElement("div");
    div.className = "turn factcheck";
    // ...
    div.innerHTML = `
        <div class="meta">
            <span class="badge ${ev.verdict}">${ev.verdict.replace("_", " ")}</span>
            <span>confidence ${Number(ev.confidence || 0).toFixed(2)}</span>
            <span>· ${by}${target}</span>
        </div>
        <div class="body">
            <div><em>Claim:</em> ${escapeHtml(ev.claim)}</div>
            <div><em>Evidence:</em> ${escapeHtml(ev.evidence_summary || "")}</div>
        </div>
        ...
    `;
    appendTurn(div);
}
```

The `.badge.${ev.verdict}` class gives each verdict a distinct colour:

| Verdict | Badge Color |
|---|---|
| `SUPPORTED` | Green |
| `PARTIALLY_SUPPORTED` | Amber |
| `UNSUPPORTED` | Amber-orange |
| `CONTRADICTED` | Red |
| `UNVERIFIABLE` | Muted grey |

### `removedNote()` — Disqualification

```javascript
function removedNote(ev) {
    const div = document.createElement("div");
    div.className = "turn removed";
    // render disqualification notice
    appendTurn(div);
    const info = [...state.participants.values()].find((v) => v.slug === ev.slug);
    if (info) {
        info.removed = true;
        renderParticipants();
    }
}
```

Marks the participant as `removed=true` which makes their card faded and greyed out (`.pcard.removed { opacity: 0.4; filter: grayscale(0.5); }`).

### `verdictNote()` — Final Verdict

```javascript
function verdictNote(ev) {
    const div = document.createElement("div");
    div.className = "turn verdict";
    div.innerHTML = `
        <div class="meta"><strong>VERDICT</strong> · winner: ${ev.winner_name || ev.winner_slug}</div>
        <div class="body">${escapeHtml(ev.rationale || "")}</div>
    `;
    appendTurn(div);
}
```

Rendered with a green left border and a subtle green gradient background, visually distinct from regular turns.

---

## `escapeHtml()` — XSS Prevention

```javascript
function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
}
```

Every time user-controlled or AI-generated text is inserted into innerHTML, it goes through `escapeHtml()` first. This prevents XSS (cross-site scripting) attacks where a malicious claim or stance could contain `<script>` tags. The AI text is not trusted; it is always escaped.

---

## Styling System

The CSS uses custom properties (CSS variables) for all colours:

```css
:root {
    --bg: #0b0f1a;          /* dark navy background */
    --panel: #141b2d;       /* slightly lighter panel */
    --panel-2: #1b2440;     /* card background */
    --border: #242f54;      /* border colour */
    --text: #e8edf7;        /* primary text */
    --muted: #8a96b6;       /* secondary text */
    --accent: #7aa2ff;      /* blue accent (links) */
    --accent-2: #b892ff;    /* purple accent */
    --good: #4ade80;        /* green (supported verdict) */
    --warn: #f59e0b;        /* amber (partial/unsupported) */
    --bad: #f87171;         /* red (contradicted/disqualified) */
    --live: #60a5fa;        /* blue (active speaker glow) */
}
```

The two-column layout (form on left, transcript on right) is achieved with `grid-template-columns: 380px 1fr` on the `<main>` element.

### Streaming-Specific Styles

```css
/* Blinking cursor on cards while text is streaming in */
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:0} }
.typing-cursor .body::after {
    content: "▋";
    animation: blink 1s step-end infinite;
    color: var(--accent);
}

/* Pipeline step badge in the card meta row */
.step-badge {
    font-size: 11px;
    color: var(--accent);
    opacity: 0.85;
    margin-left: 4px;
}

/* Collapsible research evidence panel */
.research-panel { border: 1px solid var(--border); border-radius: 6px; }
.research-panel summary { cursor: pointer; color: var(--muted); font-size: 12px; }
.research-panel .research-text {
    padding: 6px 8px;
    color: var(--muted);
    max-height: 130px;
    overflow-y: auto;
    white-space: pre-wrap;
    font-size: 11.5px;
}
```

| Element | Purpose |
|---|---|
| `.typing-cursor` | Applied to a card while text is streaming; removed on `turn_text_end` |
| `.step-badge` | Shows current pipeline step in the card meta row |
| `.research-panel` | `<details>` element, collapsed by default, holds Phase-1 evidence and sources |
