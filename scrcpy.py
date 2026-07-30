from threading import Thread, Lock
from concurrent.futures import ThreadPoolExecutor
import subprocess
import socket
import shutil
import os
import re
import time
import xml.etree.ElementTree as ET

# Suppress the console window every adb.exe subprocess would otherwise pop up
# when the server runs windowless (pythonw). Windows-only flag; 0 elsewhere.
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _run(*args, **kwargs):
    kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
    return subprocess.run(*args, **kwargs)


def _popen(*args, **kwargs):
    kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
    return subprocess.Popen(*args, **kwargs)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ADB_PATH = os.path.join(BASE_DIR, "adb.exe")
SCRCPY_VERSION = "3.3.3"
ADB_PATH = DEFAULT_ADB_PATH
DEFAULT_DEVICE_SERIAL = None
# scrcpy control message: SET_DISPLAY_POWER (screen off while mirroring continues)
CONTROL_MSG_SET_DISPLAY_POWER = 10

SCRCPY_SERVER_PATH = "scrcpy-server"
DEVICE_SERVER_PATH = "/data/local/tmp/scrcpy-server.jar"
# Avoid 5555 which conflicts with wireless adb / emulators
DEFAULT_LOCAL_PORT = 27183
LOCAL_PORT = DEFAULT_LOCAL_PORT
CONNECT_TIMEOUT_S = 8.0
CONNECT_RETRY_INTERVAL_S = 0.05


def set_adb_path(path):
    global ADB_PATH
    ADB_PATH = path


def set_device_serial(serial):
    global DEFAULT_DEVICE_SERIAL
    DEFAULT_DEVICE_SERIAL = serial.strip() if serial else None


def set_local_port(port):
    """Set default ADB local forward port for this process (multi-instance: one port each)."""
    global LOCAL_PORT
    LOCAL_PORT = _normalize_local_port(port)


def _normalize_local_port(port):
    try:
        value = int(port)
    except (TypeError, ValueError):
        raise ValueError(f"invalid local_port: {port!r}")
    if value < 1024 or value > 65535:
        raise ValueError(f"local_port must be between 1024 and 65535, got {value}")
    if value == 5555:
        raise ValueError("local_port 5555 is reserved for wireless adb")
    return value


def _resolve_local_port(local_port=None):
    return _normalize_local_port(local_port) if local_port is not None else LOCAL_PORT


def adb_cmd(*args, device_serial=None):
    serial = device_serial if device_serial is not None else DEFAULT_DEVICE_SERIAL
    cmd = [ADB_PATH]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(args)
    return cmd


def check_adb():
    if os.path.isfile(ADB_PATH) or shutil.which(ADB_PATH):
        return True
    print(f"Error: adb not found ('{ADB_PATH}').")
    print(f"Place adb.exe in the project folder: {DEFAULT_ADB_PATH}")
    return False


def list_adb_devices():
    result = _run([ADB_PATH, "devices"], capture_output=True, text=True)
    devices = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = re.split(r"\s+", line)
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices, result.stdout or result.stderr


def resolve_device_serial(requested=None):
    """Pick adb target: explicit request > CLI default > single connected device."""
    if requested and str(requested).strip():
        return str(requested).strip()
    if DEFAULT_DEVICE_SERIAL:
        return DEFAULT_DEVICE_SERIAL
    devices, _ = list_adb_devices()
    if len(devices) == 1:
        return devices[0]
    return None


_device_model_cache = {}


def get_device_model(device_serial=None):
    """设备型号（ro.product.model，如 OPPO=PLS120 / 谷歌2=Pixel 2）。
    页面据此区分是哪台手机，做设备专属版面。结果缓存，避免每次请求都跑 adb。"""
    serial = resolve_device_serial(device_serial)
    if not serial:
        return None
    if serial in _device_model_cache:
        return _device_model_cache[serial]
    try:
        r = _run(adb_cmd("shell", "getprop ro.product.model", device_serial=serial),
                 capture_output=True, text=True, timeout=5)
        model = (r.stdout or "").strip()
    except Exception:
        model = ""
    if model:
        _device_model_cache[serial] = model
    return model or None


def trigger_device_screenshot(device_serial=None):
    """
    触发设备自带的系统截屏（keyevent 120 = KEYCODE_SYSRQ）。
    截图由系统保存到手机的 Screenshots 目录（相册可见）。成功返回 True。
    """
    serial = resolve_device_serial(device_serial)
    try:
        result = _run(
            adb_cmd("shell", "input keyevent 120", device_serial=serial),
            capture_output=True, text=True, timeout=6,
        )
        if result.returncode == 0:
            print("device screenshot: triggered (saved to phone Screenshots)")
            return True
        print(f"device screenshot failed: {(result.stderr or '').strip()}")
    except Exception as e:
        print(f"device screenshot failed: {e}")
    return False


# —— 消息通知检测（QQ / 微信 / Soul）——
# 网页悬浮球据此变红：三者之一有“可清除的消息通知”且该 App 不在前台时点亮，
# 打开对应 App（切到前台）后熄灭。
NOTIFY_TARGET_PACKAGES = ("com.tencent.mm", "com.tencent.mobileqq", "cn.soulapp.android")

# 视为“不是真实消息”的通知标志位，一律跳过，避免悬浮球无消息也亮着：
#   ONGOING / NO_CLEAR / FGSRV —— 常驻或前台服务通知（如“微信运行中”）
#   GROUP_SUMMARY / AUTOGROUP_SUMMARY —— Android 自动分组产生的“摘要头”，
#     它只是子通知的容器，本身不是消息；真消息被清掉后摘要常常还赖着不走，
#     只数子通知、跳过摘要，才不会误亮。
_NOTIFY_FLAG_ONGOING = 0x00000002
_NOTIFY_FLAG_NO_CLEAR = 0x00000020
_NOTIFY_FLAG_FOREGROUND_SERVICE = 0x00000040
_NOTIFY_FLAG_GROUP_SUMMARY = 0x00000200
_NOTIFY_FLAG_AUTOGROUP_SUMMARY = 0x00000400
_NOTIFY_SKIP_FLAGS = (
    _NOTIFY_FLAG_ONGOING
    | _NOTIFY_FLAG_NO_CLEAR
    | _NOTIFY_FLAG_FOREGROUND_SERVICE
    | _NOTIFY_FLAG_GROUP_SUMMARY
    | _NOTIFY_FLAG_AUTOGROUP_SUMMARY
)
# 新版 Android（实测 OPPO/Android 16）把 flags 打成符号名而不是十六进制，
# 例如 flags=AUTO_CANCEL|GROUP_SUMMARY。只认 0x… 的话这道闸门会形同虚设。
_NOTIFY_SKIP_TOKENS = (
    "ONGOING", "NO_CLEAR", "FGSRV", "FOREGROUND_SERVICE",
    "GROUP_SUMMARY",   # 同时覆盖 AUTOGROUP_SUMMARY（子串包含）
)


