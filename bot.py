"""Бот каналу «Читанка на зв'язку».

Запускається з GitHub Actions кожні кілька хвилин, робить усю роботу й завершується:
1. пропонує адміну свіжі новини з сайтів (RSS);
2. приймає новини від людей і передає адміну на перевірку;
3. публікує заплановані пости з папки posts/;
4. вітає нових учасників чату, показує правила, прибирає посилання від не-адмінів.
"""
import hashlib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
STATE_FILE = ROOT / "state.json"
POSTS = ROOT / "posts"
KYIV = ZoneInfo("Europe/Kyiv")

TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
GROUP_ID = os.environ.get("GROUP_ID", "")


# ---------- Telegram API ----------

def api(method, files=None, **params):
    """Викликає метод Bot API. Повертає result або None, якщо Telegram відмовив."""
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    params = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
              for k, v in params.items() if v is not None}
    if files:
        boundary = uuid.uuid4().hex
        body = b""
        for k, v in params.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
        for k, path in files.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; "
                     f"filename=\"{path.name}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
            body += path.read_bytes() + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(url, data=body)
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    else:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode())
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read()).get("result")
    except urllib.error.HTTPError as e:
        print(f"[telegram] {method}: {e.code} {e.read().decode(errors='replace')[:300]}")
    except Exception as e:  # мережа впала: наступний запуск спробує знову
        print(f"[telegram] {method}: {e}")
    return None


def esc(text):
    return html.escape(text or "", quote=False)


def buttons(*pairs):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in pairs]]}


# ---------- Стан ----------

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"offset": 0, "seen": [], "pending": {}, "first_run": True}


def save_state(state):
    state["seen"] = state["seen"][-1500:]
    # тримаємо не більше 200 останніх пропозицій
    for key in list(state["pending"])[:-200]:
        del state["pending"][key]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- 1. Новини з сайтів ----------

