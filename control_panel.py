"""
手机投屏控制面板 (control_panel.py)

一个图形界面，整合所有操作：
  - 自动检测已连接手机，显示型号 / 序列号 / 端口 / 状态（不显示屏幕画面）
  - 一键：全部启动 / 全部停止 / 启动选中 / 停止选中 / 打开网页
  - 开机自启开关
  - 点某台设备，下方实时显示它的连接日志

投屏本身仍由已安装的 Python 运行 app.py（控制面板只负责管理）。
用 PyInstaller 打包成 exe：pyinstaller --onefile --windowed control_panel.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import ttk, scrolledtext, messagebox

try:
    import pystray
    from PIL import Image, ImageDraw
    _TRAY_OK = True
except Exception:
    _TRAY_OK = False

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = base_dir()
ADB_PATH = os.path.join(BASE_DIR, "adb.exe")
APP_PATH = os.path.join(BASE_DIR, "app.py")
LOG_DIR = os.path.join(BASE_DIR, "logs")
PORT_MAP_FILE = os.path.join(BASE_DIR, "port_map.json")

BASE_WEB_PORT = 5001
BASE_LOCAL_PORT = 27183
STARTUP_LNK_NAME = "手机投屏控制面板.lnk"


def find_python() -> str:
    for name in ("pythonw.exe", "pythonw", "python.exe", "python"):
        p = shutil.which(name)
        if p:
            return p
    return "python"


PYTHON_EXE = find_python()


def run_hidden(args, **kwargs):
    kwargs.setdefault("creationflags", CREATE_NO_WINDOW)
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(args, **kwargs)


def safe_name(serial: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9._-]", "_", serial)


def primary_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


# 虚拟网卡的地址无法用来访问，列出来只是噪音，直接跳过。
_SKIP_ADAPTERS = ("vmware", "virtualbox", "vethernet", "hyper-v", "loopback", "bluetooth")


def _label_for(adapter: str, ip: str) -> str:
    """给网卡起个好认的名字：Tailscale / ZeroTier / 局域网。"""
    name = adapter.lower()
    if "tailscale" in name:
        return "Tailscale"
    if "zerotier" in name:
        return "ZeroTier"
    # Tailscale 用 100.64.0.0/10（CGNAT 段），网卡名认不出来时按网段兜底
    try:
        a, b = ip.split(".")[:2]
        if a == "100" and 64 <= int(b) <= 127:
            return "Tailscale"
    except (ValueError, IndexError):
        pass
    return "局域网"


def list_access_ips() -> list[tuple[str, str]]:
    """
    列出本机所有可用于访问的 IPv4（含 Tailscale / ZeroTier / 局域网）。
    返回 [(标签, IP), ...]，按 Tailscale → ZeroTier → 局域网 排序，
    这样“用哪个网络访问就能在面板里看到哪个地址”。
    """
    found: list[tuple[str, str]] = []
    try:
        r = run_hidden(["ipconfig"], timeout=6)
        out = r.stdout or ""
    except Exception:
        out = ""

    adapter = ""
    for raw in out.splitlines():
        if raw.strip() and not raw[0].isspace():
            adapter = raw.strip().rstrip(":").strip()   # 适配器标题行
            continue
        if adapter and "IPv4" in raw:
            if any(s in adapter.lower() for s in _SKIP_ADAPTERS):
                continue
            m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", raw)
            if m:
                ip = m.group(1)
                if not ip.startswith(("127.", "169.254.")):
                    found.append((_label_for(adapter, ip), ip))

    if not found:                      # ipconfig 解析失败时的兜底
        found = [("局域网", primary_lan_ip())]

    order = {"Tailscale": 0, "ZeroTier": 1, "局域网": 2}
    # 同一网卡可能有多个地址，去重后排序
    uniq = list(dict.fromkeys(found))
    uniq.sort(key=lambda t: order.get(t[0], 9))
    return uniq


def port_in_use(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.25)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def list_adb_serials() -> list[str]:
    try:
        r = run_hidden([ADB_PATH, "devices"], timeout=10)
    except Exception:
        return []
    serials = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def get_model(serial: str) -> str:
    try:
        r = run_hidden(
            [ADB_PATH, "-s", serial, "shell", "getprop", "ro.product.model"],
            timeout=6,
        )
        model = (r.stdout or "").strip()
        return model or "未知型号"
    except Exception:
        return "未知型号"


class PortMap:
    """给每台手机分配固定端口，序列号→端口，持久化到 json，保证重启后不变。"""

    def __init__(self):
        self.web = {}
        self.local = {}
        self._load()

    def _load(self):
        try:
            with open(PORT_MAP_FILE, encoding="utf-8") as f:
                data = json.load(f)
            self.web = {k: int(v) for k, v in data.get("web", {}).items()}
            self.local = {k: int(v) for k, v in data.get("local", {}).items()}
        except Exception:
            self.web, self.local = {}, {}

    def _save(self):
        try:
            with open(PORT_MAP_FILE, "w", encoding="utf-8") as f:
                json.dump({"web": self.web, "local": self.local}, f, indent=2)
        except Exception:
            pass

    def assign(self, serial: str) -> tuple[int, int]:
        if serial in self.web:
            return self.web[serial], self.local[serial]
        used_web = set(self.web.values())
        used_local = set(self.local.values())
        wp = BASE_WEB_PORT
        while wp in used_web:
            wp += 1
        lp = BASE_LOCAL_PORT
        while lp in used_local or lp == 5555:
            lp += 1
        self.web[serial] = wp
        self.local[serial] = lp
        self._save()
        return wp, lp


def startup_folder() -> str:
    return os.path.join(
        os.environ.get("APPDATA", ""),
        "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
    )


def autostart_lnk_path() -> str:
    return os.path.join(startup_folder(), STARTUP_LNK_NAME)


def autostart_enabled() -> bool:
    return os.path.isfile(autostart_lnk_path())


def set_autostart(enabled: bool) -> bool:
    lnk = autostart_lnk_path()
    if not enabled:
        try:
            if os.path.isfile(lnk):
                os.remove(lnk)
            return True
        except Exception:
            return False
    # 创建快捷方式：开机自动打开控制面板并自动启动全部
    if getattr(sys, "frozen", False):
        target = sys.executable
        arguments = "--autostart"
    else:
        target = PYTHON_EXE
        arguments = '"{}" --autostart'.format(APP_PATH.replace("app.py", "control_panel.py"))
    ps = (
        "$s=New-Object -ComObject WScript.Shell;"
        "$l=$s.CreateShortcut('{lnk}');"
        "$l.TargetPath='{target}';"
        "$l.Arguments='{args}';"
        "$l.WorkingDirectory='{wd}';"
        "$l.Save()"
    ).format(lnk=lnk, target=target, args=arguments, wd=BASE_DIR)
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            creationflags=CREATE_NO_WINDOW, capture_output=True, text=True, timeout=15,
        )
        return os.path.isfile(lnk)
    except Exception:
        return False


class ControlPanel:
    POLL_MS = 2500

    def __init__(self, root: tk.Tk, autostart: bool = False):
        self.root = root
        self.portmap = PortMap()
        # 所有可用访问地址（Tailscale / ZeroTier / 局域网），随轮询刷新。
        self.access_ips = list_access_ips()
        self.lock = threading.Lock()
        # serial -> {model, web_port, local_port, running}
        self.devices = {}
        self.procs = {}          # serial -> Popen
        self.model_cache = {}    # serial -> model
        self.selected_serial = None
        self._log_pos = 0
        self._log_path = None
        self.tray = None
        self._quitting = False

        os.makedirs(LOG_DIR, exist_ok=True)
        self._build_ui()
        self._setup_tray()
        # 关闭窗口（点 X）不退出，而是缩到系统托盘（任务栏不再显示）。
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self._start_poller()
        self.root.after(500, self._refresh_ui)
        self.root.after(800, self._tail_log)
        if autostart:
            # 开机自启：直接缩到托盘（不弹窗口），并自动启动全部手机。
            # 刚开机时 adb server 还没起来、USB 设备也可能还没被 Windows 枚举完，
            # 单次 start_all 很容易扑空。改为反复重试，直到所有已检测到的设备都启动。
            self.root.after(200, self.hide_to_tray)
            self._autostart_attempts = 0
            self.root.after(3000, self._autostart_tick)

    def _autostart_tick(self):
        self._autostart_attempts += 1
        self.start_all()
        with self.lock:
            n_total = len(self.devices)
            n_running = sum(1 for d in self.devices.values() if d["running"])
        # 最多重试 ~3 分钟（36 次 x 5s），覆盖开机后 adb/USB 逐步就绪的情况。
        if self._autostart_attempts < 36 and (n_total == 0 or n_running < n_total):
            self.root.after(5000, self._autostart_tick)

    # —— 系统托盘 ——
    def _tray_image(self):
        ico = os.path.join(BASE_DIR, "static", "icons", "favicon.ico")
        try:
            if os.path.isfile(ico):
                return Image.open(ico)
        except Exception:
            pass
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((6, 6, 58, 58), fill=(61, 220, 132, 255))
        d.rectangle((26, 20, 38, 44), fill=(7, 20, 12, 255))
        return img

    def _setup_tray(self):
        if not _TRAY_OK:
            return
        menu = pystray.Menu(
            pystray.MenuItem("显示面板", self._tray_show, default=True),
            pystray.MenuItem("全部启动", self._tray_start_all),
            pystray.MenuItem("全部停止", self._tray_stop_all),
            pystray.MenuItem("退出", self._tray_quit),
        )
        self.tray = pystray.Icon("scrcpy_panel", self._tray_image(),
                                 "手机投屏控制面板", menu)
        threading.Thread(target=self.tray.run, daemon=True).start()

    def _tray_show(self, *a):
        self.root.after(0, self.show_window)

    def _tray_start_all(self, *a):
        self.root.after(0, self.start_all)

    def _tray_stop_all(self, *a):
        self.root.after(0, self.stop_all)

    def _tray_quit(self, *a):
        self.root.after(0, self.quit_app)

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except Exception:
            pass

    def hide_to_tray(self):
        if _TRAY_OK and self.tray:
            self.root.withdraw()   # 从任务栏移除，仅托盘图标可见
        else:
            self.root.iconify()

    def quit_app(self):
        self._quitting = True
        self._poll_stop = True
        try:
            if self.tray:
                self.tray.stop()
        except Exception:
            pass
        try:
            self.stop_all()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    # —— UI ——
    def _build_ui(self):
        self.root.title("手机投屏控制面板")
        self.root.geometry("860x600")
        self.root.minsize(720, 480)

        top = ttk.Frame(self.root, padding=(10, 8))
        top.pack(fill="x")

        ttk.Button(top, text="全部启动", command=self.start_all).pack(side="left")
        ttk.Button(top, text="全部停止", command=self.stop_all).pack(side="left", padx=(6, 0))
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Button(top, text="启动选中", command=self.start_selected).pack(side="left")
        ttk.Button(top, text="停止选中", command=self.stop_selected).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="打开网页", command=self.open_selected_web).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="刷新", command=self._poll_once_async).pack(side="left", padx=(6, 0))

        self.autostart_var = tk.BooleanVar(value=autostart_enabled())
        ttk.Checkbutton(
            top, text="开机自启", variable=self.autostart_var,
            command=self._toggle_autostart,
        ).pack(side="right")

        mid = ttk.Frame(self.root, padding=(10, 0))
        mid.pack(fill="both", expand=True)

        cols = ("model", "serial", "port", "status", "url")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=8)
        for c, txt, w in [
            ("model", "型号", 150), ("serial", "序列号", 160),
            ("port", "端口", 60), ("status", "状态", 80), ("url", "访问地址", 200),
        ]:
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True, side="left")
        self.tree.tag_configure("running", foreground="#1a7f3d")
        self.tree.tag_configure("stopped", foreground="#888888")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        sb.pack(side="left", fill="y")
        self.tree.configure(yscrollcommand=sb.set)

        logframe = ttk.LabelFrame(self.root, text="日志（点上方设备查看）", padding=(8, 4))
        logframe.pack(fill="both", expand=True, padx=10, pady=(6, 10))
        self.logbox = scrolledtext.ScrolledText(
            logframe, height=10, wrap="word", font=("Consolas", 9),
            bg="#0e1014", fg="#d8dee9", state="disabled",
        )
        self.logbox.pack(fill="both", expand=True)

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.status_var, anchor="w",
                  relief="sunken", padding=(8, 2)).pack(fill="x", side="bottom")

    # —— 设备轮询（后台线程）——
    def _start_poller(self):
        self._poll_stop = False
        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()

    def _poll_loop(self):
        while not self._poll_stop:
            self._poll_once()
            time.sleep(self.POLL_MS / 1000.0)

    def _poll_once_async(self):
        threading.Thread(target=self._poll_once, daemon=True).start()

    def _poll_once(self):
        # 网络可能中途接上/断开（比如切 Tailscale），每轮重新识别可用地址。
        self.access_ips = list_access_ips()
        serials = list_adb_serials()
        snapshot = {}
        for s in serials:
            if s not in self.model_cache:
                self.model_cache[s] = get_model(s)
            wp, lp = self.portmap.assign(s)
            running = port_in_use(wp)
            snapshot[s] = {
                "model": self.model_cache[s],
                "web_port": wp,
                "local_port": lp,
                "running": running,
            }
        with self.lock:
            self.devices = snapshot

    # —— UI 刷新 ——
    def _refresh_ui(self):
        with self.lock:
            devices = dict(self.devices)
        existing = set(self.tree.get_children())
        seen = set()
        ips = list(self.access_ips)
        # 首选地址：Tailscale > ZeroTier > 局域网（list_access_ips 已按此排序）
        primary_ip = ips[0][1] if ips else "127.0.0.1"
        for serial, d in sorted(devices.items(), key=lambda kv: kv[1]["web_port"]):
            seen.add(serial)
            url = "http://{}:{}".format(primary_ip, d["web_port"])
            status = "运行中" if d["running"] else "未启动"
            tag = "running" if d["running"] else "stopped"
            values = (d["model"], serial, d["web_port"], status, url)
            if serial in existing:
                self.tree.item(serial, values=values, tags=(tag,))
            else:
                self.tree.insert("", "end", iid=serial, values=values, tags=(tag,))
        for iid in existing - seen:
            self.tree.delete(iid)

        n = len(devices)
        running = sum(1 for d in devices.values() if d["running"])
        # 把所有可用访问地址都列出来，用哪个网络就照着哪个填。
        addr_text = " · ".join("{} {}".format(lab, ip) for lab, ip in ips) or "无可用地址"
        self.status_var.set(
            "检测到 {} 台设备，运行中 {} 台   |   {}".format(n, running, addr_text)
        )
        self.root.after(1500, self._refresh_ui)

    def _on_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        serial = sel[0]
        if serial != self.selected_serial:
            self.selected_serial = serial
            self._log_path = os.path.join(LOG_DIR, safe_name(serial) + ".log")
            self._log_pos = 0
            self._set_log("", replace=True)

    # —— 启动 / 停止 ——
    def _launch(self, serial, wp, lp):
        if port_in_use(wp):
            return False
        log_path = os.path.join(LOG_DIR, safe_name(serial) + ".log")
        try:
            logf = open(log_path, "w", encoding="utf-8", errors="replace")
        except Exception:
            logf = subprocess.DEVNULL
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        cmd = [
            PYTHON_EXE, "-u", APP_PATH,
            "--device_serial", serial,
            "--port", str(wp),
            "--local_port", str(lp),
            # Android 11 音频抓取要求设备解锁亮屏，否则没声音。保持亮屏、不锁屏。
            "--no-turn_screen_off",
            "--no-auto_lock",
        ]
        try:
            proc = subprocess.Popen(
                cmd, cwd=BASE_DIR, stdout=logf, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW, env=env,
            )
            self.procs[serial] = proc
            return True
        except Exception as e:
            messagebox.showerror("启动失败", "{}\n{}".format(serial, e))
            return False

    def start_all(self):
        with self.lock:
            devices = dict(self.devices)
        started = 0
        for serial, d in devices.items():
            if not d["running"] and self._launch(serial, d["web_port"], d["local_port"]):
                started += 1
        self.status_var.set("已启动 {} 台".format(started))
        self._poll_once_async()

    def start_selected(self):
        serial = self.selected_serial
        if not serial:
            return
        with self.lock:
            d = self.devices.get(serial)
        if d and not d["running"]:
            self._launch(serial, d["web_port"], d["local_port"])
            self._poll_once_async()

    def _kill_by_port(self, wp):
        # 兜底：按端口找 python app.py 进程结束
        ps = (
            "Get-CimInstance Win32_Process | "
            "Where-Object {{ $_.Name -match 'python' -and $_.CommandLine -match '--port {port}' }} | "
            "ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}"
        ).format(port=wp)
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                creationflags=CREATE_NO_WINDOW, capture_output=True, text=True, timeout=10,
            )
        except Exception:
            pass

    def _stop(self, serial, wp):
        proc = self.procs.pop(serial, None)
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        self._kill_by_port(wp)

    def stop_all(self):
        with self.lock:
            devices = dict(self.devices)
        for serial, d in devices.items():
            self._stop(serial, d["web_port"])
        self.status_var.set("已停止全部")
        self._poll_once_async()

    def stop_selected(self):
        serial = self.selected_serial
        if not serial:
            return
        with self.lock:
            d = self.devices.get(serial)
        if d:
            self._stop(serial, d["web_port"])
            self._poll_once_async()

    def open_selected_web(self):
        serial = self.selected_serial
        if not serial:
            return
        with self.lock:
            d = self.devices.get(serial)
        if d:
            webbrowser.open("http://localhost:{}".format(d["web_port"]))

    # —— 日志 ——
    def _set_log(self, text, replace=False):
        self.logbox.configure(state="normal")
        if replace:
            self.logbox.delete("1.0", "end")
        if text:
            self.logbox.insert("end", text)
            self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def _tail_log(self):
        path = self._log_path
        if path and os.path.isfile(path):
            try:
                size = os.path.getsize(path)
                if size < self._log_pos:
                    self._log_pos = 0
                    self._set_log("", replace=True)
                if size > self._log_pos:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(self._log_pos)
                        chunk = f.read()
                        self._log_pos = f.tell()
                    self._set_log(chunk)
            except Exception:
                pass
        self.root.after(1000, self._tail_log)

    def _toggle_autostart(self):
        want = self.autostart_var.get()
        ok = set_autostart(want)
        if ok:
            self.status_var.set("开机自启已" + ("开启" if want else "关闭"))
        else:
            self.autostart_var.set(autostart_enabled())
            messagebox.showwarning("开机自启", "设置失败，请重试。")


def main():
    autostart = "--autostart" in sys.argv[1:]
    if not os.path.isfile(ADB_PATH):
        root = tk.Tk(); root.withdraw()
        messagebox.showerror("缺少文件", "找不到 adb.exe：\n{}".format(ADB_PATH))
        return
    if not os.path.isfile(APP_PATH):
        root = tk.Tk(); root.withdraw()
        messagebox.showerror("缺少文件", "找不到 app.py：\n{}".format(APP_PATH))
        return
    root = tk.Tk()
    ControlPanel(root, autostart=autostart)
    root.mainloop()


if __name__ == "__main__":
    main()
