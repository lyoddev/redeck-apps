#!/usr/bin/env python3
"""Re Mind: размяться, выпить воды, дать глазам отдохнуть.
Живёт в трее. Когда пора, в правом нижнем углу всплывает окошко.
Сделана в том же духе, что и «Нажми и говори» (PySide6, X11).

Запуск:  python3 napominalka.py
Ярлык в меню приложений, иконка и автозапуск настраиваются сами.
"""

import copy
import json
import math
import os
import random
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QCheckBox, QFrame, QHBoxLayout, QLabel,
                               QLayout, QLineEdit, QMenu, QPushButton, QSpinBox,
                               QSystemTrayIcon, QVBoxLayout, QWidget)

IPC_NAME = "napominalka-single-instance"
NAG_SEC = 300  # если игнорить, раз в 5 минут напомнит ещё раз (со звуком)

SCRIPT = os.path.abspath(__file__)
CONFIG_PATH = Path.home() / ".config" / "napominalka" / "config.json"
STATE_PATH = Path.home() / ".config" / "napominalka" / "state.json"
LAUNCHER_PATH = Path.home() / ".local" / "share" / "applications" / "napominalka.desktop"
AUTOSTART_PATH = Path.home() / ".config" / "autostart" / "napominalka.desktop"
ICON_PATH = Path.home() / ".local" / "share" / "icons" / "hicolor" / "scalable" / "apps" / "napominalka.svg"
ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 150 150" width="150" height="150">
<rect width="150" height="150" rx="34" fill="#EC4899"/>
<g transform="translate(0 4)" stroke-linecap="round" stroke-linejoin="round">
<circle cx="75" cy="34" r="5" fill="#FFFFFF"/>
<path d="M75 40 C60 40 52 52 52 66 L52 82 L44 92 L106 92 L98 82 L98 66 C98 52 90 40 75 40 Z" fill="#FFFFFF" stroke="#FFFFFF" stroke-width="6"/>
<circle cx="75" cy="106" r="7" fill="#FFFFFF"/>
<path d="M40 50 A16 16 0 0 0 40 76" fill="none" stroke="#FFFFFF" stroke-width="6"/>
<path d="M110 50 A16 16 0 0 1 110 76" fill="none" stroke="#FFFFFF" stroke-width="6"/>
</g>
</svg>
"""

# иконка для трея: только колокольчик, без фона, крупно
TRAY_PATH = Path.home() / ".local" / "share" / "icons" / "hicolor" / "scalable" / "apps" / "napominalka-tray.svg"
TRAY_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="26 26 98 98" width="128" height="128">
<g transform="translate(0 4)" stroke-linecap="round" stroke-linejoin="round">
<circle cx="75" cy="34" r="5" fill="#FFFFFF"/>
<path d="M75 40 C60 40 52 52 52 66 L52 82 L44 92 L106 92 L98 82 L98 66 C98 52 90 40 75 40 Z" fill="#FFFFFF" stroke="#FFFFFF" stroke-width="6"/>
<circle cx="75" cy="106" r="7" fill="#FFFFFF"/>
<path d="M40 50 A16 16 0 0 0 40 76" fill="none" stroke="#FFFFFF" stroke-width="8"/>
<path d="M110 50 A16 16 0 0 1 110 76" fill="none" stroke="#FFFFFF" stroke-width="8"/>
</g>
</svg>
"""

DEFAULTS = {
    "snooze_min": 15,
    "sound": True,
    "autostart": None,
    "reminders": [
        {"id": "stretch", "title": "Размяться", "emoji": "🤸", "every_min": 100,
         "enabled": True,
         "phrases": ["Встань и потянись", "Разомни шею и плечи",
                     "Пройдись хотя бы минуту", "Руки вверх, потянись как кот"]},
        {"id": "water", "title": "Выпить воды", "emoji": "💧", "every_min": 100,
         "enabled": True,
         "phrases": ["Выпей стакан воды", "Глоток воды, и дальше",
                     "Вода рядом? Самое время"]},
        {"id": "eyes", "title": "Глаза", "emoji": "👀", "every_min": 20,
         "enabled": True,
         "phrases": ["Посмотри вдаль секунд 20", "Отведи взгляд от экрана и поморгай",
                     "Найди в окне самый дальний предмет и посмотри на него"]},
    ],
}

