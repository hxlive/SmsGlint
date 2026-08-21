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
import importlib
import importlib.util
import os
import re
import ssl
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox


# ------------------------- 依赖引导 -------------------------
DEPENDENCIES = (
    ("paho-mqtt>=2.0,<3", "paho"),
    ("pystray>=0.19,<1", "pystray"),
    ("customtkinter>=5.2,<6", "customtkinter"),
    ("pyperclip>=1.9,<2", "pyperclip"),
    ("Pillow>=10,<13", "PIL"),
)


def find_missing_dependencies(dependencies=DEPENDENCIES,
                              finder=importlib.util.find_spec):
    """只检测白名单模块，不导入或执行第三方包。"""
    return [package for package, module in dependencies if finder(module) is None]


def install_dependencies(packages, runner=subprocess.run):
    """安装用户已确认的白名单依赖，禁止 shell 和源码包构建。"""
    command = [
        sys.executable, "-m", "pip", "install",
        "--disable-pip-version-check", "--no-input",
        "--only-binary=:all:", *packages,
    ]
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    result = runner(
        command, shell=False, check=False, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=flags)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "pip 未返回错误详情").strip()
        raise RuntimeError(detail)


def _dependency_prompt(packages):
    root = tk.Tk()
    root.withdraw()
    try:
        package_list = "\n".join(f"• {name}" for name in packages)
        return messagebox.askyesno(
            "SmsGlint - 缺少运行组件",
            "检测到以下官方 Python 组件尚未安装：\n\n"
            f"{package_list}\n\n"
            "是否现在从 Python 包索引下载并安装？\n"
            "只有点击“是”后才会联网。",
            parent=root)
    finally:
        root.destroy()


def _dependency_error(message):
    root = tk.Tk()
    root.withdraw()
    try:
        messagebox.showerror(
            "SmsGlint - 组件安装失败", str(message), parent=root)
    finally:
        root.destroy()


def ensure_dependencies(confirm=_dependency_prompt,
                        installer=install_dependencies,
                        show_error=_dependency_error):
    """经用户确认后补齐源码运行依赖；取消或失败时返回 False。"""
    missing = find_missing_dependencies()
    if not missing:
        return True
    if not confirm(missing):
        return False
    try:
        installer(missing)
        importlib.invalidate_caches()
        remaining = find_missing_dependencies()
        if remaining:
            raise RuntimeError("安装完成后仍无法找到：" + "、".join(remaining))
        return True
    except (OSError, RuntimeError) as e:
        show_error(e)
        return False


if not getattr(sys, "frozen", False) and not ensure_dependencies():
    raise SystemExit(1)

import pyperclip
import paho.mqtt.client as mqtt
import customtkinter as ctk
import pystray
from PIL import Image

APP_NAME = "SmsGlint"
APP_VERSION = "1.2.0"
UI_FONT = "Microsoft YaHei UI"
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
    "use_tls": True,
    "auto_copy": True,
    "play_sound": True,
}


# ------------------------- 跨平台开机自启动 -------------------------
def get_autostart_status():
    """获取当前开机自启动状态"""
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run",
                    0, winreg.KEY_READ) as key:
                value, _ = winreg.QueryValueEx(key, APP_NAME)
                return bool(value)
        except OSError:
            return False
    elif sys.platform == "darwin":
        plist_path = os.path.expanduser(f"~/Library/LaunchAgents/com.{APP_NAME.lower()}.app.plist")
        return os.path.exists(plist_path)
    return False


def set_autostart(enable: bool):
    """设置或取消开机自启动，返回 (是否成功, 错误信息)。"""
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run",
                    0, winreg.KEY_SET_VALUE) as key:
                if enable:
                    if getattr(sys, "frozen", False):
                        target = f'"{sys.executable}" --background'
                    else:
                        script_path = os.path.abspath(__file__)
                        py_exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
                        if not os.path.exists(py_exe):
                            py_exe = sys.executable
                        target = f'"{py_exe}" "{script_path}" --background'
                    winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, target)
                else:
                    try:
                        winreg.DeleteValue(key, APP_NAME)
                    except FileNotFoundError:
                        pass
            return True, ""
        except OSError as e:
            return False, f"开机启动设置失败：{e}"
    elif sys.platform == "darwin":
        plist_path = os.path.expanduser(f"~/Library/LaunchAgents/com.{APP_NAME.lower()}.app.plist")
        if enable:
            try:
                import plistlib
                os.makedirs(os.path.dirname(plist_path), exist_ok=True)
                arguments = [sys.executable, "--background"]
                if not getattr(sys, "frozen", False):
                    arguments.insert(1, os.path.abspath(__file__))
                plist = {
                    "Label": f"com.{APP_NAME.lower()}.app",
                    "ProgramArguments": arguments,
                    "RunAtLoad": True,
                }
                with open(plist_path, "wb") as stream:
                    plistlib.dump(plist, stream)
                return True, ""
            except OSError as e:
                return False, f"开机启动设置失败：{e}"
        else:
            if os.path.exists(plist_path):
                try:
                    os.remove(plist_path)
                except OSError as e:
                    return False, f"开机启动设置失败：{e}"
            return True, ""
    if enable:
        return False, "当前系统暂不支持自动设置开机启动"
    return True, ""


