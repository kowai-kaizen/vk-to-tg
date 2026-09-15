"""
Пересылка сообщений из групповой беседы ВКонтакте в чат/канал Telegram.

Как это работает:
  Скрипт раз в POLL_INTERVAL секунд запрашивает историю сообщений беседы
  (метод messages.getHistory), сравнивает с последним уже отправленным
  сообщением и пересылает новые в Telegram (текст, фотографии и документы),
  подписывая, от кого сообщение. Если задан TG_TOPIC_ID, все сообщения
  отправляются в указанный топик (тему) супергруппы Telegram.

Установка зависимостей:
    pip install requests --break-system-packages

Заполните переменные ниже перед запуском.
"""

import time
import json
import os
import requests
from dotenv import load_dotenv
load_dotenv()
# ---------- НАСТРОЙКИ ----------


VK_PEER_ID = 2000000046                   # peer_id беседы (2000000000 + chat_id)
VK_API_VERSION = "5.199"
VK_TOKEN = os.getenv('VK_TOKEN')

TG_BOT_TOKEN = os.getenv('TG_BOT_TOKEN')
TG_CHAT_ID = os.getenv('TG_CHAT_ID')
TG_TOPIC_ID = 14                         # id топика (темы) в группе; None, если топики не используются

STATE_FILE = "last_message_id.json"       # тут храним id последнего отправленного сообщения


# --------------------------------
_name_cache = {}


def load_last_id():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f).get("last_id")
    return None


def save_last_id(msg_id):
    with open(STATE_FILE, "w") as f:
        json.dump({"last_id": msg_id}, f)


def vk_call(method, **params):
    params.update({"access_token": VK_TOKEN, "v": VK_API_VERSION})
    resp = requests.get(f"https://api.vk.com/method/{method}", params=params, timeout=15).json()
    if "error" in resp:
        raise RuntimeError(f"Ошибка VK API ({method}): {resp['error']}")
    return resp["response"]


def get_messages(count=20):
    resp = vk_call("messages.getHistory", peer_id=VK_PEER_ID, count=count)
    return resp["items"]


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
    """Возвращает список (url, имя_файла) для вложений-документов."""
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
    """Общие параметры для каждого запроса к Telegram: чат + топик (если задан)."""
    data = {"chat_id": TG_CHAT_ID}
    if TG_TOPIC_ID is not None:
        data["message_thread_id"] = TG_TOPIC_ID
    return data


def _download(url, timeout=20):
    """Скачивает файл по ссылке ВК и возвращает его содержимое (bytes)."""
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def _post_with_retry(url, data, files, attempts=3, timeout=180):
    """Отправляет запрос в Telegram с повторными попытками при сетевых сбоях."""
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
    # подпись пишем один раз — к первому вложению или к тексту, если вложений нет
    caption_used = False

    if photo_urls:
        if len(photo_urls) == 1:
            print("Скачиваю фото...", flush=True)
            data = _base_data()
            data.update({"caption": caption})
            files = {"photo": ("photo.jpg", _download(photo_urls[0]))}
            print("Отправляю фото в Telegram...", flush=True)
            resp = _post_with_retry(f"{base}/sendPhoto", data, files)
            _check_tg_response(resp)
        else:
            media = []
            files = {}
            for i, url in enumerate(photo_urls[:10]):
                print(f"Скачиваю фото {i + 1}/{len(photo_urls[:10])}...", flush=True)
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
            if not media:
                print("Ни одно фото не удалось скачать, пропускаю сообщение.", flush=True)
            else:
                data = _base_data()
                data.update({"media": json.dumps(media)})
                print("Отправляю альбом в Telegram...", flush=True)
                resp = _post_with_retry(f"{base}/sendMediaGroup", data, files)
                _check_tg_response(resp)
        caption_used = True

    for url, filename in documents:
        print(f"Скачиваю документ: {filename}...", flush=True)
        data = _base_data()
        if not caption_used:
            data["caption"] = caption
            caption_used = True
        try:
            files = {"document": (filename, _download(url, timeout=60))}
        except Exception as e:
            print(f"Не удалось скачать документ {filename}: {e}", flush=True)
            continue
        print(f"Отправляю документ {filename} в Telegram...", flush=True)
        resp = _post_with_retry(f"{base}/sendDocument", data, files)
        _check_tg_response(resp)

    if not caption_used:
        data = _base_data()
        data.update({"text": caption})
        resp = requests.post(f"{base}/sendMessage", data=data, timeout=15)
        _check_tg_response(resp)


def main():
    print("Проверка новых сообщений VK-беседы...", flush=True)
    last_id = load_last_id()
 
    messages = get_messages(count=20)
    messages.sort(key=lambda m: m["id"])
    print(f"Получено сообщений от VK API: {len(messages)}", flush=True)
 
    if last_id is None:
        if messages:
            last_id = messages[-1]["id"]
            save_last_id(last_id)
            print(f"Инициализация: последнее сообщение #{last_id}, ждём новые...", flush=True)
        return
 
    new_messages = [m for m in messages if m["id"] > last_id]
    for msg in new_messages:
        sender = get_sender_name(msg["from_id"])
        text = msg.get("text", "")
        photos = extract_photo_urls(msg)
        documents = extract_documents(msg)
        print(f"Пересылаю сообщение #{msg['id']} от {sender}", flush=True)
        try:
            send_to_telegram(sender, text, photos, documents)
        except Exception as e:
            print(f"Не удалось переслать сообщение #{msg['id']}: {e}. Пропускаю его.", flush=True)
        last_id = msg["id"]
        save_last_id(last_id)
         
    if not new_messages:
        print("Новых сообщений нет.", flush=True)

if __name__ == "__main__":
    main()
