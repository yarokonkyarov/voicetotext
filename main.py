#!/usr/bin/env python3
"""
Voice-to-Text Telegram Bot
Transcribes MP3/WAV audio via Deepgram and formats using PLAUD-style templates.
"""

import asyncio
import os
import logging
import tempfile
import subprocess
from typing import Dict, Any, Optional

from docx import Document
from docx.shared import Pt
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message, constants
from telegram.ext import (
    Application,
    MessageHandler,
    CallbackQueryHandler,
    CommandHandler,
    filters,
    ContextTypes,
)
from deepgram import DeepgramClient, DeepgramClientOptions, PrerecordedOptions, FileSource
import anthropic
from pyrogram import Client as PyroClient

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8385003837:AAGcZz-LP5exdsFy4i5mn578RRey-6mJcRM")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "8b296b68246aaa4e5468512f972a4d9ea90dfb8a")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")

# ─── State ────────────────────────────────────────────────────────────────────

# user_id -> {path, filename}
pending_audio: Dict[str, Dict[str, Any]] = {}

# user_id -> {mode: "initial"|"reformat", path?: str, transcript?: str, chat_id: int}
pending_spec_context: Dict[str, Dict[str, Any]] = {}

# user_id -> {formatted, template_name, transcript}  — last processed result
last_result: Dict[str, Dict[str, Any]] = {}



# ─── Pyrogram client (large file downloads) ───────────────────────────────────

_pyro: Optional[PyroClient] = None


async def get_pyro() -> PyroClient:
    global _pyro
    if _pyro is None:
        _pyro = PyroClient(
            "voicetotext_dl",
            api_id=TELEGRAM_API_ID,
            api_hash=TELEGRAM_API_HASH,
            bot_token=TELEGRAM_BOT_TOKEN,
            workdir="/root/voicetotext",
            no_updates=True,
        )
        await _pyro.start()
    return _pyro


async def download_large_file(chat_id: int, message_id: int, suffix: str) -> str:
    client = await get_pyro()
    msg = await client.get_messages(chat_id, message_id)
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.close()
    await client.download_media(msg, file_name=tmp.name)
    return tmp.name

# ─── Templates ────────────────────────────────────────────────────────────────

TEMPLATES: Dict[str, Dict[str, str]] = {
    "meeting": {
        "name": "📋 Meeting Notes",
        "description": "Ключевые пункты, решения и задачи",
    },
    "interview": {
        "name": "🎤 Interview",
        "description": "Формат вопрос-ответ с выводами",
    },
    "lecture": {
        "name": "📚 Lecture Notes",
        "description": "Концепции, определения и конспект",
    },
    "journal": {
        "name": "📔 Voice Journal",
        "description": "Личные заметки, мысли и планы",
    },
    "summary": {
        "name": "✨ Smart Summary",
        "description": "TL;DR и ключевые тезисы",
    },
    "spec": {
        "name": "📐 Тех. задание",
        "description": "Функциональные требования к продукту",
    },
    "client_call": {
        "name": "📞 Звонок с клиентом",
        "description": "Потребности, возражения, договорённости, следующие шаги",
    },
    "email": {
        "name": "✉️ Письмо",
        "description": "Голос → готовое письмо с темой и структурой",
    },
}

