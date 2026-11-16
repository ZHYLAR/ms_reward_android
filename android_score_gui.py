from __future__ import annotations

import io
import json
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

try:
    from PIL import Image, ImageTk
except ImportError as exc:
    raise SystemExit("请先安装依赖：python -m pip install -r requirements.txt") from exc


ROOT_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", ROOT_DIR))
QUESTION_FILE = ROOT_DIR / "questions.txt"
MODE_SEARCH = "搜索"
MODE_FEED = "信息流"
PROFILE_APP_NAME = "ms_reward_android_gui"


class AdbError(RuntimeError):
    pass


class AdbInputPermissionError(AdbError):
    def __init__(self) -> None:
        super().__init__(
            "设备拒绝了 ADB 输入注入（INJECT_EVENTS）。\n\n"
            "请在手机上打开：设置 → 更多设置/其他设置 → 开发者选项 → USB 调试（安全设置）。\n"
            "同时确认 USB 调试已开启；打开后重新授权 ADB，并在本程序中刷新设备。\n\n"
            "无线调试通常也需要先通过 USB 打开这个开关。"
        )


class AdbClient:
    def __init__(self, root_dir: Path) -> None:
        executable = "adb.exe" if os.name == "nt" else "adb"
        bundled = RESOURCE_DIR / "platform-tools" / executable
        self.adb = str(bundled) if bundled.is_file() else shutil.which("adb")
        if not self.adb:
            raise AdbError("找不到 ADB。请将 adb 放到 platform-tools，或加入系统 PATH。")

    def _run(self, args: list[str], *, binary: bool = False, timeout: float = 30.0) -> Any:
        try:
            result = subprocess.run(
                [self.adb, *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise AdbError(f"无法启动 ADB：{self.adb}") from exc
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"ADB 命令超时：{' '.join(args)}") from exc

        if binary:
            if result.returncode:
                error = result.stderr.decode("utf-8", errors="replace").strip()
                raise AdbError(error or "ADB 命令执行失败。")
            return result.stdout

        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        combined = f"{stdout}\n{stderr}"
        if "INJECT_EVENTS" in combined or "Injecting input events requires" in combined:
            raise AdbInputPermissionError()
        if result.returncode:
            raise AdbError((stderr or stdout).strip() or "ADB 命令执行失败。")
        return stdout

    def list_devices(self) -> list[dict[str, str]]:
        self._run(["start-server"], timeout=15)
        output = self._run(["devices", "-l"])
        devices: list[dict[str, str]] = []
        for line in output.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 2:
                continue
            serial, state = parts[0], parts[1]
            attrs: dict[str, str] = {}
            for part in parts[2:]:
                if ":" in part:
                    key, value = part.split(":", 1)
                    attrs[key] = value
            model = attrs.get("model", "")
            label = serial if not model else f"{serial}（{model}）"
            devices.append({"serial": serial, "state": state, "model": model, "label": label})
        return devices

    def connect(self, endpoint: str) -> str:
        return self._run(["connect", endpoint], timeout=15).strip()

    def state(self, serial: str) -> str:
        return self._run(["-s", serial, "get-state"], timeout=15).strip()

    def check_input_permission(self, serial: str) -> None:
        # KEYCODE_UNKNOWN does not change the foreground UI, but still exercises input injection.
        self._run(["-s", serial, "shell", "input", "keyevent", "0"], timeout=15)

    def screenshot(self, serial: str) -> Image.Image:
        data = self._run(["-s", serial, "exec-out", "screencap", "-p"], binary=True, timeout=30)
        start = data.find(b"\x89PNG")
        if start >= 0:
            data = data[start:]
        try:
            return Image.open(io.BytesIO(data)).convert("RGB")
        except Exception as exc:
            raise AdbError("设备返回的截图无法识别。") from exc

    def tap(self, serial: str, x: int, y: int) -> None:
        self._run(["-s", serial, "shell", "input", "tap", str(x), str(y)], timeout=15)

    def input_text(self, serial: str, text: str) -> None:
        if any(ord(char) > 127 for char in text):
            raise AdbError(f"题库包含非英文字符，无法通过 ADB 输入：{text}")
        safe = text.replace(" ", "%s").replace("'", "").replace('"', "")
        self._run(["-s", serial, "shell", "input", "text", safe], timeout=15)

    def swipe_up(self, serial: str, width: int, height: int) -> None:
        x = width // 2
        self._run(
            [
                "-s", serial, "shell", "input", "swipe",
                str(x), str(int(height * 0.78)), str(x), str(int(height * 0.30)), "350",
            ],
            timeout=15,
        )

    def screen_size(self, serial: str, fallback: tuple[int, int]) -> tuple[int, int]:
        try:
            output = self._run(["-s", serial, "shell", "wm", "size"], timeout=15)
            matches = re.findall(r"(\d+)x(\d+)", output)
            if matches:
                return int(matches[-1][0]), int(matches[-1][1])
        except AdbError:
            pass
        return fallback


class ScoreGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("安卓刷分工具")
        self.geometry("1240x850")
        self.minsize(1000, 700)

        self.adb = AdbClient(ROOT_DIR)
        self.devices: dict[str, dict[str, str]] = {}
        self.image: Image.Image | None = None
        self.photo: ImageTk.PhotoImage | None = None
        self.preview_scale = 1.0
        self.preview_offset = (0, 0)
        self.preview_size = (0, 0)
        self.ui_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.busy = False
        self.active_mode = MODE_SEARCH
        self.action_sets = {
            MODE_SEARCH: self._default_search_actions(),
            MODE_FEED: self._default_feed_actions(),
        }
        self.profile_path = self._profile_path()
        self.device_profiles = self._read_device_profiles()
        self.profile_serial = ""

        self.device_var = tk.StringVar()
        self.wireless_var = tk.StringVar()
        self.mode_var = tk.StringVar(value=MODE_SEARCH)
        self.name_var = tk.StringVar()
        self.type_var = tk.StringVar()
        self.wait_var = tk.StringVar(value="1")
        self.x_var = tk.StringVar()
        self.y_var = tk.StringVar()
        self.loop_var = tk.IntVar(value=1)
        self.jitter_var = tk.IntVar(value=0)
        self.search_wait_var = tk.DoubleVar(value=8)
        self.feed_wait_var = tk.DoubleVar(value=10)
        self.status_var = tk.StringVar(value="就绪")

        self._configure_style()
        self._build_ui()
        self.mode_var.trace_add("write", self._mode_changed)
        self.device_var.trace_add("write", self._device_changed)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(100, self._drain_queue)
        self._refresh_action_list(0)
        self.refresh_devices()

    @staticmethod
    def _default_search_actions() -> list[dict[str, Any]]:
        return [
            {"key": "search_box", "name": "搜索框", "type": "点击", "wait": 8.0, "x": None, "y": None},
            {"key": "search_button", "name": "搜索按钮", "type": "点击", "wait": 11.0, "x": None, "y": None},
            {"key": "back_home", "name": "返回主页", "type": "点击", "wait": 1.0, "x": None, "y": None},
        ]

    @staticmethod
    def _default_feed_actions() -> list[dict[str, Any]]:
        return [
            {"key": "content", "name": "信息流内容", "type": "点击", "wait": 10.0, "x": None, "y": None},
            {"key": "back_home", "name": "返回主页", "type": "点击", "wait": 2.0, "x": None, "y": None},
        ]

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=28)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        device_bar = ttk.LabelFrame(self, text="设备")
        device_bar.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        device_bar.columnconfigure(1, weight=1)
        ttk.Label(device_bar, text="ADB 设备").grid(row=0, column=0, padx=(10, 6), pady=8)
        self.device_combo = ttk.Combobox(device_bar, textvariable=self.device_var, state="readonly")
        self.device_combo.grid(row=0, column=1, sticky="ew", pady=8)
        self.refresh_button = ttk.Button(device_bar, text="刷新设备", command=self.refresh_devices)
        self.refresh_button.grid(row=0, column=2, padx=6, pady=8)
        ttk.Label(device_bar, text="无线地址").grid(row=0, column=3, padx=(12, 6), pady=8)
        ttk.Entry(device_bar, textvariable=self.wireless_var, width=22).grid(row=0, column=4, pady=8)
        self.connect_button = ttk.Button(device_bar, text="连接无线设备", command=self.connect_wireless)
        self.connect_button.grid(row=0, column=5, padx=6, pady=8)
        self.check_input_button = ttk.Button(device_bar, text="检测输入权限", command=self.check_input_permission)
        self.check_input_button.grid(row=0, column=6, padx=(0, 10), pady=8)

        content = ttk.Frame(self)
        content.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        content.columnconfigure(0, weight=3)
        content.columnconfigure(1, weight=2)
        content.rowconfigure(0, weight=1)

        preview_box = ttk.LabelFrame(content, text="截图取坐标")
        preview_box.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        preview_box.rowconfigure(1, weight=1)
        preview_box.columnconfigure(0, weight=1)
        toolbar = ttk.Frame(preview_box)
        toolbar.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        self.screenshot_button = ttk.Button(toolbar, text="获取设备截图", command=self.capture_screenshot)
        self.screenshot_button.pack(side="left")
        ttk.Label(toolbar, text="先选中右侧坐标项，再点击截图中的对应按钮。 ").pack(side="left", padx=12)
        self.canvas = tk.Canvas(preview_box, background="#20252b", highlightthickness=1, highlightbackground="#59636e")
        self.canvas.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        self.canvas.bind("<Button-1>", self._on_preview_click)
        self.canvas.bind("<Configure>", lambda _event: self._draw_preview())

        controls = ttk.Frame(content)
        controls.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        controls.columnconfigure(0, weight=1)
        controls.rowconfigure(1, weight=1)

        task_box = ttk.LabelFrame(controls, text="任务与坐标")
        task_box.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        task_box.columnconfigure(1, weight=1)
        ttk.Label(task_box, text="任务模式").grid(row=0, column=0, padx=8, pady=8, sticky="w")
        self.mode_combo = ttk.Combobox(task_box, textvariable=self.mode_var, values=(MODE_SEARCH, MODE_FEED), state="readonly")
        self.mode_combo.grid(row=0, column=1, padx=8, pady=8, sticky="ew")
        self.action_tree = ttk.Treeview(task_box, columns=("index", "name", "x", "y", "wait"), show="headings", height=5, selectmode="browse")
        for column, heading, width in (
            ("index", "序号", 42), ("name", "坐标项", 125), ("x", "X", 60), ("y", "Y", 60), ("wait", "等待秒", 70)
        ):
            self.action_tree.heading(column, text=heading)
            self.action_tree.column(column, width=width, anchor="center")
        self.action_tree.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 8))
        self.action_tree.bind("<<TreeviewSelect>>", self._on_action_select)
        ttk.Label(task_box, text="点击截图只会设置当前选中的点击坐标项。上滑操作由程序自动完成。", wraplength=330).grid(
            row=2, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 8)
        )

        params_box = ttk.LabelFrame(controls, text="当前坐标")
        params_box.grid(row=1, column=0, sticky="nsew", pady=6)
        params_box.columnconfigure(1, weight=1)
        self._form_row(params_box, 0, "功能", self.name_var, True)
        self._form_row(params_box, 1, "类型", self.type_var, True)
        self._form_row(params_box, 2, "等待秒", self.wait_var)
        self._form_row(params_box, 3, "X 坐标", self.x_var)
        self._form_row(params_box, 4, "Y 坐标", self.y_var)
        ttk.Button(params_box, text="应用坐标和等待时间", command=self.apply_action).grid(
            row=5, column=0, columnspan=2, sticky="ew", padx=8, pady=(8, 4)
        )
        ttk.Label(params_box, text="坐标按设备原始分辨率保存，窗口缩放不会改变实际点击位置。", wraplength=330).grid(
            row=6, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 8)
        )

        runner_box = ttk.LabelFrame(controls, text="运行设置")
        runner_box.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        runner_box.columnconfigure(1, weight=1)
        ttk.Label(runner_box, text="循环次数（0=持续）").grid(row=0, column=0, sticky="w", padx=8, pady=5)
        tk.Spinbox(runner_box, from_=0, to=999999, textvariable=self.loop_var, width=8).grid(row=0, column=1, sticky="w", padx=8, pady=5)
        ttk.Label(runner_box, text="随机偏移像素").grid(row=1, column=0, sticky="w", padx=8, pady=5)
        tk.Spinbox(runner_box, from_=0, to=100, textvariable=self.jitter_var, width=8).grid(row=1, column=1, sticky="w", padx=8, pady=5)
        ttk.Label(runner_box, text="搜索输入后等待秒").grid(row=2, column=0, sticky="w", padx=8, pady=5)
        tk.Spinbox(runner_box, from_=0, to=120, increment=1, textvariable=self.search_wait_var, width=8).grid(row=2, column=1, sticky="w", padx=8, pady=5)
        ttk.Label(runner_box, text="信息流浏览等待秒").grid(row=3, column=0, sticky="w", padx=8, pady=5)
        tk.Spinbox(runner_box, from_=0, to=300, increment=1, textvariable=self.feed_wait_var, width=8).grid(row=3, column=1, sticky="w", padx=8, pady=5)
        buttons = ttk.Frame(runner_box)
        buttons.grid(row=4, column=0, columnspan=2, sticky="ew", padx=8, pady=(5, 8))
        buttons.columnconfigure(0, weight=1)
        buttons.columnconfigure(1, weight=1)
        buttons.columnconfigure(2, weight=1)
        self.start_search_button = ttk.Button(buttons, text="开始搜索", command=lambda: self.start_task(MODE_SEARCH))
        self.start_search_button.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        self.start_feed_button = ttk.Button(buttons, text="开始信息流", command=lambda: self.start_task(MODE_FEED))
        self.start_feed_button.grid(row=0, column=1, sticky="ew", padx=3)
        self.stop_button = ttk.Button(buttons, text="停止", command=self.stop_task, state="disabled")
        self.stop_button.grid(row=0, column=2, sticky="ew", padx=(3, 0))

        profiles = ttk.Frame(controls)
        profiles.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        profiles.columnconfigure(0, weight=1)
        profiles.columnconfigure(1, weight=1)
        ttk.Button(profiles, text="保存配置", command=self.save_profile).grid(row=0, column=0, sticky="ew", padx=(0, 3))
        ttk.Button(profiles, text="载入配置", command=self.load_profile).grid(row=0, column=1, sticky="ew", padx=(3, 0))

        log_box = ttk.LabelFrame(self, text="运行日志")
        log_box.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 6))
        log_box.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_box, height=5, state="disabled", wrap="word")
        self.log_text.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        ttk.Label(self, textvariable=self.status_var, anchor="w").grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 8))

    def _form_row(self, parent: ttk.LabelFrame, row: int, label: str, variable: tk.Variable, readonly: bool = False) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=5)
        ttk.Entry(parent, textvariable=variable, state="readonly" if readonly else "normal").grid(row=row, column=1, sticky="ew", padx=8, pady=5)

    def _current_actions(self) -> list[dict[str, Any]]:
        return self.action_sets[self.active_mode]

    @staticmethod
    def _profile_path() -> Path:
        if os.name == "nt":
            base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        elif os.name == "darwin":
            base = Path.home() / "Library" / "Application Support"
        elif os.environ.get("XDG_CONFIG_HOME"):
            base = Path(os.environ["XDG_CONFIG_HOME"])
        else:
            base = Path.home() / ".config"
        return base / PROFILE_APP_NAME / "devices.json"

    def _read_device_profiles(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self.profile_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError, TypeError):
            return {}
        devices = payload.get("devices") if isinstance(payload, dict) else None
        return devices if isinstance(devices, dict) else {}

    def _write_device_profiles(self) -> None:
        self.profile_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "devices": self.device_profiles}
        temporary = self.profile_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.profile_path)

    def _save_device_profile(self, serial: str | None = None) -> None:
        serial = serial or self._selected_serial()
        if not serial:
            return
        self.device_profiles[serial] = {
            "action_sets": {
                mode: [dict(action) for action in actions]
                for mode, actions in self.action_sets.items()
            }
        }
        try:
            self._write_device_profiles()
        except OSError as exc:
            self._append_log(f"自动保存设备坐标失败：{exc}")

    def _load_device_profile(self, serial: str) -> None:
        saved = self.device_profiles.get(serial, {})
        loaded = saved.get("action_sets") if isinstance(saved, dict) else None
        if not isinstance(loaded, dict):
            self.action_sets = {
                MODE_SEARCH: self._default_search_actions(),
                MODE_FEED: self._default_feed_actions(),
            }
            return
        try:
            action_sets: dict[str, list[dict[str, Any]]] = {}
            for mode in (MODE_SEARCH, MODE_FEED):
                items = loaded.get(mode)
                if not isinstance(items, list):
                    raise ValueError(f"缺少{mode}坐标")
                action_sets[mode] = [self._normalize_action(item) for item in items]
            self.action_sets = action_sets
        except (TypeError, ValueError):
            self.action_sets = {
                MODE_SEARCH: self._default_search_actions(),
                MODE_FEED: self._default_feed_actions(),
            }
            self._append_log(f"设备 {serial} 的坐标配置无效，已使用默认坐标项。")

    def _device_changed(self, *_args: Any) -> None:
        serial = self._selected_serial()
        if serial == self.profile_serial:
            return
        if self.profile_serial:
            self._save_device_profile(self.profile_serial)
        self.profile_serial = serial
        if serial:
            self._load_device_profile(serial)
            self._refresh_action_list(0)
            self._append_log(f"已载入设备 {serial} 的坐标配置。")

    def _mode_changed(self, *_args: Any) -> None:
        mode = self.mode_var.get()
        if mode not in self.action_sets or mode == self.active_mode:
            return
        self.active_mode = mode
        self._refresh_action_list(0)
        self._save_device_profile()
        self._append_log(f"已切换到{mode}模式。")

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for widget in (self.refresh_button, self.connect_button, self.check_input_button, self.screenshot_button):
            widget.configure(state=state)
        self.status_var.set("正在处理..." if busy else "就绪")

    def _run_async(self, work: Callable[[], Any], callback: Callable[[Any], None]) -> None:
        if self.busy:
            return
        self._set_busy(True)

        def runner() -> None:
            try:
                self.ui_queue.put(("success", (callback, work())))
            except Exception as exc:
                self.ui_queue.put(("error", exc))

        threading.Thread(target=runner, daemon=True).start()

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "success":
                    callback, result = payload
                    self._set_busy(False)
                    callback(result)
                elif kind == "error":
                    self._set_busy(False)
                    self._show_error(str(payload))
                elif kind == "log":
                    self._append_log(str(payload))
                elif kind == "finished":
                    self._finish_task(str(payload))
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)

    def _show_error(self, message: str) -> None:
        self.status_var.set("错误")
        self._append_log(f"错误：{message}")
        messagebox.showerror("安卓刷分工具", message)

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{time.strftime('%H:%M:%S')}  {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def refresh_devices(self) -> None:
        self._run_async(self.adb.list_devices, self._apply_devices)

    def _apply_devices(self, devices: list[dict[str, str]]) -> None:
        previous = self._selected_serial()
        if previous:
            self._save_device_profile(previous)
        self.devices = {device["label"]: device for device in devices}
        labels = list(self.devices)
        self.device_combo["values"] = labels
        selected = next((label for label, item in self.devices.items() if item["serial"] == previous), "")
        self.device_var.set(selected or (labels[0] if labels else ""))
        self._append_log(f"发现 {len(labels)} 个 ADB 设备。" if labels else "没有发现在线设备，请检查 USB 或无线调试。")

    def connect_wireless(self) -> None:
        endpoint = self.wireless_var.get().strip()
        if not endpoint:
            self._show_error("请输入无线调试地址，例如 192.168.1.20:5555。")
            return

        def work() -> list[dict[str, str]]:
            result = self.adb.connect(endpoint)
            devices = self.adb.list_devices()
            if result:
                self.ui_queue.put(("log", result))
            return devices

        self._run_async(work, self._apply_devices)

    def check_input_permission(self) -> None:
        serial = self._selected_serial()
        if not serial:
            self._show_error("请先选择在线设备。")
            return

        def success(_result: Any) -> None:
            self._append_log("输入权限正常，可以执行点击和滑动。")

        self._run_async(lambda: self.adb.check_input_permission(serial), success)

    def _selected_serial(self) -> str:
        device = self.devices.get(self.device_var.get())
        return device["serial"] if device else ""

    def capture_screenshot(self) -> None:
        serial = self._selected_serial()
        if not serial:
            self._show_error("请先选择在线设备。")
            return

        def work() -> Image.Image:
            if self.adb.state(serial) != "device":
                raise AdbError(f"设备不在线：{serial}")
            return self.adb.screenshot(serial)

        self._run_async(work, self._apply_screenshot)

    def _apply_screenshot(self, image: Image.Image) -> None:
        self.image = image
        self._draw_preview()
        self._append_log(f"截图已获取：{image.width}x{image.height}。")

    def _draw_preview(self) -> None:
        self.canvas.delete("all")
        if self.image is None:
            self.canvas.create_text(self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2, text="请先获取设备截图", fill="#d8dee9")
            return
        canvas_width = max(1, self.canvas.winfo_width() - 20)
        canvas_height = max(1, self.canvas.winfo_height() - 20)
        self.preview_scale = min(canvas_width / self.image.width, canvas_height / self.image.height, 1.0)
        width = max(1, int(self.image.width * self.preview_scale))
        height = max(1, int(self.image.height * self.preview_scale))
        self.preview_size = (width, height)
        offset_x = max(0, (self.canvas.winfo_width() - width) // 2)
        offset_y = max(0, (self.canvas.winfo_height() - height) // 2)
        self.preview_offset = (offset_x, offset_y)
        self.photo = ImageTk.PhotoImage(self.image.resize((width, height), Image.Resampling.LANCZOS))
        self.canvas.create_image(offset_x, offset_y, anchor="nw", image=self.photo)
        actions = self._current_actions()
        selected = self._selected_action_index()
        for index, action in enumerate(actions):
            if action["type"] != "点击" or action.get("x") is None or action.get("y") is None:
                continue
            x = offset_x + int(action["x"] * self.preview_scale)
            y = offset_y + int(action["y"] * self.preview_scale)
            color = "#ff5252" if index == selected else "#36c5f0"
            self.canvas.create_oval(x - 7, y - 7, x + 7, y + 7, outline=color, width=2)
            self.canvas.create_text(x + 12, y - 10, text=action["name"], fill=color, anchor="w")

    def _on_preview_click(self, event: tk.Event[tk.Misc]) -> None:
        index = self._selected_action_index()
        actions = self._current_actions()
        if index is None or actions[index]["type"] != "点击":
            self._append_log("请先在右侧选中一个点击坐标项。")
            return
        offset_x, offset_y = self.preview_offset
        width, height = self.preview_size
        if not (offset_x <= event.x < offset_x + width and offset_y <= event.y < offset_y + height):
            return
        action = actions[index]
        action["x"] = round((event.x - offset_x) / self.preview_scale)
        action["y"] = round((event.y - offset_y) / self.preview_scale)
        self._load_action_fields(action)
        self._refresh_action_list(index)
        self._save_device_profile()
        self._append_log(f"{action['name']}：({action['x']}, {action['y']})")

    def _selected_action_index(self) -> int | None:
        selection = self.action_tree.selection()
        if not selection:
            return None
        try:
            return int(selection[0])
        except ValueError:
            return None

    def _refresh_action_list(self, selected: int | None = None) -> None:
        self.action_tree.delete(*self.action_tree.get_children())
        for index, action in enumerate(self._current_actions()):
            self.action_tree.insert(
                "", "end", iid=str(index),
                values=(index + 1, action["name"], "" if action.get("x") is None else action["x"], "" if action.get("y") is None else action["y"], action.get("wait", 0)),
            )
        if selected is not None and self._current_actions():
            selected = max(0, min(selected, len(self._current_actions()) - 1))
            self.action_tree.selection_set(str(selected))
            self.action_tree.focus(str(selected))
        self._draw_preview()

    def _on_action_select(self, _event: tk.Event[tk.Misc]) -> None:
        index = self._selected_action_index()
        if index is not None:
            self._load_action_fields(self._current_actions()[index])
            self._draw_preview()

    def _load_action_fields(self, action: dict[str, Any]) -> None:
        self.name_var.set(action["name"])
        self.type_var.set(action["type"])
        self.wait_var.set(str(action.get("wait", 0)))
        self.x_var.set("" if action.get("x") is None else str(action["x"]))
        self.y_var.set("" if action.get("y") is None else str(action["y"]))

    def apply_action(self) -> None:
        index = self._selected_action_index()
        if index is None:
            return
        action = self._current_actions()[index]
        try:
            action["wait"] = max(0.0, float(self.wait_var.get()))
            if action["type"] == "点击":
                action["x"] = int(self.x_var.get())
                action["y"] = int(self.y_var.get())
        except ValueError:
            self._show_error("等待时间必须是数字，坐标必须是整数。")
            return
        self._refresh_action_list(index)
        self._save_device_profile()

    def save_profile(self) -> None:
        path = filedialog.asksaveasfilename(title="保存配置", defaultextension=".json", filetypes=[("JSON 配置", "*.json"), ("所有文件", "*")])
        if not path:
            return
        payload = {
            "version": 2,
            "action_sets": self.action_sets,
            "loops": self.loop_var.get(),
            "jitter": self.jitter_var.get(),
            "search_wait": self.search_wait_var.get(),
            "feed_wait": self.feed_wait_var.get(),
        }
        try:
            Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            self._append_log(f"配置已保存：{path}")
        except OSError as exc:
            self._show_error(str(exc))

    def load_profile(self) -> None:
        path = filedialog.askopenfilename(title="载入配置", filetypes=[("JSON 配置", "*.json"), ("所有文件", "*")])
        if not path:
            return
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            loaded = payload.get("action_sets")
            if not isinstance(loaded, dict):
                raise ValueError("配置文件没有搜索和信息流坐标。")
            for mode in (MODE_SEARCH, MODE_FEED):
                items = loaded.get(mode)
                if not isinstance(items, list):
                    raise ValueError(f"配置文件缺少{mode}坐标。")
                self.action_sets[mode] = [self._normalize_action(item) for item in items]
            self.loop_var.set(int(payload.get("loops", 1)))
            self.jitter_var.set(int(payload.get("jitter", 0)))
            self.search_wait_var.set(float(payload.get("search_wait", 8)))
            self.feed_wait_var.set(float(payload.get("feed_wait", 10)))
            self._refresh_action_list(0)
            self._save_device_profile()
            self._append_log(f"配置已载入：{path}")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self._show_error(f"载入配置失败：{exc}")

    @staticmethod
    def _normalize_action(item: Any) -> dict[str, Any]:
        if not isinstance(item, dict) or item.get("type") not in {"点击"}:
            raise ValueError("配置包含无效坐标项。")
        return {
            "key": str(item.get("key", "")),
            "name": str(item.get("name", "坐标项")),
            "type": "点击",
            "wait": float(item.get("wait", 1)),
            "x": None if item.get("x") is None else int(item["x"]),
            "y": None if item.get("y") is None else int(item["y"]),
        }

    def _valid_coordinates(self, mode: str) -> bool:
        missing = [item["name"] for item in self.action_sets[mode] if item.get("x") is None or item.get("y") is None]
        if missing:
            self._show_error(f"请先设置坐标：{'、'.join(missing)}")
            return False
        return True

    def start_task(self, mode: str) -> None:
        if self.worker and self.worker.is_alive():
            return
        serial = self._selected_serial()
        if not serial:
            self._show_error("请先选择在线设备。")
            return
        if not self._valid_coordinates(mode):
            return
        try:
            loops = max(0, int(self.loop_var.get()))
            jitter = max(0, int(self.jitter_var.get()))
            search_wait = max(0.0, float(self.search_wait_var.get()))
            feed_wait = max(0.0, float(self.feed_wait_var.get()))
        except (TypeError, ValueError):
            self._show_error("运行参数必须是有效数字。")
            return

        if self.mode_var.get() != mode:
            self.mode_var.set(mode)
        actions = [dict(action) for action in self.action_sets[mode]]
        self.stop_event.clear()
        self.start_search_button.configure(state="disabled")
        self.start_feed_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set(f"正在执行{mode}...")
        self._append_log(f"开始{mode}：设备 {serial}，循环 {'持续' if loops == 0 else loops} 次。")
        self.worker = threading.Thread(target=self._run_worker, args=(mode, serial, actions, loops, jitter, search_wait, feed_wait), daemon=True)
        self.worker.start()

    def stop_task(self) -> None:
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self.status_var.set("正在停止...")
            self._append_log("已请求停止任务。")

    def _take_question(self) -> str | None:
        try:
            questions = [line.strip() for line in QUESTION_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
        except OSError as exc:
            raise AdbError(f"读取题库失败：{exc}") from exc
        if not questions:
            return None
        question = questions.pop(0)
        try:
            QUESTION_FILE.write_text("\n".join(questions) + ("\n" if questions else ""), encoding="utf-8")
        except OSError as exc:
            raise AdbError(f"保存题库失败：{exc}") from exc
        return question

    @staticmethod
    def _sleep(stop_event: threading.Event, seconds: float) -> bool:
        return stop_event.wait(max(0.0, seconds))

    def _tap_action(self, serial: str, action: dict[str, Any], jitter: int) -> None:
        x = int(action["x"])
        y = int(action["y"])
        if jitter:
            x += random.randint(-jitter, jitter)
            y += random.randint(-jitter, jitter)
        self.adb.tap(serial, x, y)
        self.ui_queue.put(("log", f"点击{action['name']}：({x}, {y})"))

    def _run_worker(self, mode: str, serial: str, actions: list[dict[str, Any]], loops: int, jitter: int, search_wait: float, feed_wait: float) -> None:
        try:
            if self.adb.state(serial) != "device":
                raise AdbError(f"设备不在线：{serial}")
            self.adb.check_input_permission(serial)
            action_map = {action["key"]: action for action in actions}
            width, height = self.adb.screen_size(serial, (self.image.width, self.image.height) if self.image else (1080, 2400))
            loop = 0
            while not self.stop_event.is_set() and (loops == 0 or loop < loops):
                loop += 1
                self.ui_queue.put(("log", f"第 {loop}{'' if loops == 0 else f'/{loops}'} 轮"))
                if mode == MODE_SEARCH:
                    question = self._take_question()
                    if question is None:
                        self.ui_queue.put(("finished", "题库已用完，搜索停止。"))
                        return
                    self._tap_action(serial, action_map["search_box"], jitter)
                    if self._sleep(self.stop_event, float(action_map["search_box"].get("wait", 0))):
                        break
                    self.adb.input_text(serial, question)
                    self.ui_queue.put(("log", f"输入问题：{question}"))
                    if self._sleep(self.stop_event, search_wait):
                        break
                    self._tap_action(serial, action_map["search_button"], jitter)
                    if self._sleep(self.stop_event, float(action_map["search_button"].get("wait", 0))):
                        break
                    for _ in range(3):
                        if self.stop_event.is_set():
                            break
                        self._tap_action(serial, action_map["back_home"], jitter)
                        if self._sleep(self.stop_event, float(action_map["back_home"].get("wait", 1))):
                            break
                else:
                    self.adb.swipe_up(serial, width, height)
                    self.ui_queue.put(("log", "上滑信息流"))
                    if self._sleep(self.stop_event, 2):
                        break
                    self._tap_action(serial, action_map["content"], jitter)
                    if self._sleep(self.stop_event, feed_wait):
                        break
                    self._tap_action(serial, action_map["back_home"], jitter)
                    if self._sleep(self.stop_event, float(action_map["back_home"].get("wait", 2))):
                        break
            self.ui_queue.put(("finished", "任务已停止。" if self.stop_event.is_set() else "任务已完成。"))
        except Exception as exc:
            self.ui_queue.put(("finished", f"任务失败：{exc}"))

    def _finish_task(self, message: str) -> None:
        self.start_search_button.configure(state="normal")
        self.start_feed_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status_var.set(message)
        self._append_log(message)

    def close(self) -> None:
        self.stop_event.set()
        self._save_device_profile()
        self.destroy()


def main() -> None:
    try:
        app = ScoreGui()
    except AdbError as exc:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("安卓刷分工具", str(exc))
        root.destroy()
        return
    app.mainloop()


if __name__ == "__main__":
    main()