# ------------------------- 提示音播放 -------------------------
def play_notification_sound():
    """轻量播放系统提示音；成功返回 None，失败返回错误文本。"""
    try:
        if sys.platform == "win32":
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        elif sys.platform == "darwin":
            subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, RuntimeError) as e:
        return str(e)
    return None


# ------------------------- 配置读写 -------------------------
CONFIG_WARNINGS = []


def _as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("1", "true", "yes", "on"):
            return True
        if normalized in ("0", "false", "no", "off", ""):
            return False
    if value is None:
        return default
    return bool(value)


def validate_config(cfg):
    """校验并返回可安全使用、保存的配置副本。"""
    raw = dict(cfg or {})
    normalized = dict(DEFAULT_CONFIG)
    normalized.update(raw)
    normalized["host"] = str(normalized.get("host", "")).strip()
    normalized["username"] = str(normalized.get("username", "")).strip()
    normalized["password"] = str(normalized.get("password", ""))
    normalized["topic"] = str(normalized.get("topic", "")).strip()

    try:
        normalized["port"] = int(str(normalized.get("port", "")).strip())
    except (TypeError, ValueError):
        raise ValueError("端口必须是 1 到 65535 之间的数字") from None
    if not 1 <= normalized["port"] <= 65535:
        raise ValueError("端口必须是 1 到 65535 之间的数字")
    if not normalized["host"]:
        raise ValueError("服务器地址不能为空")
    if not normalized["topic"]:
        raise ValueError("订阅主题不能为空")

    # 旧版配置没有 use_tls：仅此时继续按常见的 8883 端口推断。
    if "use_tls" not in raw:
        normalized["use_tls"] = normalized["port"] == 8883
    else:
        normalized["use_tls"] = _as_bool(raw.get("use_tls"), True)
    normalized["auto_copy"] = _as_bool(normalized.get("auto_copy"), True)
    normalized["play_sound"] = _as_bool(normalized.get("play_sound"), True)
    return normalized


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if not isinstance(saved, dict):
            raise ValueError("配置文件顶层必须是 JSON 对象")
        cfg.update(saved)
        if "use_tls" not in saved:
            try:
                cfg["use_tls"] = int(cfg.get("port", 0)) == 8883
            except (TypeError, ValueError):
                cfg["use_tls"] = False
    except FileNotFoundError:
        return cfg
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
        CONFIG_WARNINGS.append(f"读取配置失败，将使用默认值：{e}")
    return cfg


def save_config(cfg):
    """原子保存配置，避免异常退出留下半个 JSON 文件。"""
    temp_file = CONFIG_FILE + ".tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, CONFIG_FILE)
    except Exception:
        try:
            if os.path.exists(temp_file):
                os.remove(temp_file)
        except OSError:
            pass
        raise


def config_is_complete(cfg):
    """启动连接前检查必要字段，避免拿明显无效的配置等待超时。"""
    try:
        validate_config(cfg)
    except (TypeError, ValueError):
        return False
    return True


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


def extract_platform(content):
    """从短信签名【平台名】中提取验证码所属平台。"""
    match = re.search(r"【\s*([^【】]{1,30}?)\s*】", content or "")
    return match.group(1).strip() if match else ""


MAX_MQTT_PAYLOAD_BYTES = 64 * 1024
MAX_SMS_CONTENT_CHARS = 16 * 1024
MAX_SMS_FIELD_CHARS = 128


def _text_field(payload, name, default="", max_length=MAX_SMS_FIELD_CHARS):
    value = payload.get(name, default)
    if not isinstance(value, str):
        raise ValueError(f"字段 {name} 必须是文本")
    value = value.strip()
    if len(value) > max_length:
        raise ValueError(f"字段 {name} 超过长度限制")
    return value


def parse_sms_payload(raw_payload):
    """解析并限制 MQTT 短信载荷，返回显示所需的固定字段。"""
    if not isinstance(raw_payload, (bytes, bytearray)):
        raise ValueError("MQTT 消息载荷必须是字节数据")
    if len(raw_payload) > MAX_MQTT_PAYLOAD_BYTES:
        raise ValueError("MQTT 消息过大，已拒绝处理")
    try:
        payload = json.loads(raw_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"MQTT 消息不是有效的 UTF-8 JSON：{e}") from None
    if not isinstance(payload, dict):
        raise ValueError("MQTT 消息顶层必须是 JSON 对象")

    device_id = _text_field(payload, "device_id", "unknown") or "unknown"
    sender = _text_field(payload, "sender")
    content = _text_field(
        payload, "content", max_length=MAX_SMS_CONTENT_CHARS)
    return device_id, sender, content


