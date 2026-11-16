# 安卓刷分 GUI

这是一个中文跨平台 Android GUI，用于选择 ADB 设备、获取截图、在截图上点击设置坐标，然后分别启动“搜索”或“信息流”任务。

## Windows

直接运行 `ms-reward-gui.exe`。EXE 已内置 Windows ADB，题库文件 `questions.txt` 放在 EXE 同目录，搜索任务会逐条读取并移除已使用的问题。

也可以使用源码运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
\.venv\Scripts\python.exe android_score_gui.py
```

## macOS

安装 Python 3 和 Android Platform-Tools，并确保 `adb` 在 PATH 中或放在 `platform-tools/adb`，然后运行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
chmod +x android_score_gui.command
./android_score_gui.command
```

macOS 需要在手机上开启 USB 调试；无线调试可以在“无线地址”中输入 `IP:端口` 后连接。

## 使用

1. 点击“刷新设备”，选择目标设备；需要无线调试时先输入 `IP:端口` 并点击“连接无线设备”。
2. 选择“搜索”或“信息流”，点击“获取设备截图”。
3. 在右侧选中坐标项，再点击截图中的对应按钮。搜索需要设置搜索框、搜索按钮、返回主页；信息流需要设置信息流内容、返回主页。
4. 设置循环次数和等待时间，点击对应的启动按钮。

坐标会按 ADB 设备序列号自动保存。USB 序列号和无线地址是两个独立的设备配置；切换设备、截图取点、手动应用坐标或切换模式时都会自动保存。配置文件位置：

- Windows：`%APPDATA%\\ms_reward_android_gui\\devices.json`
- macOS：`~/Library/Application Support/ms_reward_android_gui/devices.json`

“保存配置”和“载入配置”用于手动导出和导入 JSON 配置。

如果日志出现 `INJECT_EVENTS`，请在手机开发者选项中打开“USB 调试（安全设置）”，重新授权 ADB 后再检测输入权限。截图成功不代表设备允许点击和滑动。
