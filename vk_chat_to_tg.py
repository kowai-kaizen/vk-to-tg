"""
Пересылка НОВЫХ сообщений из групповой беседы ВКонтакте в чат/канал Telegram.

Версия для запуска по расписанию (cron / GitHub Actions): скрипт делает ОДНУ
короткую проверку через Bot Long Poll API и завершается — сам цикл ожидания
обеспечивает cron в workflow, а не скрипт.

ВАЖНО: messages.getHistory для ключа сообщества в беседах всегда возвращает
"Access denied" — это ограничение VK, а не баг. Поэтому вместо получения
истории мы подписываемся на события через groups.getLongPollServer и
опрашиваем сервер LongPoll: он отдаёт только то, что произошло после
сохранённого курсора `ts`.

Состояние (курсор ts) сохраняется в файл last_ts.json, который в GitHub
Actions нужно коммитить обратно в репозиторий между запусками (см.
соответствующий шаг в workflow).

Требования на стороне VK:
  1. Сообщество должно быть добавлено участником в нужную беседу, с доступом
     ко всей переписке.
  2. В настройках сообщества: Сообщения -> "Сообщения сообщества" включены.
  3. Там же: "Настройки для бота" -> "Возможности ботов" включены, способ
     обработки событий — Long Poll API.
  4. Ключ доступа создан с правом "Сообщения сообщества" (messages).

Установка зависимостей:
    pip install requests python-dotenv --break-system-packages
"""

import json
import os
import time

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # в GitHub Actions переменные окружения приходят из workflow, .env не нужен

# ---------- НАСТРОЙКИ ----------

VK_PEER_ID = 2000000044                 # peer_id беседы (2000000000 + chat_id)
VK_GROUP_ID = os.getenv("VK_GROUP_ID", "0")  # числовой ID сообщества (положительный)
VK_API_VERSION = "5.199"
VK_TOKEN = os.getenv("VK_TOKEN")

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")
TG_TOPIC_ID = 4607                          # id топика (темы) в группе; None, если топики не используются

STATE_FILE = "last_ts.json"               # тут храним курсор ts LongPoll

# --------------------------------

_name_cache = {}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def vk_call(method, **params):
    params.update({"access_token": VK_TOKEN, "v": VK_API_VERSION})
    resp = requests.get(f"https://api.vk.com/method/{method}", params=params, timeout=15).json()
    if "error" in resp:
        raise RuntimeError(f"Ошибка VK API ({method}): {resp['error']}")
    return resp["response"]


def get_longpoll_server():
    """Возвращает свежие server/key/ts для сообщества."""
    return vk_call("groups.getLongPollServer", group_id=VK_GROUP_ID)


def poll_updates(server, key, ts, wait=5):
    """Один короткий опрос LongPoll-сервера. mode=2 включает attachments."""
    params = {"act": "a_check", "key": key, "ts": ts, "wait": wait, "mode": 2, "version": 3}
    resp = requests.get(server, params=params, timeout=wait + 10).json()
    return resp


def get_sender_name(from_id):
    if from_id in _name_cache:
        return _name_cache[from_id]
    time.sleep(1)
    try:
        if from_id > 0:
            resp = vk_call("users.get", user_ids=from_id)
            name = f"{resp[0]['first_name']} {resp[0]['last_name']}"
        else:
            resp = vk_call("groups.getById", group_id=-from_id)
            name = resp[0]["name"] if isinstance(resp, list) else resp["groups"][0]["name"]
    except Exception:
        name = f"id{from_id}"
    _name_cache[from_id] = name
    return name


def extract_photo_urls(msg):
    urls = []
    for att in msg.get("attachments", []):
        if att.get("type") == "photo":
            sizes = att["photo"].get("sizes", [])
            if sizes:
                best = max(sizes, key=lambda s: s.get("width", 0))
                urls.append(best["url"])
    return urls


def extract_documents(msg):
    docs = []
    for att in msg.get("attachments", []):
        if att.get("type") == "doc":
            doc = att["doc"]
            title = doc.get("title", "file")
            ext = doc.get("ext", "")
            filename = title if title.endswith(f".{ext}") else f"{title}.{ext}"
            docs.append((doc["url"], filename))
    return docs


def _check_tg_response(resp):
    if not resp.ok:
        print(f"ОШИБКА ОТ TELEGRAM (HTTP {resp.status_code}): {resp.text}", flush=True)
    else:
        data = resp.json()
        if not data.get("ok"):
            print(f"ОШИБКА ОТ TELEGRAM: {data}", flush=True)


def _base_data():
    data = {"chat_id": TG_CHAT_ID}
    if TG_TOPIC_ID is not None:
        data["message_thread_id"] = TG_TOPIC_ID
    return data


