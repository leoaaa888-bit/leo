"""
一键多机启动器 (start_multi.py)

自动检测当前通过 USB / 无线 ADB 连接的所有 Android 设备，为每台分配独立的
浏览器端口与 ADB 转发端口，并各启动一个 app.py 进程（各自独立窗口）。

用法：
    python start_multi.py            # 检测并启动全部设备
    python start_multi.py --https    # 以 HTTPS 启动
    python start_multi.py --dry-run  # 只打印分配方案，不真正启动

端口分配（可在下方 CONFIG 区修改起始值）：
    手机1 -> web 5001 / local 27183
    手机2 -> web 5002 / local 27184
    ...
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys

# 让中文在任意控制台代码页下都能正常输出，不因 GBK 编码报错。
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ADB_PATH = os.path.join(BASE_DIR, "adb.exe")
APP_PATH = os.path.join(BASE_DIR, "app.py")
LOG_DIR = os.path.join(BASE_DIR, "logs")

# —— CONFIG：按需修改 ——
BASE_WEB_PORT = 5001       # 第一台手机的浏览器端口，其余依次 +1
BASE_LOCAL_PORT = 27183    # 第一台手机的 ADB 转发端口，其余依次 +1
# 传给每个 app.py 的额外参数。
# 保持手机亮屏且不自动锁屏 —— Android 11 音频抓取要求设备处于解锁亮屏状态，
# 否则会没声音。这两个参数与能正常出声的 app-start.bat 保持一致。
EXTRA_ARGS: list[str] = ["--no-turn_screen_off", "--no-auto_lock"]


CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def list_devices() -> list[str]:
    """返回 adb devices 中状态为 device 的序列号列表。"""
    try:
        result = subprocess.run(
            [ADB_PATH, "devices"], capture_output=True, text=True, timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
    except FileNotFoundError:
        print(f"错误：找不到 adb（{ADB_PATH}）")
        return []
    devices = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = re.split(r"\s+", line)
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices


def primary_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def port_in_use(port: int) -> bool:
    """判断本机某 TCP 端口是否已被占用（用于跳过已在运行的实例）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def safe_name(serial: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", serial)


def main() -> int:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    use_https = "--https" in args
    hidden = "--hidden" in args
    passthrough = [a for a in args if a not in ("--dry-run", "--https", "--hidden")]

    if not os.path.isfile(APP_PATH):
        print(f"错误：找不到 app.py（{APP_PATH}）")
        return 1

    devices = list_devices()
    if not devices:
        print("未检测到已授权的设备。请确认：")
        print("  1. 手机已用数据线连接（或已无线 ADB 配对）")
        print("  2. 手机上已点『允许 USB 调试』")
        print("  3. 运行 adb devices 能看到 device 状态")
        return 1

    lan_ip = primary_lan_ip()
    scheme = "https" if use_https else "http"

    plan = []
    for i, serial in enumerate(devices):
        plan.append(
            {
                "serial": serial,
                "web_port": BASE_WEB_PORT + i,
                "local_port": BASE_LOCAL_PORT + i,
            }
        )

    print(f"检测到 {len(devices)} 台设备，分配方案：")
    print("-" * 68)
    print(f"{'序列号':<24}{'浏览器端口':<12}{'ADB端口':<10}访问地址")
    print("-" * 68)
    for p in plan:
        url = f"{scheme}://{lan_ip}:{p['web_port']}"
        print(f"{p['serial']:<24}{p['web_port']:<14}{p['local_port']:<12}{url}")
    print("-" * 68)

    if dry_run:
        print("（--dry-run：仅显示方案，未启动任何进程）")
        return 0

    https_args = ["--https"] if use_https else []

    # 隐藏模式：用 pythonw 无窗口启动，日志写到 logs/<序列号>.log。
    if hidden:
        os.makedirs(LOG_DIR, exist_ok=True)
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        python_exe = pyw if os.path.isfile(pyw) else sys.executable
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        python_exe = sys.executable
        no_window = None

    launched = 0
    skipped = 0
    for p in plan:
        # 端口已占用 = 该实例大概率已在运行，跳过以免重复启动。
        if port_in_use(p["web_port"]):
            skipped += 1
            print(f"跳过（端口 {p['web_port']} 已在运行）：{p['serial']}")
            continue

        cmd = [
            python_exe,
            APP_PATH,
            "--device_serial", p["serial"],
            "--port", str(p["web_port"]),
            "--local_port", str(p["local_port"]),
            *https_args,
            *EXTRA_ARGS,
            *passthrough,
        ]
        try:
            if hidden:
                log_path = os.path.join(LOG_DIR, safe_name(p["serial"]) + ".log")
                logf = open(log_path, "a", encoding="utf-8", errors="replace")
                subprocess.Popen(
                    cmd, cwd=BASE_DIR, stdout=logf, stderr=subprocess.STDOUT,
                    creationflags=no_window,
                )
            else:
                # 每个实例独立控制台窗口，日志分开，便于排查。
                subprocess.Popen(
                    cmd, cwd=BASE_DIR,
                    creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                )
            launched += 1
            print(f"已启动：{p['serial']} -> {scheme}://{lan_ip}:{p['web_port']}")
        except Exception as e:
            print(f"启动失败 {p['serial']}: {e}")

    print("-" * 68)
    print(f"共启动 {launched}/{len(plan)} 个实例"
          + (f"，跳过 {skipped} 个已运行" if skipped else "")
          + ("（隐藏后台运行，日志在 logs 文件夹）" if hidden else "（各在独立窗口运行）"))
    print("局域网/ZeroTier 访问时，请在防火墙放行对应的浏览器端口。")
    if not hidden:
        print("关闭某台：直接关掉它对应的命令行窗口即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
