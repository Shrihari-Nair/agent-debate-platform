"""Cartesia voice IDs per persona slug + helpers to deterministically assign a
voice to a debater based on their slug.

Voice IDs are from Cartesia's public library (https://play.cartesia.ai/voices).
Swap freely; the important property is that each agent in a single room gets a
distinct voice so the human observer can tell them apart by ear alone.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VoiceProfile:
    id: str
    label: str


# Hand-picked distinct voices. First two are conventionally used for pro/con,
# the rest rotate in for dynamic (N>2) debates.
VOICE_POOL: list[VoiceProfile] = [
    VoiceProfile("a167e0f3-df7e-4d52-a9c3-f949145efdab", "Blake (energetic US male)"),
    VoiceProfile("9626c31c-bec5-4cca-baa8-f8ba9e84c8bc", "Jacqueline (confident US female)"),
    VoiceProfile("421b3369-f63f-4b03-8980-37a44df1d4e8", "Newscaster (neutral US male)"),
    VoiceProfile("248be419-c632-4f23-adf1-5324ed7dbf1d", "Elizabeth (British female)"),
    VoiceProfile("a01c369f-6d2d-4185-bc20-b32c225eab70", "Brooke (warm US female)"),
    VoiceProfile("79a125e8-cd45-4c13-8a67-188112f4dd22", "British lady (formal)"),
]

JUDGE_VOICE = VoiceProfile("f786b574-daa5-4673-aa0c-cbe3e8534c02", "Katie (authoritative US female)")


# Named slots for conventional 2-side debates. Any debater whose slug isn't in
# this map gets a voice from VOICE_POOL by stable hash of the slug.
NAMED_VOICES: dict[str, VoiceProfile] = {
    "pro": VOICE_POOL[0],
    "con": VOICE_POOL[1],
    "optimist": VOICE_POOL[2],
    "skeptic": VOICE_POOL[3],
}


def voice_for_slug(slug: str) -> VoiceProfile:
    """Return a stable VoiceProfile for the given debater slug.

    Uses NAMED_VOICES first, then falls back to a deterministic pick from the
    pool so the same slug always gets the same voice across restarts.
    """
    if slug in NAMED_VOICES:
        return NAMED_VOICES[slug]
    idx = abs(hash(slug)) % len(VOICE_POOL)
    return VOICE_POOL[idx]