def _notification_skip(line):
    """这条通知是不是“不该点亮悬浮球”的那种（常驻通知、或自动分组的摘要头）。
    dumpsys 在不同版本上把 flags 打成十六进制或符号名，两种都要认。"""
    # 用 [^\s)]+ 而不是 \S+：flags 若正好在行尾，\S+ 会把后面的 "))" 一起吞掉，
    # 导致十六进制形式匹配不上而漏判。
    m = re.search(r"flags=([^\s)]+)", line)
    if not m:
        return False
    raw = m.group(1)
    hex_m = re.fullmatch(r"0x([0-9a-fA-F]+)", raw)
    if hex_m:
        return bool(int(hex_m.group(1), 16) & _NOTIFY_SKIP_FLAGS)
    upper = raw.upper()
    return any(tok in upper for tok in _NOTIFY_SKIP_TOKENS)

# 一次 adb 往返同时取“前台 App”“屏幕是否亮着”“通知列表”，用标记分段。
# 屏幕状态给网页判断：手机睡着了就把伪装页盖回来，让用户可以点三下唤醒进 PIN。
# 这里用 deviceidle get screen（约 150ms）而不是 dumpsys power（约 330ms）+
# dumpsys trust（约 160ms）：每 5 秒一次，手机内存紧张时这点开销也值得省。
# mIsShowing 来自 dumpsys window 里的 KeyguardStateMonitor（锁屏是否显示），
# 和 mCurrentFocus 同在这一次 dumpsys window 里，多 grep 一行即可拿到锁屏状态，
# 不必再单独跑一次 dumpsys trust（省约 150ms/轮询）。
_NOTIFY_SHELL = (
    "echo @@FG@@; dumpsys window | grep -E 'mCurrentFocus|mIsShowing='; "
    "echo @@PW@@; dumpsys deviceidle get screen; "
    "echo @@NT@@; dumpsys notification --noredact | grep 'NotificationRecord('"
)

# 轻量缓存：多端/多次轮询时避免频繁执行 dumpsys（约几百毫秒）。
# 缓存键带上 packages——这个函数现在有两种调用方式（悬浮球跑马灯不过滤包名、
# Telegram 提醒循环仍然只看 NOTIFY_TARGET_PACKAGES 那几个），键不区分的话，
# 两边共用同一个 2.5s 缓存会互相读到对方过滤范围算出来的结果。
_notify_cache = {"key": None, "state": None, "at": 0.0}
_notify_cache_lock = Lock()
_NOTIFY_CACHE_TTL_S = 2.5


def _parse_foreground_package(line):
    """从 mCurrentFocus 行里取前台 App 的包名。"""
    m = re.search(r"\bu\d+\s+([a-zA-Z0-9_.]+)/", line)
    if m:
        return m.group(1)
    m = re.search(r"([a-zA-Z0-9_.]+)/[a-zA-Z0-9_.$]+\}", line)
    return m.group(1) if m else None


def get_notification_state(device_serial=None, packages=NOTIFY_TARGET_PACKAGES):
    """
    返回 {ok, alert, packages, foreground}：
      - packages：当前有"真实消息通知"且不在前台的 App 列表
      - alert：packages 是否非空（悬浮球是否应变红）
      - foreground：当前前台 App 包名

    packages 参数：
      - 传具体的包名集合（比如默认的 NOTIFY_TARGET_PACKAGES）→ 只认这几个 App。
        Telegram 未读提醒循环用的就是这个默认值——那个功能本来就只该盯着
        聊天类 App，不该被任何 App 的通知（广告、系统消息……）刷屏。
      - 传 None → 不按包名过滤，任何 App 只要有条"真实消息通知"（下面 importance/
        flags 那两道判断）就算，对应悬浮球跑马灯"不止微信/QQ/Soul，凡是图标会
        显示通知角标的 App 都提醒"的要求——现在唯一还剩的判断标准就是"这条通知
        是不是真会显示成角标"，不再额外要求"必须是这三个 App 之一"。
    """
    serial = resolve_device_serial(device_serial)
    empty = {"ok": True, "alert": False, "packages": [], "foreground": None,
             "awake": None, "locked": None}
    if not serial:
        return {**empty, "ok": False}

    cache_key = (serial, packages)
    now = time.time()
    with _notify_cache_lock:
        c = _notify_cache
        if (c["state"] is not None and c["key"] == cache_key
                and now - c["at"] < _NOTIFY_CACHE_TTL_S):
            return c["state"]

    try:
        result = _run(
            adb_cmd("shell", _NOTIFY_SHELL, device_serial=serial),
            capture_output=True, text=True, timeout=6,
        )
    except Exception as e:
        return {**empty, "ok": False, "error": str(e)}

    target_set = set(packages) if packages is not None else None
    section = None
    foreground = None
    alerting = []
    awake = None
    locked = None
    for raw in (result.stdout or "").splitlines():
        line = raw.strip()
        if line == "@@FG@@":
            section = "fg"
            continue
        if line == "@@PW@@":
            section = "pw"
            continue
        if line == "@@NT@@":
            section = "nt"
            continue
        if section == "fg":
            if "mCurrentFocus" in line and foreground is None:
                foreground = _parse_foreground_package(line)
            elif "mIsShowing=" in line and locked is None:
                locked = "mIsShowing=true" in line   # keyguard 是否显示 = 是否锁屏
        elif section == "pw":
            if line in ("true", "false"):
                awake = line == "true"   # deviceidle get screen：屏幕是否亮着
        elif section == "nt" and line.startswith("NotificationRecord("):
            m = re.search(r"pkg=(\S+)", line)
            if not m or (target_set is not None and m.group(1) not in target_set):
                continue
            pkg = m.group(1)
            imp_m = re.search(r"importance=(-?\d+)", line)
            importance = int(imp_m.group(1)) if imp_m else 3
            if importance <= 0:
                continue  # 只跳过 IMPORTANCE_NONE（被彻底关掉的通知）。
                # 注意：MIN(=1) 也要算——Soul 的“聊天消息”渠道被降级成 MIN 后
                # 仍是真实消息（category=msg、可清除），之前一并跳过导致跑马灯不亮。
            if _notification_skip(line):
                continue  # 常驻通知 或 自动分组的摘要头，都不算真实消息
            if pkg not in alerting:
                alerting.append(pkg)

    # 打开哪个 App（切到前台）就熄灭哪个的红点。
    active = [p for p in alerting if p != foreground]
    state = {
        "ok": True,
        "alert": len(active) > 0,
        "packages": active,
        "foreground": foreground,
        # 供网页判断是否该盖回伪装页（手机睡着时盖回，方便点三下唤醒进 PIN）。
        "awake": awake,
        # 是否锁屏：网页据此决定超人导航球显不显示（解锁进主界面才显示）。
        "locked": locked,
    }
    with _notify_cache_lock:
        _notify_cache.update(at=time.time(), key=cache_key, state=state)
    return state


