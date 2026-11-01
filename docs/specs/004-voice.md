# 004. Голосовой ответ

Статус: одобрена · 01.11.2026 · спринт 4 ([roadmap](../roadmap.md))

## 1. Цель

Пользователь отвечает на вопрос сессии голосом: присылает аудиофайл (ogg/opus из Telegram, webm, mp3, wav).
API сразу отвечает `202`, аудио ложится в S3-совместимое хранилище, воркер распознаёт его faster-whisper и оценивает транскрипт
**тем же кодом**, что и текстовый ответ (003 §6, §8.2). Оценка ссылается на конкретный транскрипт. К ответу
можно добавить ручную расшифровку (`reference`) и переоценить ответы по ней прогоном — так в спринте 5
считается влияние ошибок распознавания. Аудио живёт только в хранилище инстанса и удаляется по TTL и по запросу.

Вход из спринта 3 (003 §2 «Не входит», §3 п. 4, «Итоги»):

| Что | Чем закрывается | Раздел |
|---|---|---|
| `evaluation.transcript_id` | оценка голосового ответа ссылается на транскрипт | §7, §8.3 |
| `eval_run.transcript_kind` | прогон по `asr` или `reference` | §9.1 |
| статус ответа `transcribing` | новая задача воркера «распознать» | §8.2 |
| `kind = voice` в `CHECK`-списках `answer` | миграция `0004` | §7 |
| колонки транскрипта в CSV | вид ответа, транскрипт, модель ASR | §9.3 |

## 2. Границы

**Входит:**
- S3-совместимое хранилище в compose (RustFS вместо MinIO, §6), интерфейс хранилища аудио,
  загрузка, TTL и удаление аудио;
- `packages/asr`: интерфейс `Transcriber`, провайдер faster-whisper, фейковый провайдер, проверка аудиофайла;
- таблицы `audio` и `transcript`, поля `evaluation.transcript_id` и `eval_run.transcript_kind` (миграция `0004`);
- `POST /sessions/{id}/questions/{qid}/answer/voice`, транскрипт в `GET /answers/{id}`,
  `DELETE /users/{id}/data?scope=audio`;
- задача воркера «распознать» с повторами (ASR-4), передача в оценку, подхват зависших при старте;
- промпт оценки `evaluate_answer/v2`: у голосового ответа оценщик отмечает места, где мог ошибиться
  распознаватель речи, и пользователь видит их рядом с оценкой (§8.6);
- CLI: добавить ручную расшифровку, прогон по виду транскрипта, новые колонки CSV;
- быстрая живая проверка «аудио → транскрипт → оценка» и короткий замер скорости распознавания.

**Не входит:**
- бот и голосовые сообщения Telegram — спринт 6; события смены статуса в Redis — тоже спринт 6;
- `/forget` со `scope=all` (удалить вообще всё: ответы, оценки, вакансии, прогресс) — спринт 7 вместе
  с прогрессом; в этом спринте только `scope=audio` (аудио и транскрипты, 000 §11 п. 7);
- WER, озвучка 20–30 ответов, сравнение баллов `reference` и `asr` — спринт 5;
- повторное распознавание того же аудио другой моделью ASR — спринт 5, если понадобится для WER;
- whisper в контейнере на GPU (профиль `gpu`) — спринт 8;
- длинные замеры: несколько моделей на многих записях. Замер — 1–2 записи на одну модель (§12).

## 3. Отклонения от roadmap и 000

1. **Замер скорости — одна модель вместо трёх** (roadmap: `small`, `medium`, `turbo`). По просьбе автора
   модели не качаются без нужды: мерится модель по умолчанию на 1–2 записях. `medium` и `turbo` — только
   если автор попросит (решение 1).
2. **Принимается ещё m4a** (ANS-2: ogg/opus, webm, mp3, wav). Диктофон на iPhone и Mac пишет m4a, а
   PyAV читает его так же, как остальные форматы. Это одна строка в списке форматов.
3. **Язык**: `ASR_LANGUAGE=ru` по умолчанию, `auto` — автоопределение (ASR-2 «автоопределение как запасной
   вариант» сделано настройкой, а не повтором распознавания: повтор удваивает время на CPU).
