"""
FastAPI web-scrcpy server.

Media WS  /ws/media   — video/audio binary (framed)
Control WS /ws/control — low-latency input, immediate forward
"""

from __future__ import annotations

import argparse
import asyncio
import struct
import time
import uuid
from pathlib import Path
from threading import Lock
from typing import Optional

import uvicorn
from fastapi import Body, FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from tls_util import resolve_ssl_paths
from scrcpy import (
    Scrcpy,
    trigger_device_screenshot,
    get_notification_state,
    wake_for_unlock,
    send_key,
    open_app_drawer,
    set_device_ime_visible,
    reboot_device,
    check_adb,
    list_adb_devices,
    preclean_connection,
    resolve_device_serial,
    get_device_model,
    set_adb_path,
    set_device_serial,
    set_local_port,
    DEFAULT_ADB_PATH,
    DEFAULT_LOCAL_PORT,
)
import telegram_notify

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

video_bit_rate = "2500000"
max_size = "800"
max_fps = "25"
local_port = DEFAULT_LOCAL_PORT
auto_lock_on_leave = True
turn_screen_off_on_connect = True

QUALITY_PRESETS = {
    "clear": {"video_bit_rate": "3200000", "max_size": "1080", "max_fps": "30"},
    "balanced": {"video_bit_rate": "2000000", "max_size": "800", "max_fps": "30"},
    "smooth": {"video_bit_rate": "1200000", "max_size": "720", "max_fps": "30"},
}

MSG_VIDEO = 1
MSG_AUDIO = 2

# 被新设备顶替时用这个专门的关闭码，告诉旧网页“是被其他设备接管，别自动重连”。
WS_CLOSE_SUPERSEDED = 4009

app = FastAPI(title="web-scrcpy")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

session_lock = Lock()
active_session: Optional["ClientSession"] = None
_pending_shutdowns: list[asyncio.Task] = []


