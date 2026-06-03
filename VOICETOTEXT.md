# VoiceToText Bot — Project Reference

## Обзор
Telegram-бот для транскрипции аудио и видео файлов с AI-форматированием результата.
Пользователь отправляет файл → выбирает шаблон → получает структурированный документ (текст в чате / DOCX / Markdown для Obsidian).

## Сервер
- **IP:** 72.56.110.82
- **Пользователь:** root
- **Проект:** `/root/voicetotext/`
- **Сервис:** `voicetotext.service` (systemd, autostart включён)
- **Управление:** `systemctl restart voicetotext.service` / `journalctl -u voicetotext.service -n 50`

## Стек
| Компонент | Технология | Версия |
|---|---|---|
| Бот | python-telegram-bot | 21.6 |
| Скачивание больших файлов | Pyrogram (MTProto) | 2.0.106 |
| Транскрипция | Deepgram nova-2 | SDK 3.7.0 |
| Форматирование | Anthropic Claude | claude-sonnet-4-6 |
| Аудио-обработка | ffmpeg | 6.1.1 |
| Документы | python-docx | ≥1.1.0 |
| Язык | Python | 3.12 |

## Переменные окружения (`/root/voicetotext/.env`)
```
TELEGRAM_BOT_TOKEN=     # токен бота от @BotFather
TELEGRAM_API_ID=        # API ID с my.telegram.org/apps
TELEGRAM_API_HASH=      # API Hash с my.telegram.org/apps
DEEPGRAM_API_KEY=       # ключ Deepgram
ANTHROPIC_API_KEY=      # ключ Anthropic
```

## Pipeline обработки файла
```
Пользователь отправляет файл
        │
        ▼
handle_audio()
  ├─ Тип: audio / voice / video / document
  ├─ Если file_size > 20 МБ → download_large_file() через Pyrogram
  └─ Если ≤ 20 МБ → tg_file.get_file() (стандартный Bot API)
        │
        ▼
extract_audio()          ← ffmpeg: вырезает аудиодорожку, opus 32kbps/16kHz
        │
        ▼
transcribe_audio()       ← Deepgram nova-2, ru, smart_format, diarize
        │
        ▼
format_with_claude()     ← Claude + prompt шаблона
        │
        ▼
deliver_result()
  ├─ Текст в чат (с именем исходного файла в заголовке)
  ├─ Кнопка 📄 DOCX → create_docx()
  └─ Кнопка 📝 Markdown → create_md() (YAML frontmatter для Obsidian)
```

## Ключевые технические решения

### Большие файлы (> 20 МБ)
Telegram Bot API не позволяет скачивать файлы > 20 МБ. Решение — Pyrogram (MTProto), который не имеет этого ограничения.
```python
# get_pyro() — ленивая инициализация клиента
# download_large_file(chat_id, message_id, suffix) — скачивает через MTProto
```
Требует: `TELEGRAM_API_ID` + `TELEGRAM_API_HASH` от my.telegram.org/apps.

### Таймаут Deepgram
В SDK 3.7.0 захардкожен таймаут 30 сек в `abstract_sync_client.py`.
Решение — пропатчен SDK-файл напрямую: `httpx.Timeout(30.0)` → `httpx.Timeout(None)`.
```
/usr/local/lib/python3.12/dist-packages/deepgram/clients/abstract_sync_client.py
```
**Важно:** при обновлении `deepgram-sdk` патч слетит — нужно применить снова.

### Сжатие аудио (ffmpeg)
Любой файл (mp4, webm, mp3) сначала прогоняется через ffmpeg:
- Убирает видеодорожку (`-vn`)
- Конвертирует в opus 32kbps / 16kHz
- Уменьшает размер в 10–50 раз
- Fallback на оригинал если ffmpeg упал

