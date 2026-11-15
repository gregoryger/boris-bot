# -*- coding: utf-8 -*-
"""Синхронизация памяти Бориса с локальными чатами ZCode.

Запуск на любом компьютере, где есть чаты ZCode и git с доступом к репо:
    python sync_memory.py          # добавить только новое
    python sync_memory.py --full   # переобработать все чаты целиком

Планировщик Windows может звать этот скрипт раз в день — см. README.

Ключ Z.ai берётся из env ZAI_API_KEY или файла ~/.boris/zai_key.
"""
import glob
import json
import os
import re
import subprocess
import sys
import urllib.request

OWNER_KEY = "1033935384"
URL = "https://api.z.ai/api/paas/v4/chat/completions"
STATE_FILE = "sync-state.json"
FACTS_FILE = "facts.json"
FULL = "--full" in sys.argv
MARK = "--mark" in sys.argv

# всё, что похоже на ключ/токен, вырезаем ДО отправки куда-либо
SECRET_PATTERNS = [
    re.compile(r"[a-f0-9]{32}\.[A-Za-z0-9]{8,}"),        # ключ Z.ai
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}"),         # токен бота Telegram
    re.compile(r"\b(?:ghp|gho|github_pat)_[A-Za-z0-9_]{20,}"),  # GitHub
    re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_-]{20,}"),        # OpenAI-подобные
    re.compile(r"\b[a-f0-9]{40,}\b"),                     # длинный hex
    re.compile(r"db2375b458e4415cb2e9156d41da44b4"),      # известный префикс
]


def redact(s):
    for p in SECRET_PATTERNS:
        s = p.sub("[СЕКРЕТ]", s)
    return s


def find_key():
    k = os.environ.get("ZAI_API_KEY")
    if k:
        return k.strip()
    p = os.path.join(os.path.expanduser("~"), ".boris", "zai_key")
    if os.path.exists(p):
        return open(p, encoding="utf-8").read().strip()
    raise SystemExit("Нет ключа: задай env ZAI_API_KEY или создай файл ~/.boris/zai_key")


KEY = find_key()


def glm(prompt, max_tokens=2000):
    req = urllib.request.Request(
        URL,
        data=json.dumps({"model": "glm-5.3", "messages": [{"role": "user", "content": prompt}],
                         "temperature": 0.2, "max_tokens": max_tokens, "stream": False}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + KEY},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"].strip()


def git(*args):
    return subprocess.run(["git"] + list(args), capture_output=True, text=True)


def main():
    sessions_dir = os.path.join(os.path.expanduser("~"), ".zcode", "v2", "sessions")
    if not os.path.isdir(sessions_dir):
        raise SystemExit(f"Не нашёл чаты ZCode: {sessions_dir}")

    state = {}
    if os.path.exists(STATE_FILE) and not FULL:
        state = json.load(open(STATE_FILE, encoding="utf-8"))
    if FULL:
        print("Режим --full: переобрабатываю все чаты")

    blocks, processed = [], {}
    for f in sorted(glob.glob(os.path.join(sessions_dir, "*", "*.json"))):
        rel = os.path.relpath(f, sessions_dir)
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        msgs = d.get("messages", [])
        prev = 0 if FULL else state.get("files", {}).get(rel, 0)
        processed[rel] = len(msgs)
        new = msgs[prev:]
        if not new:
            continue
        title = d.get("meta", {}).get("title", "?")[:60]
        block = f"\n=== ЧАТ: {title} ===\n" + "\n".join(
            f"{m.get('role','?')}: {redact(str(m.get('content',''))[:500])}" for m in new
            if str(m.get("content", "")).strip())
        if len(block) > 80:
            blocks.append(block)

    if MARK:
        # только отметить обработанное, ничего не анализировать
        json.dump({"files": processed}, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False)
        git("add", STATE_FILE)
        git("commit", "-m", "sync: отметка обработанных чатов")
        git("pull", "--rebase", "origin", "main")
        git("push", "origin", "HEAD:main")
        print("Базовая отметка записана и залита.")
        return

    print(f"Новых сообщений нет." if not blocks else f"Чатов с новым: {len(blocks)}")
    if not blocks:
        # всё равно отметим позицию, если state был пуст
        if state.get("files") != processed:
            json.dump({"files": processed}, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False)
            git("add", STATE_FILE)
            git("commit", "-m", "sync baseline")
            if git("pull", "--rebase", "origin", "main").returncode == 0:
                git("push", "origin", "HEAD:main")
        return

    # выделяем факты из нового
    new_facts = []
    for i, b in enumerate(blocks):
        print(f"анализ {i + 1}/{len(blocks)}...")
        cur = b if len(b) <= 25000 else b[:25000]
        try:
            new_facts.append(glm(
                "Ниже свежие сообщения пользователя из его чатов с ассистентом. Выдели долговременные факты "
                "(проекты, учёба, договорённости, предпочтения, окружение). Кратко, по-русски, только список.\n\n" + cur))
        except Exception as e:
            print("  ошибка:", e)

    if not new_facts:
        raise SystemExit("Факты извлечь не удалось")

    old = json.load(open(FACTS_FILE, encoding="utf-8")).get(OWNER_KEY, "")
    merged = glm(
        "Объедини два списка фактов об одном пользователе в один: убери дубли и устаревшее, "
        "сгруппируй по темам, от важного к менее важному. По-русски, только итоговый список.\n\n"
        f"СТАРЫЙ СПИСОК:\n{old or '(пусто)'}\n\nНОВОЕ:\n" + "\n".join(new_facts))

    facts = json.load(open(FACTS_FILE, encoding="utf-8"))
    facts[OWNER_KEY] = merged[:6000]
    json.dump(facts, open(FACTS_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump({"files": processed}, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False)

    for attempt in range(3):
        git("add", FACTS_FILE, STATE_FILE)
        if git("commit", "-m", "sync: новые факты с устройства").returncode != 0:
            break
        git("pull", "--rebase", "origin", "main")
        if git("push", "origin", "HEAD:main").returncode == 0:
            print("Залито в репозиторий. Борис заберёт в течение ~10 минут.")
            break
        import time; time.sleep(5)
    print("Готово.")


if __name__ == "__main__":
    main()
