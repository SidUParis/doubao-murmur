# Open Voice Input Linux（本地过渡版）

一个轻量的 Linux 语音输入原型：把火山引擎返回的实时草稿直接显示在当前输入框的
IBus preedit 中，再用二遍识别结果原位提交。它适用于 Ubuntu、SteamOS Desktop Mode
(Steam Deck) 和其他使用 IBus/GTK4 的 Linux 桌面。

> **macOS 用户**: 请使用项目根目录的 macOS 版本。
>
> **当前是 Open Voice Input Linux 的过渡原型。** 它不会修改 Rime/雾凇的配置或词库；
> 开始录音时临时从原输入法切到轻量 `murmur-voice` IBus 引擎，把实时草稿
> 显示为光标处的 preedit，最终结果原位提交后自动切回原输入法。

## ✨ 功能

- ⌨️ **全局热键**: 右 `Alt` 开始/停止录音，`ESC` 取消（任何应用中均可用）
- 🎮 **手柄一键语音输入**: 在 Steam Input 桌面布局中把手柄按键映射为右 Alt 即可（见下文）
- 🎙 **轻量状态按钮**: 不再显示黑色转写框；按钮区分录音、二遍整理和错误状态
- ✍️ **光标处实时草稿**: 流式结果通过 IBus preedit 原位改写，二遍结果只 commit 一次，不依赖剪贴板
- 🤖 **可选火山引擎 API**: 支持实时草稿、二遍识别、语义顺滑和标点/数字规整
- 🔐 **最小配置**: 在控制面板粘贴自己的火山引擎 API Key；接口、二遍识别、标点和安全限额自动配置
- 🛎 **系统托盘**: 常驻托盘 🎤 图标，左键打开控制面板，右键菜单登录/退出（KDE 等支持 StatusNotifierItem 的桌面）
- ⌨️ **屏幕触摸键盘**: SteamOS 桌面模式下的可拖动、可缩放软键盘，专为掌机握持设计的**分体**与**左/右单手**布局（见下文）

## ⌨️ 屏幕触摸键盘

SteamOS 桌面模式自带的虚拟键盘不能移动、不能缩放，常挡住输入框。本项目内置了一个可拖动、可缩放的触摸软键盘，把按键注入到当前焦点窗口（不抢焦点），适合 Steam Deck 等掌机。

<p align="center">
  <img src="../docs/screenshots/keyboard_full.png" width="760" alt="屏幕键盘 - 全键模式">
</p>

- **打开方式**: 托盘菜单「⌨ 软键盘」，或全局快捷键 `Ctrl + Super + Shift`（三个修饰键同时按住）
- **布局模式**（顶部按钮循环切换）:
  - **全键**: 整块标准键盘，居中
  - **分体**: 左右两个半键盘分别贴住屏幕两侧、中间留空——掌机双手握持时两个拇指正好够到
  - **左手 / 右手**: 整块键盘压成手机宽度、吸附到屏幕最左 / 最右，单拇指即可输入
- **移动 / 缩放**: 拖动顶部条移动，右上角 `⤡` 拖拽缩放，或 `S / M / L` 一键预设；位置和模式会被记住
- **符号层**: `?123` 切换符号层；`Shift / Ctrl / Alt` 为吸附式（点一下临时生效、双击锁定），可打出 `Ctrl+C`、`Ctrl+Shift+V` 等组合键
- 纯 ASCII / 拉丁键盘——中文请用本项目的语音输入

<p align="center">
  <img src="../docs/screenshots/keyboard_split.png" width="820" alt="屏幕键盘 - 分体模式">
</p>

> 仅 X11 桌面（如 SteamOS 的 KDE Plasma）；按键注入依赖宿主机的 `xdotool`（SteamOS 自带）。

## 📋 系统要求

- **OS**: SteamOS 3 / Arch Linux / 其他支持 GTK4 的 Linux 发行版
- **音频**: PipeWire (SteamOS 默认) 或 PulseAudio
- **Python**: 3.11+
- **桌面环境**: KDE Plasma (推荐) 或 GNOME

## 🚀 安装

### 方法一: Flatpak (推荐)

从 [Releases](../../../../releases) 页面下载 `doubao-murmur.flatpak`：

```bash
flatpak install --user doubao-murmur.flatpak
flatpak run com.doubao.Murmur
```

