# -*- coding: utf-8 -*-
"""Борис — телеграм-бот на GLM (Z.ai). Хостится на GitHub Actions.

Возможности:
  • текст + фото (glm-4.6v)
  • рабочая память: последние 25 сообщений диалога
  • долгосрочная память: факты (facts.json) выделяются из диалога автоматически
  • белый список: /allow <id> добавляет человека (только хозяин)
Состояние (memory.json, facts.json, trusted.json) коммитится в репозиторий.

Секреты: TELEGRAM_TOKEN, ZAI_API_KEY.
"""
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

# секреты (ключи, токены) не должны утекать в публичный memory.json
SECRET_PATTERNS = [
    re.compile(r"[a-f0-9]{32}\.[A-Za-z0-9]{8,}"),
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}"),
    re.compile(r"\b(?:ghp|gho|github_pat)_[A-Za-z0-9_]{20,}"),
    re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\b[a-f0-9]{40,}\b"),
]


def redact(s):
    for p in SECRET_PATTERNS:
        s = p.sub("[СЕКРЕТ]", s)
    return s

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ZAI_API_KEY = os.environ["ZAI_API_KEY"]

OWNER_ID = 1033935384  # Григорий (gregoryger1)
ALLOWED = {OWNER_ID}
MEMORY_FILE = "memory.json"
FACTS_FILE = "facts.json"
TRUSTED_FILE = "trusted.json"

TG = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
ZAI_URL = "https://api.z.ai/api/paas/v4/chat/completions"
TEXT_MODEL = "glm-5.3"
VISION_MODEL = "glm-4.6v"
HISTORY_LIMIT = 25          # рабочая память диалога
FACTS_EVERY = 8             # раз в сколько новых сообщений обновлять факты
FACTS_MAX_CHARS = 6000      # потолок списка фактов

SYSTEM_PROMPT = (
    "Ты — Борис, дружелюбный и полезный ассистент в Telegram твоего хозяина. "
    "Отвечай на языке пользователя, чаще по-русски. Пиши кратко и по делу, "
    "без лишних извинений и воды."
)

histories = {}  # chat_id -> [{"role":..., "content":...}]
facts = {}      # chat_id -> "строка фактов"
trusted = set()  # доп. доверенные id
since_facts = {}  # chat_id -> счётчик сообщений с последнего обновления фактов


# ---------- сохранение в репозиторий ----------
def _git_save():
    for attempt in range(3):
        try:
            subprocess.run(["git", "add", "-A"], check=True, capture_output=True)
            if subprocess.run(["git", "commit", "-m", "bot state"], capture_output=True).returncode != 0:
                return  # нечего коммитить
            subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=True, capture_output=True)
            subprocess.run(["git", "push", "origin", "HEAD:main"], check=True, capture_output=True)
            return
        except Exception as e:
            print(f"push (попытка {attempt + 1}) не удался:", e, flush=True)
            time.sleep(3)