# —— 进入投屏时唤醒手机并调出解锁界面 ——
# 手机现在会按自己的设置自动息屏/锁屏，而 scrcpy 注入的触摸唤不醒休眠中的设备，
# 所以在网页解锁伪装页进入时，用 adb 主动唤醒；若处于锁屏则上滑调出 PIN 输入盘。
_screen_size_cache = {}


def _get_screen_size(serial):
    """取设备分辨率（缓存），用于按比例计算上滑坐标。"""
    cached = _screen_size_cache.get(serial)
    if cached:
        return cached
    try:
        r = _run(
            adb_cmd("shell", "wm size", device_serial=serial),
            capture_output=True, text=True, timeout=5,
        )
        m = re.search(r"(\d+)x(\d+)", r.stdout or "")
        if m:
            size = (int(m.group(1)), int(m.group(2)))
            _screen_size_cache[serial] = size
            return size
    except Exception:
        pass
    return None


def wake_for_unlock(device_serial=None):
    """
    唤醒手机；若在锁屏则上滑调出密码输入界面。
    手机本来就醒着且未锁定时不做任何操作（避免打断正在进行的使用）。
    """
    serial = resolve_device_serial(device_serial)
    if not serial:
        return {"ok": False, "action": "no-device"}

    try:
        r = _run(
            adb_cmd(
                "shell",
                "dumpsys power | grep -o 'mWakefulness=[A-Za-z]*'; "
                "dumpsys trust | grep -o 'deviceLocked=[01]' | head -1",
                device_serial=serial,
            ),
            capture_output=True, text=True, timeout=6,
        )
    except Exception as e:
        return {"ok": False, "action": "state-failed", "error": str(e)}

    out = r.stdout or ""
    awake = "mWakefulness=Awake" in out
    locked = "deviceLocked=1" in out

    # locked 一并返回：网页据此决定要不要弹 PIN 输入框（锁屏黑屏的机型看不到密码盘）。
    # 这个状态本函数已经查过了，返回它不多花一次 adb。
    if awake and not locked:
        return {"ok": True, "action": "none", "locked": False, "awake": True}

    try:
        _run(
            adb_cmd("shell", "input keyevent KEYCODE_WAKEUP", device_serial=serial),
            capture_output=True, text=True, timeout=5,
        )
        if not locked:
            print("wake: screen on (device was not locked)")
            return {"ok": True, "action": "woke", "locked": False, "awake": True}

        time.sleep(0.5)  # 等锁屏界面起来再上滑
        size = _get_screen_size(serial)
        if size:
            w, h = size
            x = w // 2
            _run(
                adb_cmd(
                    "shell",
                    f"input swipe {x} {int(h * 0.78)} {x} {int(h * 0.28)} 200",
                    device_serial=serial,
                ),
                capture_output=True, text=True, timeout=6,
            )
        print("wake: screen on + unlock prompt shown")
        return {"ok": True, "action": "woke+unlock-prompt", "locked": True, "awake": True}
    except Exception as e:
        print(f"wake failed: {e}")
        return {"ok": False, "action": "wake-failed", "error": str(e), "locked": locked}


def open_app_drawer(device_serial=None):
    """
    从屏幕底部中间上滑，拉出抽屉式桌面的应用列表。
    抽屉式启动器（ColorOS 可切换）就是靠这个手势打开的；没有对应按键码，
    只能注入滑动。起止点用屏幕比例算，换机型/换分辨率都不用改。
    """
    serial = resolve_device_serial(device_serial)
    if not serial:
        return {"ok": False, "error": "no-device"}
    size = _get_screen_size(serial)
    if not size:
        return {"ok": False, "error": "no-screen-size"}
    w, h = size
    x = w // 2
    y1 = int(h * 0.92)   # 贴近底部起手
    y2 = int(h * 0.40)   # 上滑到屏幕中上部，行程足够触发抽屉
    try:
        _run(
            adb_cmd("shell", f"input swipe {x} {y1} {x} {y2} 180", device_serial=serial),
            capture_output=True, text=True, timeout=6,
        )
        return {"ok": True}
    except Exception as e:
        print(f"open drawer failed: {e}")
        return {"ok": False, "error": str(e)}


