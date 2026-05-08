# Data Packets — The Event Bus

All communication between the judge agent and the browser happens through LiveKit's data channel. The judge publishes JSON objects as data packets; the browser receives and renders them. This document describes every packet type, its structure, when it is emitted, and what the browser does with it.

---

## What Is a Data Packet?

In LiveKit, a data packet is a binary message sent from one participant to all others (or to specific participants) via the room's reliable data channel. It is not audio or video — it is raw bytes. The judge uses this to send structured JSON events; the browser decodes and renders them.

### Publishing (Judge Side)

```python
await room.local_participant.publish_data(
    json.dumps(event).encode("utf-8"),
    reliable=True,
    topic="debate.event",
)
```

- `json.dumps(event).encode("utf-8")` — serialise the Python dict to JSON bytes.
- `reliable=True` — use TCP-like reliable delivery (guaranteed arrival, in order). The alternative `reliable=False` is UDP-like (faster but may be dropped).
- `topic="debate.event"` — a string label. The browser filters by this topic.

### Receiving (Browser Side)

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

- `new TextDecoder().decode(payload)` — convert `Uint8Array` to a string.
- `JSON.parse(...)` — decode JSON string to a JavaScript object.
- `handleEvent(ev)` — route to the correct rendering function based on `ev.type`.

---

## The `_publish_event()` Helper

All packets from the **judge** go through this function in `judge_agent.py`:

```python
async def _publish_event(room: rtc.Room, event: dict) -> None:
    """Fire-and-forget data packet for the web observer's live transcript."""
    try:
        await room.local_participant.publish_data(
            json.dumps(event).encode("utf-8"),
            reliable=True,
            topic="debate.event",
        )
    except Exception as exc:
        logger.warning("publish_data failed: %s", exc)
```

**Debater agents** publish streaming events using the equivalent `_publish_to_room()` helper in `debater_agent.py`. Both helpers use the same `"debate.event"` topic, so all events — from both judge and debaters — flow through the browser's single `handleEvent()` dispatcher.

**Fire-and-forget** means: if publishing fails (e.g. no observers, network error), the exception is logged and execution continues. The debate does not fail because a browser isn't watching.

---

## Packet 1: `debate_started`

**When:** Immediately after the judge's opening announcement, before the first phase.

**Published by:**
```python
await _publish_event(
    ctx.room,
    {
        "type": "debate_started",
        "topic": topic,
        "debaters": [d.model_dump() for d in debaters]
    },
)
```

**Schema:**
```json
{
    "type": "debate_started",
    "topic": "Should AI agents vote in online communities?",
    "debaters": [
        {
            "slug": "pro",
            "name": "Alex",
            "stance": "Yes, AI agents should have voting rights..."
        },
        {
            "slug": "con",
            "name": "Morgan",
            "stance": "No, voting rights must remain exclusive to humans..."
        }
    ]
}
```

