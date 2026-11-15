# -*- coding: utf-8 -*-
"""Борис — телеграм-бот на GLM (Z.ai). Хостится на GitHub Actions.

Ключи берёт из переменных окружения (в Actions они приходят из Secrets):
  TELEGRAM_TOKEN — токен от @BotFather
  ZAI_API_KEY    — ключ API со страницы z.ai/manage-apikey/apikey-list
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ZAI_API_KEY = os.environ["ZAI_API_KEY"]

TG = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
ZAI_URL = "https://api.z.ai/api/paas/v4/chat/completions"
MODEL = "glm-5.3"
HISTORY_LIMIT = 12

SYSTEM_PROMPT = (
    "Ты — Борис, дружелюбный и полезный ассистент в Telegram. "
    "Отвечай на языке пользователя, чаще по-русски. Пиши кратко и по делу, "
    "без лишних извинений и воды."
)

histories = {}  # chat_id -> [{"role": ..., "content": ...}]


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


def glm_reply(messages):
    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 1024,
        "stream": False,
    }
    req = urllib.request.Request(
        ZAI_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + ZAI_API_KEY,
        },
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        resp = json.loads(r.read().decode("utf-8"))
    return resp["choices"][0]["message"]["content"].strip()


def handle_message(msg):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    if not text:
        return

    if text.startswith("/start"):
        histories.pop(chat_id, None)
        send_message(chat_id, "Привет! Я Борис 🤖 Спроси что угодно — отвечу.")
        return

    if text.startswith("/clear"):
        histories.pop(chat_id, None)
        send_message(chat_id, "Память очищена. Начнём заново!")
        return

    hist = histories.setdefault(chat_id, [])
    hist.append({"role": "user", "content": text})
    del hist[:-HISTORY_LIMIT]

    try:
        tg_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass

    try:
        answer = glm_reply([{"role": "system", "content": SYSTEM_PROMPT}] + hist)
    except Exception as e:
        answer = f"⚠️ Ошибка обращения к Z.ai: {e}"
    hist.append({"role": "assistant", "content": answer})
    del hist[:-HISTORY_LIMIT]

    send_message(chat_id, answer)
    print(f"answered chat {chat_id}", flush=True)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("Борис запущен.", flush=True)

    # чтобы параллельный инстанс (перезапуск воркфлофа) не конфликтовал
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