def set_device_ime_visible(visible, device_serial=None):
    """
    切换手机自己的屏幕输入法是否弹出。

    scrcpy 注入输入时安卓把它当外接硬件键盘，默认就不弹屏幕输入法了
    （show_ime_with_hard_keyboard=0）。置 1 后点手机上的文本框会正常弹出
    手机输入法（可用拼音候选词）；置 0 则隐藏，避免和本机键盘两套键盘打架。
    """
    serial = resolve_device_serial(device_serial)
    if not serial:
        return {"ok": False, "visible": None}
    value = "1" if visible else "0"
    try:
        _run(
            adb_cmd(
                "shell", f"settings put secure show_ime_with_hard_keyboard {value}",
                device_serial=serial,
            ),
            capture_output=True, text=True, timeout=6,
        )
        print(f"device IME visible: {value}")
        return {"ok": True, "visible": bool(visible)}
    except Exception as e:
        print(f"set device IME failed: {e}")
        return {"ok": False, "visible": None, "error": str(e)}


def reboot_device(device_serial=None):
    """
    远程重启手机（adb reboot）。手机是 USB 连着的，重启后 adb 一般会自动连回、
    停在锁屏；网页端点三下解锁会自动唤醒并跳到 PIN 输入界面。
    """
    serial = resolve_device_serial(device_serial)
    if not serial:
        return {"ok": False, "error": "no-device"}
    try:
        _run(
            adb_cmd("reboot", device_serial=serial),
            capture_output=True, text=True, timeout=10,
        )
        print(f"device reboot: sent to {serial}")
        return {"ok": True}
    except Exception as e:
        print(f"device reboot failed: {e}")
        return {"ok": False, "error": str(e)}


# —— Secure Input Engine：往一块因 FLAG_SECURE 而黑屏的数字密码界面发一个键 ——
# 覆盖场景：手机锁屏、Secret Album、OPPO/微信/支付宝应用锁、银行 App PIN 等。
# 这些界面分两种：系统标准 PIN 输入框（发 KeyEvent 就能进）、App 自绘的数字按钮
# 键盘（KeyEvent 到不了，只认触摸点击）——两者外观都是黑屏数字键盘，肉眼分不出来，
# 只能靠现场探测判断。
#
# 架构是“能力探测链”：_PROBES 是一串 _SecureInputProbe，按顺序问“这个探测器
# 判断得出该怎么发这个键吗”，第一个给出明确答案（返回某个 Strategy）的说了算，
# 判断不出来（返回 None）就交给下一个；都判断不出来，最后兜底用 KeyEvent
# （优先保证已经在用的场景，比如锁屏，不因为探测不出来而被破坏）。
# 以后遇到小米/vivo/荣耀/三星等新场景，只要现有探测器的信号识别不出来，
# 加一个新 _SecureInputProbe 子类塞进 _PROBES 列表即可——不用改已有探测器，
# 不用碰 Strategy，也不用碰调用方（send_key、/api/secure-key、前端 JS 完全
# 不知道、也不需要知道具体走了哪条路径）。
_SECURE_KEY_CODES = {str(d): 7 + d for d in range(10)}  # KEYCODE_0..9 = 7..16
_SECURE_KEY_CODES["del"] = 67     # KEYCODE_DEL（与 input.js 里退格键一致）
_SECURE_KEY_CODES["enter"] = 66   # KEYCODE_ENTER（与 input.js 里回车键一致）

_TAP_DEL_LABELS = {"删除", "清除", "清空", "退格", "backspace", "clear", "del"}
_TAP_ENTER_LABELS = {"确定", "完成", "确认", "提交", "ok", "done", "enter"}


class _SecureInputStrategy:
    name = "base"

    def send(self, key, serial):
        raise NotImplementedError


class _KeyEventStrategy(_SecureInputStrategy):
    """标准场景（含系统锁屏）：直接发 KeyEvent，就是原来一直在用、已经验证
    工作正常的路径。"""
    name = "keyevent"

    def send(self, key, serial):
        code = _SECURE_KEY_CODES.get(str(key))
        if code is None:
            return {"ok": False, "error": "invalid-key"}
        r = _run(adb_cmd("shell", f"input keyevent {code}", device_serial=serial),
                 capture_output=True, text=True, timeout=6)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()[:200]
            return {"ok": False, "error": f"adb exit {r.returncode}: {err}" if err else f"adb exit {r.returncode}"}
        return {"ok": True, "strategy": self.name}


class _UITapStrategy(_SecureInputStrategy):
    """自绘数字键盘场景（如 OPPO 私密相册的 ConfirmNumberPrivacy）：这类界面的
    密码框只是个展示用的 View，数字键是一个个独立 Button，靠触摸点击回调追加
    一位数字，不接 KeyEvent。目标按钮的坐标由探测阶段现场 dump 得到，绝不写死，
    这里只管点。"""
    name = "tap"

    def __init__(self, node):
        self._node = node

    def send(self, key, serial):
        cx, cy = _node_center(self._node)
        if cx is None:
            return {"ok": False, "error": "bad-bounds"}
        r = _run(adb_cmd("shell", f"input tap {cx} {cy}", device_serial=serial),
                 capture_output=True, text=True, timeout=6)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()[:200]
            return {"ok": False, "error": f"adb exit {r.returncode}: {err}" if err else f"adb exit {r.returncode}"}
        return {"ok": True, "strategy": self.name}


class _NoOpStrategy(_SecureInputStrategy):
    """已经确认是自绘键盘，但这一个键（目前只有 enter 会走到这）在界面上本来就
    没有对应按钮——多半是“输满位数自动提交”的设计，不是探测失败，不该报错，
    也不该硬点一个不存在的按钮。返回 ok:true 但带上 no-op 标记，调用方看得出
    这一步本来就不需要动作，跟真正的失败区分开。"""
    name = "noop"

    def __init__(self, reason):
        self._reason = reason

    def send(self, key, serial):
        return {"ok": True, "strategy": self.name, "reason": self._reason}


class _ErrorStrategy(_SecureInputStrategy):
    """已经能确定当前场景（比如认定是自绘键盘），但这一个键真的找不到对应目标
    ——明确返回错误，不静默失败，也不假装能退回别的方式。"""
    name = "error"

    def __init__(self, error):
        self._error = error

    def send(self, key, serial):
        return {"ok": False, "error": self._error}


def _node_label(node):
    return (node.get("content-desc") or node.get("text") or "").strip()


def _node_center(node):
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds") or "")
    if not m:
        return None, None
    x1, y1, x2, y2 = (int(v) for v in m.groups())
    return (x1 + x2) // 2, (y1 + y2) // 2


