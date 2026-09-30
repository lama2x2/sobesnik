# 001. Вертикальный срез: вакансия → вопросы

Статус: одобрена · 30.09.2026 · спринт 1 ([roadmap](../roadmap.md))

## 1. Цель

`docker compose up -d --build` поднимает API. Два вызова `curl` превращают реальный текст вакансии
в профиль (должность, уровень, стек, требования) и сессию из 5 вопросов, которые сгенерировала модель
в Ollama на хосте. Всё сохраняется в Postgres. Тесты и линтеры зелёные в `tools`, Ollama им не нужна.

Спринт закладывает каркас, на который дальше навешиваются модули. Поэтому важнее правильные границы
(пакеты, интерфейс `LLMProvider`, миграции, compose), чем качество генерации. Генерацию доводят
в спринте 2.

## 2. Границы

**Входит:**
- uv-воркспейс из трёх пакетов: `apps/api`, `packages/core`, `packages/llm`; ruff, mypy (strict), pytest;
- один Dockerfile на весь репозиторий; `compose.yaml` с сервисами `api`, `postgres`, `tools`;
  `deploy/.env.example`;
- `packages/llm`: интерфейс `LLMProvider`, провайдер `ollama`, провайдер `fake`, выбор провайдера через env;
- `packages/core`: доменные типы (уровень, профиль вакансии, сгенерированный вопрос) и лимиты, без I/O;
- Alembic и первая миграция: `user`, `vacancy`, `requirement`, `question`, `session`, `session_question`;
- эндпоинты `GET /health`, `GET /health/llm`, `PUT /users/telegram/{telegram_id}`, `POST /vacancies`,
  `POST /sessions`;
- JSON-логи без текстов вакансий и вопросов;
- раздел «Запуск» в README.

**Не входит (спринт):**
- ретраи на невалидный JSON и на сбои LLM, отказ на «не вакансию» (VAC-3) — спринт 2;
- провайдер `openai_compatible`, раздельные модели для генерации и оценки — спринт 2;
- промпты в файлах с версиями: в спринте 1 промпты — константы в коде с версией `v0` — спринт 2;
- справочник тем, `topic_id` у требований и вопросов, `key_points`, распределение вопросов
  по требованиям — спринт 2;
- `GET`/`PATCH /vacancies/{id}` (VAC-4), `GET /sessions/{id}`, `finish` — спринт 2 и позже;
- авторизация, сервисный и админ-токены — спринт 6;
- Redis, worker, MinIO — спринты 3–4.

## 3. Отклонения от roadmap и 000

1. **В миграцию входят `session` и `session_question`.** В roadmap их нет, но без них `POST /sessions`
   некуда сохранить. Таблицы маленькие, их схема уже есть в 000 §7.
2. **Добавлен `PUT /users/telegram/{telegram_id}`.** У вакансии есть `user_id`, значит, пользователь должен
   существовать. Эндпоинт уже есть в 000 §8, боту он понадобится как есть.
3. **Колонки добавляются вместе с фичами.** `topic_id`, `key_points`, `reference_answer`, `dataset_item_id`
   в первую миграцию не входят и появятся миграциями своих спринтов.

Пункты 1 и 2 вписаны в roadmap.

## 4. Структура

```
pyproject.toml              корень uv-воркспейса; ruff, mypy, pytest; dev-зависимости
uv.lock
Dockerfile                  python:3.12-slim + uv; venv в /opt/venv (не перекрывается монтированием)
compose.yaml
deploy/.env.example
apps/api/
  pyproject.toml            sobesnik-api
  alembic.ini, alembic/
  src/sobesnik_api/
    main.py                 FastAPI app, роутеры, обработчики ошибок
    settings.py             pydantic-settings
    db/                     engine, session, ORM-модели
    routes/                 health, users, vacancies, sessions
    services/               разбор вакансии и генерация вопросов (вызов LLM + сохранение)
    prompts.py              промпты v0
  tests/
packages/core/
  src/sobesnik_core/        Level, VacancyProfile, GeneratedQuestions, лимиты
  tests/
packages/llm/
  src/sobesnik_llm/         LLMProvider, OllamaProvider, FakeProvider, make_provider()
  tests/
```

