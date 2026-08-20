# -*- coding: utf-8 -*-
"""
验证码助手 —— 托盘常驻 + MQTT 监听 + 气泡通知 + 自动复制 + 可视化配置

消息模板：
{
    "device_id": "phone_01",
    "sender": "[from]",
    "content": "[msg]"
}

"""
import json
import os
import re
import ssl
import queue
import socket
import subprocess
import sys
import threading
import time


# ------------------------- 自动安装依赖 -------------------------
def ensure_dependencies():
    """检测并自动安装缺失的依赖"""
    # (pip 安装名, import 检测名)
    REQUIRED = [
        ("paho-mqtt>=2.0",   "paho.mqtt.client"),
        ("pystray>=0.19",    "pystray"),
        ("customtkinter>=5.2", "customtkinter"),
        ("pyperclip>=1.9",   "pyperclip"),
        ("Pillow>=10.0",     "PIL"),
    ]

    missing = []
    for pkg, mod in REQUIRED:
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)

    if missing:
        # CREATE_NO_WINDOW: 防止 pythonw 下 pip 弹出控制台
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", *missing, "-q"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags,
        )


if not getattr(sys, "frozen", False):
    ensure_dependencies()

import pyperclip
import paho.mqtt.client as mqtt
import customtkinter as ctk
import pystray
from PIL import Image

APP_NAME = "SmsGlint"
# PyInstaller 打包后 __file__ 指向临时目录，用 sys.executable 获取 exe 所在目录
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
LOCK_PORT = 47893

DEFAULT_CONFIG = {
    "host": "",
    "port": 8883,
    "username": "",
    "password": "",
    "topic": "",
    "auto_copy": True,
}


# ------------------------- 配置读写 -------------------------
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ------------------------- 验证码提取 -------------------------
def extract_code(content, sender):
    all_numbers = re.findall(r"(?<!\d)\d{4,6}(?!\d)", content)
    valid = [n for n in all_numbers if n != sender]
    if not valid:
        return None
    code = valid[0]
    if len(valid) > 1:
        m = re.search(
            r"(?:验证码|校验码|code)\s*[:：]?\s*(\d{4,6})"
            r"|(\d{4,6})\s*(?:是|为).*?(?:验证码|校验码)",
            content, re.IGNORECASE)
        if m:
            code = m.group(1) or m.group(2)
    return code


# ------------------------- MQTT 客户端 -------------------------
class MqttClient:
    def __init__(self, config, on_code, on_log):
        self.config = config
        self.on_code = on_code
        self.on_log = on_log
        self.client = None
        self.lock = threading.Lock()
        self.running = False

    def build(self):
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        c.username_pw_set(self.config.get("username", ""), self.config.get("password", ""))
        # 仅在断线时由底层事件驱动自动重连（退避 1~30s），正常时零额外开销
        c.reconnect_delay_set(min_delay=1, max_delay=30)
        if int(self.config.get("port", 0) or 0) == 8883:
            c.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLSv1_2)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        return c

    def start(self):
        with self.lock:
            self.stop_locked()
            if not self.config.get("host"):
                return
            self.running = True
            self.client = self.build()
            try:
                self.client.connect_async(
                    self.config["host"], int(self.config["port"]), 60)
                self.client.loop_start()
            except Exception as e:
                self.on_log(f"连接失败: {e}")

    def stop(self):
        with self.lock:
            self.stop_locked()

    def stop_locked(self):
        self.running = False
        if self.client:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass
            self.client = None

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            self.on_log(f"已连接 {self.config['host']}，订阅 {self.config['topic']}")
            client.subscribe(self.config["topic"])
        else:
            self.on_log(f"连接被拒绝，返回码 {reason_code}")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        if self.running:
            self.on_log(f"MQTT 连接断开 (代码: {reason_code})，正在尝试自动重连...")

    def _on_message(self, client, userdata, msg):
        # 忽略服务器缓存的历史保留消息
        if getattr(msg, "retain", False):
            self.on_log("⚠️ 忽略一条服务器缓存的历史保留消息 (retain)")
            return

        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            device_id = payload.get("device_id", "unknown")
            sender = payload.get("sender", "")
            content = payload.get("content", "").strip()
            code = extract_code(content, sender)
            if not code:
                self.on_log(f"✉️ 收到来自 [{sender}] 的短信 (未提取到验证码)\n   内容: {content}")
                return
            auto_copy = self.config.get("auto_copy", True)
            copy_hint = " [已自动复制]" if auto_copy else ""
            self.on_log(f"📩 收到验证码: 【{code}】 来自 [{sender}]{copy_hint}\n   内容: {content}")
            self.on_code(device_id, sender, code)
        except Exception as e:
            self.on_log(f"解析消息异常: {e}")


