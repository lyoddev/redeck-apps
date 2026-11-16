#!/usr/bin/env python3
"""Re Voice: зажал клавишу, сказал, отпустил, и текст вставился туда, где курсор.
Всё работает локально (Whisper), интернет нужен только чтобы один раз скачать модель.
Только для X11.

Портативный режим: если рядом с программой лежит папка models/<модель>,
берётся она, и интернет не нужен.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
from pynput import keyboard
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QActionGroup, QColor, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QHBoxLayout,
                               QLabel, QMenu, QPushButton, QSystemTrayIcon,
                               QVBoxLayout, QWidget)

MIN_SECONDS = 0.4
SAMPLE_RATE = 16000
PROMPT = "Привет. Это диктовка, с нормальными запятыми и точками."
IPC_NAME = "govori-single-instance"

SCRIPT = os.path.abspath(__file__)
BASE_DIR = Path(SCRIPT).parent
CONFIG_PATH = Path.home() / ".config" / "govori" / "config.json"
LAUNCHER_PATH = Path.home() / ".local" / "share" / "applications" / "govori.desktop"
AUTOSTART_PATH = Path.home() / ".config" / "autostart" / "govori.desktop"
ICON_PATH = Path.home() / ".local" / "share" / "icons" / "hicolor" / "scalable" / "apps" / "govori.svg"
ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 150 150" width="150" height="150">
<rect width="150" height="150" rx="34" fill="#FF5C5C"/>
<rect x="61" y="34" width="28" height="50" rx="14" fill="#FFFFFF"/>
<g fill="none" stroke="#FFFFFF" stroke-width="6" stroke-linecap="round">
<path d="M51 72 A24 24 0 0 0 99 72"/>
<path d="M75 96 V112"/>
<path d="M61 114 H89"/>
<path d="M24 64 V84"/>
<path d="M37 54 V94"/>
<path d="M113 54 V94"/>
<path d="M126 64 V84"/>
</g>
</svg>
"""