ACCENT = "#ec4899"
STYLE = """
* { font-family: "Inter", "Noto Sans", "Ubuntu", sans-serif; font-size: 14px; color: #e8e9f0; }
QWidget#win { background: #14151c; }
QFrame#card { background: #14151c; border: 1px solid #3a3e54; border-radius: 16px; }
QLabel { background: transparent; }
QLabel#title { font-size: 22px; font-weight: 700; }
QLabel#rowtitle { font-size: 15px; font-weight: 700; }
QLabel#muted { color: #8b8fa3; }
QPushButton { background: #262940; border: none; border-radius: 10px; padding: 8px 16px; }
QPushButton:hover { background: #30344f; }
QPushButton#primary { background: #ffffff; color: #14151c; font-weight: 600; }
QPushButton#primary:hover { background: #e3e3ea; }
QLineEdit, QSpinBox { background: #1c1e28; border: 1px solid #2c2f42; border-radius: 10px; padding: 7px 10px; min-height: 20px; }
QLineEdit:hover, QSpinBox:hover, QLineEdit:focus, QSpinBox:focus { border-color: #ec4899; }
QCheckBox { spacing: 10px; }
QCheckBox::indicator { width: 20px; height: 20px; border-radius: 6px; background: #1c1e28; border: 1px solid #3a3e54; }
QCheckBox::indicator:checked { background: #ec4899; border-color: #ec4899; }
QMenu { background: #1c1e28; border: 1px solid #2c2f42; }
QMenu::item:selected { background: #30344f; }
"""


# ---------- файлы и настройки ----------
def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1))


def load_config():
    cfg = copy.deepcopy(DEFAULTS)
    cfg.update(load_json(CONFIG_PATH, {}))
    if cfg.get("autostart") is None:
        # первый запуск: если автозапуск уже настраивали, не ломаем его
        cfg["autostart"] = AUTOSTART_PATH.exists()
    return cfg


def write_if_changed(path, text):
    try:
        if path.exists() and path.read_text() == text:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    except Exception as e:
        print("Не смог записать", path, e, file=sys.stderr)


def desktop_text(background):
    exec_line = f'"{sys.executable}" "{SCRIPT}"' + (" --background" if background else "")
    text = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Re Mind\n"
        "Comment=Размяться, выпить воды, дать глазам отдохнуть\n"
        f"Exec={exec_line}\n"
        f"Path={os.path.dirname(SCRIPT)}\n"
        f"Icon={ICON_PATH}\n"
        "Terminal=false\n"
        "Categories=Utility;\n"
        "StartupWMClass=napominalka\n"
    )
    if background:
        text += "X-GNOME-Autostart-enabled=true\nX-GNOME-Autostart-Delay=5\n"
    return text


def apply_autostart(enabled):
    if enabled:
        write_if_changed(AUTOSTART_PATH, desktop_text(True))
    else:
        try:
            AUTOSTART_PATH.unlink()
        except FileNotFoundError:
            pass


# ---------- мелочи ----------
def fmt_left(sec):
    if sec <= 0:
        return "пора!"
    h, m = divmod(math.ceil(sec / 60), 60)
    if h and m:
        return f"через {h} ч {m} мин"
    if h:
        return f"через {h} ч"
    return f"через {m} мин"


def fmt_every(minutes):
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} ч {m} мин"
    if h:
        return f"{h} ч"
    return f"{m} мин"


def beep():
    for cmd in (["canberra-gtk-play", "-i", "message"],
                ["paplay", "/usr/share/sounds/freedesktop/stereo/message.oga"]):
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except FileNotFoundError:
            continue


def clear_layout(lay):
    while lay.count():
        it = lay.takeAt(0)
        w = it.widget()
        if w is not None:
            w.hide()
            w.setParent(None)
            w.deleteLater()
        elif it.layout() is not None:
            clear_layout(it.layout())


def fit_height(w):
    """Высота окна = ровно сколько нужно содержимому (иначе кнопки обрезаются)."""
    lay = w.layout()
    for c in w.findChildren(QWidget):
        c.show()  # новые виджеты в уже открытом окне показываются не сразу, а с задержкой
    for child in w.findChildren(QLayout):
        child.invalidate()
    lay.invalidate()
    lay.activate()
    h = max(lay.totalHeightForWidth(w.width()), lay.totalSizeHint().height(),
            lay.totalMinimumSize().height())
    w.setFixedHeight(h)


_base_pm = {}