## Шаблоны форматирования (10 штук)
| ID | Название | Описание |
|---|---|---|
| `meeting` | 📋 Meeting Notes | Протокол: резюме, решения, задачи |
| `interview` | 🎤 Interview | Формат вопрос-ответ с выводами |
| `lecture` | 📚 Lecture Notes | Конспект: концепции, определения |
| `journal` | 📔 Voice Journal | Личные заметки, мысли, планы |
| `summary` | ✨ Smart Summary | TL;DR и ключевые тезисы |
| `spec` | 📐 Тех. задание | Функциональные требования (с доп. контекстом) |
| `client_call` | 📞 Звонок с клиентом | Потребности, возражения, договорённости, шаги |
| `email` | ✉️ Письмо | Голос → готовое деловое письмо |
| `sales_coaching` | 🎯 Оценка звонка | Коучинг по 5 этапам продаж (оценка ⭐/5) |
| `client_meeting` | 📋 Client Meeting Notes | Встреча с клиентом: потребности, продукты, договорённости |

### Шаблон `spec` — особый флоу
После выбора шаблона бот просит описать задачу текстом или голосом (или "Пропустить"). Контекст передаётся в prompt вместе с транскриптом.

## Теги Obsidian по шаблонам
```python
TEMPLATE_TAGS = {
    "meeting":        ["встреча", "протокол"],
    "action_items":   ["задачи", "action-items"],
    "summary":        ["резюме", "summary"],
    "spec":           ["тех-задание", "spec"],
    "client_call":    ["клиент", "звонок", "crm"],
    "email":          ["письмо", "email"],
    "sales_coaching": ["продажи", "коучинг", "оценка-звонка"],
    "client_meeting": ["клиент", "встреча", "crm"],
}
```

## Структура MD-файла (Obsidian)
```yaml
---
title: "Оценка звонка — meeting_2026-05-27"
date: 2026-05-27
tags:
  - voicetotext
  - продажи
  - коучинг
source: "meeting_2026-05-27.mp4"
template: "Оценка звонка"
---

[текст от Claude в Markdown]
```

## Имена файлов
Все генерируемые файлы включают имя исходного аудио/видео:
- DOCX: `meeting_call.mp4 — 🎯 Оценка звонка.docx`
- MD: `meeting_call.mp4 — 🎯 Оценка звонка.md`
- Заголовок в чате: `🎙 Оценка звонка\n📎 meeting_call`

## Поддерживаемые форматы файлов
- Аудио: mp3, wav, ogg, m4a и любые MIME audio/*
- Видео: mp4, webm (Telegram отправляет как `message.video`)
- Документы: любые с MIME audio/* (отправленные как файл)
- Голосовые сообщения Telegram (voice)

## Структура файлов проекта
```
/root/voicetotext/
├── main.py          # весь код бота (1100+ строк)
├── .env             # секреты (не в git)
├── .env.example     # шаблон переменных
├── requirements.txt # зависимости
├── .git/            # репозиторий
└── .claude/
    └── settings.local.json
```

## Частые проблемы и решения

| Проблема | Причина | Решение |
|---|---|---|
| `Conflict: terminated by other getUpdates` | Два экземпляра бота с одним токеном | Остановить второй экземпляр или сменить токен через @BotFather |
| `File is too big` | Файл > 20 МБ, Bot API не отдаёт | Pyrogram скачивает автоматически (уже настроено) |
| `ReadTimeout` от Deepgram | Захардкоженный 30-сек таймаут в SDK | Патч в abstract_sync_client.py (уже применён) |
| Бот не реагирует на webm/mp4 | Telegram шлёт как `message.video` | Фильтр `filters.VIDEO` уже добавлен |

## Команды для обслуживания
```bash
# Статус сервиса
systemctl status voicetotext.service

# Перезапуск
systemctl restart voicetotext.service

# Логи в реальном времени
journalctl -u voicetotext.service -f

# Логи без polling-шума
journalctl -u voicetotext.service -n 50 | grep -v getUpdates
```
