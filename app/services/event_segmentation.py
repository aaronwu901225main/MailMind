"""Reconstruct one source and segment *occurrences*, not whole emails.

Works with body/attachment knowledge chunks, which overlap by design. A
transaction occurrence is identified by the position of its amount; two equal
amounts at different positions remain separate transactions. Source data is
never inferred from summaries or email received dates.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Piece:
    chunk: object
    start: int                 # global document offset of chunk_text[0]
    end: int


@dataclass
class SourceDocument:
    text: str
    source_id: str
    source_type: str
    pieces: list[Piece]

    def containing(self, offset: int):
        # An overlapped substring belongs to two chunks; choose the earlier
        # source chunk, so provenance remains deterministic on reruns.
        for p in self.pieces:
            if p.start <= offset < p.end:
                return p.chunk, offset - p.start
        return None, None


def reconstruct(chunks: list) -> SourceDocument:
    """Reassemble exact chunk overlap, adding a newline for discontinuities.

    Never perform fuzzy overlap matching: that could silently lose transaction
    text or combine distinct identical-looking purchase rows.
    """
    if not chunks:
        raise ValueError('empty_source')
    ordered = sorted(chunks, key=lambda c: (c.chunk_index, c.id))
    source_id, source_type = ordered[0].source_id, ordered[0].source_type
    if any((c.source_id, c.source_type) != (source_id, source_type) for c in ordered):
        raise ValueError('mixed_sources')
    whole = ''
    pieces = []
    for c in ordered:
        body = c.chunk_text or ''
        if not body:
            continue
        if not pieces:
            start = 0
            whole = body
        else:
            previous = whole
            # The knowledge chunker uses a small exact character overlap.
            cap = min(120, len(previous), len(body))
            overlap = next((n for n in range(cap, 7, -1)
                            if previous[-n:] == body[:n]), 0)
            if overlap:
                start = len(whole) - overlap
                whole += body[overlap:]
            else:
                start = len(whole) + 1
                whole += '\n' + body
        pieces.append(Piece(c, start, start + len(body)))
    return SourceDocument(whole, source_id, source_type, pieces)
