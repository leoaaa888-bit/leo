# 同一服务器控制多台手机

本文说明如何在一台电脑上同时运行多个 Web-scrcpy 实例，实现 **一台手机 ↔ 一个服务进程 ↔ 一个 Web 端口**，各实例互不影响。

---

## 核心思路

```
┌─────────────────────────────────────────────────────────────┐
│                        同一台服务器（电脑）                    │
│                                                             │
│  手机 A ──USB──┐                                            │
│  手机 B ──USB──┼── USB 集线器 ── 电脑 USB 口                  │
│  手机 C ──USB──┘                                            │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐       │
│  │ app.py       │  │ app.py       │  │ app.py       │       │
│  │ 端口 5001    │  │ 端口 5002    │  │ 端口 5003    │       │
│  │ 设备 A       │  │ 设备 B       │  │ 设备 C       │       │
│  └──────────────┘  └──────────────┘  └──────────────┘       │
└─────────────────────────────────────────────────────────────┘

浏览器访问：
  http://服务器IP:5001  →  只控制手机 A
  http://服务器IP:5002  →  只控制手机 B
  http://服务器IP:5003  →  只控制手机 C
```

| 维度 | 做法 |
|------|------|
| 进程 | 每台手机单独起一个 `python app.py` 进程 |
| Web 端口 | 每台手机用不同 `--port`（如 5001、5002、5003） |
| 设备绑定 | 每台手机用不同 `--device_serial`（ADB 序列号） |
| 会话隔离 | 每个进程只维护一个 `active_session`，互不相干 |

---

## USB 怎么连多台手机？

### 可以，用 USB 集线器（HUB）

多台手机都通过 **USB 数据线** 连到电脑，常见接法：

```
手机1 ──┐
手机2 ──┼── 有源 USB 3.0 HUB ── USB 线 ── 电脑
手机3 ──┘
```

### 推荐设备

| 类型 | 说明 |
|------|------|
| **有源 USB 3.0 集线器** | 自带电源适配器，多口同时供电，最稳妥 |
| 普通无源 HUB | 仅适合 1～2 台；多台同时充电+传数据容易供电不足、掉线 |
| 工业级 USB HUB | 机房/工作室批量挂机时常用，稳定性更好 |

### 选购与布线注意

1. **必须开 USB 调试**，每台手机首次连接都要点「允许调试」。
2. **用数据线**，不要用只能充电的线。
3. **优先 USB 3.0** 口和线，带宽更高，多路投屏更稳。
4. **有源 HUB 优先**：手机充电+传数据功耗大，无源 HUB 容易反复断连。
5. **单条 USB 链路不宜挂太多台**：实践上一条控制器挂 **4～7 台** 较常见；更多台建议 **多个 HUB 分到电脑不同 USB 控制器**（机箱前后口、不同 PCIe USB 卡）。
6. Windows 可在「设备管理器」里看到多台 `ADB Interface` / 便携设备。

### 不用全插 USB 的替代方案：无线 ADB

手机与电脑在同一局域网时，可先 USB 配对一次，再切 Wi-Fi：

```bash
# 在已 USB 连接的手机上执行（示例 IP）
adb -s <序列号> tcpip 5555
adb connect 192.168.1.101:5555
adb connect 192.168.1.102:5555
```

之后 `adb devices` 里会出现：

```
192.168.1.101:5555    device
192.168.1.102:5555    device
```

无线方案适合手机已固定位置、不想拉很多 USB 线的场景；**稳定性和延迟通常不如 USB**。

---

## 第一步：查看每台手机的序列号

所有手机连好后执行：

```bash
adb devices
```

示例输出：

```
List of devices attached
R58M90ABCDE    device
R58N12FGHIJ    device
emulator-5554  device
```

记下每台对应的 **序列号（Serial）**，后面启动服务时要用。

也可访问任意已运行实例的接口（需该端口已启动）：

```
GET http://localhost:5001/api/devices
```

---

## 第二步：每台手机启动一个独立服务

### 基本命令模板

```bash
python app.py --device_serial <序列号> --port <Web端口> --local_port <ADB本地端口> [--https]
```

> **单机多实例时**：`--local_port` 必须每台不同（默认 `27183`）。`--port` 是浏览器访问端口，`--local_port` 是电脑本机 ADB 转发端口，两者独立。

### 三台手机示例

**终端 1 — 手机 A**

```bash
python app.py --device_serial R58M90ABCDE --port 5001 --local_port 27183 --https
```

**终端 2 — 手机 B**

```bash
python app.py --device_serial R58N12FGHIJ --port 5002 --local_port 27184
```

**终端 3 — 手机 C**

```bash
python app.py --device_serial emulator-5554 --port 5003 --local_port 27185
```

### 访问地址

| 手机 | 序列号 | Web 端口 | ADB 本地端口 | 访问地址 |
|------|--------|----------|--------------|----------|
| A | R58M90ABCDE | 5001 | 27183 | `http://<服务器IP>:5001` |
| B | R58N12FGHIJ | 5002 | 27184 | `http://<服务器IP>:5002` |
| C | emulator-5554 | 5003 | 27185 | `http://<服务器IP>:5003` |

每个浏览器标签页打开不同端口，即控制不同手机，**互不干扰**。

---

## Windows 批量启动脚本

将下面内容保存为 `start_multi.bat`，按实际序列号修改：