def _load_json(name, default):
    try:
        with open(name, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(name, obj):
    with open(name, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def load_state():
    global histories, facts, trusted
    histories = {int(k): v for k, v in _load_json(MEMORY_FILE, {}).items()}
    facts = {int(k): v for k, v in _load_json(FACTS_FILE, {}).items()}
    trusted = set(_load_json(TRUSTED_FILE, []))
    print(f"Состояние загружено: чатов={len(histories)}, фактов у {len(facts)} чатов, доверенных={len(trusted)}", flush=True)


def save_state():
    try:
        _save_json(MEMORY_FILE, {str(k): v for k, v in histories.items()})
        _save_json(FACTS_FILE, {str(k): v for k, v in facts.items()})
        _save_json(TRUSTED_FILE, sorted(trusted))
    except Exception as e:
        print("Не смог записать состояние:", e, flush=True)
        return
    _git_save()


def is_allowed(user_id):
    return user_id == OWNER_ID or user_id in trusted


def refresh_external_state():
    """Подтягивает факты/доверенных, залитые с других устройств (git pull)."""
    try:
        m0 = os.path.getmtime(FACTS_FILE) if os.path.exists(FACTS_FILE) else 0
        r = subprocess.run(["git", "pull", "--rebase", "origin", "main"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("git pull:", (r.stderr or r.stdout).strip()[:120], flush=True)
            return
        m1 = os.path.getmtime(FACTS_FILE) if os.path.exists(FACTS_FILE) else 0
        if m1 != m0:
            global facts, trusted
            facts = {int(k): v for k, v in _load_json(FACTS_FILE, {}).items()}
            trusted = set(_load_json(TRUSTED_FILE, []))
            print("Память обновлена из репозитория (синхронизация с устройствами)", flush=True)
    except Exception as e:
        print("refresh не удался:", e, flush=True)


# ---------- телеграм ----------
def tg_call(method, params=None, timeout=60):
    req = urllib.request.Request(
        TG + "/" + method,
        data=json.dumps(params or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def send_message(chat_id, text):
    for i in range(0, len(text), 4096):
        try:
            tg_call("sendMessage", {"chat_id": chat_id, "text": text[i:i + 4096]})
        except Exception as e:
            print("sendMessage error:", e, flush=True)


def download_photo(file_id):
    info = tg_call("getFile", {"file_id": file_id})
    path = info["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{path}"
    with urllib.request.urlopen(url, timeout=60) as r:
        return base64.b64encode(r.read()).decode("ascii")


# ---------- z.ai ----------
def glm_call(model, messages):
    payload = {"model": model, "messages": messages, "temperature": 0.7,
               "max_tokens": 1024, "stream": False}
    req = urllib.request.Request(
        ZAI_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + ZAI_API_KEY},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        resp = json.loads(r.read().decode("utf-8"))
    return resp["choices"][0]["message"]["content"].strip()


def update_facts(chat_id):
    """Раз в FACTS_EVERY сообщений просит модель выделить новые факты диалога."""
    hist = histories.get(chat_id, [])
    if not hist:
        return
    old = facts.get(chat_id, "")
    dialog = "\n".join(f"{m['role']}: {m['content'][:300]}" for m in hist[-FACTS_EVERY - 2:])
    prompt = (
        "Ты ведёшь долговременную память ассистента. Ниже — старые факты и свежий кусок диалога.\n"
        "Обнови список фактов: добавь важное новое (имена, планы, договорённости, предпочтения, факты о людях и проектах), "
        "убери устаревшее. Пиши краткими пунктами, по-русски. Отвечай ТОЛЬКО списком фактов, без вступлений.\n\n"
        f"СТАРЫЕ ФАКТЫ:\n{old or '(пусто)'}\n\nСВЕЖИЙ ДИАЛОГ:\n{dialog}"
    )
    try:
        new_facts = glm_call(TEXT_MODEL, [{"role": "user", "content": prompt}])
        facts[chat_id] = new_facts[:FACTS_MAX_CHARS]
        print(f"факты для чата {chat_id} обновлены ({len(facts[chat_id])} символов)", flush=True)
    except Exception as e:
        print("обновление фактов не удалось:", e, flush=True)


def build_system(chat_id):
    sys_prompt = SYSTEM_PROMPT
    f = facts.get(chat_id)
    if f:
        sys_prompt += "\n\nЧто ты помнишь об этом пользователе (долговременная память):\n" + f
    return {"role": "system", "content": sys_prompt}


# ---------- обработка ----------
def handle_message(msg):
    chat_id = msg["chat"]["id"]
    user_id = msg.get("from", {}).get("id")
    text = (msg.get("text") or msg.get("caption") or "").strip()

    # /id доступен всем — иначе новый человек не смог бы узнать свой id
    if text.startswith("/id"):
        send_message(chat_id, f"Твой id: {user_id}")
        return

    if not is_allowed(user_id):
        send_message(chat_id, "⛔ Это личный бот. Доступ только у доверенных людей.")
        print(f"чужой: user_id={user_id} chat={chat_id}", flush=True)
        return

    if text.startswith("/start"):
        send_message(chat_id, "Привет! Я Борис 🤖 Текст, фото — всё пойму, и помню всё.")
        return
    if text.startswith("/allow"):
        if user_id != OWNER_ID:
            send_message(chat_id, "⛔ Добавлять людей может только хозяин.")
            return
        try:
            new_id = int(text.split()[1])
        except (IndexError, ValueError):
            send_message(chat_id, "Формат: /allow <id>. Пусть человек напишет боту /id и пришлёт тебе его id.")
            return
        if new_id == OWNER_ID:
            send_message(chat_id, "Это ты сам 🙂")
            return
        trusted.add(new_id)
        save_state()
        send_message(chat_id, f"✅ Добавил {new_id} в доверенные.")
        return
    if text.startswith("/disallow"):
        if user_id != OWNER_ID:
            send_message(chat_id, "⛔ Только хозяин.")
            return
        try:
            rm_id = int(text.split()[1])
        except (IndexError, ValueError):
            send_message(chat_id, "Формат: /disallow <id>")
            return
        trusted.discard(rm_id)
        save_state()
        send_message(chat_id, f"Убрал {rm_id} из доверенных.")
        return
    if text.startswith("/list"):
        ids = [OWNER_ID] + sorted(trusted)
        send_message(chat_id, "Доверенные:\n" + "\n".join(str(i) for i in ids))
        return
    if text.startswith("/clear"):
        histories.pop(chat_id, None)
        since_facts.pop(chat_id, None)
        save_state()
        send_message(chat_id, "Рабочая память этого чата очищена (факты остались; убрать их: /facts clear).")
        return
    if text.startswith("/facts"):
        if user_id != OWNER_ID:
            send_message(chat_id, "⛔ Только хозяин.")
            return
        if text.strip() == "/facts clear":
            facts.pop(chat_id, None)
            save_state()
            send_message(chat_id, "Долговременная память этого чата стёрта.")
        else:
            f = facts.get(chat_id)
            send_message(chat_id, f"Долговременная память чата {chat_id}:\n\n{f or '(пока пусто)'}")
        return

    photo = (msg.get("photo") or [None])[-1]
    if not text and not photo:
        return

    hist = histories.setdefault(chat_id, [])
    hist.append({"role": "user", "content": redact(text if text else "Что на картинке?")})

    try:
        tg_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass

    try:
        if photo:
            b64 = download_photo(photo["file_id"])
            vision_msg = {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": text or "Что на этой картинке? Опиши подробно."},
                ],
            }
            answer = glm_call(VISION_MODEL, [vision_msg])
            hist[-1] = {"role": "user",
                        "content": ("[фото + текст] " if text else "[фото] ") + (text or "")}
            answer = answer[:500]
        else:
            answer = glm_call(TEXT_MODEL, [build_system(chat_id)] + hist)
    except Exception as e:
        answer = f"⚠️ Ошибка обращения к Z.ai: {e}"
    hist.append({"role": "assistant", "content": redact(answer)})

    del hist[:-HISTORY_LIMIT]

    # раз в несколько сообщений — обновить долговременную память
    since_facts[chat_id] = since_facts.get(chat_id, 0) + 1
    if since_facts[chat_id] >= FACTS_EVERY:
        since_facts[chat_id] = 0
        update_facts(chat_id)

    save_state()
    send_message(chat_id, answer)
    print(f"answered chat {chat_id}", flush=True)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("Борис запущен. Хозяин:", OWNER_ID, flush=True)

    load_state()
    tg_call("deleteWebhook", {"drop_pending_updates": False})

    # правильное меню команд вместо старого от ZCode
    try:
        tg_call("setMyCommands", {"commands": [
            {"command": "start", "description": "Приветствие"},
            {"command": "id", "description": "Показать свой Telegram id"},
            {"command": "list", "description": "Список доверенных людей"},
            {"command": "allow", "description": "(хозяин) добавить доверенного: /allow <id>"},
            {"command": "disallow", "description": "(хозяин) убрать доверенного: /disallow <id>"},
            {"command": "facts", "description": "Что я о тебе запомнил (долгая память)"},
            {"command": "clear", "description": "Очистить рабочую память чата"},
        ]})
        print("Меню команд установлено.", flush=True)
    except Exception as e:
        print("Не удалось установить меню команд:", e, flush=True)

    offset = None
    last_refresh = time.time()
    while True:
        try:
            if time.time() - last_refresh > 600:  # раз в 10 минут
                last_refresh = time.time()
                refresh_external_state()

            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            req = urllib.request.Request(
                TG + "/getUpdates?" + urllib.parse.urlencode(params)
            )
            with urllib.request.urlopen(req, timeout=35) as r:
                updates = json.loads(r.read().decode("utf-8"))

            for upd in updates.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if msg:
                    handle_message(msg)

        except KeyboardInterrupt:
            print("Остановлено.", flush=True)
            break
        except Exception as e:
            print("Ошибка:", e, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
