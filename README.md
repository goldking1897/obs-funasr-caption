# OBS 阿里云 Qwen-Audio 实时字幕

在 OBS 里用「浏览器源」显示实时字幕，音频由本机的 Python 服务采集后，通过
WebSocket 流式发送到阿里云百炼的 **Qwen-Audio-3.0-ASR-Flash-Streaming**，
识别结果再通过 SSE 推回 OBS 页面。

默认按 **1920×1080（1080p）** 画布、**两行**字幕配置，字号 64px。也兼容
`fun-asr-realtime` 模型，只需在控制台改模型名。

适合直播、录屏、会议、网课等场景。支持采集**系统声音**（扬声器回环）或
**麦克风**。

```
扬声器/麦克风
      │  WASAPI
      ▼
Python 音频采集 ──100ms PCM──► 阿里云 Qwen-Audio-ASR Streaming
      │                              │
      │                       识别结果（中间/定稿）
      ▼                              ▼
本地 HTTP 服务 ◄────────────── WebSocket 客户端
      │  SSE
      ▼
OBS 浏览器源 /overlay
```

## 为什么不是「OBS 直接连阿里云」

OBS 的浏览器源里申请麦克风权限并不可靠，而且把 API Key 写进网页会泄露。
所以让本机 Python 服务负责采集和鉴权，OBS 只负责显示一个本地网页，稳定且
不需要在 OBS 里装插件。

## 环境要求

- Windows 10 / 11（采集依赖 Windows WASAPI）
- Python **3.10 及以上**，推荐 3.13
- OBS Studio 28 及以上（浏览器源）
- 阿里云百炼账号，并已开通 **Qwen-Audio-3.0-ASR-Flash-Streaming**
  （也可用 `fun-asr-realtime`）
- 能访问 `dashscope.aliyuncs.com`

## 快速开始

### 1. 准备 API Key