def tray_icon(dot=None):
    """Колокольчик для трея, при желании с цветной точкой статуса в углу."""
    S = 128
    if "pm" not in _base_pm:
        pm0 = QIcon(str(TRAY_PATH)).pixmap(S, S)
        if pm0.isNull():
            pm0 = QPixmap(S, S)
            pm0.fill(Qt.transparent)
        _base_pm["pm"] = pm0
    pm = QPixmap(_base_pm["pm"])
    if dot:
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#14151c"))
        p.drawEllipse(S - 60, S - 60, 56, 56)
        p.setBrush(QColor(dot))
        p.drawEllipse(S - 52, S - 52, 40, 40)
        p.end()
    return QIcon(pm)


# ---------- виджеты ----------
class Ring(QWidget):
    """Круглый индикатор: заполняется, пока идёт время, оранжевый, когда пора."""

    def __init__(self, emoji):
        super().__init__()
        self.emoji, self.frac, self.due = emoji, 0.0, False
        self.setFixedSize(50, 50)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(4, 4, self.width() - 8, self.height() - 8)
        pen = QPen(QColor(255, 255, 255, 46), 3.5)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(rect)
        if self.due:
            pen.setColor(QColor("#ffb454"))
            p.setPen(pen)
            p.drawEllipse(rect)
        elif self.frac > 0:
            pen.setColor(QColor("#e8e9f0"))
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawArc(rect, 90 * 16, int(-360 * 16 * self.frac))
        f = QFont("Noto Color Emoji")
        f.setPointSize(15)
        p.setFont(f)
        p.setPen(QColor("#ffffff"))
        p.drawText(self.rect(), Qt.AlignCenter, self.emoji)