class ClientSession:
    VIDEO_QUEUE_MAX = 6
    AUDIO_QUEUE_MAX = 150

    def __init__(
        self,
        token: str,
        device_serial: Optional[str] = None,
        adb_local_port: Optional[int] = None,
        lock_on_leave: Optional[bool] = None,
    ):
        self.token = token
        self.device_serial = device_serial
        self.local_port = adb_local_port if adb_local_port is not None else local_port
        self.auto_lock_on_leave = lock_on_leave if lock_on_leave is not None else auto_lock_on_leave
        self.scrcpy = Scrcpy(
            device_serial=device_serial,
            local_port=self.local_port,
            turn_screen_off_on_connect=turn_screen_off_on_connect,
        )
        self.media_ws: Optional[WebSocket] = None
        self.control_ws: Optional[WebSocket] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._media_queue: asyncio.Queue = asyncio.Queue(
            maxsize=self.VIDEO_QUEUE_MAX + self.AUDIO_QUEUE_MAX
        )
        self._video_depth = 0
        self._audio_depth = 0
        self.video_packets_sent = 0
        self.audio_packets_sent = 0
        self.closed = False
        self._pump_task: Optional[asyncio.Task] = None
        self._shutdown_task: Optional[asyncio.Task] = None

    def bind_loop(self):
        self._loop = asyncio.get_running_loop()

    def clear_queues(self):
        while not self._media_queue.empty():
            try:
                self._media_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._video_depth = 0
        self._audio_depth = 0

    def _evict_oldest_video(self, make_room: int = 1) -> None:
        """Drop oldest queued video packets so the newest frame can be sent."""
        if make_room <= 0:
            return

        pending: list[tuple[int, bytes]] = []
        while not self._media_queue.empty():
            try:
                pending.append(self._media_queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        to_drop = max(0, self._video_depth - (self.VIDEO_QUEUE_MAX - make_room))
        dropped = 0
        kept: list[tuple[int, bytes]] = []
        new_video = 0
        new_audio = 0
        for item in pending:
            if item[0] == MSG_VIDEO:
                if dropped < to_drop:
                    dropped += 1
                    continue
                new_video += 1
            else:
                new_audio += 1
            kept.append(item)

        self._video_depth = new_video
        self._audio_depth = new_audio
        for item in kept:
            try:
                self._media_queue.put_nowait(item)
            except asyncio.QueueFull:
                if item[0] == MSG_VIDEO:
                    self._video_depth -= 1
                else:
                    self._audio_depth -= 1

    def _pack_media_frame(self, msg_type: int, chunk: bytes) -> bytes:
        if msg_type == MSG_VIDEO:
            sent_ms = int(time.time() * 1000)
            chunk = struct.pack(">Q", sent_ms) + chunk
        return struct.pack(">BI", msg_type, len(chunk)) + chunk

    def _enqueue_impl(self, msg_type: int, data: bytes, protect: bool) -> bool:
        """Enqueue media on the event-loop thread only."""
        if self.closed:
            return False

        is_video = msg_type == MSG_VIDEO
        depth = self._video_depth if is_video else self._audio_depth
        max_d = self.VIDEO_QUEUE_MAX if is_video else self.AUDIO_QUEUE_MAX

        if is_video and depth >= max_d:
            self._evict_oldest_video(make_room=1)
        elif not is_video and depth >= max_d and not protect:
            return False

        item = (msg_type, data)
        try:
            self._media_queue.put_nowait(item)
        except asyncio.QueueFull:
            if is_video:
                self._evict_oldest_video(make_room=1)
                try:
                    self._media_queue.put_nowait(item)
                except asyncio.QueueFull:
                    return False
            else:
                # Prefer dropping old video packets so audio stays continuous.
                self._evict_oldest_video(make_room=2)
                try:
                    self._media_queue.put_nowait(item)
                except asyncio.QueueFull:
                    if protect:
                        try:
                            evicted = self._media_queue.get_nowait()
                            if evicted[0] == MSG_VIDEO:
                                self._video_depth -= 1
                            else:
                                self._audio_depth -= 1
                        except asyncio.QueueEmpty:
                            pass
                        try:
                            self._media_queue.put_nowait(item)
                        except asyncio.QueueFull:
                            return False
                    else:
                        return False

        if is_video:
            self._video_depth += 1
        else:
            self._audio_depth += 1
        return True

    def _dispatch_on_loop(self, fn, data: bytes):
        loop = self._loop
        if loop is None or self.closed:
            return
        try:
            if asyncio.get_running_loop() is loop:
                fn(data)
                return
        except RuntimeError:
            pass
        loop.call_soon_threadsafe(fn, data)

    def _on_video_loop(self, data: bytes):
        if self.closed:
            return
        protect = self.video_packets_sent < 6
        if self._enqueue_impl(MSG_VIDEO, data, protect):
            self.video_packets_sent += 1

    def _on_audio_loop(self, data: bytes):
        if self.closed:
            return
        protect = self.audio_packets_sent < 8
        if self._enqueue_impl(MSG_AUDIO, data, protect):
            self.audio_packets_sent += 1

    def on_video(self, data: bytes):
        self._dispatch_on_loop(self._on_video_loop, data)

    def on_audio(self, data: bytes):
        self._dispatch_on_loop(self._on_audio_loop, data)

    def _log_pump_task_result(self, task: asyncio.Task) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"pump_media task error: {e}")

    async def pump_media(self):
        """Await items from the async queue and send framed binary to the media websocket."""
        try:
            while not self.closed and self.media_ws is not None:
                msg_type, chunk = await self._media_queue.get()
                if self.closed or self.media_ws is None:
                    break
                if msg_type == MSG_VIDEO:
                    self._video_depth -= 1
                else:
                    self._audio_depth -= 1
                await self.media_ws.send_bytes(self._pack_media_frame(msg_type, chunk))
                while not self._media_queue.empty() and not self.closed and self.media_ws is not None:
                    try:
                        msg_type, chunk = self._media_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if msg_type == MSG_VIDEO:
                        self._video_depth -= 1
                    else:
                        self._audio_depth -= 1
                    await self.media_ws.send_bytes(self._pack_media_frame(msg_type, chunk))
        except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
            pass
        except Exception as e:
            print(f"pump_media error: {e}")
        finally:
            if not self.closed:
                asyncio.create_task(self.shutdown())

    async def shutdown(self, superseded: bool = False):
        global active_session
        if self.closed:
            return
        self.closed = True

        with session_lock:
            if active_session is self:
                active_session = None

        lock_task = None
        if self.auto_lock_on_leave:
            lock_task = asyncio.ensure_future(
                asyncio.to_thread(self.scrcpy.send_lock_screen)
            )
        else:
            print("auto-lock on leave: disabled")

        pump = self._pump_task
        self._pump_task = None
        if pump and not pump.done() and pump is not asyncio.current_task():
            pump.cancel()
            try:
                await pump
            except (asyncio.CancelledError, Exception):
                pass

        self.clear_queues()

        media = self.media_ws
        control = self.control_ws
        self.media_ws = None
        self.control_ws = None
        close_code = WS_CLOSE_SUPERSEDED if superseded else 1000
        for ws in (media, control):
            if ws is None:
                continue
            try:
                await ws.close(code=close_code)
            except Exception:
                pass

        try:
            await asyncio.to_thread(self.scrcpy.scrcpy_stop)
        except Exception as e:
            print(f"scrcpy_stop error: {e}")

        try:
            if lock_task is not None:
                await asyncio.wait_for(lock_task, timeout=1.0)
        except (asyncio.TimeoutError, Exception):
            pass

        print("session closed")