def _get_focused_window_type(serial):
    """只读：取当前聚焦窗口的 Android 窗口类型（mAttrs 里的 ty=...）。
    普通 App 的 Activity 窗口固定是 BASE_APPLICATION；系统自己的界面（锁屏所在
    的 SystemUI 通知栏/Keyguard 一类窗口、状态栏、输入法等）用的是其他类型
    （比如锁屏这里实测是 NOTIFICATION_SHADE）。这是 Android WindowManager 自带
    的标准分类，不分厂商、不分 App——判断“是不是系统窗口”不需要知道也不需要
    写死任何具体包名。取不到时返回 None，调用方按“判断不出来就当系统窗口处理”
    （更保守，优先保证不误伤已经能用的场景，比如锁屏）。

    分两次小查询，不整份拉 `dumpsys window windows`——实测这份输出几百到上千行，
    偶发在 adb shell 管道上被截断，截断点又不固定，可能正好把要找的那段截掉；
    第二次查询让设备端 grep 只回传目标窗口那几行，既避开截断也更快。"""
    try:
        r1 = _run(adb_cmd("shell", "dumpsys window | grep mCurrentFocus", device_serial=serial),
                  capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=6)
        if r1.returncode != 0 or not r1.stdout:
            return None
        m = re.search(r"mCurrentFocus=Window\{(\S+)\s+u\d+\s+[^}]*\}", r1.stdout)
        if not m:
            return None
        token = m.group(1)
        grep_pattern = r"Window #[0-9]+ Window\{" + token + r" "
        r2 = _run(adb_cmd("shell", f"dumpsys window windows | grep -A5 -E '{grep_pattern}'",
                            device_serial=serial),
                  capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8)
        if r2.returncode != 0 or not r2.stdout:
            return None
        block = re.search(r"ty=([A-Z_]+)", r2.stdout)
        return block.group(1) if block else None
    except Exception:
        return None


def _dump_ui_tree(serial):
    """只读：uiautomator dump 当前界面控件树，用于判断这次该发 KeyEvent 还是点坐标。
    坐标绝不写死——每次都现场 dump、现场解析、现场算中心点。
    dump 失败（超时、系统窗口不给读等）一律返回 None，调用方按“探测不出来就
    退回 KeyEvent”处理，不会因为探测本身失败反而破坏原本能用的场景。"""
    try:
        _run(adb_cmd("shell", "uiautomator dump /sdcard/_secure_input_dump.xml",
                      device_serial=serial),
             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8)
        # dump 出来的 XML 本身是 UTF-8（label 里常有中文，比如“输入隐私密码”），
        # 这里必须显式指定编码——不指定的话 subprocess 会用 Windows 默认代码页
        # （这台机器上是 GBK）去解码，中文内容一律解码崩溃，_dump_ui_tree 就会
        # 一直命中下面的 except 返回 None，UITapStrategy 在中文界面上永远用不上。
        r = _run(adb_cmd("shell", "cat /sdcard/_secure_input_dump.xml", device_serial=serial),
                 capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8)
        if r.returncode != 0 or not r.stdout or "<hierarchy" not in r.stdout:
            return None
        return ET.fromstring(r.stdout)
    except Exception:
        return None


def _scan_keypad_nodes(root):
    """把控件树扫一遍，收集看起来像数字键的可点击节点，和 del/enter 同义词
    对应的节点。不看是哪个 App、哪个厂商，只看控件本身的可点击性 + 单字符
    数字标签这个结构特征。"""
    digit_nodes = {}
    action_nodes = []
    for node in root.iter("node"):
        if (node.get("clickable") or "").lower() != "true":
            continue
        cls = node.get("class") or ""
        if not any(t in cls for t in ("Button", "ImageView", "TextView", "View")):
            continue
        label = _node_label(node)
        if len(label) == 1 and label in "0123456789":
            digit_nodes.setdefault(label, node)
        elif label.lower() in _TAP_DEL_LABELS:
            action_nodes.append(("del", node))
        elif label.lower() in _TAP_ENTER_LABELS:
            action_nodes.append(("enter", node))
    return digit_nodes, action_nodes


class _SecureInputProbe:
    """能力探测器基类：判断当前这个键该怎么发。判断得出就返回一个具体的
    _SecureInputStrategy（包括“确认没这个按钮，报错”或“确认不用发，no-op”
    这类明确结论也算判断得出）；判断不出来（这个探测器的信号不适用于当前
    界面）就返回 None，交给探测链里的下一个探测器。"""

    def probe(self, key, serial):
        raise NotImplementedError


class _SystemWindowKeyEventProbe(_SecureInputProbe):
    """能力信号：聚焦窗口不是普通 App 的 BASE_APPLICATION —— 说明这是系统自己
    的界面（锁屏所在的 SystemUI 一类窗口），Android 对这类窗口有特殊的按键
    转发机制，KeyEvent 可用。只看 Android 自带的窗口类型，不看包名/厂商，
    任何 OEM 的锁屏实现都适用。"""

    def probe(self, key, serial):
        win_type = _get_focused_window_type(serial)
        if win_type is not None and win_type != "BASE_APPLICATION":
            return _KeyEventStrategy()
        return None  # 普通 App 窗口，或窗口类型查不到——交给下一个探测器


class _CustomKeypadTapProbe(_SecureInputProbe):
    """能力信号：dump 出的控件树里能凑齐一整套数字按钮（≥3 个不同数字，避免
    被页面上一两个偶然带数字文案的无关控件误判）——说明这是 App 自绘键盘，
    KeyEvent 到不了，只能点击。一旦认定是自绘键盘，之后不管这个键有没有找到
    对应按钮都必须给出明确结论（Tap / no-op / 报错），不再交给下一个探测器
    ——“探测不出来”和“探测出来了但这个键没有按钮”是两回事，不能混为一谈。"""

    def probe(self, key, serial):
        root = _dump_ui_tree(serial)
        if root is None:
            return None  # dump 不出来，没法判断，交给下一个/兜底

        digit_nodes, action_nodes = _scan_keypad_nodes(root)
        if len(digit_nodes) < 3:
            return None  # 没看到像键盘的结构特征，这个探测器不适用

        if key in digit_nodes:
            return _UITapStrategy(digit_nodes[key])
        for action_key, node in action_nodes:
            if action_key == key:
                return _UITapStrategy(node)
        if key == "enter":
            # 自绘键盘里没有确定/回车按钮——按“输满位数自动提交”处理，
            # 不当成失败，也不用再尝试点一个不存在的按钮。
            return _NoOpStrategy("auto-submit keypad, no enter button")
        return _ErrorStrategy(f"tap-target-not-found:{key}")


