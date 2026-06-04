"""
todoist_integration.py — интеграция VoiceToText-бота с Todoist.

Закрывает звено №4 пайплайна: после того как Claude разобрал транскрипт
и выделил задачи, они пушатся в Todoist с дедлайнами и напоминаниями.

Встраивается в существующий main.py. Требует только httpx (уже есть в проекте
как зависимость Deepgram/Telegram) и переменную окружения TODOIST_API_TOKEN.

Поток:
    format_with_claude() -> текст с блоком ===TODOIST_JSON===
            │
            ▼
    extract_action_items() -> (чистый_markdown, [задачи])
            │  чистый_markdown идёт в чат / DOCX / MD как раньше
            ▼
    кнопка "✅ В Todoist" -> todoist_callback() -> push_to_todoist()
"""

import os
import re
import json
import uuid
import logging

import httpx
from telegram import InlineKeyboardButton
from telegram.ext import ContextTypes

log = logging.getLogger(__name__)

# Токен читается лениво в push_to_todoist() — load_dotenv() должен успеть отработать
TODOIST_API = "https://api.todoist.com/api/v1"

# Проект в Todoist, куда складывать задачи. None -> Inbox (Входящие).
TODOIST_PROJECT_NAME = os.getenv("TODOIST_PROJECT_NAME", "VoiceToText")

# Шаблоны, для которых имеет смысл вытаскивать задачи в Todoist.
TODOIST_ENABLED_TEMPLATES = {"meeting", "client_call", "client_meeting", "spec"}

# Временное хранилище распарсенных задач между сообщением и нажатием кнопки.
# Ключ -> {"tasks": [...], "source": "имя_файла"}
PENDING_TASKS: dict[str, dict] = {}


# ─────────────────────────────────────────────────────────────────────────────
# 1. Добавка к промпту Claude
# ─────────────────────────────────────────────────────────────────────────────
TODOIST_PROMPT_SUFFIX = """

---
ВАЖНО. В САМОМ КОНЦЕ ответа, после всего Markdown, добавь блок с задачами
строго в таком формате (и больше ничего после него):

===TODOIST_JSON===
[
  {"text": "формулировка задачи в повелительном наклонении",
   "owner": "ответственный или null, если это задача автора",
   "due": "YYYY-MM-DD или YYYY-MM-DDTHH:MM:SS, либо null если срок не назван",
   "priority": 1}
]
===END_TODOIST_JSON===

Правила:
- Включай ТОЛЬКО реальные договорённости и поручения с конкретным действием.
- Не выдумывай сроки: если дата не прозвучала — ставь "due": null.
- priority: 4 = срочно/критично, 3 = важно, 2 = обычно, 1 = низкое.
- Если задач нет — верни пустой массив: []
- JSON должен быть валидным (двойные кавычки, без комментариев и запятых в конце).
"""


# ─────────────────────────────────────────────────────────────────────────────
# 2. Парсинг ответа Claude
# ─────────────────────────────────────────────────────────────────────────────
_BLOCK_RE = re.compile(
    r"===TODOIST_JSON===\s*(.*?)\s*===END_TODOIST_JSON===",
    re.DOTALL,
)


def extract_action_items(claude_text: str) -> tuple[str, list[dict]]:
    """
    Вырезает блок ===TODOIST_JSON=== из ответа Claude.

    Возвращает (чистый_текст_без_блока, список_задач).
    Чистый текст используется для чата / DOCX / Markdown как раньше.
    Если блока нет или JSON битый — задачи = [].
    """
    match = _BLOCK_RE.search(claude_text)
    if not match:
        return claude_text.strip(), []

    clean_text = _BLOCK_RE.sub("", claude_text).strip()

    raw = match.group(1).strip()
    try:
        tasks = json.loads(raw)
        if not isinstance(tasks, list):
            tasks = []
    except json.JSONDecodeError as e:
        log.warning("Не удалось распарсить TODOIST_JSON: %s", e)
        tasks = []

    # Нормализуем и отсеиваем мусор.
    normalized = []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        text = (t.get("text") or "").strip()
        if not text:
            continue
        normalized.append({
            "text": text,
            "owner": (t.get("owner") or "").strip() or None,
            "due": (t.get("due") or "").strip() or None,
            "priority": int(t.get("priority") or 1),
        })

    return clean_text, normalized


