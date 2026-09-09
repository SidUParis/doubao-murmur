# Open Voice Input Linux 兼容控制界面

这个 Flatpak 是独立 `openVoiceInput_linux` 语音服务的轻量控制器。它保留既有的
右 `Alt` 快捷键、`ESC` 取消、悬浮麦克风按钮和系统托盘，但不再自行录音、连接
识别供应商、读取 API Key、生成转写或向 IBus 提交文字。

真正的麦克风选择、ASR、光标内 preedit/final、五秒纠错观察和输入法恢复都由宿主机
的 `murmur-ime-voice.service` 与 `murmur-ime-engine.service` 完成。

## 调用链

```text
右 Alt / 悬浮按钮 ─┐
ESC ────────────────┼─> Flatpak controller
托盘状态 ───────────┘        │
                              │ mode-0600 Unix socket
                              ▼
                  standalone voice daemon
                    ├─ microphone / ASR
                    ├─ IBus preedit / commit
                    └─ adaptive correction
```

控制界面只允许 `start`、`stop`、`toggle`、`cancel`、`status` 五个命令。正常
开始/停止使用显式命令；仅“结束五秒观察并立即开始下一次听写”使用原子的 `toggle`。socket 固定在
`$XDG_RUNTIME_DIR/murmur-ime/voice.sock`，父目录必须仅当前用户可访问，socket
必须属于当前用户且权限为 `0600`。命令按单一后台 FIFO 顺序执行；最长 50 秒的
启动请求不会阻塞 GTK 主线程，也不会自动重试非幂等的 `toggle`。

## 功能

- X11 下物理或 XTEST 注入的右 `Alt` 按下再松开可切换语音输入；与其他键组合时不触发。
- `ESC` 始终排入 `cancel`，并清除尚未发送的 toggle。
- 悬浮按钮与右 Alt 使用完全相同的控制路径。
- 按钮显示启动、录音、等待最终结果、五秒纠错观察和错误状态。
- 当 daemon 启用远程桌面剪贴板交付时，悬浮按钮与托盘在空闲状态显示
  固定的“已启用”或“上一条已复制”提示；按钮保持完全可见。
- 活动时每 500 ms 进行一次轻量 `status` 查询；空闲后停止查询。
- 托盘只显示状态、帮助和退出，不再提供旧豆包登录、API Key、个人词表或软键盘入口。

## 前置条件

先从公开项目安装并配置 standalone 服务：

```bash
git clone https://github.com/SidUParis/openVoiceInput_linux.git
cd openVoiceInput_linux
./scripts/install-user.sh
```

确认两个用户服务和本地控制 socket 可用：

```bash
systemctl --user status murmur-ime-engine.service murmur-ime-voice.service
~/.local/share/murmur-ime/murmur-voice-daemon status
```

API Key、个人词表与手动纠错请使用 standalone 项目的设置程序管理；本 Flatpak
既看不到也不会复制 `~/.config/murmur-ime`。

## 构建和运行

```bash
cd linux
flatpak install flathub org.flatpak.Builder org.gnome.Platform//49 org.gnome.Sdk//49
make flatpak-install
flatpak run com.doubao.Murmur
```

运行时权限仅包含：

- X11/Wayland 与基本 GTK 图形能力；
- 只读暴露 `xdg-run/murmur-ime`，用于连接一个私有 socket；
- Flatpak 自己的私有 XDG 配置目录，仅保存悬浮按钮位置，不暴露宿主机旧配置；
- `org.kde.StatusNotifierWatcher`，用于可选系统托盘。

Flatpak 没有 PulseAudio/PipeWire 麦克风 socket、网络共享、Preedit1 D-Bus 权限、
portal/notification 权限或 `flatpak-spawn` host 权限。移除这些权限只影响这个
Flatpak，不会禁用宿主机的麦克风或 standalone daemon。

剪贴板状态来自 daemon 返回的固定、不含文本内容的状态码。控制器不读取剪贴板，
也不会为此增加剪贴板、麦克风或网络权限。

构建阶段仍允许 pip 下载固定版本的 `python-xlib` 与 `six`；该网络能力不会进入
安装后的运行时权限。

## 快捷键和状态

| 操作 | 行为 |
|---|---|
| 右 Alt / 点击 🎤 | 空闲或观察状态下开始；录音中停止 |
| ESC | 取消当前或正在启动的任务 |
| 关闭状态窗口 | 只隐藏窗口，控制器继续运行 |
| 托盘“完全退出（停用语音快捷键）” | 先异步 cancel，再退出控制器；右 Alt 与悬浮按钮停止，daemon 服务仍由 systemd 管理 |

关闭状态窗口的 × 只隐藏窗口，语音快捷键继续工作。完全退出后，需要重新打开
Open Voice Input Linux 控制器；单独打开后台设置窗口不会启动快捷键。
重复打开控制器会重新显示悬浮麦克风并刷新后台状态，不会自动录音。
显示器接入或移除时，可见按钮会重新检查位置；移回屏幕后的坐标会保存，避免下次启动仍使用旧屏幕坐标。

第二次 toggle 如果发生在第一次 start 尚未返回时，只记录为待停止。只有 start
回复仍处于活动状态时才发送第二个 toggle；若 start 已失败并回到 idle，则丢弃待停止，
避免一次失败后反而误启动新录音。超时后的 start 结果不确定时发送 `cancel`，绝不重试
toggle。

## 桌面限制

- 当前全局右 Alt 监听依赖 X11 XRecord。Wayland 下仍可使用悬浮按钮；全局快捷键需要
  后续接入桌面 portal。
- evdev 是非 Flatpak 开发运行时的后备方案；Flatpak 不获得 `/dev/input` 广泛权限。
- 旧 ASR、录音和粘贴模块仍保留在历史源码中，便于追溯原迁移过程，但 controller app
  不导入这些模块，Flatpak 运行权限也无法使用其中的麦克风或网络路径。

## 测试

```bash
cd linux
PYTHONPATH=src python3 -m pytest -q tests
```

测试使用临时 mode-0600 fake socket 和假 controller，不访问真实麦克风、API Key 或
供应商网络。覆盖 socket 权限、响应上限、FIFO、迟到 sequence、pending-stop、ESC
覆盖、observing UI、右 Alt press/release 以及 Flatpak 权限清单。

## License

MIT License，详见项目根目录的 `LICENSE`。