# 探测链：按顺序尝试，第一个给出明确结论的说了算。以后新增机型/场景的判断
# 方式，只需要在这里加一个新的 _SecureInputProbe 子类实例，不用改其它探测器。
_PROBES = [_SystemWindowKeyEventProbe(), _CustomKeypadTapProbe()]


def _detect_secure_input_strategy(key, serial):
    """依次问探测链里的每个探测器，返回第一个给出的明确结论。全部探测器都
    判断不出来时，兜底用 KeyEvent——优先保证已经在用的场景（比如锁屏）
    不会因为探测不出来而被破坏。"""
    for probe in _PROBES:
        strategy = probe.probe(key, serial)
        if strategy is not None:
            return strategy
    return _KeyEventStrategy()


def send_key(key, device_serial=None):
    """Secure Input Engine 的统一入口：数字 0-9 / del / enter。调用方（/api/secure-key、
    进而前端 JS）不需要知道这一下到底是 KeyEvent 还是坐标点击——由探测链
    （_PROBES）现场判断界面类型后自动选择。不打印 key 的值——连续调用的
    序列本身就是 PIN，日志里不该留下任何一位。"""
    serial = resolve_device_serial(device_serial)
    if not serial:
        return {"ok": False, "error": "no-device"}
    key = str(key)
    if key not in _SECURE_KEY_CODES:
        return {"ok": False, "error": "invalid-key"}
    try:
        strategy = _detect_secure_input_strategy(key, serial)
        result = strategy.send(key, serial)
        if not result.get("ok"):
            print(f"send_key: {strategy.name} strategy failed: {result.get('error')}")
        return result
    except Exception as e:
        print(f"send_key failed: {e}")
        return {"ok": False, "error": str(e)}


def preclean_connection(device_serial=None, local_port=None):
    """
    Scrub stale adb forward and device-side scrcpy server before a fresh connect.
    Safe to call repeatedly.
    """
    serial = resolve_device_serial(device_serial)
    port = _resolve_local_port(local_port)
    # forward 清理和 pkill 互不依赖，并行做省掉一次往返（各约 50 / 210ms）。
    # 注意 setup_adb_forward() 自己也会先 remove 一遍，这里的 remove 只是兜底。
    def _drop_forward():
        try:
            _run(
                adb_cmd("forward", "--remove", f"tcp:{port}", device_serial=serial),
                capture_output=True, text=True, timeout=3,
            )
        except Exception:
            pass

    def _kill_server():
        try:
            _run(
                adb_cmd(
                    "shell",
                    "pkill -f com.genymobile.scrcpy.Server 2>/dev/null; "
                    "pkill -f scrcpy-server 2>/dev/null; true",
                    device_serial=serial,
                ),
                capture_output=True, text=True, timeout=4,
            )
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=2) as pool:
        fs = [pool.submit(_drop_forward), pool.submit(_kill_server)]
        for f in fs:
            f.result()
    # 只需给设备一点时间释放 localabstract 套接字名；0.12s 是保守值，0.04s 够用
    # （后面 _connect_socket 本来就以 50ms 间隔重试，慢一点也能自愈）。
    time.sleep(0.04)