def _prune_shutdown_tasks():
    global _pending_shutdowns
    _pending_shutdowns = [t for t in _pending_shutdowns if not t.done()]


async def await_sessions_shutdown(sessions: list[ClientSession], timeout: float = 8.0):
    """Wait for old sessions to finish teardown; always preclean adb afterwards."""
    global _pending_shutdowns
    tasks = []
    for session in sessions:
        if session is None:
            continue
        # 这条路径专门是“新连接顶替旧会话”，用 superseded 关闭码通知旧网页礼貌让出。
        task = asyncio.create_task(session.shutdown(superseded=True))
        tasks.append(task)
        _pending_shutdowns.append(task)
    _prune_shutdown_tasks()

    device_serial = None
    adb_local_port = None
    for session in sessions:
        if session is None:
            continue
        if session.device_serial:
            device_serial = session.device_serial
        if session.local_port:
            adb_local_port = session.local_port

    for task in tasks:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            print("session shutdown still running, continuing preclean…")
        except Exception as e:
            print(f"session shutdown error: {e}")

    await asyncio.to_thread(preclean_connection, device_serial, adb_local_port)
    await asyncio.sleep(0.15)


async def prepare_fresh_connection(
    device_serial: Optional[str],
    old_sessions: list[ClientSession],
    adb_local_port: Optional[int] = None,
):
    """Tear down duplicate/stale sessions and scrub adb forward + device server."""
    await await_sessions_shutdown(old_sessions, timeout=8.0)
    await asyncio.to_thread(preclean_connection, device_serial, adb_local_port)


def _positive_int_str(value, fallback):
    try:
        number = int(str(value).strip())
        if number > 0:
            return str(number)
    except (TypeError, ValueError, AttributeError):
        pass
    return str(fallback)


def resolve_stream_settings(
    quality: Optional[str],
    bit_rate: Optional[str],
    size: Optional[str],
    fps: Optional[str],
):
    preset_name = str(quality or "").strip().lower()
    preset = QUALITY_PRESETS.get(preset_name)
    if preset:
        bit_rate = bit_rate or preset["video_bit_rate"]
        size = size or preset["max_size"]
        fps = fps or preset["max_fps"]
    else:
        preset_name = "custom"
    return {
        "quality": preset_name,
        "video_bit_rate": _positive_int_str(bit_rate, video_bit_rate),
        "max_size": _positive_int_str(size, max_size),
        "max_fps": _positive_int_str(fps, max_fps),
    }