def _download(url, timeout=20):
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def _post_with_retry(url, data, files, attempts=3, timeout=180):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return requests.post(url, data=data, files=files, timeout=timeout)
        except Exception as e:
            last_error = e
            print(f"Сетевая ошибка при отправке (попытка {attempt}/{attempts}): {e}", flush=True)
            time.sleep(3)
    raise last_error


def send_to_telegram(sender, text, photo_urls, documents):
    base = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
    caption = f"{sender}:\n{text}" if text.strip() else f"{sender}:"
    caption_used = False

    if photo_urls:
        if len(photo_urls) == 1:
            data = _base_data()
            data.update({"caption": caption})
            files = {"photo": ("photo.jpg", _download(photo_urls[0]))}
            resp = _post_with_retry(f"{base}/sendPhoto", data, files)
            _check_tg_response(resp)
        else:
            media = []
            files = {}
            for i, url in enumerate(photo_urls[:10]):
                key = f"photo{i}"
                try:
                    files[key] = (f"{key}.jpg", _download(url))
                except Exception as e:
                    print(f"Не удалось скачать фото {i + 1}: {e}", flush=True)
                    continue
                item = {"type": "photo", "media": f"attach://{key}"}
                if i == 0:
                    item["caption"] = caption
                media.append(item)
            if media:
                data = _base_data()
                data.update({"media": json.dumps(media)})
                resp = _post_with_retry(f"{base}/sendMediaGroup", data, files)
                _check_tg_response(resp)
        caption_used = True

    for url, filename in documents:
        data = _base_data()
        if not caption_used:
            data["caption"] = caption
            caption_used = True
        try:
            files = {"document": (filename, _download(url, timeout=60))}
        except Exception as e:
            print(f"Не удалось скачать документ {filename}: {e}", flush=True)
            continue
        resp = _post_with_retry(f"{base}/sendDocument", data, files)
        _check_tg_response(resp)

    if not caption_used:
        data = _base_data()
        data.update({"text": caption})
        resp = requests.post(f"{base}/sendMessage", data=data, timeout=15)
        _check_tg_response(resp)


def process_message(msg):
    sender = get_sender_name(msg["from_id"])
    text = msg.get("text", "")
    photos = extract_photo_urls(msg)
    documents = extract_documents(msg)
    print(f"Пересылаю сообщение #{msg['id']} от {sender}", flush=True)
    try:
        send_to_telegram(sender, text, photos, documents)
    except Exception as e:
        print(f"Не удалось переслать сообщение #{msg['id']}: {e}. Пропускаю его.", flush=True)


def main():
    if not VK_GROUP_ID:
        raise RuntimeError("Не задан VK_GROUP_ID (числовой ID сообщества, положительный)")

    print("Запрашиваю сервер LongPoll...", flush=True)
    server_info = get_longpoll_server()
    server, key = server_info["server"], server_info["key"]

    state = load_state()
    ts = state.get("ts") or server_info["ts"]

    print("Проверка новых событий VK...", flush=True)
    resp = poll_updates(server, key, ts, wait=5)

    if "failed" in resp:
        code = resp["failed"]
        if code == 1:
            # ts устарел, но сервер прислал новый — сохраняем и выходим,
            # события за этот промежуток уже потеряны (короткий разрыв)
            save_state({"ts": resp["ts"]})
            print("ts был немного устаревшим, синхронизировался заново.", flush=True)
            return
        else:
            # code 2 (история устарела) или 3 (ключ истёк) — берём всё заново
            print(f"LongPoll вернул failed={code}, запрашиваю новый сервер/ключ.", flush=True)
            server_info = get_longpoll_server()
            save_state({"ts": server_info["ts"]})
            return

    new_ts = resp["ts"]
    updates = resp.get("updates", [])
    print(f"Получено событий: {len(updates)}", flush=True)

    for update in updates:
        print(f"Событие: type={update.get('type')}", flush=True)
        if update.get("type") != "message_new":
            continue
        msg = update["object"]["message"]
        print(f"  peer_id={msg.get('peer_id')} (ожидаем {VK_PEER_ID}), out={msg.get('out')}", flush=True)
        if msg.get("peer_id") != VK_PEER_ID:
            continue  # событие из другого диалога/беседы, не наше
        if msg.get("out"):
            continue  # исходящее сообщение (отправлено самим сообществом) — пропускаем
        process_message(msg)

    save_state({"ts": new_ts})


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Ошибка: {e}", flush=True)
        # завершаемся без ошибки — временный сбой (например, flood control)
        # не должен считаться падением workflow, просто попробуем в следующий раз