class Scrcpy:
    _flag_secure_done = False  # class-level: only attempt once per process

    def __init__(self, device_serial=None, local_port=None, turn_screen_off_on_connect=False):
        self.device_serial = resolve_device_serial(device_serial)
        self.local_port = _resolve_local_port(local_port)
        self.turn_screen_off_on_connect = bool(turn_screen_off_on_connect)
        self.video_socket = None
        self.audio_socket = None
        self.control_socket = None

        self.android_thread = None
        self.video_thread = None
        self.audio_thread = None
        self.control_thread = None
        self.android_process = None
        self.control_lock = Lock()
        self.stop = True
        self.audio_enabled = False
        self.audio_callback = None
        self.video_callback = None
        self.video_bit_rate = "2500000"
        self.max_size = "800"
        self.max_fps = "25"
        self.forward_ready = False

    def _adb(self, *args):
        return adb_cmd(*args, device_serial=self.device_serial)

    def _prepare_socket(self, sock):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
        except OSError:
            pass
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 256 * 1024)
        except OSError:
            pass

    def _safe_recv(self, sock, size):
        if self.stop or sock is None:
            return b""
        try:
            return sock.recv(size)
        except OSError:
            return b""

    def _close_socket(self, sock, shutdown=False):
        if sock is None:
            return
        if shutdown:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            sock.close()
        except OSError:
            pass

    def try_disable_flag_secure(self):
        if Scrcpy._flag_secure_done:
            return True

        script = (
            "settings put global block_untrusted_touches 0;"
            "settings put secure screencapture_disabled 0;"
            "settings put system secure_flag_value 0;"
            "setprop debug.screencap.secure_layers true;"
            "setprop debug.disable_flag_secure 1;"
            "echo FLAG_SECURE_DONE"
        )
        try:
            r = _run(
                self._adb("shell", script),
                capture_output=True, text=True, timeout=4,
            )
            if "FLAG_SECURE_DONE" in (r.stdout or ""):
                print("FLAG_SECURE bypass: commands sent")
                Scrcpy._flag_secure_done = True
                return True
            else:
                print(f"FLAG_SECURE bypass: unexpected result: {r.stdout} {r.stderr}")
        except Exception as e:
            print(f"FLAG_SECURE bypass failed: {e}")

        Scrcpy._flag_secure_done = True
        return False

    def push_server_to_device(self):
        if not os.path.isfile(SCRCPY_SERVER_PATH):
            print(
                f"Error: '{SCRCPY_SERVER_PATH}' not found in project root. "
                f"Download scrcpy-server v{SCRCPY_VERSION} and place it there."
            )
            return False

        local_size = os.path.getsize(SCRCPY_SERVER_PATH)
        check = _run(
            self._adb("shell", "stat", "-c", "%s", DEVICE_SERVER_PATH),
            capture_output=True,
            text=True,
        )
        remote = (check.stdout or "").strip()
        if check.returncode == 0 and remote.isdigit() and int(remote) == local_size:
            print("scrcpy-server.jar already on device (skip push)")
            return True

        print("Pushing scrcpy-server.jar to device...")
        result = _run(
            self._adb("push", SCRCPY_SERVER_PATH, DEVICE_SERVER_PATH),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"Error pushing server: {result.stderr}")
            return False
        return True

    def remove_adb_forward(self):
        try:
            _run(
                self._adb("forward", "--remove", f"tcp:{self.local_port}"),
                capture_output=True, text=True, timeout=3,
            )
        except Exception:
            pass
        self.forward_ready = False

    def setup_adb_forward(self):
        self.remove_adb_forward()
        print(f"Setting up ADB forward: tcp:{self.local_port} -> localabstract:scrcpy")
        _run(
            self._adb("forward", f"tcp:{self.local_port}", "localabstract:scrcpy"),
            check=True,
            capture_output=True,
            text=True,
        )
        self.forward_ready = True

    def start_server(self):
        print("Starting scrcpy server in background...")
        if self.audio_enabled:
            audio_param = "audio=true audio_codec=aac audio_bit_rate=384000"
        else:
            audio_param = "audio=false"
        cmd = self._adb(
            "shell",
            (
                f"CLASSPATH={DEVICE_SERVER_PATH} app_process / "
                f"com.genymobile.scrcpy.Server {SCRCPY_VERSION} "
                f"tunnel_forward=true {audio_param} "
                f"clipboard_autosync=false "
                f"power_off_on_close=false "
                # 不强制保持唤醒：连接期间也按手机自己的息屏时间自动熄屏/锁屏。
                # 注意：熄屏锁屏后 Android 11 会切断音频抓取（要求解锁+亮屏），
                # 届时声音会停，需在投屏里解锁手机才恢复。
                f"stay_awake=false "
                f"log_level=INFO "
                f"video_bit_rate={self.video_bit_rate} "
                f"max_size={self.max_size} "
                f"max_fps={self.max_fps} "
                f"video_codec_options=profile=1,level=256,i-frame-interval=1"
            ),
        )
        self.android_process = _popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        try:
            while not self.stop:
                line = self.android_process.stderr.readline()
                if not line:
                    break
                text = line.decode(errors="replace").strip()
                if text:
                    print(f"Server: {text}")
        except Exception as e:
            if not self.stop:
                print(f"Server log reader error: {e}")
        if self.android_process and self.android_process.poll() is None:
            try:
                self.android_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        print("Server stopped")

    def _connect_socket(self, label, wait_dummy=False):
        deadline = time.monotonic() + CONNECT_TIMEOUT_S
        last_error = None

        while not self.stop and time.monotonic() < deadline:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._prepare_socket(sock)
            try:
                sock.settimeout(1.0)
                sock.connect(("127.0.0.1", self.local_port))
            except OSError as e:
                last_error = e
                self._close_socket(sock)
                time.sleep(CONNECT_RETRY_INTERVAL_S)
                continue

            if not wait_dummy:
                sock.settimeout(None)
                print(f"{label} connection established")
                return sock

            try:
                while not self.stop and time.monotonic() < deadline:
                    remaining = max(0.05, deadline - time.monotonic())
                    sock.settimeout(min(1.0, remaining))
                    try:
                        dummy = sock.recv(1)
                    except socket.timeout:
                        continue
                    if not dummy:
                        raise ConnectionError("video socket closed before dummy byte")
                    sock.settimeout(None)
                    print(f"{label} connection established (dummy=0x{dummy[0]:02x})")
                    return sock
                last_error = TimeoutError("timed out waiting for dummy byte")
            except Exception as e:
                last_error = e

            self._close_socket(sock)
            time.sleep(CONNECT_RETRY_INTERVAL_S)

        raise ConnectionError(f"Failed to connect {label} socket: {last_error}")

    def receive_video_data(self):
        print("Receiving video data (H.264)...")
        try:
            while not self.stop:
                data = self._safe_recv(self.video_socket, 512 * 1024)
                if not data:
                    break
                callback = self.video_callback
                if callback:
                    callback(data)
        except Exception as e:
            if not self.stop:
                print(f"Video receive error: {e}")
        print("Video data reception stopped")

    def receive_audio_data(self):
        print("Receiving audio data (AAC)...")
        first = True
        try:
            while not self.stop:
                data = self._safe_recv(self.audio_socket, 64 * 1024)
                if not data:
                    break
                if first:
                    first = False
                    head = data[:16]
                    hex_head = " ".join(f"{b:02x}" for b in head)
                    codec = int.from_bytes(data[:4], "big") if len(data) >= 4 else -1
                    print(f"audio first chunk len={len(data)} codec=0x{codec:08x} head={hex_head}")
                    if codec == 0x6F707573:
                        print("WARNING: device sent OPUS, browser expects AAC — check audio_codec=aac")
                    elif codec == 0:
                        print("WARNING: audio stream disabled by device (codec id 0)")
                    elif codec == 1:
                        print("WARNING: audio configuration error on device (codec id 1)")
                callback = self.audio_callback
                if callback:
                    callback(data)
        except Exception as e:
            if not self.stop:
                print(f"Audio receive error: {e}")
        print("Audio data reception stopped")

    def handle_control_conn(self):
        print("Control connection established (idle)...")
        try:
            while not self.stop:
                data = self._safe_recv(self.control_socket, 1024)
                if not data:
                    break
        except Exception as e:
            if not self.stop:
                print(f"Control receive error: {e}")
        print("Control connection stopped")

    def scrcpy_start(
        self,
        video_callback,
        video_bit_rate,
        audio_enabled=False,
        audio_callback=None,
        max_size="800",
        max_fps="25",
    ):
        self.video_bit_rate = str(video_bit_rate)
        self.max_size = str(max_size)
        self.max_fps = str(max_fps)
        self.video_callback = video_callback
        self.audio_enabled = audio_enabled
        self.audio_callback = audio_callback
        self.stop = False

        try:
            if not check_adb():
                return False

            devices, raw = list_adb_devices()
            if not devices:
                print("No device found. Please connect your Android device via USB.")
                print(raw)
                return False

            if self.device_serial:
                if self.device_serial not in devices:
                    print(f"Device '{self.device_serial}' not found. Connected: {', '.join(devices)}")
                    return False
                print(f"Using device: {self.device_serial}")
            elif len(devices) > 1:
                print(f"Multiple devices connected ({', '.join(devices)}). "
                      f"Pass --device_serial or ?device_serial= to pick one.")
                return False
            else:
                self.device_serial = devices[0]
                print(f"Using device: {self.device_serial}")

            # 这三步互不依赖，但每步都是一次 adb 往返（实测 preclean≈380ms、
            # jar 检查≈134ms、flag_secure≈176ms）。串行做要 ~0.7s，并行只要最慢那一步。
            # 约束：三者都必须在 setup_adb_forward / 启动服务之前完成，所以在此 join。
            t0 = time.monotonic()
            with ThreadPoolExecutor(max_workers=3) as pool:
                f_preclean = pool.submit(preclean_connection, self.device_serial, self.local_port)
                f_push = pool.submit(self.push_server_to_device)
                f_secure = pool.submit(self.try_disable_flag_secure)
                pushed = f_push.result()
                f_preclean.result()
                f_secure.result()
            print(f"pre-start probes done in {int((time.monotonic() - t0) * 1000)}ms")

            if not pushed:
                print("Failed to push server files to device.")
                return False

            self.setup_adb_forward()
            self.android_thread = Thread(target=self.start_server, daemon=True)
            self.android_thread.start()

            self.video_socket = self._connect_socket("Video", wait_dummy=True)

            if self.audio_enabled:
                self.audio_socket = self._connect_socket("Audio")

            self.control_socket = self._connect_socket("Control")

            self.video_thread = Thread(target=self.receive_video_data, daemon=True)
            self.control_thread = Thread(target=self.handle_control_conn, daemon=True)
            self.video_thread.start()
            if self.audio_enabled:
                self.audio_thread = Thread(target=self.receive_audio_data, daemon=True)
                self.audio_thread.start()
            self.control_thread.start()
            print("Background tasks started")
            # 关屏会破坏 Android 11 的音频抓取（它要求设备解锁且在前台）。
            # 开启音频时一律保持亮屏，否则会没声音——不管启动器有没有传参。
            if self.turn_screen_off_on_connect and not self.audio_enabled:
                time.sleep(0.2)
                self.turn_screen_off()
            elif self.turn_screen_off_on_connect and self.audio_enabled:
                print("turn-screen-off skipped: audio enabled (Android 11 needs screen on for audio)")
            return True
        except Exception as e:
            print(f"scrcpy_start failed: {e}")
            self.scrcpy_stop()
            return False

    def scrcpy_stop(self):
        print("Stopping Scrcpy")
        self.stop = True

        video_sock = self.video_socket
        audio_sock = self.audio_socket
        control_sock = self.control_socket
        self.video_socket = None
        self.audio_socket = None
        self.control_socket = None

        if self.forward_ready:
            self.remove_adb_forward()

        self._close_socket(video_sock, shutdown=True)
        self._close_socket(audio_sock, shutdown=True)
        self._close_socket(control_sock, shutdown=True)

        if self.android_process and self.android_process.poll() is None:
            self.android_process.terminate()
            try:
                self.android_process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.android_process.kill()
                try:
                    self.android_process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass

        for thread in (self.video_thread, self.audio_thread, self.control_thread):
            if thread and thread.is_alive():
                thread.join(timeout=1)

        if self.android_thread and self.android_thread.is_alive():
            self.android_thread.join(timeout=1)

        self.video_thread = None
        self.audio_thread = None
        self.control_thread = None
        self.android_thread = None
        self.android_process = None
        self.audio_enabled = False
        self.audio_callback = None
        self.video_callback = None
        print("Scrcpy stopped")

    def send_lock_screen(self):
        try:
            result = _run(
                self._adb(
                    "shell",
                    "dumpsys power | grep -q mWakefulness=Awake && input keyevent 26 && echo LOCKED || echo ALREADY_OFF",
                ),
                capture_output=True, text=True, timeout=4,
            )
            out = (result.stdout or "").strip()
            if "LOCKED" in out:
                print("auto-lock: screen locked via ADB")
                return True
            elif "ALREADY_OFF" in out:
                print("auto-lock: screen already off")
                return True
            else:
                print(f"auto-lock: unexpected output: {out}")
        except subprocess.TimeoutExpired:
            print("auto-lock: ADB timed out")
        except Exception as e:
            print(f"auto-lock: ADB failed: {e}")
        return False

    def turn_screen_off(self):
        """
        Turn off the device display while keeping the video stream alive.
        Same idea as scrcpy --turn-screen-off (-S).
        """
        if self.stop:
            return False

        sock = self.control_socket
        if sock:
            try:
                with self.control_lock:
                    sock.sendall(bytes([CONTROL_MSG_SET_DISPLAY_POWER, 0]))
                print("turn-screen-off: device display off (mirroring continues)")
                return True
            except OSError as e:
                print(f"turn-screen-off: control message failed: {e}")

        try:
            result = _run(
                self._adb("shell", "cmd display power-off 0"),
                capture_output=True,
                text=True,
                timeout=4,
            )
            if result.returncode == 0:
                print("turn-screen-off: device display off via adb (mirroring continues)")
                return True
            print(f"turn-screen-off: adb fallback failed: {(result.stderr or result.stdout or '').strip()}")
        except subprocess.TimeoutExpired:
            print("turn-screen-off: adb fallback timed out")
        except Exception as e:
            print(f"turn-screen-off: adb fallback error: {e}")
        return False

    def scrcpy_send_control(self, data):
        if self.stop or not self.control_socket:
            return
        with self.control_lock:
            sock = self.control_socket
            if not sock:
                return
            try:
                sock.sendall(data)
            except OSError as e:
                if not self.stop:
                    print(f"Control send error: {e}")
