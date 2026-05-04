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
};
```

- `room` — the active `Room` instance, or `null` if not connected.
- `participants` — a `Map` keyed by participant identity (e.g. `"judge"`, `"debater-pro"`, `"debater-con"`). Each entry stores the metadata needed to render a participant card.
- `turns` — an array of rendered DOM elements (used to clear the placeholder on first turn).

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
        case "debate_started":   /* pre-seed stances */; break;
        case "phase_started":    phase(ev); break;
        case "turn_spoken":      turnSpoken(ev.entry); break;
        case "fact_check":       factCheck(ev); break;
        case "debater_removed":  removedNote(ev); break;
        case "verdict":          verdictNote(ev); break;
    }
}
```

Routes events to specific rendering functions based on the `type` field.

### `turnSpoken()` — A Debater's Turn

```javascript
function turnSpoken(entry) {
    const div = document.createElement("div");
    div.className = "turn";
    const cites = (entry.citations || [])
        .slice(0, 5)
        .map((s, i) => `<a href="${s.uri}" target="_blank" rel="noopener">[${i + 1}] ${(s.title || s.uri).slice(0, 80)}</a>`)
        .join(" ");
    div.innerHTML = `
        <div class="meta"><strong>${entry.name}</strong> <span>· ${entry.phase}</span></div>
        <div class="body">${escapeHtml(entry.text)}</div>
        ${cites ? `<div class="cites">${cites}</div>` : ""}
    `;
    appendTurn(div);
}
```

Renders a turn as a card with the speaker's name, phase, spoken text, and up to 5 clickable citation links. Note `escapeHtml(entry.text)` — this prevents XSS by escaping any HTML special characters in the AI-generated text.

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
