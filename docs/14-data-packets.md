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

All packets go through this function in `judge_agent.py`:

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
        logger.debug("publish_data failed: %s", exc)
```

**Fire-and-forget** means: if publishing fails (e.g. no observers, network error), the exception is silently logged and execution continues. The debate does not fail because a browser isn't watching. This is the right pattern when the data channel is a UI feature, not a critical control path.

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
A turn card with speaker name, phase label, spoken text, and up to 5 clickable citation links:

```javascript
case "turn_spoken":
    turnSpoken(ev.entry);
    break;
```

The `key_claims` field is not shown in the browser UI (it is internal data for fact-checking). Only `text` and `citations` are displayed.

**Note:** This packet arrives AFTER the audio has finished playing (because `wait_for_playout()` must complete before the RPC returns, and the packet is published after the RPC returns). So the transcript card and the audio are slightly out of sync — you hear the audio first, then the card appears.

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

## Event Flow Timeline

```
Time →
│
├── [debate_started]    ← just after judge opens
│
├── [phase_started]     ← opening
├── [turn_spoken]       ← pro's opening
├── [turn_spoken]       ← con's opening
│
├── [phase_started]     ← rebuttal_1
├── [turn_spoken]       ← pro's rebuttal (includes fact-check callout in text)
├── [fact_check]        ← pro's check of con's claim
├── [turn_spoken]       ← con's rebuttal
├── [fact_check]        ← con's check of pro's claim
│    └── (if CONTRADICTED ≥ 0.8 found)
│         [debater_removed]   ← pro disqualified
│
├── [phase_started]     ← rebuttal_2 (only if > 1 debater alive)
...
│
├── [phase_started]     ← closing
...
│
└── [verdict]
```

---

## Packet Ordering and Timing

LiveKit's `reliable=True` data packets are delivered in order (within the same participant's stream). The judge publishes all packets sequentially (not concurrently), so the browser receives them in the exact order they were published.

However, there is a notable timing gap between audio and transcript:

- Audio playback happens **before** the RPC returns (because `wait_for_playout()` blocks the RPC)
- `turn_spoken` is published **after** the RPC returns
- Result: you hear the audio, then ~1–2 seconds later the transcript card appears

This is by design — it means the card appears as a "summary" after the speech, not predictively before it.

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