# ------------------------- MQTT 客户端 -------------------------
def configure_mqtt_client(client, config):
    """应用认证和 TLS 设置；TLS 使用系统信任库及当前安全默认值。"""
    username = config.get("username", "")
    if username:
        client.username_pw_set(username, config.get("password", ""))
    if config.get("use_tls", False):
        client.tls_set_context(ssl.create_default_context())


def subscription_failed(reason_codes):
    """兼容 paho ReasonCode 和旧式整数订阅返回码。"""
    for code in reason_codes or []:
        if getattr(code, "is_failure", False):
            return True
        try:
            if int(code) >= 128:
                return True
        except (TypeError, ValueError):
            continue
    return False


class MqttClient:
    def __init__(self, config, on_code, on_log, on_status):
        self.config = config
        self.on_code = on_code
        self.on_log = on_log
        self.on_status = on_status
        self.client = None
        self.lock = threading.Lock()
        self.running = False

    def build(self):
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        configure_mqtt_client(c, self.config)
        # 仅在断线时由底层事件驱动自动重连（退避 1~30s），正常时零额外开销
        c.reconnect_delay_set(min_delay=1, max_delay=30)
        c.on_connect = self._on_connect
        c.on_subscribe = self._on_subscribe
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        return c

    def start(self):
        with self.lock:
            self.stop_locked()
            if not self.config.get("host"):
                self.on_status(False, "缺少服务器地址")
                return
            self.running = True
            try:
                self.client = self.build()
                self.client.connect_async(
                    self.config["host"], int(self.config["port"]), 60)
                self.client.loop_start()
            except Exception as e:
                self.on_log(f"连接失败: {e}")
                self.on_status(False, str(e))

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
            try:
                result, _ = client.subscribe(self.config["topic"])
                if result != mqtt.MQTT_ERR_SUCCESS:
                    raise RuntimeError(f"订阅失败，返回码 {result}")
                self.on_log(f"已连接 {self.config['host']}，正在订阅 {self.config['topic']}")
            except Exception as e:
                self.on_log(f"订阅失败: {e}")
                self.on_status(False, str(e))
        else:
            self.on_log(f"连接被拒绝，返回码 {reason_code}")
            self.on_status(False, f"服务器拒绝连接（{reason_code}）")

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties):
        if subscription_failed(reason_codes):
            self.on_log(f"订阅被服务器拒绝：{reason_codes}")
            self.on_status(False, "服务器拒绝订阅该主题")
            return
        self.on_log(f"已订阅 {self.config['topic']}")
        self.on_status(True, "连接及订阅成功")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        if self.running:
            self.on_log(f"MQTT 连接断开 (代码: {reason_code})，正在尝试自动重连...")

    def _on_message(self, client, userdata, msg):
        # 忽略服务器缓存的历史保留消息
        if getattr(msg, "retain", False):
            self.on_log("⚠️ 忽略一条服务器缓存的历史保留消息 (retain)")
            return

        try:
            device_id, sender, content = parse_sms_payload(msg.payload)
            code = extract_code(content, sender)
            platform = extract_platform(content)
            if not code:
                source = platform or sender or "未知来源"
                self.on_log(f"✉️ 收到来自 [{source}] 的短信（未提取到验证码）")
                return
            auto_copy = self.config.get("auto_copy", True)
            copy_hint = " [已自动复制]" if auto_copy else ""
            source = platform or sender or "未知来源"
            self.on_log(f"📩 收到来自 [{source}] 的验证码{copy_hint}")
            self.on_code(device_id, sender, code, platform)
        except Exception as e:
            self.on_log(f"解析消息异常: {e}")


def test_connection(config, callback):
    def run():
        result = {"ok": False, "message": "连接超时（8 秒）"}
        result_lock = threading.Lock()
        event = threading.Event()
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

        def finish(ok, message):
            with result_lock:
                if event.is_set():
                    return
                result["ok"] = bool(ok)
                result["message"] = str(message)
                event.set()

        def on_connect(c, u, f, rc, p):
            if rc != 0:
                finish(False, f"连接被拒绝（返回码 {rc}）")
                return
            subscribe_result, _ = c.subscribe(config["topic"])
            if subscribe_result != mqtt.MQTT_ERR_SUCCESS:
                finish(False, f"订阅请求失败（返回码 {subscribe_result}）")

        def on_subscribe(c, u, mid, reason_codes, properties):
            if subscription_failed(reason_codes):
                finish(False, "连接成功，但服务器拒绝订阅该主题")
            else:
                finish(True, "连接及订阅成功！")

        def on_connect_fail(c, u):
            finish(False, "无法连接到服务器")

        client.on_connect = on_connect
        client.on_subscribe = on_subscribe
        client.on_connect_fail = on_connect_fail
        try:
            configure_mqtt_client(client, config)
            # paho 的 connect_async 成功时返回 None；真正结果由回调给出。
            client.connect_async(
                config["host"], int(config["port"]), keepalive=60)
            client.loop_start()
            if not event.wait(8):
                finish(False, "连接超时（8 秒）")
        except Exception as e:
            finish(False, f"连接失败：{e}")
        finally:
            try:
                client.disconnect()
                client.loop_stop()
            except (OSError, RuntimeError):
                pass
        callback(result["ok"], result["message"])

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
        bundled = os.path.join(getattr(sys, "_MEIPASS", APP_DIR), "app_icon.ico")
        if os.path.exists(bundled):
            return bundled
    return os.path.join(APP_DIR, "app_icon.ico")