TEMPLATE_PROMPTS: Dict[str, str] = {
    "meeting": """Ты профессиональный редактор протоколов встреч. Оформи транскрипт ниже в структурированный протокол на русском языке.

## 📋 Протокол встречи

### Краткое резюме
[2–3 предложения о чём была встреча]

### Ключевые темы обсуждения
- ...

### Принятые решения
- ...

### Задачи и поручения
- [ ] Задача — Ответственный (если упомянут)

### Следующие шаги
- ...

---
Транскрипт:
{transcript}

Верни только оформленный протокол. Никаких пояснений.""",

    "interview": """Ты профессиональный редактор интервью. Оформи транскрипт ниже в формат интервью на русском языке.

## 🎤 Интервью

[Раздели на вопросы и ответы. Если спикеры не обозначены — определи самостоятельно.]

**В:** ...
**О:** ...

### Ключевые выводы
- ...

---
Транскрипт:
{transcript}

Верни только оформленное интервью. Никаких пояснений.""",

    "lecture": """Ты редактор учебных материалов. Оформи транскрипт ниже как конспект лекции на русском языке.

## 📚 Конспект лекции

### Тема
[Основной предмет]

### Ключевые концепции
1. ...

### Определения и термины
**Термин:** Определение

### Краткое содержание
[Краткий абзац]

### Вопросы для повторения
- ...

---
Транскрипт:
{transcript}

Верни только конспект. Никаких пояснений.""",

    "journal": """Ты помогаешь вести голосовой дневник. Оформи транскрипт ниже как запись в дневнике на русском языке.

## 📔 Запись в дневнике

### Мысли и размышления
[Основные мысли из записи]

### Важные моменты
[Значимые события или идеи]

### Настроение и эмоции
[Эмоциональный контекст, если прослеживается]

### Планы и намерения
[Упомянутые цели или задачи]

---
Транскрипт:
{transcript}

Верни только запись в дневнике. Никаких пояснений.""",

    "summary": """Ты эксперт по созданию кратких резюме. Создай умное резюме транскрипта на русском языке.

## ✨ Умное резюме

### Коротко о главном (TL;DR)
[2–3 предложения]

### Ключевые тезисы
• ...
• ...
• ...

### Важные детали
[Факты и подробности, которые стоит запомнить]

### Что нужно сделать
[Конкретные действия и следующие шаги, если упомянуты]

---
Транскрипт:
{transcript}

Верни только резюме. Никаких пояснений.""",

    "client_call": """Ты эксперт по продажам и работе с клиентами. Оформи транскрипт ниже как структурированную заметку о звонке с клиентом на русском языке.

## 📞 Звонок с клиентом

### Клиент и контекст
[Кто звонил / с кем говорили, компания, должность если упомянуты]

### Потребности и запросы клиента
- ...

### Возражения и опасения
- ...

### Достигнутые договорённости
- ...

### Следующие шаги
- [ ] Задача — Ответственный (если упомянут) — Срок (если упомянут)

### Общее впечатление
[Настрой клиента, уровень заинтересованности, риски]

---
Транскрипт:
{transcript}

Верни только заметку о звонке. Никаких пояснений.""",

    "email": """Ты профессиональный бизнес-копирайтер. Преобразуй транскрипт ниже в готовое деловое письмо на русском языке.

## ✉️ Письмо

**Тема:** [Сформулируй тему письма]

---

[Приветствие]

[Основная часть письма — структурированно, по существу, деловой тон]

[Завершение и призыв к действию если нужен]

С уважением,
[Имя автора если упомянуто]

---
Транскрипт:
{transcript}

Верни только готовое письмо. Никаких пояснений.""",

    "spec": """Ты эксперт по написанию технических заданий и функциональных требований. Оформи транскрипт ниже как структурированное ТЗ на русском языке.

## 📐 Функциональные требования

### Описание продукта
[Что это за продукт, для кого предназначен, какую проблему решает]

### Цели и задачи
- ...

### Функциональные требования
#### FR-1. [Название функциональности]
- **Описание:** ...
- **Входные данные:** ...
- **Ожидаемый результат:** ...

#### FR-2. ...

### Нефункциональные требования
- **Производительность:** ...
- **Безопасность:** ...
- **Интерфейс и UX:** ...
- **Надёжность:** ...

### Ограничения и допущения
- ...

### Открытые вопросы
- ...

---
Транскрипт:
{transcript}

Верни только ТЗ. Никаких пояснений.""",
}

SPEC_CUSTOM_PROMPT = """Ты эксперт по написанию технических заданий и функциональных требований.

Контекст от заказчика:
{custom_context}

Используя этот контекст как направляющий, оформи транскрипт ниже как структурированное ТЗ на русском языке.

## 📐 Функциональные требования

### Описание продукта
[Что это за продукт, для кого предназначен, какую проблему решает]

### Цели и задачи
- ...

### Функциональные требования
#### FR-1. [Название функциональности]
- **Описание:** ...
- **Входные данные:** ...
- **Ожидаемый результат:** ...

#### FR-2. ...

### Нефункциональные требования
- **Производительность:** ...
- **Безопасность:** ...
- **Интерфейс и UX:** ...
- **Надёжность:** ...

### Ограничения и допущения
- ...

### Открытые вопросы
- ...

---
Транскрипт:
{transcript}

Верни только ТЗ. Никаких пояснений."""

# ─── Keyboards ────────────────────────────────────────────────────────────────


