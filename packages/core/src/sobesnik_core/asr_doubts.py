"""Фильтр шумных сомнений в распознавании (010 §6), без LLM.

Пункт сомнения — «распознано «X» — возможно, сказано «Y» [или «Z»]» (004 §8.6). Полезный пункт —
когда X звучит похоже на Y: распознаватель ослышался («рак» → RAG). Шум — перефраз
(«закрыт доступ» → «скрыт от глаз»), добавка слова («хранить» → «пароли хранить») и простое
написание кириллицей («джейсон» → JSON). Сравнение — по латинице: транслитерация и грубая фонетика.

Фильтр применяется при выдаче: в оценке хранится всё, что вернула модель.
"""

import re
from collections.abc import Sequence
from difflib import SequenceMatcher

SIMILAR_MIN = 0.55
"""Ниже — X и Y звучат непохоже: перефраз, а не огрех распознавания (010 §13 п. 4)."""
SPELLING_MIN = 0.8
"""X кириллицей, Y латиницей и сходство не ниже — тот же термин, просто записан кириллицей."""

_TRANSLIT = {
    **dict(zip("абвгдезийклмнопрстуфхыэ", "abvgdeziiklmnoprstufhye", strict=True)),
    **{"ё": "e", "ж": "zh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ю": "yu", "я": "ya"},
    **{"ъ": "", "ь": ""},
}
_SOUNDS = (("ph", "f"), ("w", "v"), ("c", "k"), ("q", "k"), ("x", "ks"), ("y", "i"), ("j", "g"))
"""Грубая фонетика после транслитерации: «Jill» ≈ «джил» ≈ GIL, «Celery» ≈ «келери»."""
_QUOTED = r"[«\"'“„]([^»\"'”“]+)[»\"'”“]"
_DOUBT = re.compile(r"^\s*распознано\s*" + _QUOTED + r"(.*)$", re.IGNORECASE | re.DOTALL)
_CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)


def _latin(word: str) -> str:
    word = word.lower().replace("дж", "j")
    word = "".join(_TRANSLIT.get(c, c) for c in word)
    word = re.sub(r"[^a-z0-9]", "", word)
    for a, b in _SOUNDS:
        word = word.replace(a, b)
    return word


def _words(text: str) -> list[str]:
    return [w for w in (_latin(part) for part in re.split(r"[\s,/.\-_]+", text)) if w]


def similarity(heard: str, said: str) -> float:
    """Сходство остатков после того, как общие слова убраны; 0 — одно из них пустое
    (слово добавлено или выброшено, а не искажено)."""
    heard_words, said_words = _words(heard), _words(said)
    common = set(heard_words) & set(said_words)
    a = "".join(w for w in heard_words if w not in common)
    b = "".join(w for w in said_words if w not in common)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _just_spelling(heard: str, said: str) -> bool:
    heard_words, said_words = _words(heard), _words(said)
    if heard_words == said_words or "".join(heard_words) == "".join(said_words):
        return True  # «skiplocked» = SKIP LOCKED
    cyrillic_to_latin = bool(_CYRILLIC.search(heard)) and not _CYRILLIC.search(said)
    return cyrillic_to_latin and similarity(heard, said) >= SPELLING_MIN


def is_noisy(doubt: str) -> bool:
    """`True` — пункт не похож на огрех распознавания. Неразобранный пункт не шумный:
    лучше показать лишнее, чем потерять полезное."""
    match = _DOUBT.match(doubt)
    if match is None:
        return False
    heard, rest = match.group(1), match.group(2)
    candidates = re.findall(_QUOTED, rest)
    if not candidates:
        return False
    return not any(
        not _just_spelling(heard, said) and similarity(heard, said) >= SIMILAR_MIN
        for said in candidates
    )


def filter_doubts(doubts: Sequence[str]) -> list[str]:
    return [d for d in doubts if not is_noisy(d)]