ICON_FILE = _find_icon()


def make_tray_icon():
    """优先加载 app_icon.ico，找不到时退回纯色兜底图标"""
    try:
        return Image.open(ICON_FILE).convert("RGBA")
    except Exception:
        return Image.new("RGBA", (64, 64), (43, 125, 233, 255))


def set_window_icon(window):
    """为 Tk 顶层窗口设置应用图标；图标缺失不影响窗口创建。"""
    if not os.path.exists(ICON_FILE):
        return

    def apply_icon():
        try:
            if window.winfo_exists():
                window.iconbitmap(ICON_FILE)
        except (tk.TclError, OSError):
            pass

    window.after(200, apply_icon)


def cancel_after(widget, callback_id):
    """取消可能已失效的 Tk 回调，并统一清空调用方保存的 ID。"""
    if callback_id:
        try:
            widget.after_cancel(callback_id)
        except Exception:
            pass
    return None


def set_tray_attribute(tray, name, value):
    """安全更新 pystray 属性；托盘不可用时返回 False。"""
    if not tray:
        return False
    try:
        setattr(tray, name, value)
        return True
    except Exception:
        return False


def compact_text(value, fallback, limit=16):
    """生成适合紧凑 UI 的单行文本，超长时保留省略号。"""
    text = str(value or fallback).strip() or fallback
    return text if len(text) <= limit else text[:limit - 1] + "…"


def copy_to_clipboard(value):
    """复制文本；成功返回 None，失败返回可记录的错误文本。"""
    try:
        pyperclip.copy(str(value))
    except Exception as e:
        return str(e)
    return None


