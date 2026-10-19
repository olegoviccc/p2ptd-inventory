# Запуск: пошаговая инструкция

Порядок: **InvenTree → бот на ПК рядом с olegovic-llm → проверка → Android-приложение**. Шаги, которые уже сделаны (места, категории, импорт райдеров), отмечены ✅. Их можно пропустить.

Что понадобится:

* ПК с CachyOS, на котором уже работает olegovic-llm (llama-swap или Ollama);
* Python 3.11 или новее (`python --version`);
* доступ администратора в InvenTree: https://p2ptd-inventory.online;
* Telegram.

---

## Часть 1. InvenTree

### 1.1 ✅ Места, категории, оборудование из райдеров

Если ещё не сделано: раздел «Порядок запуска» в [README](../README.md). Это `create_locations.py`, `create_categories.py`, `import_stock.py`.

### 1.2 Уникальность серийных номеров

Настройки системы (System Settings) → в поиске настроек введите **Globally Unique Serials** (ключ `SERIAL_NUMBER_GLOBALLY_UNIQUE`) → **включить**.

Без этой настройки два прибора разных моделей могут получить один номер P2PTD, если кто-то введёт его вручную.

### 1.3 Группа для сотрудников

Админка (Admin Center) → Группы (Groups) → создайте группу, например **«Сотрудники»**, и выдайте права:

| Правило (Rule set) | View | Add | Change |
|---|:-:|:-:|:-:|
| Parts | ✓ | ✓ | ✓ |
| Part Categories | ✓ | | |
| Stock Items | ✓ | ✓ | ✓ (+ Delete, если сотрудник может удалять ошибочные позиции) |
| Stock Locations | ✓ | | |
| Purchase Orders | ✓ | ✓ | |

Purchase Orders нужны потому, что производители в InvenTree относятся к этому правилу.

### 1.4 Шаблон этикетки

Этикетки печатает InvenTree по нашему шаблону. Поставьте его один раз, **после шагов 3.2–3.4** (репозиторий, venv и `.env` уже есть). В `INVENTREE_TOKEN` должен быть токен пользователя со статусом **staff**:

```
cd ~/p2ptd-inventory
.venv/bin/python install_label_template.py --dry-run
.venv/bin/python install_label_template.py
```

Размер по умолчанию — 62 × 29 мм. Под другую ленту: `--width 50 --height 25 --name "P2PTD 50x25"`, и это же имя впишите в `.env` бота: `BOT_LABEL_TEMPLATE=P2PTD 50x25`.

Проверка: откройте любую позицию в InvenTree → «Печать этикеток» → шаблон «P2PTD 62x29» → должен открыться PDF с QR, номером, моделью и местом.

### 1.5 Аккаунты сотрудников

Админка → Пользователи (Users) → у каждого сотрудника **свой** логин и пароль, группа «Сотрудники». В истории InvenTree будет видно, кто что добавил.

---

## Часть 2. Telegram

### 2.1 Отдельный бот

1. В Telegram откройте **@BotFather** → `/newbot` → придумайте имя (например «P2PTD Склад») и username, оканчивающийся на `bot`.
2. BotFather пришлёт токен вида `123456789:AA...`. Это `TELEGRAM_BOT_TOKEN`.

Это **новый** бот. У olegovic-llm свой бот, его токен не используйте: два процесса с одним токеном мешают друг другу.

### 2.2 Ваш Telegram ID

Напишите **@userinfobot**: он ответит числом `Id: 123456789`. Это ваш ID для `BOT_ADMIN_IDS`. Если белый список не нужен, ID сотрудников собирать не обязательно.

---

## Часть 3. Бот на ПК

### 3.1 Проверьте, что модель olegovic работает

```
systemctl --user status llama-swap        # должно быть active (running)
curl -s http://127.0.0.1:8080/v1/models   # список моделей, среди них qwen3.6-35b-a3b
```

Если у вас Ollama, а не llama-swap: `curl -s http://127.0.0.1:11434/v1/models`.

Какую модель возьмёт бот, видно в `~/.config/olegovic/config.toml`, раздел `[llm.models]`: `vision`, а если он пустой — `smart`. Модель должна видеть изображения (в `/model` у olegovic она отмечена 👁).

### 3.2 Скачайте репозиторий

```
git clone https://github.com/olegoviccc/p2ptd-inventory ~/p2ptd-inventory
cd ~/p2ptd-inventory
```

Папка должна называться именно `~/p2ptd-inventory`: на неё ссылается сервис из шага 3.6. Если кладёте в другое место, поправьте пути в `systemd/p2ptd-bot.service`.

### 3.3 Установите зависимости

