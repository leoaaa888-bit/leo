"""Telegram 消息推送：检测到手机新消息时，发到用户自己的机器人。

凭据（bot_token / chat_id）放在 telegram_config.json，该文件已 gitignore，
绝不进版本库。本模块只负责读配置、发消息、以及配置时查 chat_id。
全程不打印 token。
"""
import json
import os

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "telegram_config.json")


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def is_configured(config=None):
    c = config or load_config()
    return bool(c.get("enabled") and c.get("bot_token") and c.get("chat_id"))


def _proxies(c):
    # 国内直连 api.telegram.org 通常不通，走本机代理（Clash Verge 默认 7897）。
    # proxy 留空则不走代理（直连或系统代理接管时用）。
    p = (c.get("proxy") or "").strip()
    return {"http": p, "https": p} if p else None


def send_message(text, config=None):
    c = config or load_config()
    token = c.get("bot_token")
    chat_id = c.get("chat_id")
    if not (token and chat_id):
        return {"ok": False, "error": "not-configured"}
    try:
        r = requests.post(
            "https://api.telegram.org/bot{}/sendMessage".format(token),
            data={"chat_id": chat_id, "text": text},
            proxies=_proxies(c),
            timeout=15,
        )
        try:
            body = r.json()
        except Exception:
            body = {}
        if r.status_code == 200 and body.get("ok"):
            return {"ok": True}
        # 不回传 token；只给状态码和 Telegram 的描述
        return {"ok": False, "error": "HTTP {} {}".format(r.status_code, body.get("description", ""))}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def save_config(updates, config=None):
    """把 updates 合并进配置并写回（保留 proxy/poll_interval_s 等其它字段）。"""
    c = config if config is not None else load_config()
    if not isinstance(c, dict):
        c = {}
    c.update(updates)
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(c, f, ensure_ascii=False, indent=2)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def auto_connect(config=None):
    """用已存 token 找 chat_id → 自动选最新的 → 写回并 enable → 发测试消息。
    返回 {ok, chat_id, chat_name, test_sent, error/hint}。"""
    c = config or load_config()
    if not c.get("bot_token"):
        return {"ok": False, "error": "no-token"}
    r = get_chat_ids(c)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error")}
    chats = r["chats"]
    if not chats:
        return {"ok": False, "error": "no-chat",
                "hint": "先在 Telegram 里给机器人发一句话，再点连接"}
    # dict 保持插入序，最后一个是最近的对话
    cid = list(chats.keys())[-1]
    c["chat_id"] = str(cid)
    c["enabled"] = True
    saved = save_config(c, c)
    if not saved.get("ok"):
        return {"ok": False, "error": "写配置失败：" + str(saved.get("error"))}
    test = send_message("✅ web-scrcpy 消息推送已连接，以后手机来消息会推到这里。", c)
    return {"ok": True, "chat_id": str(cid), "chat_name": chats[cid],
            "test_sent": bool(test.get("ok")), "test_error": test.get("error")}


def get_chat_ids(config=None):
    """从 getUpdates 找出最近和机器人对话的 chat_id（配置时用）。"""
    c = config or load_config()
    token = c.get("bot_token")
    if not token:
        return {"ok": False, "error": "no-token"}
    try:
        r = requests.get(
            "https://api.telegram.org/bot{}/getUpdates".format(token),
            proxies=_proxies(c),
            timeout=15,
        )
        data = r.json()
        if not data.get("ok"):
            return {"ok": False, "error": data.get("description", "getUpdates failed")}
        chats = {}
        for u in data.get("result", []):
            msg = u.get("message") or u.get("channel_post") or u.get("edited_message") or {}
            chat = msg.get("chat") or {}
            cid = chat.get("id")
            if cid is None:
                continue
            name = (chat.get("title") or chat.get("username")
                    or (str(chat.get("first_name", "")) + str(chat.get("last_name", ""))).strip()
                    or "?")
            chats[cid] = name
        return {"ok": True, "chats": chats}
    except Exception as e:
        return {"ok": False, "error": str(e)}