# ------------------------- 主应用 -------------------------
class App:
    def __init__(self):
        self.root = ctk.CTk()
        self.root.withdraw()
        self.background = "--background" in sys.argv[1:]
        loaded_config = load_config()
        try:
            self.config = validate_config(loaded_config)
        except ValueError:
            # 保留无效字段供设置窗口回显，启动检查会阻止 MQTT 使用它。
            self.config = loaded_config
        self.queue = queue.Queue()
        self.logs = [f"[启动] {warning}" for warning in CONFIG_WARNINGS]
        self.bubble = None
        self.config_win = None
        self.log_win = None
        self.tray = None

        # 状态管理
        self.latest_code = None
        self.default_icon = make_tray_icon()
        self.blank_icon = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        self._is_flashing = False
        self._flash_count = 0
        self._flash_timer = None
        self._startup_check_pending = False
        self._startup_check_timer = None

        # Ctrl+C 信号处理（mainloop 会阻塞 KeyboardInterrupt）
        import signal
        signal.signal(signal.SIGINT,
                      lambda *_: self.queue.put(("quit",)))

        self.mqtt = MqttClient(
            self.config, self._on_code, self._on_log,
            self._on_connection_status)

        has_config = os.path.exists(CONFIG_FILE)
        has_valid_config = has_config and config_is_complete(self.config)
        if has_valid_config:
            self._startup_check_pending = not self.background
            self.mqtt.start()

        self._start_tray()
        self._poll()

        # 有完整配置时复用真实 MQTT 连接做启动验证；连接正常则保持托盘静默。
        # 配置缺失时立即打开设置，连接失败或超时则由状态回调打开设置。
        if not self.background:
            if not has_valid_config:
                self.root.after(0, self._open_config)
            elif self._startup_check_pending:
                self._startup_check_timer = self.root.after(
                    8000, self._startup_check_timeout)

        self.root.mainloop()

    # ---------- 跨线程消息 ----------
    def _on_code(self, device_id, sender, code, platform):
        self.queue.put(("code", device_id, sender, code, platform))

    def _on_log(self, text):
        self.queue.put(("log", text))

    def _on_connection_status(self, ok, detail):
        self.queue.put(("connection_status", bool(ok), str(detail)))

    def _finish_startup_check(self, ok, detail):
        if not self._startup_check_pending:
            return
        self._startup_check_pending = False
        self._startup_check_timer = cancel_after(
            self.root, self._startup_check_timer)
        if ok:
            self._append_log("启动连接验证通过，保持托盘后台运行")
        else:
            self._append_log(f"启动连接验证失败: {detail}")
            self._open_config()

    def _startup_check_timeout(self):
        self._startup_check_timer = None
        self._finish_startup_check(False, "8 秒内未能连接服务器")

    def _poll(self):
        try:
            while True:
                msg = self.queue.get_nowait()
                try:
                    kind = msg[0]
                    if kind == "code":
                        _, device_id, sender, code, platform = msg
                        self.latest_code = code
                        self._update_tray_menu()
                        self._start_tray_flash()
                        if self.config.get("play_sound", True):
                            error = play_notification_sound()
                            if error:
                                self._append_log(f"播放提示音失败: {error}")
                        self._show_bubble(device_id, sender, code, platform)
                    elif kind == "copy_latest":
                        self._copy_latest_code()
                    elif kind == "log":
                        self._append_log(msg[1])
                    elif kind == "connection_status":
                        _, ok, detail = msg
                        self._finish_startup_check(ok, detail)
                    elif kind == "config_test_result":
                        _, window, ok, detail = msg
                        if (window is self.config_win and
                                window.winfo_exists()):
                            window._show_test_result(ok, detail)
                    elif kind == "reconnect":
                        self._append_log("收到手动重连请求，正在连接 MQTT...")
                        self.mqtt.start()
                    elif kind == "open_config":
                        self._stop_tray_flash()
                        self._open_config()
                    elif kind == "open_log":
                        self._stop_tray_flash()
                        self._open_log()
                    elif kind == "quit":
                        self.quit()
                        return
                except Exception as e:
                    # 单个 UI 操作失败时不能终止事件泵，否则托盘菜单也会失效。
                    self._append_log(f"处理事件 {msg[0]!r} 时发生异常: {e}")
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

    # ---------- 复制最新验证码 ----------
    def _copy_latest_code(self):
        self._stop_tray_flash()
        if self.latest_code:
            error = copy_to_clipboard(self.latest_code)
            if error:
                self._append_log(f"从托盘复制验证码失败: {error}")
            else:
                self._append_log("📋 已从托盘重新复制最新验证码")

    # ---------- 气泡通知 ----------
    def _show_bubble(self, device_id, sender, code, platform):
        if self.config.get("auto_copy", True):
            error = copy_to_clipboard(code)
            if error:
                self._append_log(f"自动复制验证码失败: {error}")
        if self.bubble and self.bubble.winfo_exists():
            self.bubble._destroy_safely()
        self.bubble = BubbleWindow(
            self, device_id, sender, code, platform=platform)
        self.bubble.show()

    # ---------- 托盘构建与管理 ----------
    def _build_tray_menu(self):
        if self.latest_code:
            code_title = "📋 最新验证码已收到 (点击复制)"
            code_item = pystray.MenuItem(code_title, lambda: self.queue.put(("copy_latest",)))
        else:
            code_item = pystray.MenuItem("📋 最新验证码: (暂无)", None, enabled=False)

        return pystray.Menu(
            code_item,
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("重新连接", lambda: self.queue.put(("reconnect",))),
            pystray.MenuItem("MQTT 配置", lambda: self.queue.put(("open_config",)), default=True),
            pystray.MenuItem("查看日志", lambda: self.queue.put(("open_log",))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda: self.queue.put(("quit",))),
        )

    def _start_tray(self):
        self.tray = pystray.Icon(APP_NAME, self.default_icon, APP_NAME, self._build_tray_menu())
        self.tray.run_detached()

    def _update_tray_menu(self):
        if self.tray:
            set_tray_attribute(self.tray, "menu", self._build_tray_menu())

    # ---------- 托盘闪烁动画 ----------
    def _start_tray_flash(self):
        self._stop_tray_flash()
        self._flash_count = 0
        self._is_flashing = True
        self._do_tray_flash()

    def _do_tray_flash(self):
        if not self._is_flashing or not self.tray:
            return
        if self._flash_count >= 10:  # 闪烁10次(约4秒)后自动恢复常亮
            self._stop_tray_flash()
            return
        self._flash_count += 1
        # 奇数帧空白，偶数帧原图标
        icon_img = self.blank_icon if (self._flash_count % 2 == 1) else self.default_icon
        set_tray_attribute(self.tray, "icon", icon_img)
        self._flash_timer = self.root.after(400, self._do_tray_flash)

    def _stop_tray_flash(self):
        self._is_flashing = False
        self._flash_timer = cancel_after(self.root, self._flash_timer)
        set_tray_attribute(self.tray, "icon", self.default_icon)

    def _open_config(self):
        self._open_window("config_win", ConfigWindow)

    def _open_log(self):
        self._open_window("log_win", LogWindow)

    def _open_window(self, attribute, window_type):
        window = getattr(self, attribute)
        if window and window.winfo_exists():
            window.lift()
            window.focus_force()
            return
        setattr(self, attribute, window_type(self))

    def apply_config(self, new_cfg):
        normalized = validate_config(new_cfg)
        save_config(normalized)
        self.config = normalized
        self.mqtt.config = normalized
        self.mqtt.start()
        self._append_log("配置已保存，MQTT 已重新连接")

    def quit(self):
        self._stop_tray_flash()
        try:
            if self.tray:
                self.tray.stop()
        except Exception:
            pass
        self.mqtt.stop()
        self.root.after(200, self.root.destroy)


