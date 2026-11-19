"""Text fingerprints shared by the channel archive and the publisher."""

import hashlib
import unicodedata


# Older channel captions mix Cyrillic and Latin lookalikes, even within words.
# Map capitals before case folding (Latin H looks like Cyrillic Н).
_LOOKALIKES = str.maketrans(
    "ABCEHKMOPTXYacekopxy",
    "АВСЕНКМОРТХУасекорху",
)


def text_digest(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).translate(_LOOKALIKES)
    normalized = normalized.casefold().replace("ё", "е")
    letters = "".join(char for char in normalized if char.isalnum())
    return hashlib.sha256(letters.encode("utf-8")).hexdigest()
