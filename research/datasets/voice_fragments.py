"""Фрагменты записей собеседований автора → JSONL голосовых ответов (005 §5.3).

    research/asr_check/cut.sh ЗАПИСЬ НАЧАЛО ИМЯ СЕКУНД   # на хосте, по фрагменту
    docker compose run --rm tools uv run python research/datasets/voice_fragments.py
    docker compose run --rm tools uv run sobesnik-cli dataset import \
        research/data/asr_check/voice.jsonl

Список фрагментов и вопросов — research/data/asr_check/fragments.yaml (`name`, `question`),
аудио — research/data/asr_check/ИМЯ.ogg. Всё это личные данные: в репозитории только скрипт.
"""

from pathlib import Path
from typing import Any

import yaml

from sobesnik_core.dataset import dump_jsonl

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "research" / "data" / "asr_check"

META = {
    "name": "voice",
    "license": "private",
    "lang": "ru",
    "note": "фрагменты записей собеседований автора, только локально",
}


def convert(fragments: list[dict[str, str]], folder: Path) -> list[dict[str, Any]]:
    items = []
    for fragment in fragments:
        name = fragment["name"]
        if not (folder / f"{name}.ogg").is_file():
            raise SystemExit(f"нет файла {name}.ogg — нарежьте его cut.sh")
        items.append(
            {
                "external_id": name,
                "question_id": name,
                "question": fragment["question"],
                "audio": f"{name}.ogg",
            }
        )
    return items


def main() -> None:
    fragments = yaml.safe_load((DATA / "fragments.yaml").read_text(encoding="utf-8"))
    items = convert(fragments, DATA)
    out = DATA / "voice.jsonl"
    out.write_text(dump_jsonl({**META, "name": f"voice-{len(items)}"}, items), encoding="utf-8")
    print(f"{out.relative_to(ROOT)}: {len(items)} фрагментов")


if __name__ == "__main__":
    main()
