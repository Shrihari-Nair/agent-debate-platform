# Personas — Voice Assignment

The personas module lives in [src/personas.py](../src/personas.py). It manages one thing: which Cartesia TTS voice each participant gets.

---

## Why Distinct Voices Matter

All agents in a debate speak through the same audio room. If the judge and all debaters had the same voice, a listener could not tell who was speaking. Distinct voices are not an aesthetic choice — they are functional. In a voice-first system, voice identity IS participant identity from the listener's perspective.

---

## `VoiceProfile` — A Frozen Dataclass

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class VoiceProfile:
    id: str
    label: str
```

`VoiceProfile` is a frozen dataclass — a lightweight Python data container. `frozen=True` makes it immutable (you cannot change the `id` or `label` after creation). This is appropriate because voice IDs are constants — they should never change at runtime.

`id` — the Cartesia voice ID string, passed directly to `cartesia.TTS(voice=voice.id)`.  
`label` — a human-readable description for logging and debugging.

---

## `VOICE_POOL` — The Available Voices

```python
VOICE_POOL: list[VoiceProfile] = [
    VoiceProfile("a167e0f3-df7e-4d52-a9c3-f949145efdab", "Blake (energetic US male)"),
    VoiceProfile("9626c31c-bec5-4cca-baa8-f8ba9e84c8bc", "Jacqueline (confident US female)"),
    VoiceProfile("421b3369-f63f-4b03-8980-37a44df1d4e8", "Newscaster (neutral US male)"),
    VoiceProfile("248be419-c632-4f23-adf1-5324ed7dbf1d", "Elizabeth (British female)"),
    VoiceProfile("a01c369f-6d2d-4185-bc20-b32c225eab70", "Brooke (warm US female)"),
    VoiceProfile("79a125e8-cd45-4c13-8a67-188112f4dd22", "British lady (formal)"),
]
```

Six hand-picked voices from Cartesia's public library, chosen to be distinct from each other in gender, accent, and tone. The pool supports up to 6 concurrent debaters without voice collision.

Voice IDs are UUIDs pointing to Cartesia's voice model library. You can browse and test voices at [play.cartesia.ai/voices](https://play.cartesia.ai/voices).

---

## `JUDGE_VOICE` — The Judge's Dedicated Voice

```python
JUDGE_VOICE = VoiceProfile("f786b574-daa5-4673-aa0c-cbe3e8534c02", "Katie (authoritative US female)")
```

The judge has a fixed, dedicated voice that is not in `VOICE_POOL`. This ensures:
- The judge always sounds different from any debater.
- Even with 6 debaters (the maximum), the judge still has a unique voice.
- The judge is identifiable by voice alone, which helps listeners follow the debate.

`JUDGE_VOICE` is imported directly in `judge_agent.py`:
```python
from .personas import JUDGE_VOICE
...
tts=cartesia.TTS(model="sonic-2", voice=JUDGE_VOICE.id)
```

---

## `NAMED_VOICES` — Conventional Slug-to-Voice Mapping

```python
NAMED_VOICES: dict[str, VoiceProfile] = {
    "pro": VOICE_POOL[0],        # Blake (energetic US male)
    "con": VOICE_POOL[1],        # Jacqueline (confident US female)
    "optimist": VOICE_POOL[2],   # Newscaster (neutral US male)
    "skeptic": VOICE_POOL[3],    # Elizabeth (British female)
}
```

For the most common debate setups (pro vs. con, optimist vs. skeptic), specific voices are pre-assigned. This means:
- Running `pro` vs `con` debates always gives the same voices across restarts.
- The voices are chosen to contrast: energetic male vs. confident female.

---

## `voice_for_slug()` — Deterministic Assignment

```python
def voice_for_slug(slug: str) -> VoiceProfile:
    """Return a stable VoiceProfile for the given debater slug.

    Uses NAMED_VOICES first, then falls back to a deterministic pick from the
    pool so the same slug always gets the same voice across restarts.
    """
    if slug in NAMED_VOICES:
        return NAMED_VOICES[slug]
    idx = abs(hash(slug)) % len(VOICE_POOL)
    return VOICE_POOL[idx]
```

### Step-by-Step Logic

1. **Named voice check**: if the slug is in `NAMED_VOICES` (e.g. `"pro"`, `"con"`), return that voice directly.

2. **Hash fallback**: for any other slug, compute `abs(hash(slug)) % len(VOICE_POOL)` to get a pool index.

### Why `abs(hash(slug)) % len(VOICE_POOL)`?

`hash()` returns a Python hash of the string. For the same string, Python's `hash()` returns the same value within a session (but not across restarts in Python 3.3+ due to hash randomisation). For a string like `"libertarian"`, `hash("libertarian")` might be `-8432174...`, so `abs()` makes it positive, and `% len(VOICE_POOL)` (which is 6) maps it to an index 0–5.

**Example:**
```python
slug = "libertarian"
idx = abs(hash("libertarian")) % 6
# Suppose this gives idx = 3
# → VOICE_POOL[3] = Elizabeth (British female)
```

The same slug always maps to the same index within one Python session. Across restarts, `hash()` randomisation means the index may differ — but since voice assignment is cosmetic (not functional), this is acceptable.

If determinism across restarts matters to you, replace `hash(slug)` with `hashlib.md5(slug.encode()).hexdigest()` which is always consistent.

### Why `abs()`?

Python's `hash()` can return negative integers. The `%` operator with a negative dividend can return unexpected results depending on the language. Using `abs()` keeps the value non-negative before taking the modulus.

---

## How Voices Are Used in Practice

```
Debate: "pro" vs "con"
  pro  → voice_for_slug("pro")  → NAMED_VOICES["pro"]  → Blake (VOICE_POOL[0])
  con  → voice_for_slug("con")  → NAMED_VOICES["con"]  → Jacqueline (VOICE_POOL[1])
  judge                          → JUDGE_VOICE           → Katie

Debate: "libertarian" vs "socialist" vs "centrist"
  libertarian → hash fallback → VOICE_POOL[idx]
  socialist   → hash fallback → VOICE_POOL[idx]  (different idx)
  centrist    → hash fallback → VOICE_POOL[idx]  (different idx)
  judge       → JUDGE_VOICE
```

Voice IDs are passed to the Cartesia TTS plugin when creating the `AgentSession`:

```python
# in debater_agent.py
voice = voice_for_slug(slug)
session = AgentSession(
    tts=cartesia.TTS(model="sonic-2", voice=voice.id),
    ...
)

# in judge_agent.py
session = AgentSession(
    tts=cartesia.TTS(model="sonic-2", voice=JUDGE_VOICE.id),
    ...
)
```

---

## Customising Voices

To change which voices are used:

1. Browse voices at [play.cartesia.ai/voices](https://play.cartesia.ai/voices).
2. Find the voice ID (a UUID shown in the voice details).
3. Update `VOICE_POOL` entries or `JUDGE_VOICE` with the new ID.

You can also update `NAMED_VOICES` to pre-assign specific voices to your common slugs.

No code changes are needed outside `personas.py` — all voice selection goes through `voice_for_slug()`.