4. **Аудио по TTL удаляется из хранилища, но строка `audio` остаётся** с отметкой `deleted_at`: на неё ссылаются
   транскрипты, а их TTL не касается (ASR-5 говорит только об аудио). Транскрипты удаляются по `/forget`.
5. **Хранилище аудио — интерфейс в `packages/asr`** (`AudioStorage`), а не в `apps/api`: по CLAUDE.md
   внешние сервисы (S3) мокаются через интерфейсы из `packages/*`. Отдельный пакет ради трёх методов не заводится.
6. **У транскрипта есть `latency_ms` и `params`** (кроме полей 000 §7): по ним пишется замер и воспроизводится
   распознавание. У аудио — `size_bytes` и `deleted_at`.
7. **Сомнения в распознавании** (§8.6) — новое поле оценки `asr_doubts` и промпт `evaluate_answer/v2`.
   В 000 его нет; добавлено по просьбе автора: пользователь должен понимать, где ошибся он, а где распознаватель.
8. **RustFS вместо MinIO** (§6): образы MinIO больше не публикуются.
После одобрения спеки пункты 2–8 вписываются в 000 (§4.3, §4.4, §6, §7, §9).

## 4. Структура (что меняется)

```
packages/asr/                      новый пакет sobesnik-asr
  src/sobesnik_asr/
    base.py                        Transcriber, Transcription, ошибки
    probe.py                       probe_audio(): формат и длительность (PyAV)
    whisper.py                     FasterWhisperTranscriber
    fake.py                        FakeTranscriber
    storage.py                     AudioStorage, S3AudioStorage, MemoryStorage
    factory.py                     make_transcriber(config), ASRConfig
  tests/
packages/core/src/sobesnik_core/
  evaluation.py                    + asr_doubts в схеме, repair, ScoredEvaluation
  prompts/evaluate_answer/v2.yaml  + $answer_source и правила для расшифровки речи
apps/api/src/sobesnik_api/
  db/models.py                     + Audio, Transcript; поля Evaluation, EvalRun; новые значения CHECK
  services/voice.py                приём голосового ответа, транскрипты, удаление аудио
  services/answers.py              IN_PROGRESS + transcribing
  services/evaluation.py           evaluate_and_store(..., transcript=None)
  services/eval_runs.py            transcript_kind в фильтре и при оценке
  queue.py                         + enqueue_transcription
  worker.py                        + задача распознавания, cron очистки по TTL, подхват
  export.py                        + колонки транскрипта
  cli.py                           + transcript add, eval-run create --transcript
  routes/answers.py, routes/users.py
apps/api/alembic/versions/0004_voice.py
research/asr_check/                cut.sh, check.py (§12)
compose.yaml                       + s3 (RustFS), том моделей whisper
Dockerfile                         + packages/asr/pyproject.toml
```

## 5. Распознавание речи: `packages/asr`

### 5.1. Интерфейс

```python
@dataclass(frozen=True)
class Transcription:
    text: str
    language: str
    language_probability: float | None
    duration_s: float              # длительность аудио по декодеру
    avg_logprob: float | None      # среднее по сегментам, взвешенное длиной
    latency_ms: int
    model: str                     # "small"
    params: dict[str, Any]         # beam_size, compute_type, vad_filter, language

class Transcriber(Protocol):
    name: str                      # "faster_whisper" | "fake"
    model: str
    async def transcribe(self, audio: bytes, *, language: str | None) -> Transcription: ...
    async def aclose(self) -> None: ...

class ASRError(Exception): ...                 # прочий сбой распознавания (повторяется)
class AudioDecodeError(ASRError): ...          # файл не читается (не повторяется)
```

- `FasterWhisperTranscriber`: модель загружается лениво при первом вызове и остаётся в памяти воркера.
  Распознавание синхронное и тяжёлое, поэтому идёт через `asyncio.to_thread`: цикл событий arq не блокируется.
  Параметры: `device=cpu`, `compute_type=int8`, `beam_size=5`, `vad_filter=True` (вырезает тишину, быстрее
  на CPU), `language` из настроек (`None` при `auto`). Модель скачивается с Hugging Face при первом
  использовании в `ASR_MODEL_DIR` (том `asr-models`), дальше работает без сети.