Импорт пакетов: `sobesnik_api`, `sobesnik_core`, `sobesnik_llm`. `sobesnik_llm` не знает про `sobesnik_core`:
схема ответа передаётся ему как словарь JSON Schema.

## 5. Слой LLM (`packages/llm`)

```python
class LLMRequest(BaseModel):
    system: str
    prompt: str
    json_schema: dict[str, Any] | None = None   # structured output, если провайдер умеет
    temperature: float = 0.2
    seed: int | None = None

class LLMResponse(BaseModel):
    text: str               # сырой ответ модели
    provider: str
    model: str
    latency_ms: int
    tokens_in: int | None
    tokens_out: int | None

class LLMHealth(BaseModel):
    ok: bool
    provider: str
    model: str
    detail: str | None = None

class LLMProvider(Protocol):
    name: str
    model: str
    async def generate(self, request: LLMRequest) -> LLMResponse: ...
    async def health(self) -> LLMHealth: ...
    async def aclose(self) -> None: ...
```

- Ошибки: `LLMUnavailableError` (нет соединения, таймаут, 5xx, модель не скачана) и `LLMError` (прочее).
  Провайдер не валидирует содержимое ответа — это делает вызывающий код через Pydantic.
- **`OllamaProvider`** работает через httpx: `POST /api/chat`, `stream: false`, `format` = `json_schema`,
  `think: false` (у qwen3 и похожих моделей рассуждения не нужны), `options.temperature`, `seed`, `num_ctx`.
  `health()` делает `GET /api/tags` и проверяет, что модель скачана. Таймаут берётся из env.
  В конструктор можно передать `httpx.AsyncClient`, и тесты подставляют `httpx.MockTransport`.
- **`FakeProvider`** отдаёт заранее заданные ответы по очереди (или из функции от запроса), запоминает
  полученные запросы и умеет имитировать недоступность. Лежит в самом пакете, а не в тестах: его
  используют тесты всех пакетов.
- **`make_provider(provider, base_url=, model=, timeout_s=, num_ctx=)`** выбирает провайдер
  по `LLM_PROVIDER=ollama|fake`. Настройки приложения пакет не знает: `apps/api` передаёт значения из env.

## 6. Доменные типы (`packages/core`)

- `Level = Literal["intern", "junior", "middle", "senior", "lead"]`.
- `VacancyProfile`: `title: str`, `level: Level`, `stack: list[str]` (1–30 элементов),
  `requirements: list[Requirement]` (1–15), где `Requirement = {text: str, is_required: bool}`.
- `GeneratedQuestions`: `questions: list[{text: str, requirement_index: int | None}]`.
- Лимиты: `MAX_VACANCY_CHARS = 20_000`, `QUESTIONS_MIN = 3`, `QUESTIONS_MAX = 15`, `QUESTIONS_DEFAULT = 5`.

Эти Pydantic-модели — одновременно JSON Schema для `format` и валидатор ответа модели.
Для вопросов схема собирается под конкретное `n` (`minItems = maxItems = n`).

## 7. Данные (миграция `0001`)

Id — UUID, время — `timestamptz`. `user` в Postgres — зарезервированное слово, поэтому таблица
называется `users`. Остальные имена таблиц — как в 000 §7.

| Таблица | Поля |
|---|---|
| `users` | id, telegram_id bigint unique null, settings jsonb default `{}`, created_at |
| `vacancy` | id, user_id → users, raw_text, title, level, stack text[], parser_model, prompt_version, created_at |
| `requirement` | id, vacancy_id → vacancy (cascade), position, text, is_required |
| `question` | id, requirement_id → requirement null (set null), text, source (`generated`), gen_model, prompt_version, created_at |
| `session` | id, user_id → users, vacancy_id → vacancy null, mode (`vacancy`), status (`active`), created_at, finished_at null |
| `session_question` | session_id → session (cascade), question_id → question, position; PK (session_id, position) |

`level`, `source`, `mode`, `status` хранятся как `text` с `CHECK`, а не как enum Postgres: так их проще
расширять миграциями.

Миграции накатывает сам `api` при старте (`alembic upgrade head` перед uvicorn). Ручная команда из
CLAUDE.md тоже работает. `alembic downgrade base` откатывает всё.

