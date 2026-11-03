# Sobesnik

Тренажёр технических собеседований в Telegram, который работает у тебя на компьютере.

Присылаешь боту вакансию. Sobesnik вытаскивает из неё стек и уровень и задаёт вопросы как на настоящем
собесе. Отвечаешь текстом или голосовым, и модель разбирает ответ по критериям: что сказано верно,
что упущено, что почитать. Слабые темы запоминаются, `/repeat` возвращает к ним новыми вопросами.

- **Бесплатно, с открытым кодом.**
- **Локально:** языковая модель (Ollama) и распознавание речи (whisper) работают на твоём железе.
  Ответы и записи хранятся только у тебя.
- **Без облачных ключей.** Если хочется, можно подключить облачную модель одной строкой в `.env`.

## Что нужно заранее

| Что | Зачем | Где взять |
|---|---|---|
| Mac на Apple Silicon с 16 ГБ памяти или Linux | модель на 4 млрд параметров + Docker | — |
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) 4.27+ | всё, кроме модели, запускается в контейнерах | установщик с сайта, запустить |
| [Ollama](https://ollama.com/download) | языковая модель на Маке работает вне Docker, чтобы задействовать видеоядро | установщик с сайта, запустить |
| токен Telegram-бота | бот — это интерфейс Sobesnik | написать [@BotFather](https://t.me/BotFather) `/newbot`, придумать имя — он пришлёт токен вида `123456789:AAE…` |

## Запуск

Один раз скачать модель (~3,4 ГБ):

```bash
ollama pull qwen3.5:4b
```

Дальше три команды:

```bash
git clone https://github.com/lama2x2/sobesnik && cd sobesnik
```

```bash
cp deploy/.env.example .env
```

Откройте `.env` в любом редакторе и вставьте токен бота в строку `TELEGRAM_BOT_TOKEN=` (без кавычек).

```bash
docker compose up -d --build
```

Первая сборка занимает 3–5 минут. Проверить, что всё поднялось:

```bash
docker compose logs bot | tail -3
```

Должна быть строка `Бот запущен: @имя_вашего_бота; модель qwen3.5:4b готова`. Если вместо неё
подсказка, что сделать, — она же есть в таблице [«Если не работает»](#если-не-работает).

## Первая тренировка

Откройте своего бота в Telegram:

1. `/start` — короткая инструкция.
2. Пришлите вакансию: ссылку на hh.ru или текст целиком. Через ~30–40 с бот покажет должность,
   уровень и ключевые требования. Уровень можно поправить кнопками.
3. «Начать тренировку» — бот готовит 5 вопросов (~30–40 с) и присылает первый. Кнопкой
   «🎯 Выбрать темы» можно взять только нужные темы и выбрать число вопросов.
4. Ответьте текстом или голосовым (кружочек тоже подходит). Бот сразу подтвердит приём, покажет,
   что распознал из голосового, и через 15–30 с пришлёт оценку: балл, что упущено, совет и что почитать.
   «Пропустить» — если вопрос не про вас.
5. После последнего вопроса — сводка.
6. `/progress` — темы: где слабо, где «мало данных», где в порядке.
7. `/repeat` — новые вопросы только по слабым темам.

Все команды — `/help`. Удалить свои записи или все данные — `/forget`.

## Если не работает

| Что видите | Что сделать |
|---|---|
| бот пишет «Модель qwen3.5:4b не скачана» | `ollama pull qwen3.5:4b` на компьютере с Sobesnik, потом прислать вакансию ещё раз |
| бот пишет «Не достучался до Ollama» | запустить приложение Ollama (или `ollama serve`) |
| бот молчит, в `docker compose logs bot` — «в .env пустой TELEGRAM_BOT_TOKEN» | вписать токен в `.env`, `docker compose up -d` |
| в логе бота «Telegram не принял токен» | скопировать токен заново у @BotFather (`/mybots` → API Token), `docker compose up -d` |
| в логе бота «Этот токен уже использует другой запущенный бот» | остановить вторую копию бота с тем же токеном |
| в логе бота «Нет связи с api.telegram.org» | проверить интернет или VPN; бот переподключится сам |
| бот пишет «Похоже, включён VPN в режиме fake-IP» | прислать текст вакансии вместо ссылки или добавить в `.env` строку из сообщения |
| `docker compose up` — `port is already allocated` | в `.env` раскомментировать `API_PORT=` и поставить свободный порт, например `8001` |
| бот отвечает «Это закрытый Sobesnik. Ваш telegram_id: …» | добавить этот id в `BOT_ALLOWED_IDS` в `.env`, `docker compose up -d` |
| первое голосовое долго «распознаётся» | воркер при старте скачивает модель распознавания (~0,5 ГБ); `docker compose logs worker` покажет `asr model ready` |
| что-то другое | `docker compose ps` — все ли сервисы `Up`; `docker compose logs api worker bot` |

Состояние модели можно проверить и без бота: `curl localhost:8000/api/v1/health/llm` — поле `hint`
подскажет, что сделать.

## Другие профили

Профиль — одна или несколько строк в `.env`, готовые блоки — в `deploy/.env.example` (раздел 3).

- **gpu** — Linux-сервер с NVIDIA (нужен NVIDIA Container Toolkit): Ollama и whisper в контейнерах
  на видеокарте, модель скачивается сама. Строка `COMPOSE_FILE=compose.yaml:deploy/compose.gpu.yaml`.
  На реальном GPU не проверялся.
- **cloud** — любая модель с OpenAI-совместимым API: `LLM_PROVIDER=openai_compatible`, `LLM_BASE_URL`,
  `LLM_API_KEY`, `LLM_MODEL`. Модели для генерации вопросов и для оценки можно задать раздельно —
  `LLM_GEN_*` и `LLM_EVAL_*`.

После правки `.env` — `docker compose up -d`.

## Данные и приватность

- Вакансии, ответы, оценки — в Postgres, голосовые записи — в S3-хранилище; оба в томах Docker на вашем
  компьютере. Записи удаляются сами через 30 дней (`AUDIO_TTL_DAYS`), `/forget` — сразу.
- В логи не пишутся ни тексты ответов, ни записи, ни токен бота.
- Ограничение Telegram: голосовое проходит через серверы Telegram, прежде чем попасть к вам.
- Чтобы ботом не пользовались посторонние, впишите свой telegram_id в `BOT_ALLOWED_IDS`
  (его подскажет [@userinfobot](https://t.me/userinfobot)).
- API открыт только на этом компьютере (`127.0.0.1:8000`). Если открываете порт наружу — задайте
  `API_SERVICE_TOKEN`.

## Без бота: через API

Документация — `http://localhost:8000/docs`. Сценарий целиком:

```bash
API=localhost:8000/api/v1
USER_ID=$(curl -s -X PUT $API/users/telegram/1 | jq -r .id)
VAC=$(curl -s -X POST $API/vacancies -H 'content-type: application/json' \
  -d "{\"user_id\": \"$USER_ID\", \"url\": \"https://hh.ru/vacancy/137966266\"}" | jq -r .id)
SESSION=$(curl -s -X POST $API/sessions -H 'content-type: application/json' \
  -d "{\"vacancy_id\": \"$VAC\", \"n\": 5}" | tee /dev/stderr | jq -r .id)
QUESTION=$(curl -s $API/sessions/$SESSION | jq -r '.questions[0].id')
ANSWER=$(curl -s -X POST $API/sessions/$SESSION/questions/$QUESTION/answer \
  -H 'content-type: application/json' -d '{"text": "Мой ответ…"}' | jq -r .answer_id)
sleep 30 && curl -s $API/answers/$ANSWER | jq
```

Голосом — `POST …/answer/voice` с файлом (`-F file=@answer.ogg`: ogg/opus, webm, mp3, wav, m4a, до 5 минут).
Прогресс — `GET /users/$USER_ID/progress`, удалить записи — `DELETE /users/$USER_ID/data?scope=audio`
(`scope=all` — всё).

## Для исследователя и разработчика

- Проверка качества оценки на открытых датасетах, перепрогон другой моделью, выгрузка в CSV —
  [research/README.md](research/README.md).
- Команды тестов, линтеров и CLI — [CLAUDE.md](CLAUDE.md#запуск); спецификации — [docs/specs](docs/specs).

```
apps/api        HTTP API и воркер
apps/bot        Telegram-бот
packages/core   предметная область: вопросы, рубрики, оценки, прогресс
packages/llm    провайдеры языковых моделей (Ollama, OpenAI-совместимый API)
packages/asr    распознавание речи (faster-whisper)
deploy          пример .env и профиль gpu
research        эксперименты по качеству оценки
docs            спецификации и roadmap
```