def test_connection(config, callback):
    def run():
        result = {}
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.username_pw_set(config["username"], config["password"])
        port = int(config["port"])
        if port == 8883:
            client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLSv1_2)
        event = threading.Event()

        def on_connect(c, u, f, rc, p):
            result["rc"] = rc
            event.set()

        client.on_connect = on_connect
        try:
            client.connect(config["host"], port, 10)
            client.loop_start()
            if event.wait(8):
                if result.get("rc") == 0:
                    callback(True, "连接成功！")
                else:
                    callback(False, f"连接被拒绝（返回码 {result['rc']}）")
            else:
                callback(False, "连接超时")
        except Exception as e:
            callback(False, f"连接失败：{e}")
        finally:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass

    threading.Thread(target=run, daemon=True).start()


# ------------------------- 单实例锁 -------------------------
class SingleInstance:
    def __init__(self):
        self.sock = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", LOCK_PORT))
            s.listen(1)
            self.sock = s
        except OSError:
            pass

    @property
    def ok(self):
        return self.sock is not None


# ------------------------- 托盘图标 -------------------------
def _find_icon():
    """frozen: 从 _MEIPASS 读内嵌资源；开发: 从源码目录读。"""
    if getattr(sys, "frozen", False):
        # PyInstaller 将 datas 解压到 sys._MEIPASS
        return os.path.join(sys._MEIPASS, "app_icon.ico")
    return os.path.join(APP_DIR, "app_icon.ico")

ICON_FILE = _find_icon()

def make_tray_icon():
    """优先加载 app_icon.ico，找不到时退回纯色兜底图标"""
    try:
        return Image.open(ICON_FILE).convert("RGBA")
    except Exception:
        # 兜底：纯蓝色方块，不依赖 ImageDraw/FreeType
        img = Image.new("RGBA", (64, 64), (43, 125, 233, 255))
        return img