## 8. API

Префикс `/api/v1`, авторизации нет. Ошибки приложения приходят в формате
`{"error": {"code": "...", "message": "..."}}`. Ошибки валидации запроса — стандартный 422 FastAPI.

| Метод и путь | Запрос | Ответ |
|---|---|---|
| `GET /health` | — | `200 {"status": "ok", "db": "ok"}`, при недоступной БД — `503` |
| `GET /health/llm` | — | `200 {"status": "ok", "provider", "model"}`, `503 {"status": "unavailable", "provider", "model", "detail"}` |
| `PUT /users/telegram/{telegram_id}` | — | `201` при создании, `200` если уже был: `{id, telegram_id, created_at}` |
| `POST /vacancies` | `{user_id, text}` | `201` профиль |
| `POST /sessions` | `{vacancy_id, n?}` (3–15, по умолчанию 5) | `201` сессия с вопросами |

Профиль вакансии:
```json
{
  "id": "…", "user_id": "…",
  "title": "Python-разработчик", "level": "middle",
  "stack": ["python", "fastapi", "postgresql"],
  "requirements": [{"id": "…", "text": "Опыт с asyncio", "is_required": true}],
  "parser_model": "qwen3:8b", "prompt_version": "v0", "created_at": "…"
}
```

Сессия:
```json
{
  "id": "…", "vacancy_id": "…", "mode": "vacancy", "status": "active",
  "questions": [{"id": "…", "position": 1, "text": "…", "requirement_id": "…"}]
}
```

Ошибки `POST /vacancies` и `POST /sessions`:
- `404 user_not_found` / `vacancy_not_found`;
- `422` — пустой текст, текст длиннее 20 000 символов, `n` вне 3–15;
- `502 llm_bad_output` — ответ модели не прошёл валидацию или вопросов меньше `n`
  (ретраев в спринте 1 нет, они появятся в спринте 2);
- `502 llm_error` — провайдер вернул прочую ошибку (например, 400 от Ollama);
- `503 llm_unavailable` — Ollama недоступна, модель не скачана или истёк таймаут.

Если `requirement_index` вне диапазона, у вопроса `requirement_id = null`. Разбор и генерация идут
синхронно в запросе (000 §6).

## 9. Конфигурация

`deploy/.env.example`, копируется в `.env` в корне. Если `.env` нет, compose всё равно стартует на
значениях по умолчанию.

| Переменная | По умолчанию | |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://sobesnik:sobesnik@postgres:5432/sobesnik` | |
| `LLM_PROVIDER` | `ollama` | `ollama` \| `fake` |
| `LLM_BASE_URL` | `http://host.docker.internal:11434` | на Linux работает через `extra_hosts: host-gateway` |
| `LLM_MODEL` | `qwen3:8b` | временный выбор, окончательный — в спринте 2 |
| `LLM_TIMEOUT_S` | `120` | |
| `LLM_NUM_CTX` | `16384` | вакансия на 20 000 символов не влезает в стандартный контекст Ollama |
| `LOG_LEVEL` | `INFO` | |

## 10. Compose

- `postgres`: `postgres:17`, volume, healthcheck; наружу порт не публикуется.
- `api`: образ из `Dockerfile`, порт `8000:8000`, `depends_on: postgres (healthy)`, `extra_hosts`
  для `host.docker.internal`.
- `tools`: тот же образ с dev-зависимостями, репозиторий смонтирован в `/app`, `LLM_PROVIDER=fake`,
  `TEST_DATABASE_URL` указывает на отдельную БД `sobesnik_test`. Сервис в профиле `tools`: `up`
  его не поднимает, `run` — работает.

## 11. Тесты

Внешняя зависимость в тестах одна — Postgres из compose. Ollama и сеть не нужны.

- `packages/core`: схемы и граничные значения лимитов.
- `packages/llm`: `OllamaProvider` на `httpx.MockTransport` — формирует правильный запрос (`format`, `think`,
  `options`), разбирает ответ и токены, превращает таймаут, отказ соединения, 404 модели в
  `LLMUnavailableError`, проверяет `health()`; `FakeProvider`; `make_provider`.