- `FakeTranscriber` возвращает заданный текст или бросает заданную ошибку — для тестов и `tools`.
- `make_transcriber(ASRConfig)` выбирает провайдер по `ASR_PROVIDER` (`faster_whisper | fake`), как `make_provider` в `packages/llm`.
- Текст транскрипта и аудио не пишутся в лог; пишутся длительность, язык, модель, задержка.

### 5.2. Проверка файла

```python
@dataclass(frozen=True)
class AudioInfo:
    format: Literal["ogg", "webm", "mp3", "wav", "m4a"]
    mime: str                      # audio/ogg, audio/webm, audio/mpeg, audio/wav, audio/mp4
    duration_s: float

def probe_audio(data: bytes) -> AudioInfo   # AudioDecodeError, если не аудио или формат не из списка
```

Формат определяется PyAV по содержимому, а не по имени файла или `Content-Type` клиента. PyAV ставится вместе
с faster-whisper, отдельного ffmpeg в образе не нужно.

### 5.3. Хранилище аудио

```python
class AudioStorage(Protocol):
    async def put(self, key: str, data: bytes, mime: str) -> None: ...
    async def get(self, key: str) -> bytes: ...          # AudioNotFoundError, если объекта нет
    async def delete(self, key: str) -> None: ...        # отсутствующий объект — не ошибка
    async def ensure_bucket(self) -> None: ...

class StorageUnavailableError(Exception): ...
```

- `S3AudioStorage` — boto3 через `asyncio.to_thread`, адрес и ключи из env. Работает с любым S3-совместимым
  хранилищем, RustFS — только сервис в compose.
- `MemoryStorage` — словарь в памяти для тестов.
- Ключ объекта — `audio/<answer_id>`: без имён файлов и пользователей. Бакет `sobesnik-audio` создаётся
  при старте API и воркера (`ensure_bucket`), ошибка пишется в лог и не роняет старт.

## 6. Хранилище аудио в compose

- **Вместо MinIO — RustFS** (отклонение от 000, §3 п. 8). При реализации выяснилось, что образы MinIO
  больше не публикуются: `minio/minio` нет ни на Docker Hub, ни на quay.io. RustFS — S3-совместимое
  хранилище под Apache-2.0, повторяет API и переменные MinIO.
- Сервис `s3`: образ `rustfs/rustfs:1.0.3`, ключи из `S3_ACCESS_KEY`/`S3_SECRET_KEY`, консоль выключена,
  порт наружу не публикуется, том `s3data`. Healthcheck — `curl /health`. `api`, `worker` и `tools`
  зависят от здорового `s3`.
- Код от RustFS не зависит (boto3, `S3_ENDPOINT_URL`): замена на SeaweedFS, Garage или облачный S3 —
  правка compose и env.

## 7. Данные (миграция `0004`)

| Таблица | Изменение |
|---|---|
| `answer` | `kind` `CHECK in ('text', 'voice')`; `status` `CHECK in ('received', 'transcribing', 'evaluating', 'done', 'failed')`. У голосового ответа `text` пуст |
| `audio` | `id uuid PK`, `answer_id → answer` (`unique`, `ON DELETE CASCADE`), `s3_key text`, `mime text`, `size_bytes int`, `duration_s real`, `expires_at timestamptz`, `deleted_at timestamptz null`, `created_at`; индекс по `expires_at` где `deleted_at IS NULL` |
| `transcript` | `id uuid PK`, `audio_id → audio` (`ON DELETE CASCADE`), `kind text` `CHECK in ('asr', 'reference')`, `text text`, `language text`, `language_prob real null`, `asr_model text null`, `avg_logprob real null`, `latency_ms int null`, `params jsonb null`, `created_at`; индекс `(audio_id, kind)` |
| `evaluation` | `transcript_id → transcript null` (`ON DELETE SET NULL`) |
| `eval_run` | `transcript_kind text null` `CHECK in ('asr', 'reference')` |