# иконка для трея: только микрофон со звуковыми полосками, без фона, крупно
TRAY_PATH = Path.home() / ".local" / "share" / "icons" / "hicolor" / "scalable" / "apps" / "govori-tray.svg"
TRAY_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="20 20 110 110" width="128" height="128">
<rect x="61" y="34" width="28" height="50" rx="14" fill="#FFFFFF"/>
<g fill="none" stroke="#FFFFFF" stroke-width="7" stroke-linecap="round">
<path d="M51 72 A24 24 0 0 0 99 72"/>
<path d="M75 96 V112"/>
<path d="M61 114 H89"/>
<path d="M24 64 V84"/>
<path d="M37 54 V94"/>
<path d="M113 54 V94"/>
<path d="M126 64 V84"/>
</g>
</svg>
"""

LANGUAGES = (("ru", "Русский"), ("en", "English"), ("de", "Deutsch"), ("auto", "Определять само"))
LANG_NAMES = dict(LANGUAGES)

DEFAULTS = {
    "hotkey": "Key.ctrl_r",
    "model": "small",
    "language": "ru",
    "terminal_paste": "ctrl+shift+v",
    "restore_clipboard": True,
    "autostart": False,
}

TERMINALS = ("terminal", "kitty", "alacritty", "konsole", "xterm", "tilix",
             "terminator", "wezterm", "foot", "ptyxis", "cosmic-term")

KEY_NAMES = {
    "Key.ctrl_r": "Правый Ctrl", "Key.ctrl_l": "Левый Ctrl",
    "Key.alt_r": "Правый Alt", "Key.alt_l": "Левый Alt", "Key.alt_gr": "Alt Gr",
    "Key.shift_r": "Правый Shift", "Key.shift_l": "Левый Shift",
    "Key.cmd": "Super", "Key.cmd_r": "Правый Super",
    "Key.pause": "Pause", "Key.scroll_lock": "Scroll Lock", "Key.menu": "Menu",
    "Key.caps_lock": "Caps Lock", "Key.insert": "Insert",
}


# ---------- настройки ----------
def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except Exception:
            pass
    else:
        # первый запуск: если автозапуск уже настраивали руками, не ломаем его
        cfg["autostart"] = AUTOSTART_PATH.exists()
    return cfg


def save_config(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))


def model_source(size):
    """Если рядом лежит папка models/<размер>, берём её (портативный режим)."""
    local = BASE_DIR / "models" / size
    if (local / "model.bin").exists():
        return str(local)
    return size


def key_to_str(k):
    if isinstance(k, keyboard.Key):
        return "Key." + k.name
    if isinstance(k, keyboard.KeyCode):
        if k.char:
            return "char:" + k.char.lower()
        return f"vk:{k.vk}"
    return str(k)


def key_label(s):
    if s in KEY_NAMES:
        return KEY_NAMES[s]
    if s.startswith("Key."):
        return s[4:].upper()
    if s.startswith("char:"):
        return s[5:].upper()
    return "клавиша " + s


def desktop_text(background):
    exec_line = f'"{sys.executable}" "{SCRIPT}"' + (" --background" if background else "")
    text = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Re Voice\n"
        "Comment=Диктовка голосом: зажал клавишу, сказал, текст вставился\n"
        f"Exec={exec_line}\n"
        f"Path={os.path.dirname(SCRIPT)}\n"
        f"Icon={ICON_PATH}\n"
        "Terminal=false\n"
        "Categories=AudioVideo;Utility;\n"
    )
    if background:
        text += "X-GNOME-Autostart-enabled=true\n"
    return text


def write_if_changed(path, text):
    try:
        if path.exists() and path.read_text() == text:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    except Exception as e:
        print("Не смог записать", path, e, file=sys.stderr)


def apply_autostart(enabled):
    if enabled:
        write_if_changed(AUTOSTART_PATH, desktop_text(True))
    else:
        try:
            AUTOSTART_PATH.unlink()
        except FileNotFoundError:
            pass


# ---------- вставка текста ----------
def paste_text(text, terminal_combo, restore):
    old = None
    if restore:
        try:
            r = subprocess.run(["xclip", "-selection", "clipboard", "-o"],
                               capture_output=True, timeout=2)
            if r.returncode == 0 and r.stdout:
                old = r.stdout
        except Exception:
            pass

    p = subprocess.Popen(["xclip", "-selection", "clipboard"], stdin=subprocess.PIPE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p.stdin.write(text.encode())
    p.stdin.close()
    time.sleep(0.08)

    cls = ""
    try:
        cls = subprocess.run(["xdotool", "getactivewindow", "getwindowclassname"],
                             capture_output=True, text=True, timeout=2).stdout.lower()
    except Exception:
        pass
    combo = terminal_combo if any(t in cls for t in TERMINALS) else "ctrl+v"
    subprocess.run(["xdotool", "key", "--clearmodifiers", combo])

    time.sleep(0.35)
    if old is not None:
        q = subprocess.Popen(["xclip", "-selection", "clipboard"], stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        q.stdin.write(old)
        q.stdin.close()


class Recorder:
    def __init__(self):
        self.chunks = []
        self.stream = None

    def start(self):
        self.chunks = []
        self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                     dtype="float32", callback=self._cb)
        self.stream.start()

    def _cb(self, indata, frames, t, status):
        self.chunks.append(indata.copy())

    def stop(self):
        s, self.stream = self.stream, None
        if s:
            s.stop()
            s.close()
        if not self.chunks:
            return np.zeros(0, dtype="float32")
        return np.concatenate(self.chunks)[:, 0]


class Overlay(QWidget):
    """Плашка внизу экрана. Не забирает фокус, чтобы текст вставился куда надо."""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                            | Qt.WindowDoesNotAcceptFocus | Qt.X11BypassWindowManagerHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel()
        lay.addWidget(self.label)

    def show_text(self, text, color="#e8e9f0"):
        self.label.setStyleSheet(
            f"background: rgba(20,21,28,235); color: {color}; border-radius: 18px;"
            "padding: 10px 24px; font-size: 16px; font-weight: 600;")
        self.label.setText(text)
        self.adjustSize()
        geo = QApplication.primaryScreen().geometry()
        self.move(geo.center().x() - self.width() // 2, geo.bottom() - 130)
        self.show()


STYLE = """
* { font-family: "Inter", "Noto Sans", "Ubuntu", sans-serif; font-size: 14px; color: #e8e9f0; }
QWidget#settings { background: #14151c; }
QLabel#title { font-size: 22px; font-weight: 700; }
QLabel#muted { color: #8b8fa3; }
QLabel#keyname { background: #262940; border-radius: 8px; padding: 8px 14px; font-weight: 700; }
QComboBox { background: #1c1e28; border: 1px solid #2c2f42; border-radius: 10px; padding: 8px 12px; min-height: 22px; min-width: 260px; }
QComboBox:hover { border-color: #ff5c5c; }
QComboBox QAbstractItemView { background: #1c1e28; border: 1px solid #2c2f42; selection-background-color: #ff5c5c; }
QPushButton { background: #262940; border: none; border-radius: 10px; padding: 9px 16px; }
QPushButton:hover { background: #30344f; }
QCheckBox { spacing: 10px; }
QCheckBox::indicator { width: 20px; height: 20px; border-radius: 6px; background: #1c1e28; border: 1px solid #3a3e54; }
QCheckBox::indicator:checked { background: #ff5c5c; border-color: #ff5c5c; }
"""


class Bridge(QObject):
    key_down = Signal()
    key_up = Signal()
    key_cancel = Signal()
    key_captured = Signal(str)
    model_loaded = Signal()
    model_failed = Signal(str)
    finished = Signal()
    failed = Signal(str)


class SettingsWindow(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setObjectName("settings")
        self.setWindowTitle("Re Voice: настройки")
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(560)
        cfg = app.cfg

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(16)

        title = QLabel("Re Voice")
        title.setObjectName("title")
        root.addWidget(title)

        self.key_label = QLabel(key_label(cfg["hotkey"]))
        self.key_label.setObjectName("keyname")
        self.key_btn = QPushButton("Изменить клавишу")
        self.key_btn.clicked.connect(self.begin_capture)
        key_box = QHBoxLayout()
        key_box.addWidget(self.key_label)
        key_box.addWidget(self.key_btn)
        key_box.addStretch(1)
        root.addLayout(self.row("Клавиша для диктовки (держать)", key_box))

        self.lang = QComboBox()
        for code, name in LANGUAGES:
            self.lang.addItem(name, code)
        self.lang.setCurrentIndex(max(0, self.lang.findData(cfg["language"])))
        self.lang.currentIndexChanged.connect(lambda: app.set_language(self.lang.currentData()))
        root.addLayout(self.row("Язык. Меняется ещё и в меню трея", self.lang))

        self.model = QComboBox()
        for code, name in (("tiny", "tiny — самая быстрая, хуже понимает"),
                           ("base", "base — быстрая"),
                           ("small", "small — золотая середина"),
                           ("medium", "medium — точнее, но медленнее")):
            self.model.addItem(name, code)
        self.model.setCurrentIndex(max(0, self.model.findData(cfg["model"])))
        self.model.currentIndexChanged.connect(self.on_model)
        root.addLayout(self.row("Модель распознавания", self.model))

        self.term = QComboBox()
        self.term.addItem("Ctrl+V (как везде)", "ctrl+v")
        self.term.addItem("Ctrl+Shift+V (по умолчанию в терминалах)", "ctrl+shift+v")
        self.term.setCurrentIndex(max(0, self.term.findData(cfg["terminal_paste"])))
        self.term.currentIndexChanged.connect(self.on_term)
        root.addLayout(self.row("Вставка в терминале", self.term))

        self.restore = QCheckBox("Возвращать прежний буфер обмена после вставки")
        self.restore.setChecked(cfg["restore_clipboard"])
        self.restore.toggled.connect(self.on_restore)
        self.auto = QCheckBox("Запускать при входе в систему")
        self.auto.setChecked(cfg["autostart"])
        self.auto.toggled.connect(self.on_auto)
        root.addWidget(self.restore)
        root.addWidget(self.auto)

        hint = QLabel("Изменения сохраняются сразу. Совсем короткие нажатия клавиши игнорируются, "
                      "а если нажать другую клавишу вместе с этой, запись отменится (чтобы не мешать шорткатам).")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        root.addWidget(hint)

        app.bridge.key_captured.connect(self.on_captured)

    def row(self, text, widget):
        box = QVBoxLayout()
        box.setSpacing(6)
        label = QLabel(text)
        label.setObjectName("muted")
        box.addWidget(label)
        if isinstance(widget, QHBoxLayout):
            box.addLayout(widget)
        else:
            box.addWidget(widget)
        return box

    def sync_language(self):
        self.lang.blockSignals(True)
        self.lang.setCurrentIndex(max(0, self.lang.findData(self.app.cfg["language"])))
        self.lang.blockSignals(False)

    def begin_capture(self):
        self.key_btn.setText("Нажми нужную клавишу… (Esc отмена)")
        self.app.capturing = True

    def on_captured(self, s):
        self.key_btn.setText("Изменить клавишу")
        if s == "Key.esc":
            return
        self.app.cfg["hotkey"] = s
        self.app.save()
        self.key_label.setText(key_label(s))

    def on_model(self):
        self.app.cfg["model"] = self.model.currentData()
        self.app.save()
        self.app.reload_model()

    def on_term(self):
        self.app.cfg["terminal_paste"] = self.term.currentData()
        self.app.save()

    def on_restore(self, v):
        self.app.cfg["restore_clipboard"] = v
        self.app.save()

    def on_auto(self, v):
        self.app.cfg["autostart"] = v
        self.app.save()
        apply_autostart(v)


def dot_icon(color, _base={}):
    """Иконка трея (микрофон без фона) с цветной точкой статуса в углу."""
    S = 128
    if "pm" not in _base:
        pm0 = QIcon(str(TRAY_PATH)).pixmap(S, S)
        if pm0.isNull():
            pm0 = QPixmap(S, S)
            pm0.fill(Qt.transparent)
        _base["pm"] = pm0
    pm = QPixmap(_base["pm"])
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#14151c"))
    p.drawEllipse(S - 60, S - 60, 56, 56)
    p.setBrush(QColor(color))
    p.drawEllipse(S - 52, S - 52, 40, 40)
    p.end()
    return QIcon(pm)


class App:
    def __init__(self, server, background):
        self.cfg = load_config()
        self.bridge = Bridge()
        self.overlay = Overlay()
        self.rec = Recorder()
        self.model = None
        self.model_ready = False
        self.recording = False
        self.busy = False
        self.held = False
        self.capturing = False
        self.t0 = 0.0

        # иконка, ярлык в меню приложений + автозапуск
        write_if_changed(ICON_PATH, ICON_SVG)
        write_if_changed(TRAY_PATH, TRAY_SVG)
        write_if_changed(LAUNCHER_PATH, desktop_text(False))
        if self.cfg["autostart"]:
            apply_autostart(True)
        save_config(self.cfg)

        self.timer = QTimer()
        self.timer.setInterval(150)
        self.timer.timeout.connect(self.tick)
        self.hide_timer = QTimer()
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.overlay.hide)

        self.settings = SettingsWindow(self)
        self.settings.setWindowIcon(QIcon(str(ICON_PATH)))

        # --- трей ---
        self.tray = QSystemTrayIcon(dot_icon("#8b8fa3"))
        self.tray.setToolTip("Re Voice: загружаю модель…")
        menu = QMenu()
        self.status_action = menu.addAction("Загружаю модель…")
        self.status_action.setEnabled(False)
        menu.addAction("Настройки…", self.show_settings)
        self.lang_menu = menu.addMenu("Язык")
        self.lang_group = QActionGroup(self.lang_menu)
        self.lang_group.setExclusive(True)
        self.lang_actions = {}
        for code, name in LANGUAGES:
            a = self.lang_menu.addAction(name)
            a.setCheckable(True)
            a.setChecked(self.cfg["language"] == code)
            self.lang_group.addAction(a)
            a.triggered.connect(lambda _=False, c=code: self.set_language(c))
            self.lang_actions[code] = a
        self.update_lang_title()
        menu.addSeparator()
        menu.addAction("Выход", QApplication.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self.show_settings() if r == QSystemTrayIcon.Trigger else None)
        self.tray.show()

        b = self.bridge
        b.key_down.connect(self.start_rec)
        b.key_up.connect(self.stop_rec)
        b.key_cancel.connect(self.cancel_rec)
        b.model_loaded.connect(self.on_model_loaded)
        b.model_failed.connect(self.on_model_failed)
        b.finished.connect(self.on_finished)
        b.failed.connect(self.on_failed)

        # второй запуск (например из меню) просто открывает настройки уже работающей программы
        self.server = server
        server.newConnection.connect(self.on_ipc)

        threading.Thread(target=self.load_model, args=(self.cfg["model"],), daemon=True).start()
        self.listener = keyboard.Listener(on_press=self.on_press, on_release=self.on_release)
        self.listener.start()

        if not background:
            self.show_settings()

    def save(self):
        save_config(self.cfg)

    def on_ipc(self):
        s = self.server.nextPendingConnection()
        if s:
            s.disconnectFromServer()
        self.show_settings()

    def show_settings(self):
        self.settings.show()
        self.settings.raise_()
        self.settings.activateWindow()

    # --- язык ---
    def update_lang_title(self):
        self.lang_menu.setTitle("Язык: " + LANG_NAMES.get(self.cfg["language"], "?"))

    def set_language(self, code):
        self.cfg["language"] = code
        self.save()
        self.lang_actions[code].setChecked(True)
        self.update_lang_title()
        self.settings.sync_language()
        self.flash("Язык: " + LANG_NAMES[code], "#e8e9f0", 1300)

    # --- модель ---
    def load_model(self, size):
        try:
            from faster_whisper import WhisperModel
            model = WhisperModel(model_source(size), device="cpu", compute_type="int8")
            self.model = model
            self.bridge.model_loaded.emit()
        except Exception as e:
            self.bridge.model_failed.emit(str(e))

    def reload_model(self):
        self.model_ready = False
        self.tray.setIcon(dot_icon("#8b8fa3"))
        self.status_action.setText("Загружаю модель…")
        self.flash("Загружаю другую модель…", "#f5a524", 3000)
        threading.Thread(target=self.load_model, args=(self.cfg["model"],), daemon=True).start()

    def on_model_loaded(self):
        self.model_ready = True
        self.tray.setIcon(dot_icon("#3ecf8e"))
        self.tray.setToolTip("Re Voice: готово")
        self.status_action.setText("Готово. Держи клавишу и говори")
        self.flash("Готово, можно диктовать", "#3ecf8e", 2000)

    def on_model_failed(self, err):
        self.tray.setIcon(dot_icon("#e5484d"))
        self.status_action.setText("Ошибка загрузки модели")
        self.flash("Не загрузилась модель (в первый раз нужен интернет)", "#ff6b70", 6000)
        print("Ошибка загрузки модели:", err, file=sys.stderr)

    # --- клавиши (поток pynput) ---
    def on_press(self, key):
        s = key_to_str(key)
        if self.capturing:
            self.capturing = False
            self.bridge.key_captured.emit(s)
            return
        if s == self.cfg["hotkey"]:
            if not self.held:
                self.held = True
                self.bridge.key_down.emit()
        elif self.held:
            # нажали что-то ещё вместе с нашей клавишей: это шорткат, а не диктовка
            self.bridge.key_cancel.emit()

    def on_release(self, key):
        if key_to_str(key) == self.cfg["hotkey"] and self.held:
            self.held = False
            self.bridge.key_up.emit()

    # --- запись ---
    def flash(self, text, color="#e8e9f0", ms=1800):
        self.overlay.show_text(text, color)
        self.hide_timer.start(ms)

    def start_rec(self):
        if self.busy or self.recording:
            return
        if not self.model_ready:
            self.flash("Модель ещё грузится, подожди…", "#f5a524")
            return
        try:
            self.rec.start()
        except Exception as e:
            self.flash("Не могу открыть микрофон", "#ff6b70", 3000)
            print("Микрофон:", e, file=sys.stderr)
            return
        self.recording = True
        self.t0 = time.time()
        self.hide_timer.stop()
        self.tray.setIcon(dot_icon("#e5484d"))
        self.tick()
        self.timer.start()

    def tick(self):
        self.overlay.show_text(f"● Слушаю…  {time.time() - self.t0:.1f} с", "#ff6b70")

    def cancel_rec(self):
        if not self.recording:
            return
        self.recording = False
        self.timer.stop()
        self.rec.stop()
        self.overlay.hide()
        self.tray.setIcon(dot_icon("#3ecf8e"))

    def stop_rec(self):
        if not self.recording:
            return
        self.recording = False
        self.timer.stop()
        audio = self.rec.stop()
        if len(audio) / SAMPLE_RATE < MIN_SECONDS:
            self.overlay.hide()
            self.tray.setIcon(dot_icon("#3ecf8e"))
            return
        self.busy = True
        self.tray.setIcon(dot_icon("#f5a524"))
        self.overlay.show_text("Распознаю…", "#f5a524")
        threading.Thread(target=self.work, args=(audio,), daemon=True).start()

    # --- распознавание диктовки (отдельный поток) ---
    def work(self, audio):
        try:
            lang = self.cfg["language"]
            lang = None if lang == "auto" else lang
            segments, _ = self.model.transcribe(
                audio, language=lang, vad_filter=True, beam_size=5,
                initial_prompt=PROMPT if lang == "ru" else None)
            text = " ".join(s.text.strip() for s in segments).strip()
            if text:
                paste_text(text, self.cfg["terminal_paste"], self.cfg["restore_clipboard"])
            self.bridge.finished.emit()
        except Exception as e:
            self.bridge.failed.emit(str(e))

    def on_finished(self):
        self.busy = False
        self.overlay.hide()
        self.tray.setIcon(dot_icon("#3ecf8e"))

    def on_failed(self, err):
        self.busy = False
        self.tray.setIcon(dot_icon("#3ecf8e"))
        self.flash("Что-то пошло не так, смотри терминал", "#ff6b70", 3000)
        print("Ошибка:", err, file=sys.stderr)


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setDesktopFileName("govori")

    # если уже запущено, просто просим открыть настройки и выходим
    sock = QLocalSocket()
    sock.connectToServer(IPC_NAME)
    if sock.waitForConnected(300):
        sock.write(b"window")
        sock.flush()
        return 0

    QLocalServer.removeServer(IPC_NAME)
    server = QLocalServer()
    server.listen(IPC_NAME)

    keep = App(server, background="--background" in sys.argv)  # noqa: F841
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