class Row(QWidget):
    def __init__(self, rem, app, phrase=None):
        super().__init__()
        self.rem, self.phrase, self._due = rem, phrase, None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)
        self.ring = Ring(rem["emoji"])
        lay.addWidget(self.ring, 0, Qt.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(4)
        title = QLabel(rem["title"])
        title.setObjectName("rowtitle")
        self.sub = QLabel()
        self.sub.setObjectName("muted")
        self.sub.setWordWrap(True)
        btns = QHBoxLayout()
        btns.setSpacing(8)
        done = QPushButton("Сделал")
        done.setObjectName("primary")
        done.clicked.connect(lambda _=False, i=rem["id"]: app.done(i))
        snz = QPushButton(f"Через {app.cfg['snooze_min']} мин")
        snz.clicked.connect(lambda _=False, i=rem["id"]: app.snooze(i))
        btns.addWidget(done)
        btns.addWidget(snz)
        btns.addStretch(1)
        col.addWidget(title)
        col.addWidget(self.sub)
        col.addSpacing(4)
        col.addLayout(btns)
        lay.addLayout(col, 1)

    def refresh(self, next_ts):
        every = self.rem["every_min"] * 60
        left = next_ts - time.time()
        due = left <= 0
        self.ring.frac = max(0.0, min(1.0, 1 - left / every))
        self.ring.due = due
        self.ring.update()
        if due != self._due:
            self._due = due
            self.sub.setStyleSheet("color: #ffb454;" if due else "")
        text = self.phrase or f"{fmt_left(left)} · каждые {fmt_every(self.rem['every_min'])}"
        if self.sub.text() != text:
            self.sub.setText(text)


class Popup(QWidget):
    """Всплывашка в правом нижнем углу. Висит, пока не нажмёшь кнопку."""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.ids, self.rows, self.phr, self.last_nag = [], {}, {}, 0.0
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                            | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedWidth(430)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.card = QFrame()
        self.card.setObjectName("card")
        outer.addWidget(self.card)
        self.lay = QVBoxLayout(self.card)
        self.lay.setContentsMargins(22, 18, 22, 20)
        self.lay.setSpacing(16)

    def place(self):
        fit_height(self)
        g = QApplication.primaryScreen().availableGeometry()
        self.move(g.right() - self.width() - 16, g.bottom() - self.height() - 16)

    def pick_phrase(self, rem):
        options = rem.get("phrases") or [rem["title"]]
        last = self.app.last_phrase.get(rem["id"])
        pool = [p for p in options if p != last] or options
        p = random.choice(pool)
        self.app.last_phrase[rem["id"]] = p
        return p

    def sync(self, due):
        now = time.time()
        ids = [r["id"] for r in due]
        if not ids:
            if self.isVisible():
                self.hide()
            self.ids, self.rows, self.phr = [], {}, {}
            return
        if ids != self.ids:
            new = [i for i in ids if i not in self.ids]
            clear_layout(self.lay)
            self.rows = {}
            self.phr = {i: self.phr.get(i) for i in ids}
            head = QLabel("Напоминания")
            head.setObjectName("muted")
            self.lay.addWidget(head)
            for r in due:
                if not self.phr.get(r["id"]):
                    self.phr[r["id"]] = self.pick_phrase(r)
                row = Row(r, self.app, phrase=self.phr[r["id"]])
                self.rows[r["id"]] = row
                self.lay.addWidget(row)
            if len(due) > 1:
                allb = QPushButton("Сделал всё")
                allb.clicked.connect(lambda _=False, x=ids: self.app.done_all(x))
                self.lay.addWidget(allb, 0, Qt.AlignLeft)
            self.ids = ids
            self.place()
            self.show()
            self.place()
            if new and self.app.cfg.get("sound", True):
                beep()
            self.last_nag = now
        for i, row in self.rows.items():
            row.refresh(self.app.state[i])
        if now - self.last_nag > NAG_SEC:
            self.last_nag = now
            self.raise_()
            if self.app.cfg.get("sound", True):
                beep()


class Panel(QWidget):
    """Главное окно: сколько осталось до каждого напоминания."""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.rows, self.pause_btn = {}, None
        self.setObjectName("win")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setWindowTitle("Re Mind")
        self.setFixedWidth(470)
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(28, 24, 28, 26)
        self.root.setSpacing(18)

    def rebuild(self):
        clear_layout(self.root)
        self.rows = {}
        title = QLabel("Re Mind")
        title.setObjectName("title")
        info = QLabel("Работает в фоне. Когда придёт время, в правом нижнем углу "
                      "всплывёт окошко. Здесь видно, сколько осталось до каждого "
                      "напоминания. Окно можно закрыть, программа останется в трее.")
        info.setObjectName("muted")
        info.setWordWrap(True)
        self.root.addWidget(title)
        self.root.addWidget(info)

        active = self.app.active()
        if not active:
            empty = QLabel("Сейчас нет включённых напоминаний. Добавь их в настройках.")
            empty.setWordWrap(True)
            self.root.addWidget(empty)
        for r in active:
            row = Row(r, self.app)
            self.rows[r["id"]] = row
            self.root.addWidget(row)

        bar1 = QHBoxLayout()
        bar1.setSpacing(8)
        allb = QPushButton("Сделал всё")
        allb.clicked.connect(lambda _=False: self.app.done_all())
        self.pause_btn = QPushButton("Пауза на 1 час")
        self.pause_btn.clicked.connect(lambda _=False: self.app.toggle_pause())
        bar1.addWidget(allb)
        bar1.addWidget(self.pause_btn)
        bar1.addStretch(1)
        bar2 = QHBoxLayout()
        bar2.setSpacing(8)
        gear = QPushButton("⚙ Настройки")
        gear.clicked.connect(lambda _=False: self.app.open_settings())
        hide = QPushButton("Скрыть окно")
        hide.clicked.connect(lambda _=False: self.hide())
        bar2.addWidget(gear)
        bar2.addWidget(hide)
        bar2.addStretch(1)
        self.root.addLayout(bar1)
        self.root.addLayout(bar2)
        self.root.addStretch(1)  # лишняя высота уходит вниз, а не между заголовком и текстом
        self.fit()

    def fit(self):
        """Подогнать высоту окна под содержимое (иначе кнопки могут обрезаться)."""
        fit_height(self)


class Settings(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setObjectName("win")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setWindowTitle("Re Mind: настройки")
        self.setMinimumWidth(760)
        cfg = app.cfg

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)
        title = QLabel("Настройки")
        title.setObjectName("title")
        hint = QLabel("Эмодзи · название · как часто · вкл · фразы через | "
                      "(каждый раз выпадает случайная, чтобы глаз не привыкал)")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(hint)

        self.list = QVBoxLayout()
        self.list.setSpacing(8)
        self.items = []
        root.addLayout(self.list)
        for r in cfg["reminders"]:
            self.add_item(r)

        add = QPushButton("+ Добавить напоминание")
        add.clicked.connect(lambda _=False: self.add_item(
            {"title": "", "emoji": "🔔", "every_min": 60, "enabled": True, "phrases": []}))
        root.addWidget(add, 0, Qt.AlignLeft)

        line = QHBoxLayout()
        line.addWidget(QLabel("Кнопка «Через…» откладывает на"))
        self.snooze = QSpinBox()
        self.snooze.setRange(1, 120)
        self.snooze.setValue(cfg["snooze_min"])
        self.snooze.setSuffix(" мин")
        line.addWidget(self.snooze)
        line.addStretch(1)
        root.addLayout(line)

        self.sound = QCheckBox("Звук при напоминании")
        self.sound.setChecked(cfg.get("sound", True))
        self.auto = QCheckBox("Запускать при входе в систему")
        self.auto.setChecked(bool(cfg.get("autostart")))
        root.addWidget(self.sound)
        root.addWidget(self.auto)

        save = QPushButton("Сохранить")
        save.setObjectName("primary")
        save.clicked.connect(lambda _=False: self.save())
        root.addWidget(save, 0, Qt.AlignRight)

    def add_item(self, r):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        emoji = QLineEdit(r["emoji"])
        emoji.setFixedWidth(54)
        title = QLineEdit(r["title"])
        title.setPlaceholderText("название")
        title.setFixedWidth(160)
        spin = QSpinBox()
        spin.setRange(1, 720)
        spin.setValue(r["every_min"])
        spin.setSuffix(" мин")
        on = QCheckBox("вкл")
        on.setChecked(r.get("enabled", True))
        phr = QLineEdit(" | ".join(r.get("phrases", [])))
        phr.setPlaceholderText("фразы через | (можно пусто)")
        delb = QPushButton("✕")
        delb.setFixedWidth(42)
        for x in (emoji, title, spin, on):
            h.addWidget(x)
        h.addWidget(phr, 1)
        h.addWidget(delb)
        item = {"id": r.get("id") or f"r{int(time.time() * 1000)}", "w": w,
                "emoji": emoji, "title": title, "spin": spin, "on": on, "phr": phr}
        delb.clicked.connect(lambda _=False, it=item: self.remove_item(it))
        self.items.append(item)
        self.list.addWidget(w)
        if self.isVisible():
            w.show()

    def remove_item(self, it):
        self.items.remove(it)
        it["w"].hide()
        it["w"].setParent(None)
        it["w"].deleteLater()
        QTimer.singleShot(0, self.adjustSize)

    def save(self):
        rems = []
        for it in self.items:
            title = it["title"].text().strip()
            if not title:
                continue
            phrases = [p.strip() for p in it["phr"].text().split("|") if p.strip()]
            rems.append({"id": it["id"], "title": title,
                         "emoji": it["emoji"].text().strip() or "🔔",
                         "every_min": it["spin"].value(),
                         "enabled": it["on"].isChecked(), "phrases": phrases})
        cfg = self.app.cfg
        cfg["reminders"] = rems
        cfg["snooze_min"] = self.snooze.value()
        cfg["sound"] = self.sound.isChecked()
        cfg["autostart"] = self.auto.isChecked()
        self.app.save()
        apply_autostart(cfg["autostart"])
        self.app.rebuild()
        self.app.tick()
        self.close()


# ---------- сама программа ----------
class App:
    def __init__(self, server, background):
        self.cfg = load_config()
        self.state = load_json(STATE_PATH, {})
        self.last_phrase = {}
        self.settings = None
        self.tray_state = "init"

        # иконка, ярлык в меню приложений + автозапуск
        write_if_changed(ICON_PATH, ICON_SVG)
        write_if_changed(TRAY_PATH, TRAY_SVG)
        write_if_changed(LAUNCHER_PATH, desktop_text(False))
        if self.cfg["autostart"]:
            apply_autostart(True)
        self.save()
        QApplication.instance().setWindowIcon(QIcon(str(ICON_PATH)))

        self.popup = Popup(self)
        self.panel = Panel(self)

        # --- трей ---
        self.tray = QSystemTrayIcon(tray_icon())
        self.tray.setToolTip("Re Mind")
        menu = QMenu()
        self.status_action = menu.addAction("…")
        self.status_action.setEnabled(False)
        menu.addAction("Показать окно", self.show_panel)
        menu.addAction("Настройки…", self.open_settings)
        self.pause_action = menu.addAction("Пауза на 1 час", self.toggle_pause)
        menu.addSeparator()
        menu.addAction("Выход", QApplication.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self.show_panel() if r == QSystemTrayIcon.Trigger else None)
        self.tray.show()

        # второй запуск (например из меню) просто открывает окно уже работающей программы
        self.server = server
        server.newConnection.connect(self.on_ipc)

        self.rebuild()
        self.timer = QTimer()
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.tick()
        if not background:
            self.show_panel()

    def save(self):
        save_json(CONFIG_PATH, self.cfg)

    def on_ipc(self):
        s = self.server.nextPendingConnection()
        if s:
            s.disconnectFromServer()
        self.show_panel()

    def show_panel(self):
        self.panel.show()
        self.panel.fit()
        QTimer.singleShot(0, self.panel.fit)
        self.panel.raise_()
        self.panel.activateWindow()

    def open_settings(self):
        if self.settings is not None and self.settings.isVisible():
            self.settings.raise_()
            self.settings.activateWindow()
            return
        self.settings = Settings(self)
        self.settings.destroyed.connect(lambda *_: setattr(self, "settings", None))
        self.settings.show()

    def active(self):
        return [r for r in self.cfg["reminders"] if r.get("enabled", True)]

    def rebuild(self):
        now = time.time()
        for r in self.active():
            lim = now + r["every_min"] * 60
            self.state[r["id"]] = min(self.state.get(r["id"], lim), lim)
        save_json(STATE_PATH, self.state)
        self.panel.rebuild()
        self.popup.ids = []  # пересобрать поп-ап с новыми кнопками

    def _find(self, rid):
        return next(r for r in self.cfg["reminders"] if r["id"] == rid)

    def done(self, rid, tick=True):
        self.state[rid] = time.time() + self._find(rid)["every_min"] * 60
        save_json(STATE_PATH, self.state)
        if tick:
            self.tick()

    def snooze(self, rid):
        self.state[rid] = time.time() + self.cfg["snooze_min"] * 60
        save_json(STATE_PATH, self.state)
        self.tick()

    def done_all(self, ids=None):
        for r in self.active():
            if ids is None or r["id"] in ids:
                self.done(r["id"], tick=False)
        self.tick()

    def toggle_pause(self):
        if time.time() < self.state.get("_paused_until", 0):
            self.state["_paused_until"] = 0
        else:
            self.state["_paused_until"] = time.time() + 3600
        save_json(STATE_PATH, self.state)
        self.tick()

    def tick(self):
        now = time.time()
        pause_left = self.state.get("_paused_until", 0) - now
        paused = pause_left > 0
        pause_txt = (f"Снять паузу (ещё {math.ceil(pause_left / 60)} мин)"
                     if paused else "Пауза на 1 час")
        self.pause_action.setText(pause_txt)
        if self.panel.pause_btn:
            self.panel.pause_btn.setText(pause_txt)

        due, nearest = [], None
        for r in self.active():
            nxt = self.state.get(r["id"], now)
            if r["id"] in self.panel.rows and self.panel.isVisible():
                self.panel.rows[r["id"]].refresh(nxt)
            if nxt <= now:
                due.append(r)
            elif nearest is None or nxt < nearest[0]:
                nearest = (nxt, r["title"])
        self.popup.sync([] if paused else due)

        if paused:
            status, dot = f"Пауза: ещё {math.ceil(pause_left / 60)} мин", "#8b8fa3"
        elif due:
            status, dot = "Пора: " + ", ".join(r["title"] for r in due), "#ffb454"
        elif nearest:
            status, dot = f"Дальше: {nearest[1]}, {fmt_left(nearest[0] - now)}", None
        else:
            status, dot = "Нет включённых напоминаний", None
        self.status_action.setText(status)
        if dot != self.tray_state:
            self.tray_state = dot
            self.tray.setIcon(tray_icon(dot))
        self.tray.setToolTip("Re Mind: " + status)


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("napominalka")
    app.setDesktopFileName("napominalka")
    app.setStyleSheet(STYLE)

    # если уже запущено, просто просим открыть окно и выходим
    sock = QLocalSocket()
    sock.connectToServer(IPC_NAME)
    if sock.waitForConnected(300):
        sock.write(b"window")
        sock.flush()
        sock.waitForBytesWritten(300)
        return 0

    QLocalServer.removeServer(IPC_NAME)
    server = QLocalServer()
    server.listen(IPC_NAME)

    keep = App(server, background="--background" in sys.argv)  # noqa: F841
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