- **Транскрипт для оценки** голосового ответа — последний по `created_at` транскрипт вида `asr` его аудио.
- У одного аудио может быть несколько транскриптов обоих видов (ASR-3). `reference` у `asr_model`,
  `avg_logprob`, `latency_ms` пустые.
- `/forget` удаляет транскрипты: у оценок `transcript_id` становится пустым, сами оценки остаются (EVAL-4).
- `downgrade` удаляет новые таблицы и поля и возвращает старые `CHECK`-списки (если в БД нет голосовых ответов).

## 8. Ответ голосом и воркер

### 8.1. Приём (ANS-2, ANS-3, ASR-1)

`POST /sessions/{id}/questions/{qid}/answer/voice`, `multipart/form-data`, поле `file`:
1. Те же проверки сессии и вопроса, что у текста (003 §8.1 п. 2–3): `404`, `409 session_finished`,
   `409 already_answered`.
2. Размер файла ≤ `AUDIO_MAX_BYTES` (25 МБ), иначе `413 audio_too_large`. Чтение идёт кусками и
   прерывается на лимите.
3. `probe_audio`: не аудио или формат не из списка — `422 audio_unsupported`; короче 1 с — `422 audio_too_short`;
   длиннее `AUDIO_MAX_SECONDS` (300 с) — `422 audio_too_long`.
4. Аудио кладётся в хранилище. Хранилище недоступно — `503 storage_unavailable`, в БД ничего не пишется.
5. В одной транзакции пишутся `answer` (`kind = voice`, `status = received`) и `audio` с
   `expires_at = now + AUDIO_TTL_DAYS`. Если коммит упал — объект удаляется из хранилища.
6. Задача `transcribe_answer` ставится с `job_id = "transcribe:<answer_id>"`. Redis недоступен — всё равно `202`,
   ответ подхватит воркер при старте (как в 003 §8.1). Ответ API — `202 {"answer_id", "status": "received"}`.

В лог пишутся `answer_id`, формат, размер, длительность. Имя файла и содержимое — нет.

### 8.2. Задача `transcribe_answer(answer_id)` (ASR-1…4)

1. Ответа нет, он не голосовой или не в статусе `received`/`transcribing` — ничего не делает.
   Если у аудио уже есть транскрипт `asr` — ставит оценку в очередь и выходит (повторная постановка безопасна).
2. Статус → `transcribing`, коммит.
3. Аудио из хранилища. Объекта нет (удалён по TTL или `/forget`) → ответ `failed`, `error = "audio_missing"`.
4. `Transcriber.transcribe(audio, language=…)`.
5. `AudioDecodeError` → ответ `failed`, `error = "audio_unreadable"`, без повтора.
6. `StorageUnavailableError` или `ASRError` (например, модель не скачалась) → повтор через arq `Retry`
   с паузой 10 с, 30 с, 1 мин; всего `ASR_MAX_TRIES = 4` попытки (первая и 3 повтора, ASR-4). Попытки кончились →
   ответ `failed`, `error = "asr_failed"`.
7. Успех → строка `transcript` (`kind = asr`, модель, язык, уверенность, задержка, параметры).
   Пустой текст после обрезки пробелов (тишина) → транскрипт сохраняется, ответ `failed`, `error = "asr_empty"`:
   бот попросит повторить или ответить текстом (С3).
8. Непустой текст → ответ `evaluating` в той же транзакции, затем в очередь ставится `evaluate_answer`
   (003 §8.2) — дальше всё как у текста.

### 8.3. Оценка голосового ответа (EVAL-5)

`process_answer` (003 §8.2) не меняется по логике. Для голосового ответа он берёт транскрипт для оценки (§7)
и передаёт его в `evaluate_and_store(..., transcript=…)`: оценивается `transcript.text`, а в оценку пишется
`transcript_id`. Нет транскрипта — ответ `failed`, `error = "no_transcript"` (не должно случаться, но не зависает).
Промпт и рубрика те же: рубрика v1 уже не штрафует слова-паразиты и огрехи распознавания (000 §9).

### 8.4. Воркер