# ------------------------- 主应用 -------------------------
class App:
    def __init__(self):
        self.root = ctk.CTk()
        self.root.withdraw()
        self.config = load_config()
        self.queue = queue.Queue()
        self.logs = []
        self.bubble = None
        self.config_win = None
        self.log_win = None
        self.tray = None

        # Ctrl+C 信号处理（mainloop 会阻塞 KeyboardInterrupt）
        import signal
        signal.signal(signal.SIGINT,
                      lambda *_: self.queue.put(("quit",)))

        self.mqtt = MqttClient(self.config, self._on_code, self._on_log)

        # 首次运行（无配置文件）：弹出配置窗口，暂不连接 MQTT
        if not os.path.exists(CONFIG_FILE):
            self._start_tray()
            self._poll()
            self._open_config()
        else:
            self.mqtt.start()
            self._start_tray()
            self._poll()

        self.root.mainloop()

    # ---------- 跨线程消息 ----------
    def _on_code(self, device_id, sender, code):
        self.queue.put(("code", device_id, sender, code))

    def _on_log(self, text):
        self.queue.put(("log", text))

    def _poll(self):
        try:
            while True:
                msg = self.queue.get_nowait()
                kind = msg[0]
                if kind == "code":
                    _, device_id, sender, code = msg
                    self._show_bubble(device_id, sender, code)
                elif kind == "log":
                    self._append_log(msg[1])
                elif kind == "reconnect":
                    self._append_log("收到手动重连请求，正在连接 MQTT...")
                    self.mqtt.start()
                elif kind == "open_config":
                    self._open_config()
                elif kind == "open_log":
                    self._open_log()
                elif kind == "quit":
                    self.quit()
                    return
        except queue.Empty:
            pass
        self.root.after(150, self._poll)

    # ---------- 日志 ----------
    def _append_log(self, text):
        stamp = time.strftime("%H:%M:%S")
        self.logs.append(f"[{stamp}] {text}")
        if len(self.logs) > 500:
            self.logs.pop(0)
        if self.log_win and self.log_win.winfo_exists():
            self.log_win.append_line(f"[{stamp}] {text}")

    # ---------- 气泡通知 ----------
    def _show_bubble(self, device_id, sender, code):
        if self.config.get("auto_copy", True):
            try:
                pyperclip.copy(code)
            except Exception:
                pass
        if self.bubble and self.bubble.winfo_exists():
            self.bubble.destroy()
        self.bubble = BubbleWindow(self.root, device_id, sender, code)
        self.bubble.show()

    # ---------- 托盘 ----------
    def _start_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("重新连接", lambda: self.queue.put(("reconnect",))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("MQTT 配置", lambda: self.queue.put(("open_config",))),
            pystray.MenuItem("查看日志", lambda: self.queue.put(("open_log",))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda: self.queue.put(("quit",))),
        )
        self.tray = pystray.Icon(APP_NAME, make_tray_icon(), APP_NAME, menu)
        self.tray.run_detached()

    # ---------- 事件分发 ----------
    def _dispatch(self, kind):
        if kind == "open_config":
            self._open_config()
        elif kind == "open_log":
            self._open_log()
        elif kind == "quit":
            self.quit()

    def _open_config(self):
        if self.config_win and self.config_win.winfo_exists():
            self.config_win.lift()
            self.config_win.focus_force()
            return
        self.config_win = ConfigWindow(self)

    def _open_log(self):
        if self.log_win and self.log_win.winfo_exists():
            self.log_win.lift()
            self.log_win.focus_force()
            return
        self.log_win = LogWindow(self)

    def apply_config(self, new_cfg):
        self.config = new_cfg
        save_config(new_cfg)
        self.mqtt.config = new_cfg
        self.mqtt.start()
        self._append_log("配置已保存，MQTT 已重新连接")

    def quit(self):
        try:
            if self.tray:
                self.tray.stop()
        except Exception:
            pass
        self.mqtt.stop()
        self.root.after(200, self.root.destroy)


# ------------------------- 气泡窗口 -------------------------
TRANSPARENT = "#010101"

BUBBLE_W = 300
BUBBLE_H = 140