@app.get("/")
def index():
    # 明确禁用缓存：否则 iOS Safari 会长时间保留旧 index.html，页面改动传不到手机。
    return FileResponse(
        TEMPLATES_DIR / "index.html",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/api/whoami")
def api_whoami():
    """本实例绑定的设备（serial + model）。页面据此给 <html> 打标，做设备专属版面——
    例如 OPPO(PLS120) 的 iPhone 填满/安全区只在 OPPO 页面生效，谷歌2 页面不受影响。"""
    serial = resolve_device_serial(None)
    model = get_device_model(serial)
    return JSONResponse({"serial": serial, "model": model})


@app.post("/api/drawer")
async def api_drawer(device_serial: Optional[str] = Body(None, embed=True)):
    """从底部上滑拉出抽屉式桌面的应用列表（没有对应按键码，只能注入滑动）。"""
    serial = device_serial
    if serial is None:
        with session_lock:
            if active_session is not None:
                serial = active_session.device_serial
    state = await asyncio.to_thread(open_app_drawer, serial)
    return JSONResponse(state)


@app.get("/api/devices")
def api_devices():
    devices, raw = list_adb_devices()
    return JSONResponse({"devices": devices, "raw": raw})


@app.get("/api/screenshot")
def api_screenshot(device_serial: Optional[str] = Query(None)):
    """触发手机自带的系统截屏，图片保存在谷歌2手机自己的相册里。"""
    serial = device_serial
    if serial is None:
        with session_lock:
            if active_session is not None:
                serial = active_session.device_serial
    ok = trigger_device_screenshot(serial)
    if ok:
        return JSONResponse({"ok": True})
    return JSONResponse({"error": "screenshot failed"}, status_code=500)


@app.post("/api/secure-key")
async def api_secure_key(
    key: str = Body(..., embed=True),
    device_serial: Optional[str] = Body(None, embed=True),
):
    """Secure Input：往一块因 FLAG_SECURE 黑屏的数字密码界面发一个键
    （0-9 / del / enter）。锁屏、Secret Album、微信/支付宝应用锁、银行 App
    等所有黑屏数字键盘场景通用，走同一个接口。key 走请求体，不进 URL 日志。"""
    serial = device_serial
    if serial is None:
        with session_lock:
            if active_session is not None:
                serial = active_session.device_serial
    state = await asyncio.to_thread(send_key, key, serial)
    return JSONResponse(state)


@app.post("/api/click-debug")
async def api_click_debug(request: Request):
    """排查"部分按钮点不动"专用：网页每次按下/触摸起点把坐标换算的完整过程发
    到这里，直接打印进服务端日志——操作手机的人和看日志的人不是同一边，浏览器
    控制台里的输出传不过来，只能落在这。诊断用完应该整段删掉，不是长期接口。"""
    try:
        d = await request.json()
    except Exception:
        return JSONResponse({"ok": False})
    print(
        "click-debug: client=({},{}) rect={} offset=({},{}) display=({},{}) "
        "device=({},{}) local=({},{}) verdict={} tap=({},{})".format(
            d.get("clientX"), d.get("clientY"), d.get("rect"),
            d.get("offsetX"), d.get("offsetY"), d.get("displayW"), d.get("displayH"),
            d.get("deviceW"), d.get("deviceH"), d.get("localX"), d.get("localY"),
            d.get("verdict"), d.get("deviceX"), d.get("deviceY"),
        )
    )
    return JSONResponse({"ok": True})


@app.post("/api/reboot")
async def api_reboot(device_serial: Optional[str] = Query(None)):
    """远程重启手机（诊断面板里的按钮，需二次确认）。"""
    serial = device_serial
    if serial is None:
        with session_lock:
            if active_session is not None:
                serial = active_session.device_serial
    state = await asyncio.to_thread(reboot_device, serial)
    return JSONResponse(state)


@app.get("/api/ime")
async def api_ime(on: int = Query(...), device_serial: Optional[str] = Query(None)):
    """切换手机自己的屏幕输入法是否弹出（长按网页上的键盘按钮触发）。"""
    serial = device_serial
    if serial is None:
        with session_lock:
            if active_session is not None:
                serial = active_session.device_serial
    state = await asyncio.to_thread(set_device_ime_visible, bool(on), serial)
    return JSONResponse(state)


@app.get("/api/wake")
async def api_wake(device_serial: Optional[str] = Query(None)):
    """进入投屏时唤醒手机；若锁屏则直接调出 PIN 输入界面。"""
    serial = device_serial
    if serial is None:
        with session_lock:
            if active_session is not None:
                serial = active_session.device_serial
    state = await asyncio.to_thread(wake_for_unlock, serial)
    return JSONResponse(state)


@app.get("/api/notifications")
async def api_notifications(device_serial: Optional[str] = Query(None)):
    """任意 App 有"真实消息通知"（图标会显示角标的那种，不限于微信/QQ/Soul）
    且不在前台时返回 alert=true，供网页悬浮球变红。packages=None 让
    get_notification_state 不按包名过滤——见该函数的文档字符串。"""
    serial = device_serial
    if serial is None:
        with session_lock:
            if active_session is not None:
                serial = active_session.device_serial
    state = await asyncio.to_thread(get_notification_state, serial, None)
    return JSONResponse(state)


# 浏览器会自动请求这两个根路径，而根路径没法加版本号/改名，一旦被缓存就长期
# 钉住旧图（Safari 的 favicon 库尤其顽固）。所以这里显式禁缓存。
_ICON_NO_CACHE = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


@app.get("/favicon.ico")
def favicon():
    ico = STATIC_DIR / "icons" / "globe.ico"
    if ico.is_file():
        return FileResponse(ico, headers=_ICON_NO_CACHE)
    return FileResponse(STATIC_DIR / "icons" / "globe.svg", headers=_ICON_NO_CACHE)


@app.get("/apple-touch-icon.png")
def apple_touch_icon():
    png = STATIC_DIR / "icons" / "globe-touch.png"
    if png.is_file():
        return FileResponse(png, headers=_ICON_NO_CACHE)
    return FileResponse(STATIC_DIR / "icons" / "globe-full.svg", headers=_ICON_NO_CACHE)


@app.websocket("/ws/media")
async def ws_media(
    websocket: WebSocket,
    audio_enabled: bool = Query(False),
    quality: Optional[str] = Query(None),
    video_bit_rate_q: Optional[str] = Query(None, alias="video_bit_rate"),
    max_size_q: Optional[str] = Query(None, alias="max_size"),
    max_fps_q: Optional[str] = Query(None, alias="max_fps"),
    device_serial: Optional[str] = Query(None),
):
    global active_session
    await websocket.accept()

    old_sessions: list[ClientSession] = []
    with session_lock:
        if active_session is not None:
            old_sessions.append(active_session)
            active_session = None
        _prune_shutdown_tasks()
        token = uuid.uuid4().hex
        resolved_serial = resolve_device_serial(device_serial)
        session = ClientSession(
            token,
            device_serial=resolved_serial,
            adb_local_port=local_port,
            lock_on_leave=auto_lock_on_leave,
        )
        active_session = session

    if old_sessions:
        ids = ", ".join(s.token[:8] + "…" for s in old_sessions)
        print(f"replacing {len(old_sessions)} previous session(s): {ids}")
        await prepare_fresh_connection(session.device_serial, old_sessions, session.local_port)

    session.media_ws = websocket
    session.bind_loop()
    settings = resolve_stream_settings(quality, video_bit_rate_q, max_size_q, max_fps_q)
    session.clear_queues()
    session.video_packets_sent = 0
    session.audio_packets_sent = 0

    started = False
    for attempt in range(3):
        if attempt > 0:
            await asyncio.to_thread(preclean_connection, session.device_serial, session.local_port)
            await asyncio.sleep(0.35 * attempt)
        started = await asyncio.to_thread(
            session.scrcpy.scrcpy_start,
            session.on_video,
            settings["video_bit_rate"],
            audio_enabled,
            session.on_audio if audio_enabled else None,
            settings["max_size"],
            settings["max_fps"],
        )
        if started:
            break
        print(f"scrcpy_start attempt {attempt + 1} failed, retrying…")

    if not started:
        try:
            await websocket.send_json({"type": "error", "message": "failed to start scrcpy"})
        except Exception:
            pass
        await session.shutdown()
        return

    session._pump_task = asyncio.create_task(session.pump_media())
    session._pump_task.add_done_callback(session._log_pump_task_result)

    try:
        await websocket.send_json(
            {
                "type": "session",
                "token": token,
                "audio_enabled": audio_enabled,
                "quality": settings["quality"],
                "video_bit_rate": settings["video_bit_rate"],
                "max_size": settings["max_size"],
                "max_fps": settings["max_fps"],
                "device_serial": session.device_serial,
                "local_port": session.local_port,
                "auto_lock_on_leave": session.auto_lock_on_leave,
                "turn_screen_off_on_connect": turn_screen_off_on_connect,
                "backend": "fastapi-ws",
            }
        )
    except Exception as e:
        print(f"failed to send session hello: {e}")
        await session.shutdown()
        return

    print(
        f"media connected token={token[:8]}… device={session.device_serial or 'default'} "
        f"local_port={session.local_port} "
        f"quality={settings['quality']} bit_rate={settings['video_bit_rate']} "
        f"size={settings['max_size']} fps={settings['max_fps']} audio={audio_enabled}"
    )
    try:
        while not session.closed:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("text") is not None:
                text = msg.get("text")
                if text == "ping":
                    try:
                        await websocket.send_text("pong")
                    except Exception:
                        pass
                elif text == "suppress-lock":
                    # Client is switching quality/audio (reload) — don't lock the
                    # phone during this teardown so the user's unlock persists.
                    session.auto_lock_on_leave = False
                continue
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"media ws error: {e}")
    finally:
        await session.shutdown()