def build_template_keyboard(user_id: str, prefix: str = "tpl") -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton(
            f"{t['name']}  —  {t['description']}",
            callback_data=f"{prefix}:{tid}:{user_id}",
        )]
        for tid, t in TEMPLATES.items()
    ]
    if prefix == "retpl":
        keyboard.append([InlineKeyboardButton("✖ Отмена", callback_data=f"action:reformat_cancel:{user_id}")])
    return InlineKeyboardMarkup(keyboard)


def build_action_keyboard(user_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Другой шаблон", callback_data=f"action:reformat:{user_id}")],
        [InlineKeyboardButton("📄 Скачать DOCX", callback_data=f"action:docx:{user_id}")],
    ])


# ─── Core helpers ─────────────────────────────────────────────────────────────


async def send_long_message(chat_id: int, text: str, bot) -> Message:
    """Split and send messages longer than Telegram's 4096-char limit. Returns last message."""
    limit = 4000
    chunks = [text[i: i + limit] for i in range(0, len(text), limit)]
    last = None
    for chunk in chunks:
        last = await bot.send_message(chat_id=chat_id, text=chunk)
    return last



def extract_audio(input_path: str) -> str:
    out = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
    out.close()
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", input_path,
            "-vn",
            "-acodec", "libopus",
            "-b:a", "32k",
            "-ar", "16000",
            out.name,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return out.name


def transcribe_audio(tmp_path: str) -> str:
    deepgram = DeepgramClient(
        DEEPGRAM_API_KEY,
        DeepgramClientOptions(options={"timeout": 300}),
    )
    with open(tmp_path, "rb") as f:
        buffer_data = f.read()
    payload: FileSource = {"buffer": buffer_data}
    options = PrerecordedOptions(
        model="nova-2",
        smart_format=True,
        language="ru",
        punctuate=True,
        paragraphs=True,
        diarize=True,
    )
    response = deepgram.listen.rest.v("1").transcribe_file(payload, options)
    return response.results.channels[0].alternatives[0].transcript


def format_with_claude(template_id: str, transcript: str, custom_context: Optional[str] = None) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    MAX_TRANSCRIPT_CHARS = 60_000
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        transcript = transcript[:MAX_TRANSCRIPT_CHARS] + "\n\n[... транскрипт обрезан ...]"

    if template_id == "spec" and custom_context:
        prompt = SPEC_CUSTOM_PROMPT.format(custom_context=custom_context.strip(), transcript=transcript)
    else:
        prompt = TEMPLATE_PROMPTS[template_id].format(transcript=transcript)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8096,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def create_docx(template_name: str, formatted_text: str) -> str:
    """Convert formatted text to .docx. Returns temp file path."""
    doc = Document()

    title = doc.add_heading(template_name, level=0)
    title.runs[0].font.size = Pt(18)

    for line in formatted_text.splitlines():
        if line.startswith("#### "):
            doc.add_heading(line[5:], level=3)
        elif line.startswith("### "):
            doc.add_heading(line[4:], level=2)
        elif line.startswith("## "):
            doc.add_heading(line[3:], level=1)
        elif line.startswith(("- ", "• ")):
            doc.add_paragraph(line[2:], style="List Bullet")
        elif line.startswith("- [ ] "):
            doc.add_paragraph("☐ " + line[6:], style="List Bullet")
        elif line.strip() in ("---", "─" * 10):
            doc.add_paragraph("─" * 40)
        elif line.strip():
            # Bold **text** → strip markers for simplicity
            clean = line.replace("**", "")
            doc.add_paragraph(clean)

    tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
    doc.save(tmp.name)
    tmp.close()
    return tmp.name



# ─── Result delivery ──────────────────────────────────────────────────────────


async def deliver_result(
    chat_id: int,
    bot,
    user_id: str,
    template_id: str,
    transcript: str,
    formatted: str,
):
    """Send formatted result and store it with action buttons."""
    template_name = TEMPLATES[template_id]["name"]

    last_result[user_id] = {
        "transcript": transcript,
        "formatted": formatted,
        "template_id": template_id,
        "template_name": template_name,
    }

    header = f"🎙 {template_name}\n{'─' * 32}\n\n"
    await send_long_message(chat_id=chat_id, text=header + formatted, bot=bot)
    await bot.send_message(
        chat_id=chat_id,
        text="Действия с результатом:",
        reply_markup=build_action_keyboard(user_id),
    )


# ─── Spec context flow ────────────────────────────────────────────────────────