```bat
@echo off
cd /d %~dp0

start "web-scrcpy-A" cmd /k python app.py --device_serial R58M90ABCDE --port 5001 --local_port 27183 --https
start "web-scrcpy-B" cmd /k python app.py --device_serial R58N12FGHIJ --port 5002 --local_port 27184
start "web-scrcpy-C" cmd /k python app.py --device_serial R58M12KLMNO --port 5003 --local_port 27185

echo 已启动，请分别访问 :
echo   http://localhost:5001
echo   http://localhost:5002
echo   http://localhost:5003
pause
```

双击后会打开 3 个独立命令行窗口，每个窗口一个进程。

---

## Linux systemd 多实例（可选）

适合服务器长期开机、自动拉起。为每台手机写一个 service 文件。

`/etc/systemd/system/web-scrcpy@.service`：

```ini
[Unit]
Description=Web-scrcpy instance %i
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/web-scrcpy
ExecStart=/usr/bin/python3 app.py --device_serial %i --port %PORT%
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

更简单的做法是 **每台手机一个独立 unit 文件**，例如 `web-scrcpy-phone-a.service`：

```ini
[Service]
WorkingDirectory=/opt/web-scrcpy
ExecStart=/usr/bin/python3 app.py --device_serial R58M90ABCDE --port 5001 --local_port 27183
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now web-scrcpy-phone-a
sudo systemctl enable --now web-scrcpy-phone-b
```

---

## 用 Nginx 统一入口（可选）

若不想记多个端口，可用反向代理按路径或子域名分发：

```nginx
# 按路径：/phone-a/ /phone-b/
location /phone-a/ {
    proxy_pass http://127.0.0.1:5001/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
}

location /phone-b/ {
    proxy_pass http://127.0.0.1:5002/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
}
```

> WebSocket 必须配置 `Upgrade` / `Connection`，否则画面连不上。

---

## ADB 本地转发端口（`--local_port`）

每台手机连接时，服务端会在 **电脑本机** 上占用一个 TCP 端口做 ADB 转发：

```bash
adb -s <序列号> forward tcp:<local_port> localabstract:scrcpy
```

默认 `--local_port` 为 `27183`（单实例无需修改）。

**同一台电脑并行多个实例时**，每个进程必须使用 **不同的 `--local_port`**，否则会争抢 `127.0.0.1` 上的同一端口，导致只有一台能正常工作。

### 推荐端口规划

| 手机 | `--port`（浏览器） | `--local_port`（ADB 转发） |
|------|-------------------|----------------------------|
| A | 5001 | 27183 |
| B | 5002 | 27184 |
| C | 5003 | 27185 |
| D | 5004 | 27186 |

启动日志会打印当前实例使用的 `local_port`，WebSocket 会话 JSON 里也会包含 `local_port` 字段便于排查。

### 约束

- 有效范围：`1024`～`65535`
- 不可使用 `5555`（无线 ADB 常用端口）
- 只需在 **防火墙放行 Web 端口**（如 5001）；`local_port` 仅本机 `127.0.0.1` 使用，无需对外暴露

---

## 资源与性能参考

同时跑 N 个实例时，电脑资源大约按 N 倍增长：

| 资源 | 单台大致占用 | 说明 |
|------|--------------|------|
| CPU | 10%～30% | 与画质预设、帧率有关 |
| 内存 | 100～300 MB / 进程 | Python + 解码缓冲 |
| 网络 | 1～4 Mbps / 路 | 清晰模式更高 |
| USB 带宽 | 共享控制器 | 多路 1080p 建议分流到多个 USB 控制器 |

建议：

- 批量挂机用 **「流畅」或「均衡」** 预设，减轻 CPU 与带宽压力。
- 监控 `adb devices` 是否偶发 `offline`，掉线多半是线材、供电或 HUB 问题。

---

## 防火墙与端口

若从局域网其他电脑访问，需放行对应 TCP 端口：

**Windows 防火墙**：入站规则放行 `5001`、`5002`、`5003` …

**Linux（ufw）**：

```bash
sudo ufw allow 5001/tcp
sudo ufw allow 5002/tcp
sudo ufw allow 5003/tcp
```

---

## 常见问题

### Q：多台手机插上去后 `adb devices` 只显示一台？

- 换 **有源 USB 3.0 HUB**
- 换 **数据线**
- 在手机上重新授权 USB 调试
- 执行 `adb kill-server && adb start-server`

### Q：某台能连，另一台一直「连接失败」？

- 确认该实例的 `--device_serial` 与 `adb devices` 中一致
- 确认 `--port` 没被其他程序占用
- 若多实例并行，确认每台 `--local_port` 互不重复

### Q：一个浏览器能同时开多个手机吗？

可以。开多个标签页，分别访问 `5001`、`5002`、`5003` 即可，互不影响。

### Q：一个 `app.py` 能同时服务多台手机吗？

**当前架构不支持。** 一个进程只绑定一台设备、一个 `active_session`。要「一机一服务一端口」，请起多个进程。

### Q：能不能一个端口里下拉选手机？

前端目前未做设备选择器；多机场景请用 **多端口 + 多进程**。后续可在同一页面加设备列表，但底层仍建议每设备独立会话。

---

## 快速检查清单

部署多机前逐项确认：

- [ ] 每台手机序列号已从 `adb devices` 抄录
- [ ] 每台手机对应唯一 `--port`
- [ ] 若多进程并行：每台对应唯一 `--local_port`（如 27183、27184…）
- [ ] USB 使用有源 HUB，线材可靠
- [ ] 项目根目录有 `adb.exe` 与 `scrcpy-server`（v3.3.3）
- [ ] 防火墙已放行 Web 端口
- [ ] 浏览器分别访问不同端口验证画面

---

## 相关文档

- [README.md](../README.md) — 项目总览与单机构建
- `GET /api/devices` — 查询当前 ADB 已连接设备列表