# ─────────────────────────────────────────────────────────────────────────────
# 3. Работа с Todoist API
# ─────────────────────────────────────────────────────────────────────────────
_PROJECT_CACHE: dict[str, str] = {}  # имя -> project_id


async def _get_or_create_project(client: httpx.AsyncClient, name: str) -> str | None:
    """Находит project_id по имени, при отсутствии создаёт проект. Кэширует."""
    if not name:
        return None
    if name in _PROJECT_CACHE:
        return _PROJECT_CACHE[name]

    r = await client.get(f"{TODOIST_API}/projects")
    r.raise_for_status()
    for p in r.json():
        if p.get("name", "").lower() == name.lower():
            _PROJECT_CACHE[name] = p["id"]
            return p["id"]

    # Не нашли — создаём.
    r = await client.post(f"{TODOIST_API}/projects", json={"name": name})
    r.raise_for_status()
    pid = r.json()["id"]
    _PROJECT_CACHE[name] = pid
    return pid


def _todoist_priority(p: int) -> int:
    """Наш 1..4 -> приоритет Todoist API (4 = p1/высший). Защита от выхода за границы."""
    return max(1, min(4, int(p or 1)))


async def push_to_todoist(tasks: list[dict], source: str = "") -> dict:
    """
    Создаёт задачи в Todoist. Возвращает {"created": N, "failed": M, "errors": [...]}.
    """
    TODOIST_API_TOKEN = os.getenv("TODOIST_API_TOKEN", "")
    if not TODOIST_API_TOKEN:
        return {"created": 0, "failed": len(tasks),
                "errors": ["TODOIST_API_TOKEN не задан в .env"]}

    headers = {"Authorization": f"Bearer {TODOIST_API_TOKEN}"}
    created, failed, errors = 0, 0, []

    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
        try:
            project_id = await _get_or_create_project(client, TODOIST_PROJECT_NAME)
        except Exception as e:
            log.warning("Не удалось получить/создать проект: %s", e)
            project_id = None

        for t in tasks:
            content = t["text"]
            if t.get("owner"):
                content = f"{content} (@{t['owner']})"

            payload = {
                "content": content,
                "priority": _todoist_priority(t.get("priority", 1)),
            }
            if project_id:
                payload["project_id"] = project_id
            if source:
                payload["description"] = f"Источник: {source}"

            due = t.get("due")
            if due:
                if "T" in due:
                    payload["due_datetime"] = due
                else:
                    payload["due_date"] = due

            try:
                r = await client.post(
                    f"{TODOIST_API}/tasks",
                    json=payload,
                    headers={"X-Request-Id": uuid.uuid4().hex},
                )
                r.raise_for_status()
                created += 1
            except Exception as e:
                failed += 1
                errors.append(f"{content[:40]}…: {e}")
                log.warning("Todoist: задача не создана: %s", e)

    return {"created": created, "failed": failed, "errors": errors}


# ─────────────────────────────────────────────────────────────────────────────
# 4. Кнопка и обработчик callback
# ─────────────────────────────────────────────────────────────────────────────
def build_todoist_button(tasks: list[dict], source: str = "") -> InlineKeyboardButton | None:
    """
    Возвращает кнопку «✅ В Todoist (N)» или None, если задач нет.
    """
    if not tasks:
        return None
    token = uuid.uuid4().hex[:16]
    PENDING_TASKS[token] = {"tasks": tasks, "source": source}
    return InlineKeyboardButton(
        f"✅ В Todoist ({len(tasks)})",
        callback_data=f"todoist:{token}",
    )


async def todoist_callback(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик нажатия кнопки."""
    query = update.callback_query
    token = query.data.split(":", 1)[1]
    data = PENDING_TASKS.pop(token, None)

    if not data:
        await query.answer("Задачи устарели — обработай файл заново.", show_alert=True)
        return

    await query.answer("Отправляю в Todoist…")
    result = await push_to_todoist(data["tasks"], data["source"])

    if result["created"] and not result["failed"]:
        text = f"✅ В Todoist добавлено задач: {result['created']}"
    elif result["created"]:
        text = (f"⚠️ Добавлено {result['created']}, "
                f"не удалось {result['failed']}.\n" + "\n".join(result["errors"][:3]))
    else:
        text = "❌ Не удалось добавить задачи.\n" + "\n".join(result["errors"][:3])

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.message.reply_text(text)