# ------------------------- 气泡窗口 -------------------------
TRANSPARENT = "#010101"

BUBBLE_W = 280
BUBBLE_H = 145


def make_code_glow_image(width=210, height=3):
    """生成无接缝的水平渐变微光线，中间亮、两端透明。"""
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    pixels = image.load()
    center_x = (width - 1) / 2
    center_y = (height - 1) / 2
    for x in range(width):
        horizontal = max(0.0, 1.0 - abs(x - center_x) / center_x)
        horizontal = horizontal ** 1.25
        for y in range(height):
            vertical = max(0.0, 1.0 - abs(y - center_y) / (center_y + 1))
            alpha = int(145 * horizontal * (0.3 + 0.7 * vertical))
            pixels[x, y] = (104, 208, 255, alpha)
    return image


class BubbleWindow(ctk.CTkToplevel):
    def __init__(self, app, device_id, sender, code,
                 platform="", auto_close_ms=8000):
        super().__init__(app.root)
        self.app = app
        self.code = code
        self.auto_close_ms = auto_close_ms
        self._closing = False
        self._animation_after_id = None
        self._auto_close_after_id = None
        # 先以透明度 0 显示（不用 withdraw，避免 overrideredirect 与 deiconify 的 Windows 兼容性问题）
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0)
        self.configure(fg_color=TRANSPARENT)
        self.attributes("-transparentcolor", TRANSPARENT)

        click_targets = [self]

        def packed(widget, **options):
            widget.pack(**options)
            click_targets.append(widget)
            return widget

        card = packed(
            ctk.CTkFrame(self, width=BUBBLE_W, height=BUBBLE_H,
                         corner_radius=16, fg_color="#171B24",
                         border_width=1, border_color="#303A4A"),
            fill="both", expand=True)
        card.pack_propagate(False)

        # 顶部保留紧凑的软件图标、品牌名和手机来源。
        header = packed(
            ctk.CTkFrame(card, fg_color="transparent", height=28),
            fill="x", padx=13, pady=(10, 0))
        header.pack_propagate(False)

        if os.path.exists(ICON_FILE):
            try:
                icon_image = Image.open(ICON_FILE).convert("RGBA")
                self._brand_icon_image = ctk.CTkImage(
                    light_image=icon_image, dark_image=icon_image,
                    size=(22, 22))
                packed(ctk.CTkLabel(
                    header, text="", image=self._brand_icon_image,
                    width=24, height=24), side="left", pady=2)
            except Exception:
                pass

        packed(ctk.CTkLabel(
            header, text="SmsGlint", anchor="w",
            font=ctk.CTkFont("Segoe UI", 13, weight="bold"),
            text_color="#F4F7FB", height=22),
            side="left", padx=(7, 0), pady=2)

        source = compact_text(device_id or sender, "未知设备")
        packed(ctk.CTkLabel(
            header, text=source, height=22, corner_radius=8,
            fg_color="#222936",
            font=ctk.CTkFont(UI_FONT, 9),
            text_color="#AAB4C3"), side="right", pady=4, ipadx=7)

        code_area = packed(
            ctk.CTkFrame(card, fg_color="transparent"),
            fill="x", padx=14, pady=(8, 0))
        platform_text = compact_text(platform, "验证码")
        packed(ctk.CTkLabel(
            code_area, text=platform_text,
            font=ctk.CTkFont(UI_FONT, 11, weight="bold"),
            text_color="#8FA4B9", height=15))
        packed(ctk.CTkLabel(
            code_area, text="  ".join(str(code)),
            font=ctk.CTkFont("Segoe UI Variable Display", 34, weight="bold"),
            text_color="#68D0FF", height=42))

        # 逐像素生成连续渐变，避免多段 Frame 叠加产生可见接缝。
        glow_image = make_code_glow_image()
        self._code_glow_image = ctk.CTkImage(
            light_image=glow_image, dark_image=glow_image,
            size=(210, 3))
        packed(ctk.CTkLabel(
            code_area, text="", image=self._code_glow_image,
            width=210, height=3, fg_color="transparent"), pady=(0, 1))

        # 页脚先固定在底部，分割线随后贴在它上方，避免底部残留大块空白。
        footer = packed(
            ctk.CTkFrame(card, fg_color="transparent", height=22),
            side="bottom", fill="x", padx=16, pady=(0, 7))
        footer.pack_propagate(False)
        copied = bool(app.config.get("auto_copy", True))
        packed(ctk.CTkLabel(
            footer,
            text="●  已复制" if copied else "●  待复制",
            font=ctk.CTkFont(UI_FONT, 9),
            text_color="#63D6A3" if copied else "#59C7FF",
            height=18), side="left")
        packed(ctk.CTkLabel(
            footer, text="点击复制  ·  8s",
            font=ctk.CTkFont(UI_FONT, 9),
            text_color="#697588", height=18), side="right")

        divider = packed(
            ctk.CTkFrame(card, height=1, fg_color="#252D3A"),
            side="bottom", fill="x", padx=14, pady=(0, 2))
        divider.pack_propagate(False)

        for widget in click_targets:
            widget.bind("<Button-1>", self._on_click)
        self.bind("<Escape>", self._on_escape)

    def _get_work_area(self):
        """Windows API 获取工作区像素范围（不含任务栏）"""
        if sys.platform == "win32":
            try:
                import ctypes
                import ctypes.wintypes
                rect = ctypes.wintypes.RECT()
                SPI_GETWORKAREA = 0x0030
                ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
                screen_w = ctypes.windll.user32.GetSystemMetrics(0)
                return rect.right, rect.bottom, screen_w
            except Exception:
                pass
        ws_w = self.winfo_screenwidth()
        ws_h = self.winfo_screenheight()
        return ws_w, ws_h, ws_w

    def show(self):
        work_right, work_bottom, phys_screen_w = self._get_work_area()
        self._screen_w = phys_screen_w

        # geometry 的尺寸是逻辑像素，实际渲染会乘以 window_scaling
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
        self._animation_after_id = self.after(20, lambda: self._animate_in(0))
        if self.auto_close_ms is not None:
            self._auto_close_after_id = self.after(
                self.auto_close_ms, self._auto_close)

    def _animate_in(self, step):
        self._animation_after_id = None
        if self._closing or not self.winfo_exists():
            return
        progress = step / 20
        x = self._target_x + int((self._screen_w - self._target_x) * (1 - progress))
        self.geometry(f"{BUBBLE_W}x{BUBBLE_H}+{x}+{self._target_y}")
        if step < 20:
            self._animation_after_id = self.after(
                10, lambda: self._animate_in(step + 1))
        else:
            self._out = False

    def _auto_close(self):
        self._auto_close_after_id = None
        self._slide_out()

    def _slide_out(self):
        if self._closing or getattr(self, "_out", False):
            return
        self._cancel_scheduled_callbacks()
        self._out = True
        self._animate_out(0)

    def _animate_out(self, step):
        self._animation_after_id = None
        if self._closing or not self.winfo_exists():
            return
        progress = step / 20
        x = self._target_x + int((self._screen_w - self._target_x) * progress)
        self.geometry(f"{BUBBLE_W}x{BUBBLE_H}+{x}+{self._target_y}")
        if step < 20:
            self._animation_after_id = self.after(
                10, lambda: self._animate_out(step + 1))
        else:
            self._destroy_safely()

    def _cancel_scheduled_callbacks(self):
        for attr in ("_animation_after_id", "_auto_close_after_id"):
            setattr(self, attr, cancel_after(self, getattr(self, attr, None)))

    def _destroy_safely(self):
        self._cancel_scheduled_callbacks()
        try:
            if self.winfo_exists():
                self.destroy()
        except Exception:
            pass

    def _on_click(self, _event=None):
        self._copy_and_close()

    def _on_escape(self, _event=None):
        self._slide_out()

    def _copy_and_close(self):
        if self._closing:
            return
        self._closing = True
        self._cancel_scheduled_callbacks()
        if self.app:
            self.app._stop_tray_flash()
        error = copy_to_clipboard(self.code)
        if error and hasattr(self.app, "_append_log"):
            self.app._append_log(f"点击复制验证码失败: {error}")
        # 等当前 Tk 点击事件的全部绑定执行完再销毁，避免 Tcl 调用已删除命令。
        self.after_idle(self._destroy_safely)