- Задачи: `evaluate_answer` (как было), `transcribe_answer` (таймаут 15 мин, `max_tries = ASR_MAX_TRIES`),
  `run_eval` (как было) и cron `cleanup_audio` раз в час.
- `max_jobs = 1` по-прежнему: распознавание и оценка на Маке идут по очереди.
- Подхват при старте: голосовые ответы в `received`/`transcribing` → `transcribe_answer`; ответы
  в `evaluating` и текстовые в `received` → `evaluate_answer`; прогоны — как было.
- Транскрайбер создаётся при старте воркера, модель грузится при первой задаче и держится в памяти
  (`small` в int8 — около 0,5–1 ГБ).

### 8.5. TTL и удаление (ASR-5)

- `cleanup_audio`: аудио с `expires_at < now` и `deleted_at IS NULL` удаляется из хранилища, затем ставится
  `deleted_at`. Хранилище недоступно — пропуск до следующего часа.
- `DELETE /users/{id}/data?scope=audio`: удаляет из хранилища все аудио пользователя, затем строки `audio`
  (транскрипты удаляются каскадом). Ответ — `200 {"audio_deleted": N, "transcripts_deleted": M}`.
  Если хранилище недоступно — `503 storage_unavailable`, в БД ничего не удаляется: аудио не должно остаться
  в хранилище без строки, по которой его можно найти. Повторный вызов безопасен.
- Голосовые ответы и их оценки после удаления остаются: в `GET /answers/{id}` транскрипт `null`.
- Другие значения `scope` — `422` (спринт 7).

### 8.6. Сомнения в распознавании

Распознаватель путает термины: «асинк ай о» вместо «asyncio», «кэш» вместо «кеш» не страшно, а «индекс»
вместо «инцидент» уже меняет смысл. Пользователь должен видеть, где ошибся он сам, а где, возможно, распознаватель.

- Промпт `evaluate_answer/v2` = v1 + переменная `$answer_source` («письменный ответ» или «автоматическая
  расшифровка устного ответа») и правила для расшифровки:
  - написание (кириллица или латиница, «асинк ай о» = «asyncio») не влияет на баллы;
  - если слово похоже на огрех распознавания (созвучно уместному термину), оценивать по смыслу, как будто
    сказан правильный термин, и записать это место в `asr_doubts`;
  - если непонятно, ошибся кандидат или распознаватель, тоже записать в `asr_doubts`, чтобы кандидат сам проверил;
  - `asr_doubts` — до 5 коротких пунктов вида «распознано "…" — возможно, сказано "…"».
- Схему строит код: поле `asr_doubts: list[str]` есть в схеме только у голосовых ответов. У текстовых его нет,
  модель не может его выдумать, а `repair` при пустом поле ничего не делает. `repair`: пустые строки и дубли
  выбрасываются, обрезка до 5 пунктов и до 200 символов.
- Хранится в `evaluation.feedback.asr_doubts`, отдаётся в `GET /answers/{id}` (`evaluation.asr_doubts`) и в CSV.
- Рубрика v1 не меняется: она уже не штрафует огрехи распознавания (000 §9). Новая версия промпта меняет
  `prompt_version` у новых оценок, старые прогоны по-прежнему воспроизводятся промптом v1 (переменная
  `$answer_source` ему просто не нужна).

## 9. Прогоны, расшифровки и CSV

### 9.1. Прогон по виду транскрипта (EVAL-6)

```
sobesnik-cli eval-run create --name NAME [--transcript asr|reference] … (остальное как в 003 §9.1)
```

| `transcript_kind` | Какие ответы | Что оценивается |
|---|---|---|
| не задан (как в бою) | текстовые с текстом и голосовые с транскриптом `asr` | текст или последний `asr` |
| `asr` | только голосовые с транскриптом `asr` | последний `asr` |
| `reference` | только голосовые с транскриптом `reference` | последний `reference` |

Выбор транскрипта — одна функция, общая для «боевой» оценки и прогонов. Пропуск уже оценённых, `restart`, счётчики — без изменений.
Сравнение «эталон против ASR» в спринте 5 — два прогона одной моделью с `--transcript reference` и `--transcript asr`.

