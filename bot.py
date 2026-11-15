# -*- coding: utf-8 -*-
"""Борис — телеграм-бот на GLM (Z.ai). Хостится на GitHub Actions.

Возможности: текст + фото (glm-4.6v), постоянная память (memory.json
коммитится обратно в репозиторий), белый список пользователей.

Секреты (из окружения Actions): TELEGRAM_TOKEN, ZAI_API_KEY.
Опционально: ALLOWED_USER_IDS — id через запятую (по умолчанию только хозяин).
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ZAI_API_KEY = os.environ["ZAI_API_KEY"]

OWNER_ID = 1033935384  # Григорий (gregoryger1)
ALLOWED = {OWNER_ID}
if os.environ.get("ALLOWED_USER_IDS"):
    ALLOWED |= {int(x) for x in os.environ["ALLOWED_USER_IDS"].split(",") if x.strip()}

TG = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
ZAI_URL = "https://api.z.ai/api/paas/v4/chat/completions"
TEXT_MODEL = "glm-5.3"
VISION_MODEL = "glm-4.6v"
HISTORY_LIMIT = 12
MEMORY_FILE = "memory.json"

SYSTEM_PROMPT = (
    "Ты — Борис, дружелюбный и полезный ассистент в Telegram твоего хозяина. "
    "Отвечай на языке пользователя, чаще по-русски. Пиши кратко и по делу, "
    "без лишних извинений и воды."
)

histories = {}  # chat_id -> [{"role":..., "content":...}]


# ---------- память ----------
def load_memory():
    global histories
    try:
        with open(MEMORY_FILE, encoding="utf-8") as f:
            histories = {int(k): v for k, v in json.load(f).items()}
        print("Память загружена:", sum(len(v) for v in histories.values()), "сообщений", flush=True)
    except FileNotFoundError:
        print("Памяти ещё нет — начинаем с чистого листа.", flush=True)
    except Exception as e:
        print("Не смог прочитать память:", e, flush=True)


def save_memory():
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in histories.items()}, f, ensure_ascii=False)
    except Exception as e:
        print("Не смог записать память:", e, flush=True)
        return
    # коммитим обратно в репозиторий, чтобы память пережила перезапуск
    for attempt in range(3):
        try:
            subprocess.run(["git", "add", MEMORY_FILE], check=True, capture_output=True)
            if subprocess.run(["git", "commit", "-m", "memory"], capture_output=True).returncode != 0:
                return  # нечего коммитить — ок
            subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=True, capture_output=True)
            subprocess.run(["git", "push", "origin", "HEAD:main"], check=True, capture_output=True)
            return
        except Exception as e:
            print(f"push памяти (попытка {attempt + 1}) не удался:", e, flush=True)
            time.sleep(3)


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


# ---------- обработка ----------
def handle_message(msg):
    chat_id = msg["chat"]["id"]
    user_id = msg.get("from", {}).get("id")

    if user_id not in ALLOWED:
        send_message(chat_id, "⛔ Это личный бот. Доступ только у моего хозяина.")
        print(f"чужой: user_id={user_id} chat={chat_id}", flush=True)
        return

    text = (msg.get("text") or msg.get("caption") or "").strip()

    if text.startswith("/start"):
        send_message(chat_id, "Привет, хозяин! Я Борис 🤖 Текст, фото — всё пойму, и помню всё.")
        return
    if text.startswith("/id"):
        send_message(chat_id, f"Твой id: {user_id}")
        return
    if text.startswith("/clear"):
        histories.pop(chat_id, None)
        save_memory()
        send_message(chat_id, "Память этого чата очищена.")
        return

    photo = (msg.get("photo") or [None])[-1]  # самый большой размер
    if not text and not photo:
        return

    hist = histories.setdefault(chat_id, [])
    hist.append({"role": "user", "content": text if text else "Что на картинке?"})

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
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": text or "Что на этой картинке? Опиши подробно."},
                ],
            }
            answer = glm_call(VISION_MODEL, [vision_msg])
            # в текстовую память кладём сокращённую версию обмена
            hist[-1] = {"role": "user", "content": (f"[фото] " if not text else "[фото + текст] ") + (text or "")}
            hist.append({"role": "assistant", "content": answer[:500]})
        else:
            answer = glm_call(TEXT_MODEL, [{"role": "system", "content": SYSTEM_PROMPT}] + hist)
            hist.append({"role": "assistant", "content": answer})
    except Exception as e:
        answer = f"⚠️ Ошибка обращения к Z.ai: {e}"
        hist.append({"role": "assistant", "content": answer})

    del hist[:-HISTORY_LIMIT]
    save_memory()
    send_message(chat_id, answer)
    print(f"answered chat {chat_id}", flush=True)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("Борис запущен. Хозяева:", ALLOWED, flush=True)

    load_memory()
    tg_call("deleteWebhook", {"drop_pending_updates": False})

    offset = None
    while True:
        try:
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
