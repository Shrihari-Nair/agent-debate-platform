"""Episodic Memory — Feature 2.

`DebateMemory` gives each debater agent a per-job vector store backed by FAISS
and Gemini embeddings.  As the debate progresses the debater stores every turn
(its own and opponents') as embedded episodes.  Before generating each argument
it retrieves the most semantically relevant past episodes to enrich the
Phase-1 Google Search grounding prompt.

Architecture
────────────

  ┌──────────────────────────────────────┐
  │          DebateMemory                │
  │                                      │
  │  FAISS index (in-memory)             │
  │  ├── episode_0: {slug, phase, text}  │
  │  ├── episode_1: ...                  │
  │  └── ...                             │
  │                                      │
  │  compress_older_phases()             │
  │  └── LLM summarises episodes         │
  │      older than (current_phase - 2)  │
  └──────────────────────────────────────┘

Compression
───────────
After each phase transition the debater calls `compress_older_phases(current_phase)`.
Episodes from phases more than 2 phases back are summarised by
`ChatGoogleGenerativeAI` into a single "phase_summary" document, then the
original fine-grained entries are replaced.  This keeps the index small and the
retrieved context dense as debates get longer.

Google Search grounding is NOT used here — embeddings + LLM summarisation are
pure reasoning operations.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from langchain_core.documents import Document
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from langchain_community.vectorstores import FAISS

from .config import settings
from .schemas import TranscriptEntry

logger = logging.getLogger(__name__)

# Number of semantically relevant episodes to retrieve per turn.
_RETRIEVAL_K = 4

# Phases older than (current_phase_index - _COMPRESSION_LAG) are compressed.
_COMPRESSION_LAG = 2

# Ordered list of canonical phases — used to compute phase indices.
_PHASE_ORDER = ["opening", "rebuttal_1", "rebuttal_2", "closing"]


def _phase_index(phase: str) -> int:
    try:
        return _PHASE_ORDER.index(phase)
    except ValueError:
        return len(_PHASE_ORDER)  # unknown phases are treated as "late"


def _make_embeddings() -> GoogleGenerativeAIEmbeddings:
    return GoogleGenerativeAIEmbeddings(
        model="models/gemini-embedding-001",
        google_api_key=settings.gemini_api_key,
    )


@dataclass
class DebateMemory:
    """Per-debater-job episodic memory backed by FAISS.

    Create one instance per debater job in `debater_agent.entrypoint()` and
    pass it through each `build_turn()` call.
    """

    my_slug: str
    _embeddings: GoogleGenerativeAIEmbeddings = field(init=False, repr=False)
    _store: FAISS | None = field(default=None, init=False, repr=False)
    # doc_id → phase, for compression bookkeeping
    _doc_phases: dict[str, str] = field(default_factory=dict, init=False)
    # track which phases have already been compressed
    _compressed_phases: set[str] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        self._embeddings = _make_embeddings()
        logger.info("memory[%s]: DebateMemory initialised (FAISS empty, embeddings ready)", self.my_slug)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_store(self, first_doc: Document) -> None:
        """Initialise FAISS on the first document (FAISS needs ≥1 doc)."""
        if self._store is None:
            self._store = FAISS.from_documents([first_doc], self._embeddings)
        else:
            self._store.add_documents([first_doc])

    def _add_document(self, doc: Document, phase: str) -> None:
        if self._store is None:
            logger.info("memory[%s]: first document — initialising FAISS index (phase=%s)", self.my_slug, phase)
            self._store = FAISS.from_documents([doc], self._embeddings)
        else:
            ids = self._store.add_documents([doc])
            if ids:
                self._doc_phases[ids[0]] = phase

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def store(self, entry: TranscriptEntry) -> None:
        """Embed and store one transcript turn.

        Stores the full turn text plus key_claims so retrieval captures both
        what was said and what factual claims were made.
        """
        is_mine = entry.slug == self.my_slug
        claims_block = (
            " | ".join(entry.key_claims) if entry.key_claims else ""
        )
        content = (
            f"[{entry.phase}] {'MY TURN' if is_mine else 'OPPONENT'} "
            f"({entry.slug}): {entry.text}"
        )
        if claims_block:
            content += f"\nKey claims: {claims_block}"

        doc = Document(
            page_content=content,
            metadata={
                "slug": entry.slug,
                "phase": entry.phase,
                "is_mine": is_mine,
                "type": "turn",
            },
        )
        self._add_document(doc, entry.phase)
        logger.debug(
            "memory[%s]: stored episode phase=%s slug=%s is_mine=%s chars=%d",
            self.my_slug,
            entry.phase,
            entry.slug,
            is_mine,
            len(content),
        )

    def retrieve(self, query: str, k: int = _RETRIEVAL_K) -> list[str]:
        """Return the k most relevant past episodes for the given query.

        Returns a list of plain strings suitable for injecting into a prompt.
        Returns an empty list if the store is empty or retrieval fails.
        """
        if self._store is None:
            logger.debug("memory[%s]: retrieve called but index is empty", self.my_slug)
            return []
        logger.info(
            "memory[%s]: retrieving top-%d episodes  query=%r",
            self.my_slug, k, query[:60],
        )
        try:
            results = self._store.similarity_search_with_relevance_scores(query, k=k)
            episodes = []
            for doc, score in results:
                if score > 0.3:  # ignore very low relevance hits
                    episodes.append(doc.page_content)
            logger.info(
                "memory[%s]: retrieved %d/%d episodes (score>0.3)",
                self.my_slug, len(episodes), len(results),
            )
            return episodes
        except Exception as exc:
            logger.warning("memory[%s]: retrieval failed: %s", self.my_slug, exc)
            return []

    def compress_older_phases(self, current_phase: str) -> None:
        """LLM-summarise episodes from phases older than (current - lag).

        Replaces individual turn documents with a single dense summary document
        per old phase, keeping the index compact for long debates.
        """
        if self._store is None:
            return

        current_idx = _phase_index(current_phase)
        compress_before_idx = current_idx - _COMPRESSION_LAG

        if compress_before_idx <= 0:
            return  # nothing old enough to compress yet

        phases_to_compress = [
            p
            for p in _PHASE_ORDER[:compress_before_idx]
            if p not in self._compressed_phases
        ]
        if not phases_to_compress:
            logger.debug("memory[%s]: no phases ready for compression at phase=%s", self.my_slug, current_phase)
            return

        logger.info(
            "memory[%s]: compressing phases %s (current_phase=%s)",
            self.my_slug, phases_to_compress, current_phase,
        )
        for phase in phases_to_compress:
            self._compress_phase(phase)

    def _compress_phase(self, phase: str) -> None:
        """Replace all per-turn docs for `phase` with one LLM summary doc."""
        if self._store is None:
            return

        # Find doc IDs for this phase
        ids_to_remove: list[str] = [
            doc_id
            for doc_id, p in self._doc_phases.items()
            if p == phase
        ]
        if not ids_to_remove:
            return

        # Retrieve the actual documents before deleting them
        docs: list[Document] = []
        try:
            # FAISS stores docs by integer index; use docstore directly
            for doc_id in ids_to_remove:
                doc = self._store.docstore.search(doc_id)
                if isinstance(doc, Document):
                    docs.append(doc)
        except Exception as exc:
            logger.warning(
                "memory[%s]: could not retrieve docs for compression (phase=%s): %s",
                self.my_slug, phase, exc,
            )
            return

        if not docs:
            return

        # Summarise with LLM
        combined = "\n\n".join(d.page_content for d in docs)
        prompt = (
            f"Summarise the following debate turns from the '{phase}' phase into "
            f"a single concise paragraph (max 120 words) that captures: "
            f"the main arguments made, key factual claims, and any opponent "
            f"weaknesses revealed.  Output only the summary.\n\n{combined}"
        )
        try:
            llm = ChatGoogleGenerativeAI(
                model=settings.debate_model,
                google_api_key=settings.gemini_api_key,
                temperature=0.0,
            )
            result = llm.invoke(prompt)
            summary_text = result.content.strip()
        except Exception as exc:
            logger.warning(
                "memory[%s]: compression LLM call failed (phase=%s): %s",
                self.my_slug, phase, exc,
            )
            return

        # Delete old fine-grained docs and insert summary
        try:
            self._store.delete(ids_to_remove)
        except Exception as exc:
            logger.warning(
                "memory[%s]: FAISS delete failed (phase=%s): %s",
                self.my_slug, phase, exc,
            )
            return

        for doc_id in ids_to_remove:
            del self._doc_phases[doc_id]

        summary_doc = Document(
            page_content=f"[SUMMARY of {phase}] {summary_text}",
            metadata={"phase": phase, "type": "phase_summary", "slug": self.my_slug},
        )
        new_ids = self._store.add_documents([summary_doc])
        if new_ids:
            self._doc_phases[new_ids[0]] = phase

        self._compressed_phases.add(phase)
        logger.info(
            "memory[%s]: compressed phase=%s (%d docs → 1 summary, %d chars)",
            self.my_slug,
            phase,
            len(docs),
            len(summary_text),
        )
