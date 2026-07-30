"""配置 Telegram 推送：自动找出 chat_id、写回配置、发一条测试消息。

用法：
  1. 在 telegram_config.json 里填好 bot_token（找 @BotFather 创建机器人后拿到）
  2. 打开 Telegram，给你的机器人随便发一句话（比如 hi）——这样它才知道你的 chat_id
  3. 运行：  python tg_setup.py
     脚本会列出 chat_id、自动写回配置(enabled=true)、并发一条测试消息
  4. 重启服务，之后收到 QQ/微信/Soul 新消息就会推送到你的机器人
"""
import json
import sys

import telegram_notify as tg


def main():
    cfg = tg.load_config()
    if not cfg.get("bot_token"):
        print("× 请先在 telegram_config.json 里填 bot_token（@BotFather 给的那串）")
        return
    print("正在向 Telegram 拉取最近对话（getUpdates）…")
    r = tg.get_chat_ids(cfg)
    if not r.get("ok"):
        print("× 失败：", r.get("error"))
        print("  排查：bot_token 是否正确？proxy 能连 Telegram 吗？是否已给机器人发过消息？")
        return
    chats = list(r["chats"].items())
    if not chats:
        print("× 没找到任何对话。请先在 Telegram 里给机器人发一句话，再重跑本脚本。")
        return
    print("找到以下对话：")
    for i, (cid, name) in enumerate(chats):
        print("  [{}] chat_id={}  {}".format(i, cid, name))
    if len(chats) == 1:
        chosen = chats[0][0]
        print("只有一个，自动选用 chat_id={}".format(chosen))
    else:
        sel = input("选哪个？输序号后回车：").strip()
        try:
            chosen = chats[int(sel)][0]
        except (ValueError, IndexError):
            print("× 序号无效"); return
    cfg["chat_id"] = str(chosen)
    cfg["enabled"] = True
    with open(tg.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print("√ 已写回 telegram_config.json（chat_id 已填，enabled=true）")
    print("发送测试消息…")
    res = tg.send_message("✅ web-scrcpy 消息推送配置成功，以后手机来消息会推到这里。", cfg)
    if res.get("ok"):
        print("√ 测试消息已发出，去 Telegram 看看收到没。")
        print("  最后一步：重启两个服务（或让它们下次重启），后台推送就生效了。")
    else:
        print("× 测试消息失败：", res.get("error"))


if __name__ == "__main__":
    main()