### 9.2. Ручная расшифровка

```
sobesnik-cli transcript add ANSWER_ID --file ref.txt [--language ru]
```

Добавляет транскрипт `reference` к аудио голосового ответа (аудио может быть уже удалено по TTL — строка есть).
Текст 1…`ANSWER_MAX_CHARS`. Ответ не голосовой — понятная ошибка.

### 9.3. CSV (EVAL-7)

После колонки `answer` добавляются `answer_kind`, `transcript_id`, `transcript_kind`, `asr_model`,
`audio_duration_s`, а после `advice` — `asr_doubts`. В `answer` теперь лежит **тот текст, который оценивался**: транскрипт у голосового
ответа, текст — у текстового. У старых оценок без транскрипта новые колонки пустые.

## 10. API

| Метод и путь | Запрос | Ответ |
|---|---|---|
| `POST /sessions/{id}/questions/{qid}/answer/voice` | multipart `file` | `202 {answer_id, status}`; `404`, `409` как у текста; `413 audio_too_large`; `422 audio_unsupported`, `audio_too_short`, `audio_too_long`; `503 storage_unavailable` |
| `GET /answers/{id}` | — | как в 003 + `transcript` и `audio` (ниже) |
| `GET /sessions/{id}` | — | как в 003 + у ответа `kind` |
| `DELETE /users/{id}/data?scope=audio` | — | `200 {audio_deleted, transcripts_deleted}`; `404 user_not_found`; `422`; `503 storage_unavailable` |

```json
{
  "id": "…", "kind": "voice", "text": null, "status": "done", "error": null,
  "audio": {"mime": "audio/ogg", "duration_s": 58.4, "expires_at": "…", "deleted": false},
  "transcript": {"id": "…", "kind": "asr", "text": "…", "language": "ru", "asr_model": "small", "latency_ms": 14100},
  "evaluation": { "…": "как в 003 §10", "asr_doubts": ["распознано «асинк ай о» — возможно, сказано «asyncio»"] }
}
```

`transcript` — тот, что оценён «боевой» оценкой, а пока её нет — последний `asr`. У текстового ответа
`audio` и `transcript` — `null`. Транскрипты `reference` пользователю не показываются.

## 11. Конфигурация

| Переменная | По умолчанию | |
|---|---|---|
| `S3_ENDPOINT_URL` | `http://s3:9000` | S3-совместимое хранилище |
| `S3_ACCESS_KEY`, `S3_SECRET_KEY` | `sobesnik`, `sobesnik-secret` | они же учётка сервиса `s3` в compose |
| `S3_BUCKET` | `sobesnik-audio` | |
| `AUDIO_TTL_DAYS` | `30` | ASR-5 |
| `AUDIO_MAX_BYTES` | `26214400` (25 МБ) | |
| `AUDIO_MAX_SECONDS` | `300` | ANS-2 |
| `ASR_PROVIDER` | `faster_whisper` | `fake` в `tools` |
| `ASR_MODEL` | `small` | решение 1 |
| `ASR_LANGUAGE` | `ru` | `auto` — автоопределение |
| `ASR_COMPUTE_TYPE` | `int8` | |
| `ASR_BEAM_SIZE` | `5` | |
| `ASR_MODEL_DIR` | `/models` | том `asr-models` у `worker` и `tools` |
| `ASR_MAX_TRIES` | `4` | первая попытка и 3 повтора |

Новые зависимости: `faster-whisper` (MIT; тянет `ctranslate2` MIT, `av` BSD, `onnxruntime` MIT) и `boto3`
(Apache-2.0) в `packages/asr`; `python-multipart` (Apache-2.0) в `apps/api`; `boto3-stubs[s3]` в dev.
Образ общий для `api` и `worker`, поэтому он вырастет примерно на 250–300 МБ. Веса моделей в образ не попадают.

## 12. Быстрая живая проверка и замер

Всё укладывается в несколько минут, после проверки — `ollama stop qwen3.5:4b`. Модель whisper — одна,
`small` (решение 1). Аудио — только в `research/data/asr_check/`, в git не попадает.