class BubbleWindow(ctk.CTkToplevel):
    def __init__(self, master, device_id, sender, code):
        super().__init__(master)
        self.code = code
        # 先以透明度 0 显示（不用 withdraw，避免 overrideredirect 与 deiconify 的 Windows 兼容性问题）
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0)
        self.configure(fg_color=TRANSPARENT)
        self.attributes("-transparentcolor", TRANSPARENT)

        card = ctk.CTkFrame(self, width=BUBBLE_W, height=BUBBLE_H,
                            corner_radius=14, fg_color="#1f2430",
                            border_width=1, border_color="#3a4152")
        card.pack_propagate(False)
        card.pack(fill="both", expand=True)
        card.bind("<Button-1>", lambda e: self._copy_and_close())

        title = "收到验证码" if not sender else "来自短信验证码"
        ctk.CTkLabel(card, text=title, font=ctk.CTkFont("Microsoft YaHei UI", 12),
                     text_color="#9aa5b8").pack(pady=(10, 0))

        info = (sender or "未知来源") + (f"  ·  {device_id}" if device_id else "")
        ctk.CTkLabel(card, text=info, font=ctk.CTkFont("Microsoft YaHei UI", 11),
                     text_color="#6b7689").pack(pady=(1, 0))

        code_font = ctk.CTkFont("Consolas", 26, weight="bold")
        ctk.CTkLabel(card, text=code, font=code_font,
                     text_color="#4fc3f7").pack(pady=(4, 0))

        ctk.CTkLabel(card, text="已自动复制 · 点击可再复制",
                     font=ctk.CTkFont("Microsoft YaHei UI", 10),
                     text_color="#5a6580").pack(pady=(0, 8))

    @staticmethod
    def _get_work_area():
        """获取 Windows 工作区右下角坐标（物理像素），geometry() 直接使用。"""
        try:
            import ctypes

            class RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_int), ("top",    ctypes.c_int),
                            ("right", ctypes.c_int), ("bottom", ctypes.c_int)]

            wa = RECT()
            ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(wa), 0)
            # 物理屏幕宽度（用于滑入动画起始位置）
            phys_w = ctypes.windll.user32.GetSystemMetrics(0)
            return wa.right, wa.bottom, phys_w
        except Exception:
            return 1920, 1080, 1920   # 合理的 fallback

    def show(self):
        self.update_idletasks()
        # geometry() 使用物理像素坐标，直接用 ctypes 获取工作区
        work_right, work_bottom, phys_screen_w = self._get_work_area()
        self._screen_w = phys_screen_w

        # geometry 的尺寸是逻辑像素，实际渲染会乘以 window_scaling
        # 计算坐标时必须用实际物理尺寸预留空间
        try:
            ws = ctk.ScalingTracker.get_window_scaling(self)
        except Exception:
            ws = 1.0
        phys_w = int(BUBBLE_W * ws)
        phys_h = int(BUBBLE_H * ws)

        self._target_x = work_right  - phys_w - 16
        self._target_y = work_bottom - phys_h - 8

        # 初始位置在屏幕右侧外部（物理像素坐标）
        self.geometry(f"{BUBBLE_W}x{BUBBLE_H}+{self._screen_w}+{self._target_y}")
        self.update()
        self.attributes("-alpha", 1)
        self.after(20, lambda: self._animate_in(0))
        self.after(8000, self._slide_out)

    def _animate_in(self, step):
        progress = step / 20
        x = self._target_x + int((self._screen_w - self._target_x) * (1 - progress))
        self.geometry(f"{BUBBLE_W}x{BUBBLE_H}+{x}+{self._target_y}")
        if step < 20:
            self.after(10, lambda: self._animate_in(step + 1))
        else:
            self._out = False

    def _slide_out(self):
        if getattr(self, "_out", False):
            return
        self._out = True
        self._animate_out(0)

    def _animate_out(self, step):
        progress = step / 20
        x = self._target_x + int((self._screen_w - self._target_x) * progress)
        self.geometry(f"{BUBBLE_W}x{BUBBLE_H}+{x}+{self._target_y}")
        if step < 20:
            self.after(10, lambda: self._animate_out(step + 1))
        else:
            self.destroy()

    def _copy_and_close(self):
        try:
            pyperclip.copy(self.code)
        except Exception:
            pass
        self.destroy()