WebKitGTK 等依赖打包在 GNOME runtime 中，**不受 SteamOS 系统更新影响**。

实时光标原型还需要安装 Murmur IME 的用户级 IBus engine；若该服务没有安装或
不可达，应用才会退回依赖宿主机 `xdotool` 的窗口绑定粘贴方式。

也可以从源码自行构建：

```bash
cd linux
flatpak install flathub org.flatpak.Builder org.gnome.Platform//49 org.gnome.Sdk//49
make flatpak-install
```

安装光标内实时草稿所需的用户级引擎（无需 root，也无需重启 IBus/电脑）：

```bash
git clone https://github.com/SidUParis/openVoiceInput_linux.git
cd openVoiceInput_linux
./scripts/install-user.sh
```

该脚本只写入用户级 XDG 目录并启动 `murmur-ime-engine.service`，不会读写
`~/.config/ibus/rime`。开发原型结束时可运行 `./scripts/uninstall-user.sh` 撤销。

### 方法二: 直接运行 (开发模式)

```bash
# 1. 安装系统依赖（SteamOS 需要先 sudo steamos-readonly disable）
sudo pacman -S python python-pip python-gobject gtk4 webkitgtk-6.0 xdotool

# 2. 安装 Python 依赖
cd linux
python3 -m venv --system-site-packages .venv
.venv/bin/pip install websockets sounddevice python-xlib

# 3. 运行
PYTHONPATH=src .venv/bin/python -m doubao_murmur
```

> ⚠️ SteamOS 系统更新会清除 pacman 安装的包，届时需重装 `webkitgtk-6.0`。推荐用 Flatpak。

## 🎮 使用方法

1. 先在自己的火山引擎账号开通语音识别大模型服务并创建 API Key；打开控制面板，粘贴 Key 后点击 **保存并启用火山引擎**。其余推荐参数会自动填写，无需 WebView 登录
2. 应用启动后驻留在系统托盘（右下角 🎤 图标）
3. 将光标放到任意输入框，按 **右 Alt** 开始说话；实时草稿会直接出现在光标处，悬浮按钮显示 ⏹
4. 再按一次 **右 Alt** 结束；按钮显示 ✨，等待二遍识别与文本规整
5. 二遍结果会替换草稿、原位提交一次，然后自动切回录音前的输入法
6. 如果录音期间切换输入框，preedit 会清除且本次语音立即取消，迟到结果不会提交

这里使用的是真正的 IBus preedit，不会不断粘贴、回删来伪造实时效果，因此
不会污染正文或撤销历史。当前过渡版本在录音期间临时停用 Rime；最终合体版会把
同一套会话保护接入 librime/ibus-rime，让键盘雾凇与语音常驻在同一个引擎中。

### 快捷键

| 快捷键 | 功能 |
|--------|------|
| 右 Alt 键 | 开始 / 停止录音 |
| ESC 键 | 取消当前录音（不粘贴） |

### 🎮 手柄一键语音输入 (Steam Deck / 掌机)

桌面模式下 Steam 接管了手柄，原生按键事件不会透传，但可以让 Steam 把手柄按键转成键盘键：

1. 打开 **Steam → 设置 → 控制器 → 桌面布局 → 编辑**
2. 把 **R3（右摇杆按下）** 或任意顺手的按键 → 添加命令 → **键盘 → 右 Alt**
3. （可选）把 **B 键** → **键盘 → Escape**，用于取消录音

之后在任何应用里按 R3 即可开始/结束语音输入。应用通过 X11 层（XRecord）监听按键，
Steam 注入的按键和物理键盘都能识别，无需额外权限。

## 🏗 项目结构