1. 登录[阿里云百炼控制台](https://bailian.console.aliyun.com/)；
2. 在模型广场开通「Qwen-Audio-3.0-ASR-Flash-Streaming」/ 实时语音识别；
3. 在 API Key 页面创建一个 Key（通常以 `sk-` 开头）。

### 2. 启动

双击项目目录里的 **`start.bat`**。

如果双击没有反应或窗口一闪而过，在项目文件夹的地址栏输入 `cmd` 回车，
然后执行：

```bat
start.bat
```

也可以直接运行：

```bat
.venv\Scripts\python.exe main.py
```

首次运行会自动：

- 在项目下创建 `.venv` 虚拟环境；
- 安装 `PyAudioWPatch`、`numpy`、`websocket-client`；
- 启动服务并打开控制台。

控制台地址：

```
http://127.0.0.1:8765/control
```

如果 8765 被占用，可以改用其他端口：

```bat
start.bat --port 8766
```

### 3. 在控制台配置

1. 把 API Key 粘贴到「DashScope API Key」，点「保存并应用」；
2. 音频来源选「系统声音（推荐）」或「麦克风」；
3. 语言一般选「中文」；中英混说可以选「自动」；
4. 断句静音默认 1000 毫秒，觉得断句太碎可调大到 1500–2000；
5. 点「保存并应用」，右上角状态变成「正在识别」后开始说话/播放声音。

API Key 只保存在本机的 `config.json`，不会出现在网页源码里，也不会回显。
也可以不用网页，改用环境变量：

```bat
set DASHSCOPE_API_KEY=sk-xxxx
start.bat
```

环境变量的优先级高于 `config.json`，且不会写进配置文件。

### 4. 添加到 OBS

1. 在 OBS 的「来源」里点 `+` →「浏览器」；
2. URL 填：

   ```
   http://127.0.0.1:8765/overlay
   ```

3. 宽度填 `1920`，高度填 `1080`，与 1080p 画布一致；
4. 不要勾选「本地文件」；
5. 建议取消勾选「关闭源时关闭页面」，切换场景后字幕仍能继续更新；
6. 「自定义 CSS」留空；
7. 背景默认透明，直接叠在画面上即可。

> 默认字号 64px、每屏最多 2 行，字幕固定在画面底部居中。字号不会自动缩小；
> 文字超过 94% 宽度时自动换行，但最多只显示 2 行，超出部分从上方裁掉，
> 保留最新的内容。如果手动改成 1 行，超宽部分会用省略号截断。

## 控制台功能

- **连接阿里云**：API Key、Base URL、模型名、语言提示、断句静音、自定义 WebSocket 地址；
- **音频来源**：系统声音 / 麦克风 / 指定设备，支持刷新设备列表；
- **字幕样式**：字号、每屏行数、距底部、静音清屏、文字颜色、背景框、状态角标；
- **实时状态**：连接状态、错误信息、当前音频设备、WebSocket 地址；
- **日志**：最近的连接与设备事件；
- **样式预览**：不用开 OBS 也能调样式；
- **保存并应用 / 重新连接 / 清空字幕**。

样式修改会立即推送到已经打开的 OBS 浏览器源；音频或连接配置变化时，
服务会自动重启采集和识别管线。

## 配置文件

首次启动会生成 `config.json`。常用字段：

| 字段 | 默认值 | 说明 |
|---|---|---|
| `api_key` | `""` | DashScope API Key；留空则读环境变量 |
| `base_url` | `https://dashscope.aliyuncs.com` | 服务地址 |
| `ws_url` | `""` | 自定义 WebSocket 地址；留空自动生成 |
| `model` | `qwen-audio-3.0-asr-flash-streaming` | 模型名；可改回 `fun-asr-realtime` |
| `language` | `zh` | 语言提示；空字符串为自动 |
| `audio_source` | `loopback` | `loopback` / `mic` / `device` |
| `device_index` | `null` | 指定设备索引；`null` 为默认设备 |
| `max_sentence_silence` | `1000` | 断句静音，200–6000 毫秒 |
| `heartbeat` | `true` | WebSocket 心跳 |
| `max_task_seconds` | `3600` | 单任务最长时长，到时自动轮换；0 不轮换 |
| `font_size` | `64` | 字幕字号（固定值，超宽自动换行，不缩放） |
| `max_lines` | `2` | 每屏最多行数，1–4；默认两行 |
| `clear_after` | `8` | 多少秒没有新字幕后清屏；0 不清屏 |
| `bottom` | `6` | 距底部百分比 |
| `text_color` | `#ffffff` | 文字颜色 |
| `box_color` | `rgba(0,0,0,0.52)` | 背景框颜色 |
| `show_status` | `false` | 是否在左上角显示连接状态 |
| `demo` | `false` | 演示模式，使用本地示例字幕，不连云端 |
| `server_host` | `127.0.0.1` | 监听地址，不建议改成公网 |
| `server_port` | `8765` | 本地端口 |

`config.example.json` 是一份不含密钥的示例。也可以直接复制为 `config.json`
再修改。

### 环境变量

| 变量 | 说明 |
|---|---|
| `DASHSCOPE_API_KEY` | 优先于 `config.json` 中的 Key，且不会落盘 |
| `DASHSCOPE_BASE_URL` | 优先于 `config.json` 中的 Base URL |

## 命令行

```bat
.venv\Scripts\python.exe main.py --help
.venv\Scripts\python.exe main.py --demo            # 本地演示，不连云端
.venv\Scripts\python.exe main.py --port 8766       # 换端口
.venv\Scripts\python.exe main.py --open            # 启动后打开控制台
.venv\Scripts\python.exe main.py --log-level DEBUG # 调试日志
```

`--demo` 启动时只覆盖本次进程的演示开关；如果随后在控制台点「保存并应用」，
界面上当前勾选的状态（包括演示模式）会写入 `config.json`。

## 计费与静音

- 云端按发送的音频时长计费。程序在检测到声音停止后，最多再补发 **3 秒**
  静音，让云端 VAD 完成断句；之后不再发送持续静音。
- 没有声音时不会一直上传静音（前提是驱动在静音时不产生非零数据；程序也会
  过滤全零帧）。
- 连接本身会保持，以便随时继续识别；不用时按 `Ctrl+C` 停止服务。
- 具体价格与免费额度以阿里云百炼控制台为准。

## 常见问题

### 状态显示「缺少 API Key」

没有配置 Key，或者 `demo` 没关。到控制台填入 Key 后保存，或设置
`DASHSCOPE_API_KEY` 环境变量。

### 报 `InvalidApiKey` / `AccessDenied` / `ModelNotFound`

- Key 必须是以 `sk-` 开头的一串字符，不要把模型名当成 Key；
- Key 是否复制完整、是否已被删除；
- Key 与 `base_url` 是否属于同一地域（北京 Key 配 `dashscope.aliyuncs.com`，
  新加坡 Key 配 `dashscope-intl.aliyuncs.com`）；
- 账号是否已开通 Qwen-Audio-ASR（或 `fun-asr-realtime`）；
- `model` 是否为 `qwen-audio-3.0-asr-flash-streaming`；
- 工作空间专属域名是否需要用「高级：自定义 WebSocket 地址」单独填写。

### 选了系统声音但没有字幕

- 确认扬声器正在播放声音，系统音量不是静音；
- 控制台「设备」里选的是当前实际使用的扬声器回环，而不是 HDMI/虚拟声卡；
- 某些播放器使用 ASIO/独占模式会绕过 WASAPI 回环，换共享模式输出；
- 蓝牙耳机切换输出设备后，回环设备索引会变，点「刷新」重新选择。

### 麦克风没有字幕

- Windows 设置里允许应用访问麦克风；
- 选默认麦克风或正确的阵列麦克风；
- 麦克风被其他软件独占时，关闭占用程序再重启服务。

### OBS 里是黑底/白底，不是透明

- 不要勾选「本地文件」；
- 「自定义 CSS」留空；
- 背景透明依赖 OBS 浏览器源的 alpha 合成，页面本身已设置
  `background: transparent`；
- 如果使用了「色源」垫底，检查图层顺序。

### 字幕有延迟

- 网络到阿里云的 RTT 会影响首字延迟；
- 断句静音越大，定稿越晚；可适当调小；
- 机器负载高时，音频回调可能堆积，关闭占 CPU 的程序。

### 端口被占用

```bat
start.bat --port 8766
```

然后把 OBS 的 URL 改成 `http://127.0.0.1:8766/overlay`。

## 测试

测试不调用真实云端，使用本地假 WebSocket 上游和本机回环设备。先安装测试依赖：

```bat
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

然后运行：

```bat
.venv\Scripts\python.exe tests\test_protocol.py
.venv\Scripts\python.exe tests\test_audio_capture.py
.venv\Scripts\python.exe tests\test_integration.py
.venv\Scripts\python.exe tests\test_events.py
```

- `test_protocol.py`：校验 `run-task` 字段、鉴权头、音频分片和字幕事件；
- `test_audio_capture.py`：播放 440 Hz 音调，验证 WASAPI 回环采集与 16k 转换；
- `test_integration.py`：采集 → 假云端 → SSE 字幕状态的端到端链路。

测试需要能播放声音；没有音频设备时 `test_audio_capture.py` 会失败。

## 目录结构

```
obs-funasr-caption/
├─ main.py             启动入口
├─ app.py              配置、采集、ASR、事件的总装
├─ asr_client.py       阿里云 Qwen-Audio / Fun-ASR WebSocket 客户端
├─ audio_capture.py    WASAPI 回环/麦克风采集与 16k 转换
├─ events.py           SSE 事件总线与字幕状态
├─ config.py           配置校验与持久化
├─ demo.py             本地演示字幕
├─ server.py           本地 HTTP / SSE 服务
├─ web/
│  ├─ overlay.html     OBS 字幕页
│  └─ control.html     控制台
├─ tests/              本地测试
├─ start.bat           一键启动
├─ requirements.txt
├─ requirements-dev.txt
└─ config.example.json
```

## 隐私与安全

- 服务默认只监听 `127.0.0.1`，局域网其他机器访问不到；
- API Key 仅保存在本机 `config.json`，控制台不回显；
- 音频只发送到阿里云百炼的 Qwen-Audio / Fun-ASR 接口，不经过其他服务器；
- 不要把 `config.json` 上传到公开仓库或发给别人。

## 已知限制

- 目前只支持 Windows（WASAPI）；
- 不支持把字幕直接推流到远端 OBS，需要 OBS 和本服务在同一台机器；
- 单实例运行；多路音频/多语言同时识别需要扩展；
- 两行模式会先换行，最多显示两行；超出部分从上方裁掉，保留最新内容；
- 云端断线会自动重连，但断线期间的语音不会被补识别。