def strip_tags(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def read_feed(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ChytankaBot)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        root = ET.fromstring(r.read())
    items = []
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        data = {}
        for ch in it:
            name = ch.tag.split("}")[-1]
            if name == "link":
                data["link"] = (ch.text or ch.get("href") or "").strip() or data.get("link", "")
            elif name in ("title", "description", "summary") and name not in data:
                data[name] = strip_tags(ch.text)
        if data.get("title") and data.get("link"):
            items.append(data)
    return items


def collect_news(state):
    words = [w.lower() for w in CONFIG.get("keywords", [])]
    limit = CONFIG.get("max_proposals_per_run", 5)
    sent = 0
    for feed in CONFIG.get("feeds", []):
        try:
            items = read_feed(feed["url"])
        except Exception as e:
            print(f"[rss] {feed['name']}: {e}")
            continue
        for item in items:
            key = hashlib.sha1(item["link"].encode()).hexdigest()[:10]
            if key in state["seen"]:
                continue
            state["seen"].append(key)
            if state.get("first_run"):
                continue  # перший запуск: запам'ятовуємо старі новини, не засипаємо адміна
            if feed.get("link_contains") and feed["link_contains"] not in item["link"]:
                continue
            text = (item["title"] + " " + item.get("description", "")).lower()
            if feed.get("filter") and words and not any(w in text for w in words):
                continue
            if sent >= limit:
                state["seen"].remove(key)  # запропонуємо наступного разу
                continue
            if CONFIG.get("auto_publish"):
                if publish_news({"title": item["title"], "link": item["link"], "source": feed["name"]}):
                    sent += 1
                else:
                    state["seen"].remove(key)
                continue
            summary = item.get("description") or item.get("summary") or ""
            msg = api("sendMessage", chat_id=ADMIN_ID, parse_mode="HTML",
                      link_preview_options={"url": item["link"], "prefer_large_media": True, "show_above_text": True},
                      text=(f"📰 <b>{esc(item['title'])}</b>\n\n{esc(summary[:400])}\n\n"
                            f"Джерело: {esc(feed['name'])}\n{esc(item['link'])}\n\n"
                            "<i>Кнопка публікує заголовок із посиланням. Щоб опублікувати своїми "
                            "словами, дайте відповідь (reply) на це повідомлення своїм текстом.</i>"),
                      reply_markup=buttons(("✅ Опублікувати", f"r+:{key}"), ("❌ Пропустити", f"r-:{key}")))
            if msg:
                state["pending"][key] = {"title": item["title"], "link": item["link"],
                                         "source": feed["name"], "mid": msg["message_id"]}
                sent += 1
    state["first_run"] = False


def publish_news(p, own_text=None):
    body = esc(own_text) if own_text else f"<b>{esc(p['title'])}</b>"
    return api("sendMessage", chat_id=CHANNEL_ID, parse_mode="HTML",
               link_preview_options={"url": p["link"], "prefer_large_media": True, "show_above_text": True},
               text=f"{body}\n\n<a href=\"{html.escape(p['link'])}\">Джерело: {esc(p['source'])}</a>")


# ---------- 2. Новини від людей і кнопки адміна ----------

def close_buttons(cq, note):
    m = cq.get("message") or {}
    api("editMessageReplyMarkup", chat_id=m.get("chat", {}).get("id"), message_id=m.get("message_id"),
        reply_markup={"inline_keyboard": []})
    api("answerCallbackQuery", callback_query_id=cq["id"], text=note)


def on_callback(cq, state):
    if cq["from"]["id"] != ADMIN_ID:
        api("answerCallbackQuery", callback_query_id=cq["id"], text="Ця кнопка лише для адміністратора")
        return
    kind, _, rest = cq.get("data", "").partition(":")
    if kind in ("r+", "r-"):
        p = state["pending"].pop(rest, None)
        if kind == "r+" and p and not publish_news(p):
            state["pending"][rest] = p
            api("answerCallbackQuery", callback_query_id=cq["id"], text="Не вдалося опублікувати, спробуйте ще раз")
            return
        close_buttons(cq, "Опубліковано ✅" if kind == "r+" and p else "Пропущено")
    elif kind in ("u+", "u-"):
        chat_id, _, mid = rest.partition(":")
        if kind == "u+":
            if not api("copyMessage", chat_id=CHANNEL_ID, from_chat_id=chat_id, message_id=mid):
                api("answerCallbackQuery", callback_query_id=cq["id"], text="Не вдалося опублікувати, спробуйте ще раз")
                return
            api("sendMessage", chat_id=chat_id, text="Вашу новину опубліковано в каналі. Дякуємо! 💛")
        close_buttons(cq, "Опубліковано ✅" if kind == "u+" else "Відхилено")


def on_private(msg, state):
    chat_id, user = msg["chat"]["id"], msg.get("from", {})
    text = msg.get("text", "")
    if text.startswith("/start") or text.startswith("/help"):
        api("sendMessage", chat_id=chat_id, text=CONFIG["start_text"])
        return
    reply = msg.get("reply_to_message")
    if user.get("id") == ADMIN_ID and reply:
        for key, p in list(state["pending"].items()):
            if p.get("mid") == reply["message_id"]:
                if text and publish_news(p, own_text=text):
                    del state["pending"][key]
                    api("editMessageReplyMarkup", chat_id=chat_id, message_id=p["mid"],
                        reply_markup={"inline_keyboard": []})
                    api("sendMessage", chat_id=chat_id, text="Опубліковано з вашим текстом ✅")
                else:
                    api("sendMessage", chat_id=chat_id, text="Не вдалося опублікувати. Відповідь має бути текстом.")
                return
    name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or "без імені"
    uname = f" (@{user['username']})" if user.get("username") else ""
    api("sendMessage", chat_id=ADMIN_ID, text=f"📩 Новина від {name}{uname}:")
    ok = api("copyMessage", chat_id=ADMIN_ID, from_chat_id=chat_id, message_id=msg["message_id"],
             reply_markup=buttons(("✅ У канал", f"u+:{chat_id}:{msg['message_id']}"),
                                  ("❌ Відхилити", f"u-:{chat_id}:{msg['message_id']}")))
    if ok and user.get("id") != ADMIN_ID:
        api("sendMessage", chat_id=chat_id, text=CONFIG["thanks_text"])


# ---------- 4. Чат ----------

def on_group(msg, admins):
    chat_id = msg["chat"]["id"]
    for member in msg.get("new_chat_members", []):
        if not member.get("is_bot"):
            api("sendMessage", chat_id=chat_id,
                text=CONFIG["welcome_text"].replace("{name}", member.get("first_name", "друже")))
    if msg.get("new_chat_members") or msg.get("left_chat_member"):
        api("deleteMessage", chat_id=chat_id, message_id=msg["message_id"])  # прибираємо «приєднався/вийшов»
        return
    text = msg.get("text") or msg.get("caption") or ""
    if text.split("@")[0].strip().lower() in ("/rules", "/pravyla"):
        api("sendMessage", chat_id=chat_id, text=CONFIG["rules_text"])
        return
    sender = msg.get("from", {})
    if msg.get("is_automatic_forward") or msg.get("sender_chat") or sender.get("id") in admins:
        return
    ents = (msg.get("entities") or []) + (msg.get("caption_entities") or [])
    has_link = any(e["type"] in ("url", "text_link") for e in ents) or re.search(r"t\.me/|https?://", text)
    if has_link and CONFIG.get("delete_links", True):
        if api("deleteMessage", chat_id=chat_id, message_id=msg["message_id"]):
            api("sendMessage", chat_id=chat_id,
                text=f"{sender.get('first_name', 'Друже')}, посилання в чаті можуть публікувати лише "
                     "адміністратори. Оголошення пишіть без посилань 🙏")


def handle_updates(state):
    updates = api("getUpdates", offset=state["offset"], timeout=0,
                  allowed_updates=["message", "callback_query"]) or []
    admins = set()
    if GROUP_ID and updates:
        admins = {a["user"]["id"] for a in (api("getChatAdministrators", chat_id=GROUP_ID) or [])}
    for up in updates:
        state["offset"] = up["update_id"] + 1
        try:
            if "callback_query" in up:
                on_callback(up["callback_query"], state)
            elif "message" in up:
                msg = up["message"]
                if msg["chat"]["type"] == "private":
                    on_private(msg, state)
                elif GROUP_ID and GROUP_ID in (str(msg["chat"]["id"]), "@" + msg["chat"].get("username", "")):
                    on_group(msg, admins)
        except Exception as e:  # одне зламане повідомлення не має зупиняти решту
            print(f"[update {up['update_id']}] {e}")


# ---------- 3. Пости за розкладом ----------

def publish_scheduled():
    now = datetime.now(KYIV)
    done = POSTS / "published"
    done.mkdir(parents=True, exist_ok=True)
    for path in sorted(POSTS.glob("*.txt")):
        try:
            when = datetime.strptime(path.stem, "%Y-%m-%d_%H-%M").replace(tzinfo=KYIV)
        except ValueError:
            print(f"[posts] {path.name}: назва має бути РРРР-ММ-ДД_ГГ-ХХ.txt")
            continue
        if when > now:
            continue
        text = path.read_text(encoding="utf-8").strip()
        image = next((path.with_suffix(e) for e in (".jpg", ".jpeg", ".png") if path.with_suffix(e).exists()), None)
        if image:
            ok = api("sendPhoto", chat_id=CHANNEL_ID, caption=text[:1024], files={"photo": image})
        else:
            ok = api("sendMessage", chat_id=CHANNEL_ID, text=text)
        if ok:
            path.rename(done / path.name)
            if image:
                image.rename(done / image.name)
            print(f"[posts] опубліковано {path.name}")


def main():
    if not (TOKEN and ADMIN_ID and CHANNEL_ID):
        sys.exit("Не задано секрети BOT_TOKEN, ADMIN_ID або CHANNEL_ID")
    state = load_state()
    handle_updates(state)
    publish_scheduled()
    collect_news(state)
    save_state(state)


if __name__ == "__main__":
    main()