```
linux/
├── src/doubao_murmur/
│   ├── __main__.py          # 入口点
│   ├── app.py               # 主 GtkApplication
│   ├── app_state.py         # 应用状态管理
│   ├── config.py            # 配置常量
│   ├── asr_client.py        # WebSocket ASR 客户端
│   ├── audio_capture.py     # 麦克风音频采集
│   ├── transcription.py     # 录音状态机
│   ├── preedit_client.py     # 临时 IBus 引擎的 D-Bus preedit/commit 桥
│   ├── params_store.py      # 凭证持久化
│   ├── hotkey/              # 输入管理
│   │   ├── manager.py       # 热键管理器（统一封送到 GTK 主线程）
│   │   ├── overlay_button.py # 屏幕 PTT 按钮
│   │   ├── x11_listener.py  # X11/XRecord 全局键监听（主用）
│   │   └── evdev_listener.py # /dev/input 监听（非 X11 后备）
│   ├── ui/                  # 用户界面
│   │   ├── overlay.py       # 旧版转写窗（当前不创建）
│   │   ├── windowing.py     # X11 置顶/定位/无焦点处理
│   │   ├── tray_icon.py     # 系统托盘 + 控制面板
│   │   ├── sni_tray.py      # StatusNotifierItem/dbusmenu 纯 DBus 实现
│   │   └── login_window.py  # WebView 登录
│   ├── paste/               # 剪贴板/粘贴
│   │   └── paste_helper.py
│   └── resources/           # JS 注入脚本
├── flatpak/                 # Flatpak 打包
├── tests/                   # 单元测试
└── run.sh                   # 开发启动脚本
```

## 🔧 配置

配置文件存储在 `~/.config/doubao-murmur/`：

| 文件 | 用途 |
|------|------|
| `backend.json` | 选择转写后端及非敏感调优参数；不存放密钥 |
| `volcengine.json` | 仅保存火山引擎凭证；应设为权限 `0600` |
| `personal_vocabulary.json` | 用户显式填写的个人词表；原子保存并设为权限 `0600` |
| `asr_params.json` | 内置豆包 WebView 登录后提取的旧后端凭证 |

删除 `asr_params.json` 可以强制内置豆包后端重新登录：

```bash
rm ~/.config/doubao-murmur/asr_params.json
```

### 火山引擎 API 语音输入

项目推荐使用火山引擎的优化版双向流式接口 `bigmodel_async`：录音时连续发送
小块 PCM 音频，服务端持续返回草稿，并在完整语句结束后用二遍识别结果修正文字。
当前本地原型不显示草稿黑框：服务端每个累计草稿都直接替换光标处的 IBus
preedit，二遍权威结果再原位提交。配合语义顺滑、标点恢复与数字规整，适合日常语音输入。