1. **Записи** (решение 2). Источник — записи настоящих собеседований автора (2 видео `.mov` по 65 и 94 мин,
   аудио `.m4a` на 28 мин), они лежат вне репозитория. Целиком они не используются: длиннее 5 минут (ANS-2),
   и замер был бы многочасовым. `research/asr_check/cut.sh SRC START NAME` (на хосте, `ffmpeg`) вырезает
   фрагмент 60 с, оставляет только звук, сводит в моно и пишет `.ogg` (opus, как у Telegram)
   в `research/data/asr_check/`. Берутся 2 фрагмента, где автор отвечает на вопрос. В записях есть и голоса
   интервьюеров — это ещё одна причина держать фрагменты только локально и не печатать транскрипты в лог.
2. **Замер скорости** (`research/asr_check/check.py --asr-only`, в `tools`): распознать фрагменты моделью
   из `ASR_MODEL`, напечатать длительность, время, скорость (секунд распознавания на минуту речи), язык и,
   если рядом лежит `.txt` с ручной расшифровкой, простой WER. Первая загрузка модели меряется отдельно
   и в скорость не входит.
3. **Сквозная проверка** (`check.py`, через API, как `eval_check`): вакансия из кэша → сессия из 2 вопросов →
   фрагменты отправляются голосом → ждём `done` → печатаются транскрипт и оценка. Фрагмент отвечает
   на настоящий вопрос собеседования, а не на вопрос сессии, поэтому балл ничего не значит: проверяется путь
   «аудио → транскрипт → оценка». Затем `transcript add` с `.txt` и прогон `--transcript reference` той же
   моделью → CSV `research/data/asr_check/<session>.csv`, где оценки по `asr` и `reference` стоят рядом.
   Ручная расшифровка — тот же текст `small` без правки (решение 4): проверяется механика, а не точность.

Цифры записываются в «Итоги». Ориентир 000 §5 — 1 минута речи ≤ 30 с на CPU.

## 13. Тесты

Без Ollama, whisper, S3, Redis и сети: `FakeProvider`, `FakeTranscriber`, `MemoryStorage`, подменённый `TaskQueue`.

- `packages/asr`:
  - `probe_audio` на крошечных файлах, сгенерированных в тесте через PyAV (ogg, wav): формат, mime, длительность;
    текст вместо аудио → `AudioDecodeError`;
  - `MemoryStorage`: put/get/delete, удаление отсутствующего не ошибка; `FakeTranscriber`; фабрика по `ASR_PROVIDER`;
  - `FasterWhisperTranscriber` — только параметры вызова через подменённую модель (без загрузки весов).
- `packages/core`: схема с `asr_doubts` только для голоса; `repair` чистит и обрезает список; промпт v2
  рендерится для обоих видов ответа, v1 — по-прежнему.
- `apps/api`:
  - приём голоса: `202`, `answer` + `audio` + объект в хранилище, задача `transcribe` в очереди; размер, формат,
    длительность → `413`/`422`; хранилище недоступно → `503` и пустая БД; Redis недоступен → `202`;
    ошибки сессии как у текста; повтор после `failed`;
  - задача распознавания: happy path → транскрипт со всеми полями, ответ `evaluating`, оценка в очереди;
    пустой текст → `asr_empty`; битый файл → `audio_unreadable` без повтора; сбой ASR → `Retry`, на последней
    попытке `asr_failed`; нет объекта → `audio_missing`; повторный запуск не плодит транскрипты;
  - оценка голосового ответа: оценивается текст транскрипта, `transcript_id` записан, `asr_doubts` сохранены
    и видны в API; текстовые ответы оцениваются как раньше (регрессия 003), `asr_doubts` пустые;
  - прогоны: `transcript_kind` не задан / `asr` / `reference` отбирают нужные ответы и оценивают нужный текст;
    `transcript add` и ошибки;
  - CSV: новые колонки, в `answer` — оценённый текст;
  - TTL: просроченное аудио удалено из хранилища и помечено, транскрипты на месте; `DELETE …/data`:
    аудио и транскрипты удалены, оценки остались с пустым `transcript_id`, `503` ничего не удаляет;
  - подхват при старте по статусам и виду ответа;
  - текст транскрипта и аудио не попадают в лог;
  - миграции `upgrade → downgrade → upgrade` и `compare_metadata`.