```
python -m venv .venv
.venv/bin/pip install -r requirements.txt -r bot/requirements.txt -r scraper/requirements.txt
```

### 3.4 Настройки

```
cp .env.example .env
nano .env
```

Заполните:

```
INVENTREE_URL=https://p2ptd-inventory.online
INVENTREE_TOKEN=inv-...
TELEGRAM_BOT_TOKEN=123456789:AA...
BOT_ADMIN_IDS=123456789
BOT_RECOGNIZER=olegovic
```

* `INVENTREE_TOKEN` нужен только скриптам импорта. Бот работает от имени каждого сотрудника, его токен он получает при входе.
* `TELEGRAM_BOT_TOKEN` — из шага 2.1, `BOT_ADMIN_IDS` — ваш ID из шага 2.2.

Необязательно:

* `BOT_ALLOWED_TELEGRAM_IDS=111,222,333` — бот будет отвечать только этим людям (администраторы добавляются сами);
* `BOT_RECOGNIZER=olegovic,claude` и `ANTHROPIC_API_KEY=...` — если модель на ПК не ответила, распознаёт Claude.

Комментарии пишите на отдельной строке, не после значения: иначе комментарий попадёт в значение.

### 3.5 Пробный запуск

```
.venv/bin/python -m bot
```

В выводе должно быть:

```
... бот запущен: InvenTree https://p2ptd-inventory.online, распознавание local
... своя модель: qwen3.6-35b-a3b на http://127.0.0.1:8080/v1 (настройки: /home/.../.config/olegovic/config.toml)
```

и **не** должно быть строк `WARNING ... распознавание local: ...`. Если они есть, см. «Если что-то не так» ниже.

Проверьте в Telegram (шаги 4.1–4.3), потом остановите бота: `Ctrl+C`.

### 3.6 Автозапуск

```
mkdir -p ~/.config/systemd/user
cp systemd/p2ptd-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now p2ptd-bot
systemctl --user status p2ptd-bot          # active (running)
```

Бот запускается вместе с вашим сеансом, так же как сервисы olegovic. Чтобы он работал и без входа в систему (если olegovic у вас так уже не настроен):

```
sudo loginctl enable-linger $USER
```

### 3.7 Ночной поиск мануалов и фото

Фоновый процесс раз в сутки находит модели без картинки или мануала, проверяет находки моделью olegovic-llm и прикладывает их. Управляется из бота кнопкой «🗂 Каталог» (у администраторов). Подробности: [SCRAPER.md](SCRAPER.md).

```
.venv/bin/python -m scraper --stats
.venv/bin/python -m scraper --dry-run --limit 3
cp systemd/p2ptd-scraper.service systemd/p2ptd-scraper.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now p2ptd-scraper.timer
```

Если хотите, чтобы мануалы искал и olegovic-llm (scraper приложит найденное им), включите у него `[manuals] enabled = true` в `~/.config/olegovic/config.toml`.

---

## Часть 4. Первая проверка в Telegram

### 4.1 Вход

1. Откройте своего бота → **Start**.
2. «🔑 Войти» → логин InvenTree → пароль. Сообщение с паролем бот сразу удаляет.
3. Ответ «Готово, вы вошли как …». Если бот пишет, что не хватает прав, вернитесь к шагу 1.3.

### 4.2 Добавление прибора

1. «➕ Добавить прибор» → заведение → место.
2. 1–3 фото: шильдик крупно и общий вид. Файлом мелкий текст читается лучше.
3. «🔍 Распознать». Первый раз может занять минуту: llama-swap загружает модель.
4. Проверьте карточку. Если что-то не так, нажмите «✏️ Исправить» и отправьте исправленный текст. Если всё верно — «✅ Сохранить».
5. Бот пришлёт номер `P2PTD0001` и этикетку. В InvenTree у позиции будут серийник P2PTD, заводской S/N в поле Batch и фото во вложениях.

### 4.3 Поиск и журнал

* «🔎 Найти» → номер P2PTD, заводской серийник или фото QR-этикетки.
* Под найденной позицией — кнопки правки: переместить, статус, заводской S/N, заметка, фото, этикетка, удалить.
* «📜 Журнал» (виден только администраторам) — последние действия всех сотрудников.

Когда всё работает, раздайте ссылку на бота сотрудникам. Каждый входит своим логином.

---

## Часть 5. Android-приложение p2p-inventory

Нужно для обхода со сканированием QR-этикеток. Подробно: [ANDROID.md](ANDROID.md).

