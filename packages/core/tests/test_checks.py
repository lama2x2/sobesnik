import pytest

from sobesnik_core.checks import is_experience_question, normalize_question, question_problems
from sobesnik_core.questions import GeneratedQuestion

EXPERIENCE = [
    "Расскажите о своём опыте работы с Kafka.",
    "Расскажи, как ты использовал asyncio в своих проектах.",
    "Приходилось ли вам оптимизировать медленные SQL-запросы?",
    "Был ли у вас опыт настройки CI/CD?",
    "С какими базами данных вы работали?",
    "Как вы обычно организуете код в FastAPI-проекте?",
    "Какой фреймворк вы предпочитаете и почему?",
    "Расскажите о проекте, где применялись микросервисы.",
    "В вашем последнем проекте как была устроена авторизация?",
    "Использовали ли вы Kubernetes в продакшене?",
    "Опишите случай из вашей практики, когда пришлось откатывать релиз.",
    "Какие инструменты мониторинга ты применял?",
    "Какой у вас опыт с PostgreSQL?",
]

UNDERSTANDING = [
    "Почему составной индекс (a, b) не помогает запросу с условием только по b?",
    "Как бы вы спроектировали сервис сокращения ссылок на 10 000 RPS?",
    "Что произойдёт, если в asyncio вызвать блокирующую функцию внутри корутины?",
    "Сравните уровни изоляции READ COMMITTED и REPEATABLE READ: какие аномалии они допускают?",
    "Опишите ситуацию, в которой индекс не будет использован планировщиком.",
    "Вы получили жалобу, что эндпоинт отвечает 5 секунд. Как бы вы искали причину?",
    "Чем процесс отличается от потока в Linux?",
    "Объясните, как работает сборщик мусора в CPython.",
    "Какие гарантии доставки сообщений даёт Kafka и как их настроить?",
    "Почему в Go context передают первым аргументом?",
    "Как вы думаете, почему REST плохо подходит для стриминга данных?",
    "Что такое пользовательский опыт (UX) и как его измеряют?",
    "Что бы вы выбрали для кэша сессий: Redis или Memcached, и почему?",
]


@pytest.mark.parametrize("text", EXPERIENCE)
def test_experience_detected(text: str) -> None:
    assert is_experience_question(text)


@pytest.mark.parametrize("text", UNDERSTANDING)
def test_understanding_passes(text: str) -> None:
    assert not is_experience_question(text)


def test_normalize_question() -> None:
    assert normalize_question("  Чем Ёлка — «лучше»?! ") == "чем елка лучше"


def q(text: str) -> GeneratedQuestion:
    return GeneratedQuestion(text=text, key_points=["a", "b", "c"])


def test_problems() -> None:
    questions = [
        q("Чем процесс отличается от потока?"),
        q("Приходилось ли вам писать на Go?"),
        q("чем процесс отличается от потока"),
        q("Что такое GIL?"),
    ]
    problems = question_problems(questions, asked=["Что такое GIL!"])
    assert problems == [
        "questions.1: вопрос о личном опыте кандидата, нужен вопрос на понимание",
        "questions.2: повторяет вопрос 0",
        "questions.3: этот вопрос уже задавался по вакансии",
    ]


def test_no_problems() -> None:
    assert question_problems([q("Что такое GIL?"), q("Что такое MRO?")]) == []