async def process_after_spec_context(
    chat_id: int,
    status_message: Message,
    bot,
    user_id: str,
    spec_state: Dict[str, Any],
    custom_context: Optional[str],
):
    """Transcribe (if needed) and format as spec with optional custom context."""
    loop = asyncio.get_event_loop()
    try:
        if spec_state["mode"] == "initial":
            tmp_path = spec_state["path"]
            await status_message.edit_text(
                f"✅ Шаблон: {TEMPLATES['spec']['name']}\n⏳ Транскрибирую аудио…"
            )
            try:
                compressed_path = await loop.run_in_executor(None, extract_audio, tmp_path)
            except Exception:
                compressed_path = tmp_path
            try:
                transcript = await loop.run_in_executor(None, transcribe_audio, compressed_path)
            finally:
                for p in set([tmp_path, compressed_path]):
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
        else:
            transcript = spec_state["transcript"]

        if not transcript.strip():
            await status_message.edit_text("❌ Deepgram не смог распознать речь. Попробуй снова.")
            return

        await status_message.edit_text(
            f"✅ Транскрипция готова ({len(transcript)} символов)\n⏳ Формирую ТЗ…"
        )

        if ANTHROPIC_API_KEY:
            try:
                formatted = await loop.run_in_executor(
                    None, format_with_claude, "spec", transcript, custom_context
                )
            except Exception as e:
                logger.exception("Claude failed")
                await bot.send_message(chat_id=chat_id, text=f"⚠️ Claude ошибка: {e}\n\nСырой транскрипт:")
                formatted = transcript
        else:
            formatted = transcript

        await status_message.edit_text(f"✅ Готово! Шаблон: {TEMPLATES['spec']['name']}")
        await deliver_result(chat_id, bot, user_id, "spec", transcript, formatted)

    except Exception as exc:
        logger.exception("Spec processing error")
        await status_message.edit_text(f"❌ Ошибка: {exc}")