**Browser renders:**
Pre-seeds the participant cards with names and stances for each debater. This happens before the agents physically connect to the room (there's a race condition where the browser may receive this event before the participant joins). The `...existing` spread in `ParticipantConnected` handler merges the pre-seeded data with the participant attributes when they arrive.

```javascript
case "debate_started":
    (ev.debaters || []).forEach((d) => {
        const identity = `debater-${d.slug}`;
        const existing = state.participants.get(identity) || {};
        state.participants.set(identity, {
            ...existing,
            role: "debater",
            name: d.name,
            slug: d.slug,
            stance: d.stance,
        });
    });
    renderParticipants();
    break;
```

---

## Packet 2: `phase_started`

**When:** At the start of each phase, before any debater speaks.

**Published by:**
```python
await _publish_event(ctx.room, {"type": "phase_started", "phase": phase})
```

**Schema:**
```json
{
    "type": "phase_started",
    "phase": "rebuttal_1"
}
```

**Browser renders:**
A horizontal separator with the phase name in the transcript:

```javascript
case "phase_started":
    phase(ev);
    break;

function phase(ev) {
    const div = document.createElement("div");
    div.className = "turn phase";
    div.textContent = `— ${ev.phase.replace("_", " ")} —`;
    appendTurn(div);
}
```

Renders as: `— rebuttal 1 —` (underscores replaced with spaces, centred, muted text).

---

## Packet 3: `turn_spoken`

**When:** After each debater's RPC call completes — meaning after the debater has finished speaking.

**Published by:**
```python
entry = TranscriptEntry(
    slug=slug,
    name=spec.name,
    phase=phase,
    text=reply.text,
    key_claims=reply.key_claims,
    citations=reply.citations,
)
await _publish_event(
    ctx.room,
    {"type": "turn_spoken", "entry": entry.model_dump()},
)
```

**Schema:**
```json
{
    "type": "turn_spoken",
    "entry": {
        "slug": "pro",
        "name": "Alex",
        "phase": "opening",
        "text": "Remote work has proven itself. A 2024 MIT study found...",
        "key_claims": [
            "A 2024 MIT study found remote workers complete 13% more work daily.",
            "Stanford 2023 data shows remote work reduces commute stress by 35%."
        ],
        "citations": [
            {
                "title": "MIT Study on Remote Work Productivity",
                "uri": "https://mitsloan.mit.edu/..."
            }
        ]
    }
}
```

**Browser renders:**
A turn card with speaker name, phase label, spoken text, and up to 5 clickable citation links.

**Interaction with streaming events:** `turn_spoken` is the judge's canonical, post-turn event. By the time it arrives, the card may already exist (created by `turn_text_start` and populated by `turn_text_chunk`). In that case, `turnSpoken` only adds the citation links and removes the step badge — it does not replace the text body.

If no streaming events were received (fallback), `turnSpoken` creates the full card from scratch.

---

## Packet 4: `fact_check`

**When:** Immediately after each debater's turn, if they performed a fact-check (i.e. non-opening phases where a suitable opponent claim was found).

**Published by:**
```python
if reply.fact_check is not None and reply.target_slug:
    record = {
        "phase": phase,
        "by_slug": slug,               # who did the checking
        "target_slug": reply.target_slug,   # whose claim was checked
        **reply.fact_check.model_dump(),    # spreads: claim, verdict, confidence, evidence_summary, citations
    }
    await _publish_event(ctx.room, {"type": "fact_check", **record})
```

**Schema:**
```json
{
    "type": "fact_check",
    "phase": "rebuttal_1",
    "by_slug": "con",
    "target_slug": "pro",
    "claim": "A 2024 MIT study found remote workers complete 13% more work daily.",
    "verdict": "CONTRADICTED",
    "confidence": 0.91,
    "evidence_summary": "The cited MIT study from 2013 (not 2024) showed 13% improvement, but recent studies are mixed...",
    "citations": [
        {
            "title": "Stanford Remote Work Study 2023",
            "uri": "https://siepr.stanford.edu/..."
        }
    ]
}
```

**Browser renders:**
A fact-check card with colour-coded verdict badge, confidence score, and the claim/evidence:

```javascript
case "fact_check":
    factCheck(ev);
    break;

function factCheck(ev) {
    // class="badge CONTRADICTED" → red badge
    // confidence displayed as decimal
    // by_slug → target_slug shown as "con → pro"
}
```

Verdict badge colours:
- `SUPPORTED` → green background
- `PARTIALLY_SUPPORTED` → amber
- `UNSUPPORTED` → amber-orange
- `CONTRADICTED` → red
- `UNVERIFIABLE` → grey

---

## Packet 5: `debater_removed`

**When:** During end-of-phase adjudication, when a debater is disqualified.

**Published by:**
```python
await _publish_event(
    ctx.room,
    {
        "type": "debater_removed",
        "slug": target,
        "reason": "hallucination",
        "by_slug": hit["by_slug"],
        "claim": hit["claim"],
        "evidence": hit.get("evidence_summary", ""),
    },
)
```

**Schema:**
```json
{
    "type": "debater_removed",
    "slug": "pro",
    "reason": "hallucination",
    "by_slug": "con",
    "claim": "ChatGPT was released in 2020.",
    "evidence": "ChatGPT was launched on November 30, 2022, per OpenAI's blog."
}
```

**Browser renders:**
A red-bordered card in the transcript, plus marks the debater's participant card as disqualified:

```javascript
case "debater_removed":
    removedNote(ev);
    break;

function removedNote(ev) {
    const div = document.createElement("div");
    div.className = "turn removed";
    // renders: "<debater slug> disqualified (hallucination)"
    // with claim and evidence if present
    appendTurn(div);

    // Find the participant by slug and mark them removed
    const info = [...state.participants.values()].find((v) => v.slug === ev.slug);
    if (info) {
        info.removed = true;
        renderParticipants();  // participant card becomes greyed out + "disqualified" label
    }
}
```

The participant card gets `.pcard.removed` CSS class: `opacity: 0.4; filter: grayscale(0.5)`.

---

## Packet 6: `verdict`

**When:** After the judge delivers the final verdict via TTS.

**Published by:**
```python
await _publish_event(
    ctx.room,
    {
        "type": "verdict",
        **final.model_dump(),     # spreads: winner_slug, scores, rationale
        "winner_name": winner_name,    # the display name, not just slug
    },
)
```

**Schema:**
```json
{
    "type": "verdict",
    "winner_slug": "con",
    "winner_name": "Morgan",
    "scores": [
        { "slug": "pro", "score": 0.62 },
        { "slug": "con", "score": 0.81 }
    ],
    "rationale": "Morgan demonstrated stronger argumentative structure throughout the debate, with well-cited evidence and effective rebuttals in rounds two and three..."
}
```

**Browser renders:**
A green-accented verdict card:

```javascript
case "verdict":
    verdictNote(ev);
    break;

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

The `scores` array is not displayed in the current UI (only the winner name and rationale). It is available in the packet if you want to add a score breakdown to the UI.

---

## Packet 7: `turn_text_start`

**When:** Published by the debater agent **before** `build_turn()` is called — the very first thing the debater does when it receives an RPC call.

**Published by:** Debater agent (`_publish_to_room`)

**Schema:**
```json
{
    "type": "turn_text_start",
    "slug": "pro",
    "name": "Alex",
    "phase": "rebuttal_1"
}
```

**Browser renders:**
Creates an empty turn card with a blinking cursor. Registers the card in `state.activeTurns` keyed by `"pro:rebuttal_1"`. All subsequent streaming events for this slug+phase pair update this card.

---

## Packet 8: `turn_status`

**When:** At each of the 7 pipeline step boundaries inside `build_turn()`.

**Published by:** Debater agent via `on_status` callback

**Schema:**
```json
{
    "type": "turn_status",
    "slug": "pro",
    "name": "Alex",
    "phase": "rebuttal_1",
    "status": "researching"
}
```

**Possible `status` values:** `retrieving_memory`, `planning_strategy`, `researching`, `composing_argument`, `verifying_claims`, `reflecting`, `speaking`

**Browser renders:**
Updates (or creates) a small `.step-badge` element in the card's meta row with a human-readable label (e.g. `"🔍 researching…"`). The badge is replaced on each new status, so only the current step is shown.

---

## Packet 9: `research_chunk`

**When:** For each token batch (~80 chars) during Phase-1 Gemini streaming. Fires many times per turn (one per sentence boundary or buffer flush).

**Published by:** Debater agent via `on_research_chunk` callback

**Schema:**
```json
{
    "type": "research_chunk",
    "slug": "pro",
    "name": "Alex",
    "phase": "rebuttal_1",
    "text": "According to a 2025 Stanford study, remote workers report 23% higher"
}
```

**Browser renders:**
Appends the text fragment to a collapsible `<details>` panel inside the turn card. The panel (`🔍 research notes`) is closed by default — users can expand it to read the live evidence stream.

---

## Packet 10: `research_done`

**When:** Immediately after `build_turn()` returns (Phase-1 is complete).

**Published by:** Debater agent

**Schema:**
```json
{
    "type": "research_done",
    "slug": "pro",
    "name": "Alex",
    "phase": "rebuttal_1",
    "sources": [
        { "title": "Stanford Remote Work 2025", "uri": "https://..." }
    ]
}
```

**Browser renders:**
Updates the research panel summary to show the source count (e.g. `"🔍 research notes (3 sources)"`) and appends clickable source links inside the panel.

---

## Packet 11: `turn_text_chunk`

**When:** One per sentence of the spoken text, published 50ms apart after `build_turn()` returns and before TTS starts.

**Published by:** Debater agent

**Schema:**
```json
{
    "type": "turn_text_chunk",
    "slug": "pro",
    "name": "Alex",
    "phase": "rebuttal_1",
    "text": "The evidence clearly shows that remote work increases productivity. "
}
```

**Browser renders:**
Appends the sentence to the `.body` of the active card and auto-scrolls. The 50ms inter-sentence delay creates a typewriter effect.

---

## Packet 12: `turn_text_end`

**When:** After all `turn_text_chunk` packets for a turn have been sent.

**Published by:** Debater agent (also published on error, to clean up a stuck cursor)

**Schema:**
```json
{
    "type": "turn_text_end",
    "slug": "pro",
    "name": "Alex",
    "phase": "rebuttal_1"
}
```

**Browser renders:**
Removes the `.typing-cursor` CSS class (stops the blinking cursor) and removes the `.step-badge`. The card is now static text. TTS audio begins immediately after this packet is published.

---

## Event Flow Timeline

Streaming events from debater agents are interleaved with judge events:

```
Time →
│
├── [debate_started]          ← judge: debate begins
│
├── [phase_started]           ← judge: opening phase
│
├── [turn_text_start]         ← debater-pro: card created (empty)
├── [turn_status: retrieving_memory]
├── [turn_status: planning_strategy]
├── [turn_status: researching]
├── [research_chunk] ...
├── [research_chunk] ...       ← real-time Phase-1 tokens
├── [research_chunk] ...
├── [turn_status: composing_argument]
├── [turn_status: reflecting]
├── [research_done]           ← sources attached to research panel
├── [turn_text_chunk] ...      ← sentence 1 (50ms pacing)
├── [turn_text_chunk] ...
├── [turn_text_end]           ← cursor removed; TTS starts
│
│   <<< audio plays here (20–60s) >>>
│
├── [turn_spoken]             ← judge: enriches card with citations
├── [fact_check]              ← judge: verdict card
│
├── [turn_text_start]         ← debater-con: card created ...
│   (same pattern)
...
└── [verdict]
```

Note: `turn_spoken` arrives after audio completes but text is already visible from `turn_text_chunk` events. The user sees text appear sentence-by-sentence (~0.5s total), then hears the audio, then citations are attached.

---

## Packet Ordering and Timing

LiveKit's `reliable=True` data packets are delivered in order (within the same participant's stream). The judge and debater agents each publish sequentially within their own streams.

With streaming, the turn now follows this timing pattern:

| When | What happens |
|---|---|
| Turn starts (before `build_turn`) | `turn_text_start` — card appears immediately |
| During Phase-1 research (~15–30s) | `turn_status` + `research_chunk` events stream continuously |
| After `build_turn` returns (~35–60s) | `research_done` + `turn_text_chunk` ×N + `turn_text_end` (~0.5s) |
| TTS plays (~20–60s) | Audio heard; text already visible |
| After playout (judge event) | `turn_spoken` enriches card with citations |

This is a significant UX improvement over the old behaviour where the card appeared only after the audio finished.

---

## Adding New Event Types

To add a new data packet type:

1. **Judge side** — call `_publish_event(ctx.room, {"type": "my_new_type", ...})` at the appropriate point in `judge_agent.py`.

2. **Browser side** — add a case to `handleEvent()` in `index.html` and write a renderer function:

```javascript
// in handleEvent():
case "my_new_type":
    myNewRenderer(ev);
    break;

// new renderer function:
function myNewRenderer(ev) {
    const div = document.createElement("div");
    div.className = "turn";
    div.innerHTML = `
        <div class="meta">New Event: ${escapeHtml(ev.some_field)}</div>
    `;
    appendTurn(div);
}
```

3. No schema definition needed in `schemas.py` for the packet itself — data packets are informal JSON objects, not Pydantic-validated. They are only validated by the browser's own handling logic.