@app.websocket("/ws/control")
async def ws_control(websocket: WebSocket, token: str = Query(...)):
    await websocket.accept()
    with session_lock:
        session = active_session
        if session is None or session.closed or session.token != token:
            try:
                await websocket.send_json({"type": "error", "message": "invalid or expired session"})
            except Exception:
                pass
            await websocket.close(code=4001)
            return
        old_control = session.control_ws
        session.control_ws = websocket

    if old_control is not None and old_control is not websocket:
        try:
            await old_control.close()
        except Exception:
            pass

    print(f"control connected token={token[:8]}…")
    try:
        while not session.closed:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            data = message.get("bytes")
            if data:
                await asyncio.to_thread(session.scrcpy.scrcpy_send_control, bytes(data))
            elif message.get("text") is not None:
                text = message.get("text")
                if text == "ping":
                    try:
                        await websocket.send_text("pong")
                    except Exception:
                        pass
                elif text == "suppress-lock":
                    session.auto_lock_on_leave = False
                continue
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"control ws error: {e}")
    finally:
        if session.control_ws is websocket:
            session.control_ws = None
            print("control disconnected (media session kept)")


# —— Telegram 消息推送：后台轮询，未读消息持续超过 N 分钟才提醒一次 ——
# 只在配置齐全(telegram_config.json 填好 token+chat_id 且 enabled=true)时才轮询，
# 否则完全休眠——所以没配置时不产生任何额外 adb 或网络开销。
_tg_unread_since = {}   # pkg -> 首次看到它未读的时间戳；打开 App 读掉就清除
_tg_notified = set()    # 本轮未读期间已经提醒过的包名，读过一次后随上面一起清除

