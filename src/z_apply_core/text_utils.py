from __future__ import annotations


def alnum_key(text: str) -> str:
    """Case-folded alphanumeric-only key for tolerant label comparisons.

    DOM ``name`` attributes are camelCase or placeholder text while evidence
    labels are spaced and titled ("firstName" vs "First Name"), so comparison
    is insensitive to case and punctuation.
    """
    return "".join(character for character in text.casefold() if character.isalnum())


def collapsed_label(text: str) -> str:
    """Case-folded whitespace-collapsed label for evidence line matching."""
    normalized = text.casefold().strip().rstrip("*").strip()
    return " ".join(normalized.split())