1. 在[火山引擎语音控制台](https://console.volcengine.com/speech/new/setting/apikeys?projectName=default)
   用自己的账号开通语音识别大模型服务并创建 API Key。应用不能代替用户开通云服务，
   也不会内置共享 Key；调用费用、配额和数据处理都归用户自己的火山账号。接口模式与参数可参阅
   [优化版双向流式 WebSocket 文档](https://www.volcengine.com/docs/6561/1354869)。
2. 打开 Open Voice Input Linux 控制面板，在掩码输入框粘贴 Key，点击
   **保存并启用火山引擎**。程序会原子写入权限 `0600` 的凭证文件，并自动选择
   `bigmodel_async`、二遍识别、语义顺滑、数字规整、自动标点、10 分钟单次录音上限
   和 10 秒网络积压上限。应用空闲时下一次录音立即使用新配置。

如果正在录音，请先结束或取消，再保存一次；如果即时重载失败，重启应用即可，
不需要注销桌面、重启电脑或改动 IBus/Rime。

以下手工配置仅用于无图形界面的高级场景。从 `linux/` 目录复制安全占位符：

```bash
install -d -m 700 ~/.config/doubao-murmur
install -m 600 volcengine.example.json \
  ~/.config/doubao-murmur/volcengine.json
```

新版控制台使用单个 API Key，`volcengine.json` 只需要：

```json
{
  "api_key": "YOUR_VOLCENGINE_API_KEY"
}
```

项目仍兼容旧版控制台的 `app_id` + `access_token`，但新用户应优先使用单个
API Key。不要同时填写两套凭证；若两套都存在，程序优先使用新版 `api_key`。
不要把真实密钥提交到 Git、写进 `backend.json`，或粘贴到 issue/日志中。

高级用户可在 `backend.json` 调整非敏感参数；一份等价于控制面板默认值的配置是：

```json
{
  "backend": "volcengine",
  "endpoint": "bigmodel_async",
  "resource_id": "volc.seedasr.sauc.duration",
  "language": "zh-CN",
  "enable_nonstream": true,
  "enable_ddc": true,
  "enable_itn": true,
  "enable_punc": true,
  "show_utterances": true,
  "result_type": "full",
  "chunk_ms": 200,
  "final_result_timeout": 20.0,
  "max_pending_audio_seconds": 10,
  "max_recording_seconds": 600
}
```

保存到 `~/.config/doubao-murmur/backend.json`，并限制两个配置文件只允许当前用户读取：

```bash
chmod 600 ~/.config/doubao-murmur/backend.json \
  ~/.config/doubao-murmur/volcengine.json
```

应用空闲时可以即时读取控制面板保存的新配置；手工编辑 JSON 后重启应用最稳妥，
但都不需要注销桌面、重启电脑或改动 IBus/Rime。

可用的接口模式：

| `endpoint` | 返回方式 | 建议用途 |
|------------|----------|----------|
| `bigmodel_async` | 实时返回草稿，并对完整语句进行二遍识别 | 默认推荐；日常语音输入 |
| `bigmodel_nostream` | 音频流式上传，但只返回一次权威结果 | 不需要实时草稿、准确率优先 |
| `bigmodel` | 标准双向流式，边说边返回中间结果 | 实时字幕 |

推荐配置中的识别选项：

| 选项 | 作用 |
|------|------|
| `enable_nonstream: true` | 在双向流式草稿之外，对检测到的完整语句再做一次更准确的识别 |
| `enable_ddc: true` | 开启语义顺滑，改善口语识别结果的可读性 |
| `enable_itn: true` | 开启数字规整，例如把口述数字整理成适合阅读的形式 |
| `enable_punc: true` | 自动恢复标点 |
| `max_pending_audio_seconds: 10` | 网络暂时发不出去时，最多在内存保留 10 秒待发 PCM |
| `max_recording_seconds: 600` | 单次录音最多 10 分钟，到时正常停止并等待二遍结果 |

这些能力都是**火山引擎 ASR 原生的识别与文本后处理**，不是生成式 LLM。
它们会修正识别、格式和标点，但不会替用户续写、扩写内容或改变原意。
`enable_nonstream` 在这里表示开启二遍识别，并不表示麦克风音频改为整段上传；
音频仍然按块流式发送。

`endpoint` 也接受完整的 `wss://` 地址。音频格式固定为
16 kHz、16-bit、单声道 PCM。`chunk_ms` 控制每个音频包的时长，通常无需修改。

资源 ID 必须与控制台已开通的服务版本一致：

| 版本 | `resource_id` |
|------|---------------|
| 语音识别大模型 2.0 小时版（默认） | `volc.seedasr.sauc.duration` |
| 语音识别大模型 1.0 小时版 | `volc.bigasr.sauc.duration` |

如果返回无权限或资源不存在，先检查控制台实际开通的是 2.0 还是 1.0，
再修改 `resource_id`；切换后只需重启 Open Voice Input Linux，不需要改 IBus/Rime 或重启桌面。

#### 录音安全限额

推荐配置包含两层本地安全上限，它们是 **Open Voice Input Linux 的产品默认值**，
不是火山引擎公布的服务时长限制：

- Volcengine 待发送 PCM 默认最多保留 10 秒，即 16 kHz、16-bit、单声道下的
  320 KB；`max_pending_audio_seconds` 可在 1–30 秒之间调整。网络持续堵塞并超过
  高水位时，本次录音会安全取消，只显示固定错误，不把语音文字或 API Key 写入错误。
- 单次录音默认最多 600 秒；`max_recording_seconds` 可在 1–3600 秒之间调整。
  到达上限时会像用户再次按右 Alt 一样进入正常 stop/final 路径，PTT 按钮转为
  ✨ 二遍整理，随后提交最终结果，而不是直接丢弃录音。

控制面板保存 API Key 时会自动写入推荐值。需要手工调整时，在 `backend.json`
修改对应数字即可；旧会话的计时器带有 session 标识，取消或开始新录音后不会误停新会话。

#### 个人词表（可选）

控制面板提供显式个人词表，一行填写一个希望提高识别率的专名或术语。点击
**保存个人词表**后，去空、去重后的内容会写入独立的
`~/.config/doubao-murmur/personal_vocabulary.json`，下一次录音立即生效；留空保存即可清除。

这是完全手动的功能：程序默认不会读取剪贴板、普通键盘输入、输入历史或已识别文本，
也不会自动学习或自动纠错。使用火山引擎后端时，当前词表会随**每一次语音请求**
发送给火山引擎，服务端请求使用官方的 request-level `context` JSON 字符串，例如：

```json
{
  "request": {
    "context": "{\"hotwords\":[{\"word\":\"DeepSeek\"},{\"word\":\"雾凇拼音\"}]}"
  }
}
```

本地 MVP 最多保存 200 项，每项最多 64 个字符，以限制每次请求附带的数据量。
火山引擎建议热词使用有辨识度的中英文实体词，避免大量常用单字或口语词，以免降低
整体识别效果。请求格式可参考[大模型 ASR SDK 参数示例](https://www.volcengine.com/docs/6561/1395846)，
热词使用建议可参考[火山引擎热词说明](https://www.volcengine.com/docs/6561/155739)。

已有火山引擎控制台词表的高级用户，可以手工在 `backend.json` 加入
`"boosting_table_id": "你的热词ID"`；程序会把它组装为
`request.corpus.boosting_table_id`。这个字段是可选项，普通用户无需创建或填写。

要切回内置豆包后端，把 `backend.json` 改成：

```json
{"backend": "doubao"}
```

#### 隐私与快捷键

- 右 `Alt` 只负责开始/停止一次录音，`ESC` 会取消当前录音且不粘贴结果；程序不会接管普通键盘输入。
- 录音期间的原始麦克风音频会随录音实时发送到所选的识别服务。选择 `volcengine` 时音频发送给火山引擎；按 `ESC` 只能取消本地结果，不能撤回已经上传的音频，请按你的数据合规要求使用。
- IBus preedit 成功取得焦点后，识别文字不会进入系统剪贴板；焦点丢失会取消且不降级粘贴。只有本机没有可用 preedit 服务时，旧的窗口绑定 fallback 才会使用剪贴板。
- API 凭证只应保存在本机 `volcengine.json` 中并保持 `0600` 权限。项目提供的示例文件永远只放占位符。
- 个人词表只来自控制面板的显式填写，本机文件保持 `0600`；启用火山引擎时，它会随每次语音请求发送给火山引擎。
- 网络积压超过 PCM 高水位时，待发缓冲会被丢弃并安全取消本次录音；不会为了等网络而阻塞麦克风音频线程。

## ❓ 常见问题

### 启动后什么都没出现
- 已登录时应用驻留系统托盘，查看右下角是否有 🎤 图标，按右 Alt 即可录音
- 点击托盘图标或再启动一次应用（单实例）可打开控制面板
- 桌面不支持托盘（如原版 GNOME）时没有图标，功能不受影响

### 没有声音/录音失败
- 检查麦克风权限: `arecord -l` 查看可用设备
- 检查 PipeWire: `pactl info` 确认音频系统正常
- 测试 Python 音频: `python3 -c "import sounddevice; print(sounddevice.query_devices())"`

### 看不到光标处的实时草稿
- 确认 `systemctl --user is-active murmur-ime-engine.service` 返回 `active`
- 确认 Flatpak 权限包含 `org.murmur.IME.Preedit1=talk`
- 录音开始后 IBus 会临时显示 `murmur-voice`，结束或取消后应恢复原输入法
- 某些不实现 IBus preedit 的输入控件会被拒绝；密码/PIN/私密字段始终拒绝录音

### 旧版自动粘贴 fallback 不工作
- 只有 preedit 服务不可用时才进入此 fallback
- 确认宿主机有 `xdotool`（SteamOS 自带；其他发行版 `sudo pacman -S xdotool`）
- 如果焦点窗口已改变，结果只会复制而不会模拟 `Ctrl+V`

### 手柄按键不触发
- 确认是在 **桌面布局**（Desktop Layout）里设置的映射，不是某个游戏的布局
- 确认映射的目标是键盘的 **右 Alt**（Right Alt），不是左 Alt

### WebView 无法加载
- 安装 WebKitGTK: `sudo pacman -S webkitgtk-6.0`
- 确认网络连接正常

## 📝 开发

```bash
# 安装开发依赖
pip3 install --user -e ".[dev]"

# 运行测试
make test

# 运行应用
make run
```

## 📄 许可证

MIT License - 详见项目根目录的 LICENSE 文件。

## 🔗 相关链接

- [macOS 版本](../README.md)
- [豆包官网](https://www.doubao.com)
- [SteamOS](https://www.steamdeck.com)