_TG_DEFAULT_REMINDER_MIN = 10


async def _telegram_notify_loop():
    global _tg_unread_since, _tg_notified
    serial = resolve_device_serial(None)
    model = get_device_model(serial) or serial or "Phone"
    while True:
        cfg = telegram_notify.load_config()
        if not telegram_notify.is_configured(cfg):
            _tg_unread_since = {}
            _tg_notified = set()
            await asyncio.sleep(20)
            continue
        try:
            state = await asyncio.to_thread(get_notification_state, serial)
        except Exception as e:
            print(f"telegram loop poll error: {e}")
            state = None
        if state and state.get("ok"):
            current = set(state.get("packages") or [])
            now = time.time()

            # 新出现的未读：记下首次看到的时间，从这一刻开始计时。
            for pkg in current - set(_tg_unread_since):
                _tg_unread_since[pkg] = now
            # 已经读掉的：清计时和提醒标记，下次再未读会重新计时。
            for pkg in set(_tg_unread_since) - current:
                _tg_unread_since.pop(pkg, None)
                _tg_notified.discard(pkg)

            try:
                threshold_min = float(cfg.get("unread_reminder_min") or _TG_DEFAULT_REMINDER_MIN)
                if threshold_min <= 0:
                    threshold_min = _TG_DEFAULT_REMINDER_MIN
            except (TypeError, ValueError):
                threshold_min = _TG_DEFAULT_REMINDER_MIN
            threshold_s = threshold_min * 60

            # 未读超过阈值、且这轮未读期间还没提醒过的，推一次。
            for pkg in sorted(current):
                since = _tg_unread_since.get(pkg)
                if since is None or pkg in _tg_notified or now - since < threshold_s:
                    continue
                res = await asyncio.to_thread(
                    telegram_notify.send_message,
                    f"📱 New message ({model})", cfg,
                )
                if res.get("ok"):
                    _tg_notified.add(pkg)
                else:
                    print(f"telegram send failed: {res.get('error')}")
        interval = cfg.get("poll_interval_s") or 10
        try:
            interval = max(5, int(interval))
        except (TypeError, ValueError):
            interval = 10
        await asyncio.sleep(interval)


@app.on_event("startup")
async def _start_background_tasks():
    asyncio.create_task(_telegram_notify_loop())