# ------------------------- 配置窗口 -------------------------
class ConfigWindow(ctk.CTkToplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("SmsGlint - MQTT 配置")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.geometry("+{}+{}".format(
            int((self.winfo_screenwidth() - 400) / 2),
            int((self.winfo_screenheight() - 560) / 2)))
        set_window_icon(self)

        pad = {"padx": 24, "pady": 4}
        ctk.CTkLabel(self, text="Serverless MQTT 配置",
                     font=ctk.CTkFont(UI_FONT, 18, weight="bold")).pack(pady=(16, 4))

        def field(label, var, show=None):
            ctk.CTkLabel(self, text=label,
                         font=ctk.CTkFont(UI_FONT, 12)).pack(anchor="w", **pad)
            entry = ctk.CTkEntry(
                self, textvariable=var, show=show, width=340,
                font=ctk.CTkFont(UI_FONT, 12))
            entry.pack(**pad)

        self.var_host = ctk.StringVar(value=app.config.get("host", ""))
        self.var_port = ctk.StringVar(value=str(app.config.get("port", 8883)))
        self.var_user = ctk.StringVar(value=app.config.get("username", ""))
        self.var_pass = ctk.StringVar(value=app.config.get("password", ""))
        self.var_topic = ctk.StringVar(value=app.config.get("topic", ""))
        self.var_tls = ctk.BooleanVar(value=_as_bool(app.config.get("use_tls"), True))
        self.var_autocopy = ctk.BooleanVar(value=_as_bool(app.config.get("auto_copy"), True))
        self.var_sound = ctk.BooleanVar(value=_as_bool(app.config.get("play_sound"), True))
        self.var_autostart = ctk.BooleanVar(value=get_autostart_status())

        field("服务器地址 (Host)", self.var_host)
        field("端口 (1883 / 8883)", self.var_port)
        field("用户名", self.var_user)
        field("密码", self.var_pass, show="*")
        field("订阅主题 (Topic)", self.var_topic)

        opts_frame = ctk.CTkFrame(self, fg_color="transparent")
        opts_frame.pack(fill="x", padx=24, pady=(8, 2))

        def checkbox(text, variable):
            ctk.CTkCheckBox(
                opts_frame, text=text, variable=variable,
                font=ctk.CTkFont(UI_FONT, 12),
                checkbox_width=18, checkbox_height=18
            ).pack(anchor="w", pady=3)

        checkbox("启用 TLS 加密连接", self.var_tls)
        checkbox("自动复制验证码到剪切板", self.var_autocopy)
        checkbox("收到验证码时播放提示音", self.var_sound)
        checkbox("开机自动启动", self.var_autostart)

        self.status = ctk.CTkLabel(self, text="", font=ctk.CTkFont(UI_FONT, 12))
        self.status.pack(pady=(4, 0))

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(pady=(10, 16))

        def button(text, colors, command):
            widget = ctk.CTkButton(
                btns, text=text, width=100, fg_color=colors[0],
                hover_color=colors[1], command=command)
            widget.pack(side="left", padx=5)
            return widget

        self.test_button = button(
            "测试连接", ("#2b7de9", "#1f6fd6"), self._test)
        button("保存", ("#2e7d32", "#25632a"), self._save)
        button("退出程序", ("#9c2f2f", "#7d2525"), self.app.quit)

        self.grab_set()

    def _collect(self):
        return validate_config({
            "host": self.var_host.get().strip(),
            "port": self.var_port.get().strip(),
            "username": self.var_user.get().strip(),
            "password": self.var_pass.get(),
            "topic": self.var_topic.get().strip(),
            "use_tls": bool(self.var_tls.get()),
            "auto_copy": bool(self.var_autocopy.get()),
            "play_sound": bool(self.var_sound.get()),
        })

    def _test(self):
        try:
            cfg = self._collect()
        except ValueError as e:
            self.status.configure(text=str(e), text_color="#ef5350")
            return
        self.status.configure(text="测试中...", text_color="#f0b429")
        self.test_button.configure(state="disabled")
        test_connection(
            cfg,
            lambda ok, msg: self.app.queue.put(
                ("config_test_result", self, ok, msg)))

    def _show_test_result(self, ok, msg):
        """仅由 App 的 Tk 主线程事件泵调用。"""
        self.test_button.configure(state="normal")
        self.status.configure(
            text=msg, text_color="#66bb6a" if ok else "#ef5350")

    def _save(self):
        try:
            cfg = self._collect()
            self.app.apply_config(cfg)
        except (ValueError, OSError) as e:
            self.status.configure(text=str(e), text_color="#ef5350")
            return

        # 同步开机自启动设置
        ok, error = set_autostart(bool(self.var_autostart.get()))
        if not ok:
            self.status.configure(text=error, text_color="#ef5350")
            return

        self.destroy()


# ------------------------- 日志窗口 -------------------------
class LogWindow(ctk.CTkToplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.title("运行日志")
        self.resizable(True, True)
        self.geometry("520x360")
        self.attributes("-topmost", True)
        self.geometry("+{}+{}".format(
            int((self.winfo_screenwidth() - 520) / 2),
            int((self.winfo_screenheight() - 360) / 2)))
        set_window_icon(self)

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


# ------------------------- 弹窗预览 -------------------------
def preview_bubble():
    """只显示示例弹窗；不启动 MQTT、托盘或单实例锁。"""
    root = ctk.CTk()
    root.withdraw()

    class PreviewApp:
        def __init__(self, master):
            self.root = master
            self.config = {"auto_copy": True}

        @staticmethod
        def _stop_tray_flash():
            pass

    app = PreviewApp(root)
    bubble = BubbleWindow(
        app, "phone_01", "10690000", "482915",
        platform="示例平台", auto_close_ms=None)

    def close_preview(event):
        if event.widget is bubble and root.winfo_exists():
            root.after_idle(root.destroy)

    bubble.bind("<Destroy>", close_preview, add="+")
    bubble.show()
    root.mainloop()


# ------------------------- 入口 -------------------------
def main():
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    if "--preview" in sys.argv[1:]:
        preview_bubble()
        return

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
