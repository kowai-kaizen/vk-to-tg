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
    


VK_API_VERSION = "5.199"
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")
STATE_FILE = os.getenv("STATE_FILE", "last_ts.json")               # тут храним курсор ts LongPoll




COMMUNITIES = [
    {
        "name": "Группа 1",
        "group_id": os.getenv("VK_GROUP_ID"),
        "token": os.getenv("VK_TOKEN"),
        "peer_id": 2000000002,
        "tg_chat_id": os.getenv("TG_CHAT_ID"),
        "tg_topic_id": 4607,
    },
    {
        "name": "Группа 2",
        "group_id": os.getenv("VK_GROUP_ID"),
        "token": os.getenv("VK_TOKEN"),
        "peer_id": 2000000003,          # поменяйте на peer_id нужной беседы
        "tg_chat_id": os.getenv("TG_CHAT_ID_2"),
        "tg_topic_id": 21825,
    },
]
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


def vk_call(token, method, **params):
    params.update({"access_token": token, "v": VK_API_VERSION})
    resp = requests.get(f"https://api.vk.com/method/{method}", params=params, timeout=15).json()
    if "error" in resp:
        raise RuntimeError(f"Ошибка VK API ({method}): {resp['error']}")
    return resp["response"]


def poll_updates(server, key, ts, wait=5):
    params = {"act": "a_check", "key": key, "ts": ts, "wait": wait, "mode": 2, "version": 3}
    return requests.get(server, params=params, timeout=wait + 25).json()


def get_longpoll_server(cfg):
    return vk_call(cfg["token"], "groups.getLongPollServer", group_id=cfg["group_id"])


def get_sender_name(token, from_id):
    cache_key = (token, from_id)
    if cache_key in _name_cache:
        return _name_cache[cache_key]
    time.sleep(1)
    try:
        if from_id > 0:
            resp = vk_call(token, "users.get", user_ids=from_id)
            name = f"{resp[0]['first_name']} {resp[0]['last_name']}"
        else:
            resp = vk_call(token, "groups.getById", group_id=-from_id)
            name = resp[0]["name"] if isinstance(resp, list) else resp["groups"][0]["name"]
    except Exception:
        name = f"id{from_id}"
    _name_cache[cache_key] = name
    return name


def _best_photo_url(photo):
    sizes = photo.get("sizes", [])
    if sizes:
        return max(sizes, key=lambda s: s.get("width", 0))["url"]
    return None


def extract_wall_posts(msg):
    posts = []
    for att in msg.get("attachments", []):
        if att.get("type") == "wall":
            wall = att["wall"]
            link = f"https://vk.com/wall{wall.get('owner_id')}_{wall.get('id')}"
            photos = []
            for sub_att in wall.get("attachments", []):
                if sub_att.get("type") == "photo":
                    url = _best_photo_url(sub_att["photo"])
                    if url:
                        photos.append(url)
            posts.append({"text": wall.get("text", ""), "link": link, "photos": photos})
    return posts


def extract_voice_messages(msg):
    voices = []
    for att in msg.get("attachments", []):
        if att.get("type") == "audio_message":
            url = att["audio_message"].get("link_ogg")
            if url:
                voices.append(url)
    return voices


def extract_photo_urls(msg):
    urls = []
    for att in msg.get("attachments", []):
        if att.get("type") == "photo":
            url = _best_photo_url(att["photo"])
            if url:
                urls.append(url)
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


def _base_data(cfg):
    data = {"chat_id": cfg["tg_chat_id"]}
    if cfg.get("tg_topic_id") is not None:
        data["message_thread_id"] = cfg["tg_topic_id"]
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


def send_voices(cfg, caption, voice_urls, caption_used):
    base = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
    for url in voice_urls:
        data = _base_data(cfg)
        if not caption_used:
            data["caption"] = caption
            caption_used = True
        try:
            files = {"voice": ("voice.ogg", _download(url))}
        except Exception as e:
            print(f"Не удалось скачать голосовое: {e}", flush=True)
            continue
        resp = _post_with_retry(f"{base}/sendVoice", data, files)
        _check_tg_response(resp)
    return caption_used


def send_to_telegram(cfg, sender, text, photo_urls, documents, voice_urls):
    base = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
    caption = f"{sender}:\n{text}" if text.strip() else f"{sender}:"
    caption_used = False

    if photo_urls:
        if len(photo_urls) == 1:
            data = _base_data(cfg)
            data["caption"] = caption
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
                if not media:
                    item["caption"] = caption
                media.append(item)
            if media:
                data = _base_data(cfg)
                data["media"] = json.dumps(media)
                resp = _post_with_retry(f"{base}/sendMediaGroup", data, files)
                _check_tg_response(resp)
        caption_used = True

    caption_used = send_voices(cfg, caption, voice_urls, caption_used)

    for url, filename in documents:
        data = _base_data(cfg)
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
        data = _base_data(cfg)
        data["text"] = caption
        resp = requests.post(f"{base}/sendMessage", data=data, timeout=15)
        _check_tg_response(resp)