# ─── Handlers ────────────────────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привет! Отправь мне аудиофайл (MP3, WAV, OGG, WEBM) или голосовое сообщение, "
        "и я транскрибирую его с помощью Deepgram.\n\n"
        "После загрузки выбери шаблон оформления текста."
    )


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user_id = str(update.effective_user.id)

    SUPPORTED_MIME = {
        "audio/mpeg", "audio/mp3",
        "audio/wav", "audio/wave", "audio/x-wav",
        "audio/ogg", "audio/opus",
        "audio/webm", "video/webm",
    }

    # Audio received while waiting for spec context voice → treat as context voice
    if user_id in pending_spec_context and (message.voice or message.audio):
        tg_file = await (message.voice or message.audio).get_file()
        suffix = ".ogg" if message.voice else ".mp3"
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        await tg_file.download_to_drive(tmp.name)
        tmp.close()

        status = await message.reply_text("⏳ Транскрибирую контекст…")
        loop = asyncio.get_event_loop()
        try:
            ctx_text = await loop.run_in_executor(None, transcribe_audio, tmp.name)
        except Exception as exc:
            await status.edit_text(f"❌ Не удалось транскрибировать контекст: {exc}")
            return
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

        spec_state = pending_spec_context.pop(user_id)
        await status.edit_text(f"✅ Контекст получен\n⏳ Обрабатываю запись…")
        await process_after_spec_context(
            chat_id=message.chat_id,
            status_message=status,
            bot=context.bot,
            user_id=user_id,
            spec_state=spec_state,
            custom_context=ctx_text,
        )
        return

    # Normal audio upload
    MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB — Telegram bot limit

    if message.audio:
        media = message.audio
        filename = media.file_name or "audio.mp3"
    elif message.voice:
        media = message.voice
        filename = "voice.ogg"
    elif message.video:
        media = message.video
        filename = media.file_name or "video.webm"
    elif message.document and message.document.mime_type in SUPPORTED_MIME:
        media = message.document
        filename = media.file_name or "audio.mp3"
    else:
        await message.reply_text("⚠️ Пожалуйста, отправь аудиофайл MP3, WAV, OGG, WEBM или голосовое сообщение.")
        return

    if media.file_size and media.file_size > MAX_FILE_SIZE:
        size_mb = media.file_size / 1024 / 1024
        status = await message.reply_text(
            f"📦 Файл {size_mb:.1f} МБ — скачиваю через расширенный режим…"
        )
        suffix = os.path.splitext(filename)[-1] or ".webm"
        try:
            tmp_path = await download_large_file(message.chat_id, message.message_id, suffix)
        except Exception as exc:
            logger.exception("Pyrogram download failed")
            await status.edit_text(f"❌ Не удалось скачать файл: {exc}")
            return
        await status.edit_text("✅ Файл получен! Выбери шаблон оформления:")
        pending_audio[user_id] = {"path": tmp_path, "filename": filename}
        await status.edit_text(
            "✅ Файл получен! Выбери шаблон оформления:",
            reply_markup=build_template_keyboard(user_id),
        )
        return

    tg_file = await media.get_file()

    status = await message.reply_text("⏳ Загружаю файл…")
    suffix = os.path.splitext(filename)[-1] or ".mp3"
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    await tg_file.download_to_drive(tmp.name)
    tmp.close()

    pending_audio[user_id] = {"path": tmp.name, "filename": filename}
    await status.edit_text(
        "✅ Файл получен! Выбери шаблон оформления:",
        reply_markup=build_template_keyboard(user_id),
    )


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text: spec context input or email address input."""
    user_id = str(update.effective_user.id)
    text = update.message.text.strip()

    if user_id in pending_spec_context:
        spec_state = pending_spec_context.pop(user_id)
        status = await update.message.reply_text("✅ Контекст получен\n⏳ Обрабатываю запись…")
        await process_after_spec_context(
            chat_id=update.message.chat_id,
            status_message=status,
            bot=context.bot,
            user_id=user_id,
            spec_state=spec_state,
            custom_context=text,
        )
        return



async def handle_template_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initial template selection after audio upload."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":", 2)
    if parts[0] != "tpl" or len(parts) != 3:
        return
    _, template_id, user_id = parts

    if user_id not in pending_audio:
        await query.edit_message_text("❌ Аудиофайл не найден. Пожалуйста, отправь файл ещё раз.")
        return

    template = TEMPLATES.get(template_id)
    if not template:
        await query.edit_message_text("❌ Неизвестный шаблон.")
        return

    audio_info = pending_audio.pop(user_id)

    if template_id == "spec":
        pending_spec_context[user_id] = {
            "mode": "initial",
            "path": audio_info["path"],
            "chat_id": query.message.chat_id,
        }
        await query.edit_message_text(
            "📐 Тех. задание\n\n"
            "Опиши задачу — что за продукт, для кого, какие цели?\n"
            "Отправь текстом или голосовым сообщением.\n\n"
            "Или нажми «Пропустить» для стандартного оформления.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Пропустить →", callback_data=f"spec_skip:{user_id}")
            ]]),
        )
        return

    await _run_standard_template(query, context.bot, user_id, template_id, audio_info["path"])


async def _run_standard_template(query, bot, user_id: str, template_id: str, tmp_path: str):
    template = TEMPLATES[template_id]
    loop = asyncio.get_event_loop()
    try:
        await query.edit_message_text(f"✅ Шаблон: {template['name']}\n⏳ Извлекаю аудио…")
        try:
            compressed_path = await loop.run_in_executor(None, extract_audio, tmp_path)
        except Exception:
            compressed_path = tmp_path
        await query.edit_message_text(f"✅ Шаблон: {template['name']}\n⏳ Транскрибирую…")
        transcript = await loop.run_in_executor(None, transcribe_audio, compressed_path)
        if compressed_path != tmp_path:
            try:
                os.unlink(compressed_path)
            except OSError:
                pass

        if not transcript.strip():
            await query.edit_message_text("❌ Deepgram не смог распознать речь. Попробуй снова.")
            return

        await query.edit_message_text(
            f"✅ Транскрипция готова ({len(transcript)} символов)\n⏳ Форматирую…"
        )

        if ANTHROPIC_API_KEY:
            try:
                formatted = await loop.run_in_executor(None, format_with_claude, template_id, transcript)
            except Exception as e:
                logger.exception("Claude failed")
                await bot.send_message(chat_id=query.message.chat_id, text=f"⚠️ Claude ошибка: {e}\n\nСырой транскрипт:")
                formatted = transcript
        else:
            formatted = transcript

        await query.edit_message_text(f"✅ Готово! Шаблон: {template['name']}")
        await deliver_result(query.message.chat_id, bot, user_id, template_id, transcript, formatted)

    except Exception as exc:
        logger.exception("Error processing audio")
        await query.edit_message_text(f"❌ Ошибка: {exc}")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def handle_spec_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Skip spec context — process with standard prompt."""
    query = update.callback_query
    await query.answer()
    user_id = query.data.split(":", 1)[1]

    if user_id not in pending_spec_context:
        await query.edit_message_text("❌ Сессия устарела. Отправь аудио заново.")
        return

    spec_state = pending_spec_context.pop(user_id)
    await query.edit_message_text("⏳ Обрабатываю запись…")
    await process_after_spec_context(
        chat_id=query.message.chat_id,
        status_message=query.message,
        bot=context.bot,
        user_id=user_id,
        spec_state=spec_state,
        custom_context=None,
    )