1. Один раз создайте ключ подписи и добавьте 4 секрета в GitHub (Settings → Secrets and variables → Actions): `ANDROID_KEYSTORE_BASE64`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`, `ANDROID_KEY_PASSWORD`.
2. GitHub → Actions → **Android app** → Run workflow → версия `0.25.2`.
3. Минут через 11 APK появится в Releases. Установите его на телефоны, в приложении укажите сервер `https://p2ptd-inventory.online`.

Файл ключа и пароли сохраните в менеджере паролей: без них обновить приложение не получится.

---

## Переход на номера без дефиса

С этой версии номера выглядят как `P2PTD0001` (раньше `P2PTD-00001`): на этикетке влезает последняя цифра. Новые приборы сразу получают новый формат и продолжают нумерацию после старых. Уже выданные номера переведите один раз:

```
cd ~/p2ptd-inventory
.venv/bin/python renumber_serials.py --dry-run   # список: P2PTD-00008 → P2PTD0008
.venv/bin/python renumber_serials.py
```

Скрипту нужен `INVENTREE_TOKEN` в `.env` с правами Stock Items: View и Change. QR на уже наклеенных этикетках продолжат работать. Если нужна этикетка с новым номером, перепечатайте её из бота: найти позицию → «🖨 Этикетка».

## Обслуживание

| Что | Команда |
|---|---|
| Лог в реальном времени | `journalctl --user -u p2ptd-bot -f` |
| Лог-файл | `~/p2ptd-inventory/bot_data/logs/bot.log` |
| Перезапуск после правки `.env` | `systemctl --user restart p2ptd-bot` |
| Обновление | `cd ~/p2ptd-inventory && git pull && .venv/bin/pip install -r bot/requirements.txt && systemctl --user restart p2ptd-bot` |
| Остановить | `systemctl --user stop p2ptd-bot` |
| Подробный лог (каждый запрос к InvenTree) | `BOT_LOG_LEVEL=DEBUG` в `.env`, перезапуск |

В `bot_data/` лежат база пользователей и журнал. Не удаляйте её, иначе всем придётся войти заново.

---

## Если что-то не так

| Признак | Что сделать |
|---|---|
| При запуске `Не заданы переменные: …` | Впишите названные переменные в `.env` (шаг 3.4) |
| `Не найден конфиг olegovic-llm` | Укажите путь в `.env`: `OLEGOVIC_CONFIG=/путь/к/config.toml` |
| `WARNING распознавание local: нет связи с http://127.0.0.1:8080/v1` | Модель olegovic не запущена: `systemctl --user start llama-swap`, проверка — шаг 3.1 |
| `WARNING … модели X нет на сервере` | Впишите в `.env` `LOCAL_LLM_MODEL=` с именем из `curl …/v1/models` |
| Бот молчит | Проверьте `systemctl --user status p2ptd-bot`. Если задан `BOT_ALLOWED_TELEGRAM_IDS`, ваш ID должен быть в списке или в `BOT_ADMIN_IDS`. Ошибка `Conflict` в логе означает, что этот токен уже использует другой процесс |
| «Неверный логин или пароль» | Проверьте вход на сайте InvenTree тем же логином |
| После входа «не хватает прав» | Права группы (шаг 1.3) |
| «Сессия InvenTree больше не действует» | Токен отозван или пользователь отключён: войдите заново |
| Распознаёт долго | Первый раз модель грузится. Если постоянно медленно, проверьте, не включены ли размышления (`LOCAL_LLM_THINK` или `[llm.think]` в olegovic) |
| «не распознал: local: ответ не JSON» | Модель ответила не по формату. Начало её ответа записано в лог (`не разобран ответ модели`): `grep -A2 'не разобран' ~/p2ptd-inventory/bot_data/logs/bot.log`. Пришлите эту строку — разберём |
| «модель упёрлась в лимит токенов» | Модель ушла в размышления. Уберите `LOCAL_LLM_THINK=true` и `true` для этой модели в `[llm.think]` olegovic |
| «Не получилось распознать» | Переснимите ближе и ровнее, фото отправьте файлом. Если модель недоступна, включите запасной Claude: `BOT_RECOGNIZER=olegovic,claude` |
| Бот присылает этикетку PNG, а не PDF | Бот не нашёл шаблон или InvenTree не отрисовал его за 30 секунд. Причина в логе: `grep 'этикетка из InvenTree' ~/p2ptd-inventory/bot_data/logs/bot.log`. Чаще всего шаблон не установлен (шаг 1.4) или его имя не совпадает с `BOT_LABEL_TEMPLATE` |
| Серийник с ⚠️ «сверьте с фото» | Штрихкода не было или он не совпал. Сверьте серийник глазами, при необходимости «✏️ Исправить» |
| «Прибор с этим S/N уже заведён» | Он уже в базе, номер указан в сообщении. Найдите его через «🔎 Найти» |
