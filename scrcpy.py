from threading import Thread, Lock
import subprocess
import socket
import shutil
import os
import re
import time

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

# 视为“常驻/非消息”的通知标志位——微信/QQ 的“运行中”前台服务通知带这些位，
# 一律跳过，避免悬浮球一直红着。
_NOTIFY_FLAG_ONGOING = 0x00000002
_NOTIFY_FLAG_NO_CLEAR = 0x00000020
_NOTIFY_FLAG_FOREGROUND_SERVICE = 0x00000040
_NOTIFY_PERSISTENT_FLAGS = (
    _NOTIFY_FLAG_ONGOING | _NOTIFY_FLAG_NO_CLEAR | _NOTIFY_FLAG_FOREGROUND_SERVICE
)

# 一次 adb 往返同时取“前台 App”“屏幕是否亮着”“通知列表”，用标记分段。
# 屏幕状态给网页判断：手机睡着了就把伪装页盖回来，让用户可以点三下唤醒进 PIN。
# 这里用 deviceidle get screen（约 150ms）而不是 dumpsys power（约 330ms）+
# dumpsys trust（约 160ms）：每 5 秒一次，手机内存紧张时这点开销也值得省。
_NOTIFY_SHELL = (
    "echo @@FG@@; dumpsys window | grep mCurrentFocus; "
    "echo @@PW@@; dumpsys deviceidle get screen; "
    "echo @@NT@@; dumpsys notification --noredact | grep 'NotificationRecord('"
)

# 轻量缓存：多端/多次轮询时避免频繁执行 dumpsys（约几百毫秒）。
_notify_cache = {"at": 0.0, "serial": None, "state": None}
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
      - packages：当前有“真实消息通知”且不在前台的目标 App 列表
      - alert：packages 是否非空（悬浮球是否应变红）
      - foreground：当前前台 App 包名
    """
    serial = resolve_device_serial(device_serial)
    empty = {"ok": True, "alert": False, "packages": [], "foreground": None}
    if not serial:
        return {**empty, "ok": False}

    now = time.time()
    with _notify_cache_lock:
        c = _notify_cache
        if (c["state"] is not None and c["serial"] == serial
                and now - c["at"] < _NOTIFY_CACHE_TTL_S):
            return c["state"]

    try:
        result = _run(
            adb_cmd("shell", _NOTIFY_SHELL, device_serial=serial),
            capture_output=True, text=True, timeout=6,
        )
    except Exception as e:
        return {**empty, "ok": False, "error": str(e)}

    target_set = set(packages)
    section = None
    foreground = None
    alerting = []
    awake = None
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
        if section == "fg" and "mCurrentFocus" in line and foreground is None:
            foreground = _parse_foreground_package(line)
        elif section == "pw":
            if line in ("true", "false"):
                awake = line == "true"   # deviceidle get screen：屏幕是否亮着
        elif section == "nt" and line.startswith("NotificationRecord("):
            m = re.search(r"pkg=(\S+)", line)
            if not m or m.group(1) not in target_set:
                continue
            pkg = m.group(1)
            imp_m = re.search(r"importance=(-?\d+)", line)
            importance = int(imp_m.group(1)) if imp_m else 3
            fl_m = re.search(r"flags=0x([0-9a-fA-F]+)", line)
            flags = int(fl_m.group(1), 16) if fl_m else 0
            if importance <= 1:
                continue  # IMPORTANCE_MIN/NONE：不是真正的消息提醒
            if flags & _NOTIFY_PERSISTENT_FLAGS:
                continue  # 常驻/前台服务通知（如“微信运行中”），跳过
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
    }
    with _notify_cache_lock:
        _notify_cache.update(at=time.time(), serial=serial, state=state)
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

    if awake and not locked:
        return {"ok": True, "action": "none"}  # 正在用，别打扰

    try:
        _run(
            adb_cmd("shell", "input keyevent KEYCODE_WAKEUP", device_serial=serial),
            capture_output=True, text=True, timeout=5,
        )
        if not locked:
            print("wake: screen on (device was not locked)")
            return {"ok": True, "action": "woke"}

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
        return {"ok": True, "action": "woke+unlock-prompt"}
    except Exception as e:
        print(f"wake failed: {e}")
        return {"ok": False, "action": "wake-failed", "error": str(e)}


def preclean_connection(device_serial=None, local_port=None):
    """
    Scrub stale adb forward and device-side scrcpy server before a fresh connect.
    Safe to call repeatedly.
    """
    serial = resolve_device_serial(device_serial)
    port = _resolve_local_port(local_port)
    try:
        _run(
            adb_cmd("forward", "--remove", f"tcp:{port}", device_serial=serial),
            capture_output=True, text=True, timeout=3,
        )
    except Exception:
        pass
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
    time.sleep(0.12)


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

            preclean_connection(self.device_serial, self.local_port)

            if not self.push_server_to_device():
                print("Failed to push server files to device.")
                return False

            self.try_disable_flag_secure()
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
