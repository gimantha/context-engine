"""Engine-side lineage: placing passages in the text the engine indexed (ADR 0008)."""

from .locator import TextIndex, locate
from .sentences import sentence_range, sentence_starts

__all__ = ["TextIndex", "locate", "sentence_range", "sentence_starts"]