- `apps/api` (FakeProvider передаётся в `create_app(settings, llm=...)`, БД `sobesnik_test`,
  миграции в фикстуре):
  - happy path: пользователь → вакансия → сессия из 5 вопросов, всё лежит в БД;
  - `PUT /users/telegram` идемпотентен (201, потом 200);
  - 422 на пустой и слишком длинный текст и на `n` вне диапазона;
  - 404 на несуществующего пользователя и вакансию;
  - 502 на невалидный JSON и на нехватку вопросов, 503 на недоступный провайдер;
  - `/health` и `/health/llm` в обоих состояниях;
  - миграции: `upgrade head` → `downgrade base` → `upgrade head`; модели совпадают с миграциями
    (`compare_metadata` без расхождений).
- Живой тест на реальной Ollama помечен `@pytest.mark.ollama` и по умолчанию пропускается.
  Запуск: `docker compose run --rm tools uv run pytest -m ollama` при запущенной Ollama.

## 12. Демо

```bash
cp deploy/.env.example .env
ollama pull qwen3:8b
docker compose up -d --build
curl -s localhost:8000/api/v1/health/llm
USER=$(curl -s -X PUT localhost:8000/api/v1/users/telegram/1 | jq -r .id)
VAC=$(jq -n --arg u "$USER" --rawfile t vacancy.txt '{user_id: $u, text: $t}' \
  | curl -s -X POST localhost:8000/api/v1/vacancies -H 'content-type: application/json' -d @- | tee /dev/stderr | jq -r .id)
curl -s -X POST localhost:8000/api/v1/sessions -H 'content-type: application/json' \
  -d "{\"vacancy_id\": \"$VAC\", \"n\": 5}" | jq
```

## 13. Критерии приёмки

1. На Mac с Ollama на хосте `docker compose up -d --build` поднимает `api` и `postgres`. Миграции
   накатываются сами, `/health` отвечает `200`.
2. `/health/llm` отвечает `200`, когда Ollama запущена и модель скачана, и `503` в остальных случаях.
3. Демо из §12 на реальной вакансии возвращает профиль с уровнем, стеком и требованиями и сессию
   из 5 вопросов. Всё это видно в БД.
4. Все команды из CLAUDE.md реально работают: `pytest`, `ruff check`, `ruff format --check`, `mypy`
   в `tools` и `alembic upgrade head` в `api`. Результат зелёный.
5. Тесты проходят при выключенной Ollama.
6. Время разбора и генерации на демо-вакансии записано в этот файл, в раздел «Итоги». Это ориентир
   для спринта 2, а не критерий.

## 14. Открытый вопрос

**Модель по умолчанию.** Сейчас в Ollama скачана только `qwen3:1.7b`. Для демо предлагаю скачать `qwen3:8b`
(~5 ГБ, на Mac с 16 ГБ помещается). Если нужна скорость на время разработки, в `.env` можно поставить
`qwen3:1.7b`. Окончательный выбор — в спринте 2.

## Итоги

30.09.2026. Все критерии приёмки выполнены. В `tools` 68 тестов и живой тест на Ollama, ruff и mypy (strict)
зелёные.

**Время на Mac** (qwen3:8b Q4_K_M, Ollama на хосте; вакансия ~1 700 символов, 5 вопросов):

| Операция | Время | Токены (вход / выход) | Ориентир 000 §5 |
|---|---|---|---|
| Разбор вакансии | 16,3 с | 643 / 254 | ≤ 30 с |
| Генерация 5 вопросов | 18,9 с | 393 / 326 | ≤ 40 с |

**Что видно на живой модели (вход для спринта 2):**
- На текст «вакансия» модель выдумывает полный профиль: VAC-3 обязателен.
- Требования приходят в нижнем регистре: правило «нижний регистр» из промпта для `stack`
  модель применила ко всему. Пункты «будет плюсом» в требования не попали, `is_required = false` не встречается.
- Вопросы получаются про опыт («Расскажи, как ты…»), а не на понимание. Промпту генерации нужны примеры
  и явный запрет на вопросы об опыте, иначе оценивать по рубрике будет нечего.
- В БД `vacancy.stack` хранит нормализованные названия; справочник тем закроет разнобой вроде `ci/cd`.