def main():
    global video_bit_rate, max_size, max_fps, local_port, auto_lock_on_leave, turn_screen_off_on_connect
    parser = argparse.ArgumentParser(description="Web server for scrcpy (FastAPI)")
    parser.add_argument("--video_bit_rate", default="2500000", help="default video bit rate")
    parser.add_argument("--max_size", default="800", help="default max video dimension")
    parser.add_argument("--max_fps", default="25", help="default max fps")
    parser.add_argument("--adb_path", default=DEFAULT_ADB_PATH, help="path to adb executable")
    parser.add_argument("--device_serial", default=None, help="adb device serial (required if multiple devices)")
    parser.add_argument(
        "--local_port",
        type=int,
        default=DEFAULT_LOCAL_PORT,
        help="ADB local forward port on this host (must be unique per parallel instance)",
    )
    parser.add_argument(
        "--auto_lock",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="lock device screen when the browser disconnects or leaves (default: enabled)",
    )
    parser.add_argument(
        "--turn_screen_off",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="turn off device display on connect (default); use --no-turn_screen_off to keep phone screen on",
    )
    parser.add_argument("--host", default="0.0.0.0", help="bind host")
    parser.add_argument("--port", type=int, default=5000, help="bind port")
    parser.add_argument(
        "--https",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="enable HTTPS (auto-install cryptography and generate LAN certificate if needed)",
    )
    parser.add_argument(
        "--ssl_certfile",
        default=None,
        help="custom TLS certificate PEM (optional; with --https uses auto-generated cert when omitted)",
    )
    parser.add_argument(
        "--ssl_keyfile",
        default=None,
        help="custom TLS private key PEM (optional; pair with --ssl_certfile)",
    )
    parser.add_argument(
        "--tls_domain",
        action="append",
        default=None,
        metavar="DOMAIN",
        help="domain name for auto-generated HTTPS cert (repeatable), e.g. --tls_domain scrcpy.test.com",
    )
    args = parser.parse_args()
    video_bit_rate = str(args.video_bit_rate)
    max_size = str(args.max_size)
    max_fps = str(args.max_fps)
    auto_lock_on_leave = args.auto_lock
    turn_screen_off_on_connect = args.turn_screen_off
    set_adb_path(args.adb_path)
    set_device_serial(args.device_serial)
    try:
        set_local_port(args.local_port)
        local_port = args.local_port
    except ValueError as e:
        raise SystemExit(str(e))
    if not check_adb():
        raise SystemExit(1)
    devices, _ = list_adb_devices()
    if len(devices) > 1 and not args.device_serial:
        print(f"Warning: multiple devices ({', '.join(devices)}); use --device_serial to pick one.")

    try:
        ssl_certfile, ssl_keyfile, local_ips, tls_domains = resolve_ssl_paths(
            BASE_DIR,
            use_https=args.https,
            ssl_certfile=args.ssl_certfile,
            ssl_keyfile=args.ssl_keyfile,
            tls_domains=args.tls_domain,
        )
    except FileNotFoundError as e:
        raise SystemExit(str(e)) from e
    except ValueError as e:
        raise SystemExit(str(e)) from e
    except Exception as e:
        raise SystemExit(f"HTTPS setup failed: {e}") from e

    scheme = "https" if ssl_certfile and ssl_keyfile else "http"
    print(
        f"Starting web-scrcpy on {scheme}://{args.host}:{args.port} "
        f"(adb local_port={local_port}, auto_lock={'on' if auto_lock_on_leave else 'off'}, "
        f"turn_screen_off={'on' if turn_screen_off_on_connect else 'off'})"
    )
    if scheme == "https":
        lan_urls = [
            f"https://{ip}:{args.port}"
            for ip in local_ips
            if ip != "127.0.0.1"
        ]
        print(f"  Local:  https://127.0.0.1:{args.port}")
        print(f"  Local:  https://localhost:{args.port}")
        for url in lan_urls:
            print(f"  LAN:    {url}")
        for domain in tls_domains:
            print(f"  Domain: https://{domain}:{args.port}")
        if not lan_urls:
            print("  LAN:    (no IPv4 detected — use your machine IP manually)")
        print("  Safari/iOS: HTTPS enables better audio (AudioWorklet). Accept the cert warning once.")

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
    )


if __name__ == "__main__":
    main()
