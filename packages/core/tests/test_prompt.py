import hashlib

import pytest

from sobesnik_core.prompt import load_prompt, parse_prompt, prompt_versions

RAW = b"""\
temperature: 0.5
seed: 7
system: |
  sys $a
user: |
  user $b
"""


def test_parse_prompt() -> None:
    prompt = parse_prompt("x", 3, RAW)
    assert prompt.version == "v3#" + hashlib.sha256(RAW).hexdigest()[:8]
    assert (prompt.temperature, prompt.seed) == (0.5, 7)
    rendered = prompt.render(a="A", b="B {не шаблон}")
    assert rendered.system == "sys A\n"
    assert rendered.user == "user B {не шаблон}\n"


def test_hash_changes_with_content() -> None:
    edited = RAW.replace(b"sys $a", b"system $a")
    assert parse_prompt("x", 3, RAW).version != parse_prompt("x", 3, edited).version


def test_missing_variable() -> None:
    with pytest.raises(KeyError):
        parse_prompt("x", 1, RAW).render(a="A")


def test_null_seed() -> None:
    assert parse_prompt("x", 1, RAW.replace(b"seed: 7", b"seed: null")).seed is None


@pytest.mark.parametrize("name", ["parse_vacancy", "generate_questions"])
def test_package_prompts_load_latest(name: str) -> None:
    versions = prompt_versions(name)
    assert versions
    prompt = load_prompt(name)
    assert prompt.version.startswith(f"v{versions[-1]}#")
    assert load_prompt(name, versions[-1]) == prompt


def test_unknown_prompt() -> None:
    with pytest.raises(FileNotFoundError):
        load_prompt("nope")