async def handle_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle action buttons: reformat, docx, email, cancels."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":", 2)
    action = parts[1]
    user_id = parts[2] if len(parts) == 3 else None

    # ── Cancels ──
    if action == "reformat_cancel":
        await query.edit_message_text("Отменено.")
        return

    if not user_id or user_id not in last_result:
        await query.answer("Результат устарел. Отправь аудио заново.", show_alert=True)
        return

    result = last_result[user_id]

    # ── Reformat ──
    if action == "reformat":
        await query.edit_message_text(
            "Выбери новый шаблон:",
            reply_markup=build_template_keyboard(user_id, prefix="retpl"),
        )

    # ── DOCX ──
    elif action == "docx":
        await query.edit_message_text("⏳ Генерирую DOCX…")
        docx_path = None
        try:
            docx_path = create_docx(result["template_name"], result["formatted"])
            with open(docx_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=query.message.chat_id,
                    document=f,
                    filename=f"{result['template_name']}.docx",
                )
            await query.edit_message_text("✅ DOCX готов")
        except Exception as exc:
            logger.exception("DOCX generation failed")
            await query.edit_message_text(f"❌ Ошибка генерации DOCX: {exc}")
        finally:
            if docx_path:
                try:
                    os.unlink(docx_path)
                except OSError:
                    pass



async def handle_reformat_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Re-format stored transcript with a different template."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":", 2)
    if parts[0] != "retpl" or len(parts) != 3:
        return
    _, template_id, user_id = parts

    if user_id not in last_result:
        await query.edit_message_text("❌ Результат устарел. Отправь аудио заново.")
        return

    transcript = last_result[user_id]["transcript"]
    template = TEMPLATES.get(template_id)
    if not template:
        await query.edit_message_text("❌ Неизвестный шаблон.")
        return

    if template_id == "spec":
        pending_spec_context[user_id] = {
            "mode": "reformat",
            "transcript": transcript,
            "chat_id": query.message.chat_id,
        }
        await query.edit_message_text(
            "📐 Тех. задание\n\n"
            "Опиши задачу — что за продукт, для кого, какие цели?\n"
            "Отправь текстом или голосовым, или нажми «Пропустить».",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Пропустить →", callback_data=f"spec_skip:{user_id}")
            ]]),
        )
        return

    await query.edit_message_text(f"⏳ Переформатирую как {template['name']}…")
    loop = asyncio.get_event_loop()
    try:
        if ANTHROPIC_API_KEY:
            formatted = await loop.run_in_executor(None, format_with_claude, template_id, transcript)
        else:
            formatted = transcript
        await query.edit_message_text(f"✅ Переформатировано: {template['name']}")
        await deliver_result(query.message.chat_id, context.bot, user_id, template_id, transcript, formatted)
    except Exception as exc:
        logger.exception("Reformat failed")
        await query.edit_message_text(f"❌ Ошибка: {exc}")


# ─── Entry point ──────────────────────────────────────────────────────────────


def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(
        filters.AUDIO | filters.VOICE | filters.VIDEO | filters.Document.AUDIO | filters.Document.VIDEO,
        handle_audio,
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    app.add_handler(CallbackQueryHandler(handle_template_selection, pattern=r"^tpl:"))
    app.add_handler(CallbackQueryHandler(handle_reformat_selection, pattern=r"^retpl:"))
    app.add_handler(CallbackQueryHandler(handle_spec_skip, pattern=r"^spec_skip:"))
    app.add_handler(CallbackQueryHandler(handle_action, pattern=r"^action:"))

    logger.info("Bot is running…")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=[
            Update.MESSAGE,
            Update.CALLBACK_QUERY,
        ],
    )


if __name__ == "__main__":
    main()