- Живой (`-m asr`, новая метка): распознать запись из `research/data/asr_check/`, если она есть (иначе `skip`);
  текст непустой, язык `ru`.

Трассировка:

| Требование | Где | Тесты |
|---|---|---|
| ANS-2 | §5.2, §8.1 | asr: probe; api: приём голоса |
| ANS-3 (+ `transcribing`, без событий) | §8.1–8.2 | api: статусы, задачи |
| ASR-1 | §5.3, §8.1 | api: объект в хранилище, транскрипт в БД |
| ASR-2 | §5.1, §11 | asr: параметры вызова |
| ASR-3 | §7 | api: поля транскрипта, `asr` и `reference` у одного аудио |
| ASR-4 | §8.2 | api: `Retry`, `asr_failed` |
| ASR-5 | §8.5 | api: TTL, `DELETE …/data`, логи |
| EVAL-5 | §8.3, §8.6 | api: `transcript_id`, оценённый текст, `asr_doubts` |
| EVAL-6 (вид транскрипта) | §9.1 | api: прогоны по `asr`/`reference` |
| EVAL-7 (колонки транскрипта) | §9.3 | api: CSV |

## 14. Демо

```bash
docker compose up -d --build                      # + s3
research/asr_check/cut.sh "$SRC" 00:12:30 answer1   # на хосте: 60 с из записи → research/data/asr_check/answer1.ogg

curl -s -X POST localhost:8000/api/v1/sessions/$SESSION/questions/$QID/answer/voice \
  -F file=@research/data/asr_check/answer1.ogg | jq
curl -s localhost:8000/api/v1/answers/$ANSWER | jq   # transcribing → evaluating → done, транскрипт и оценка

docker compose run --rm tools uv run sobesnik-cli transcript add $ANSWER --file research/data/asr_check/answer1.txt
docker compose run --rm tools uv run sobesnik-cli eval-run create --name ref --transcript reference \
  --session $SESSION --wait
docker compose run --rm tools uv run sobesnik-cli export --live --run $RUN --session $SESSION \
  -o research/data/asr_demo.csv
ollama stop qwen3.5:4b
```

## 15. Критерии приёмки

1. Голосовой ответ ~1 мин через API получает `202`, проходит `received → transcribing → evaluating → done`,
   и `GET /answers/{id}` показывает транскрипт и оценку, которая ссылается на этот транскрипт (000 §11 п. 3).
2. Ручная расшифровка добавляется через CLI, прогон `--transcript reference` даёт вторую оценку, обе стоят рядом в CSV.
3. Аудио лежит только в хранилище `s3` (ни в БД, ни в логах, ни в репозитории); TTL и `DELETE …/data?scope=audio`
   удаляют его, что проверено тестами (000 §11 п. 7 — в части аудио и транскриптов).
4. Время распознавания 1–2 записей моделью по умолчанию записано в «Итоги» (ориентир ≤ 30 с на минуту речи).
5. ANS-2, ASR-1…5, EVAL-5 закрыты тестами по таблице трассировки; тесты проходят без whisper, S3, Ollama и Redis.
6. `pytest`, `ruff check`, `ruff format --check`, `mypy` зелёные в `tools`.

## 16. Решения по открытым вопросам

1. **Модель whisper для старта** — `small` (ответ автора: А). `medium`, `turbo` и уже скачанная `large-v3`
   не мерятся.
2. **Тестовые записи** — фрагменты по минуте из записей настоящих собеседований автора (§12 п. 1).
3. **Ручное удаление голоса** остаётся (ответ автора: А): `DELETE /users/{id}/data?scope=audio` (§8.5)
   закрывает 000 §11 п. 7 в части аудио и транскриптов. Автоудаление по TTL — тоже.
4. **Ручная расшифровка для демо не правится** (ответ автора): оценщик и так понимает разное написание
   терминов. Вместо этого у голосового ответа оценщик отмечает места, где мог ошибиться распознаватель (§8.6).