def collect_fwd_messages(msg):
    result = []
    if msg.get("reply_message"):
        result.append(msg["reply_message"])
        result.extend(collect_fwd_messages(msg["reply_message"]))
    for fwd in msg.get("fwd_messages", []):
        result.append(fwd)
        result.extend(collect_fwd_messages(fwd))
    return result


def process_message(cfg, msg):
    token = cfg["token"]
    sender = get_sender_name(token, msg["from_id"])
    text = msg.get("text", "")
    photos = extract_photo_urls(msg)
    documents = extract_documents(msg)
    voices = extract_voice_messages(msg)
    fwd_list = collect_fwd_messages(msg)

    wall_lines = []
    for m in [msg] + fwd_list:
        for post in extract_wall_posts(m):
            line = f"[пост со стены] {post['link']}"
            if post["text"]:
                line += f"\n{post['text']}"
            wall_lines.append(line)
            photos.extend(post["photos"])

    if fwd_list:
        fwd_lines = []
        for fwd in fwd_list:
            fwd_sender = get_sender_name(token, fwd["from_id"])
            fwd_text = fwd.get("text", "")
            fwd_lines.append(f"[переслано от {fwd_sender}]: {fwd_text}" if fwd_text else f"[переслано от {fwd_sender}]")
            photos.extend(extract_photo_urls(fwd))
            documents.extend(extract_documents(fwd))
            voices.extend(extract_voice_messages(fwd))
        text = (text + "\n\n" + "\n".join(fwd_lines)).strip() if text else "\n".join(fwd_lines)

    if wall_lines:
        text = (text + "\n\n" + "\n\n".join(wall_lines)).strip() if text else "\n\n".join(wall_lines)

    print(f"[{cfg['name']}] Пересылаю сообщение #{msg['id']} от {sender}", flush=True)
    try:
        send_to_telegram(cfg, sender, text, photos, documents, voices)
    except Exception as e:
        print(f"[{cfg['name']}] Не удалось переслать сообщение #{msg['id']}: {e}. Пропускаю.", flush=True)

def run_community(cfgs, state):
    """Один опрос сообщества, события раскладываются по беседам из cfgs."""
    first = cfgs[0]
    gid = str(first["group_id"])
    routes = {c["peer_id"]: c for c in cfgs}

    print(f"[{gid}] Запрашиваю сервер LongPoll...", flush=True)
    server_info = get_longpoll_server(first)
    server, key = server_info["server"], server_info["key"]

    ts = state.get(gid) or server_info["ts"]

    print(f"[{gid}] Проверка новых событий VK...", flush=True)
    resp = poll_updates(server, key, ts, wait=5)

    if "failed" in resp:
        code = resp["failed"]
        if code == 1:
            state[gid] = resp["ts"]
            print(f"[{gid}] ts был немного устаревшим, синхронизировался заново.", flush=True)
            return
        print(f"[{gid}] LongPoll вернул failed={code}, беру новый сервер/ключ.", flush=True)
        state[gid] = get_longpoll_server(first)["ts"]
        return

    updates = resp.get("updates", [])
    print(f"[{gid}] Получено событий: {len(updates)}", flush=True)

    for update in updates:
        if update.get("type") != "message_new":
            continue
        msg = update["object"]["message"]
        print(f"  peer_id={msg.get('peer_id')}, out={msg.get('out')}", flush=True)
        if msg.get("out"):
            continue
        cfg = routes.get(msg.get("peer_id"))
        if cfg is None:
            continue  # беседа не из списка
        process_message(cfg, msg)

    state[gid] = resp["ts"]


def main():
    state = load_state()

    # группируем записи по сообществу, чтобы опрашивать каждое сообщество один раз
    groups = {}
    for cfg in COMMUNITIES:
        if not (cfg.get("group_id") and cfg.get("token") and cfg.get("tg_chat_id")):
            print(f"[{cfg['name']}] Не заданы group_id / token / tg_chat_id, пропускаю.", flush=True)
            continue
        groups.setdefault(str(cfg["group_id"]), []).append(cfg)

    for gid, cfgs in groups.items():
        try:
            run_community(cfgs, state)
        except Exception as e:
            print(f"[{gid}] Ошибка: {e}", flush=True)
        save_state(state)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Ошибка: {e}", flush=True)