# ------------------------- 配置窗口 -------------------------
class ConfigWindow(ctk.CTkToplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("MQTT 配置")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.geometry("+{}+{}".format(
            int((self.winfo_screenwidth() - 400) / 2),
            int((self.winfo_screenheight() - 500) / 2)))
        if os.path.exists(ICON_FILE):
            self.after(200, lambda: self.iconbitmap(ICON_FILE))

        pad = {"padx": 24, "pady": 6}
        ctk.CTkLabel(self, text="Serverless MQTT 配置",
                     font=ctk.CTkFont("Microsoft YaHei UI", 18, weight="bold")).pack(pady=(18, 6))

        def field(label, var, show=None):
            ctk.CTkLabel(self, text=label,
                         font=ctk.CTkFont("Microsoft YaHei UI", 12)).pack(anchor="w", **pad)
            e = ctk.CTkEntry(self, textvariable=var, show=show, width=340,
                             font=ctk.CTkFont("Microsoft YaHei UI", 12))
            e.pack(**pad)
            return e

        self.var_host = ctk.StringVar(value=app.config.get("host", ""))
        self.var_port = ctk.StringVar(value=str(app.config.get("port", 8883)))
        self.var_user = ctk.StringVar(value=app.config.get("username", ""))
        self.var_pass = ctk.StringVar(value=app.config.get("password", ""))
        self.var_topic = ctk.StringVar(value=app.config.get("topic", ""))
        self.var_autocopy = ctk.BooleanVar(value=bool(app.config.get("auto_copy", True)))

        field("服务器地址 (Host)", self.var_host)
        field("端口 (1883 / 8883)", self.var_port)
        field("用户名", self.var_user)
        field("密码", self.var_pass, show="*")
        field("订阅主题 (Topic)", self.var_topic)

        ctk.CTkCheckBox(self, text="自动复制验证码到剪切板",
                        variable=self.var_autocopy,
                        font=ctk.CTkFont("Microsoft YaHei UI", 12),
                        checkbox_width=20, checkbox_height=20).pack(anchor="w", padx=24, pady=(8, 4))

        self.status = ctk.CTkLabel(self, text="", font=ctk.CTkFont("Microsoft YaHei UI", 12))
        self.status.pack(pady=(4, 0))

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(pady=(12, 16))
        ctk.CTkButton(btns, text="测试连接", width=110,
                      fg_color="#2b7de9", hover_color="#1f6fd6",
                      command=self._test).pack(side="left", padx=8)
        ctk.CTkButton(btns, text="保存", width=110,
                      fg_color="#2e7d32", hover_color="#25632a",
                      command=self._save).pack(side="left", padx=8)

        self.grab_set()

    def _collect(self):
        return {
            "host": self.var_host.get().strip(),
            "port": int(self.var_port.get().strip() or 1883),
            "username": self.var_user.get().strip(),
            "password": self.var_pass.get(),
            "topic": self.var_topic.get().strip(),
            "auto_copy": bool(self.var_autocopy.get()),
        }

    def _test(self):
        self.status.configure(text="测试中...", text_color="#f0b429")
        test_connection(self._collect(), self._test_done)

    def _test_done(self, ok, msg):
        self.after(0, lambda: self.status.configure(
            text=msg, text_color="#66bb6a" if ok else "#ef5350"))

    def _save(self):
        try:
            cfg = self._collect()
        except ValueError:
            self.status.configure(text="端口必须是数字", text_color="#ef5350")
            return
        self.app.apply_config(cfg)
        self.status.configure(text="已保存并重新连接", text_color="#66bb6a")
        self.destroy()


# ------------------------- 日志窗口 -------------------------
class LogWindow(ctk.CTkToplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("运行日志")
        self.resizable(True, True)
        self.geometry("520x360")
        self.attributes("-topmost", True)
        self.geometry("+{}+{}".format(
            int((self.winfo_screenwidth() - 520) / 2),
            int((self.winfo_screenheight() - 360) / 2)))
        if os.path.exists(ICON_FILE):
            self.after(200, lambda: self.iconbitmap(ICON_FILE))

        self.text = ctk.CTkTextbox(self, font=ctk.CTkFont("Consolas", 12),
                                   wrap="word", state="disabled")
        self.text.pack(fill="both", expand=True, padx=12, pady=12)
        for line in app.logs:
            self._append(line)

    def append_line(self, line):
        self.after(0, lambda: self._append(line))

    def _append(self, line):
        self.text.configure(state="normal")
        self.text.insert("end", line + "\n")
        self.text.see("end")
        self.text.configure(state="disabled")


# ------------------------- 入口 -------------------------
def main():
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    lock = SingleInstance()
    if not lock.ok:
        import tkinter.messagebox as mb
        mb.showerror(APP_NAME, "程序已在运行中")
        return

    App()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
