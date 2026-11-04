#!/usr/bin/env python3
"""Слушай и переводи.

Кинул аудио или видео, получил текст (Whisper), при желании перевёл его (Argos Translate).
Языки: русский, English, Deutsch. Всё работает на твоём компьютере.

Портативный режим: если рядом лежит папка models/<модель>, берётся она,
а если папка argos-packages, то языковые модели перевода берутся из неё.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
from pathlib import Path

import numpy as np
from PySide6.QtCore import QByteArray, QObject, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog,
                               QFrame, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMenu, QPlainTextEdit, QProgressBar,
                               QPushButton, QSizePolicy, QSplitter, QStackedWidget,
                               QVBoxLayout, QWidget)

SAMPLE_RATE = 16000
PROMPT = "Привет. Это расшифровка, с нормальными запятыми и точками."
IPC_NAME = "slushai-single-instance"

SCRIPT = os.path.abspath(__file__)
BASE_DIR = Path(SCRIPT).parent
_ARGOS_LOCAL = BASE_DIR / "argos-packages"  # портативный режим: языковые модели перевода лежат рядом
if _ARGOS_LOCAL.is_dir():
    os.environ.setdefault("ARGOS_TRANSLATE_PACKAGES_DIR", str(_ARGOS_LOCAL))

CONFIG_PATH = Path.home() / ".config" / "slushai" / "config.json"
HISTORY_PATH = Path.home() / ".config" / "slushai" / "history.json"
HISTORY_KEYS = ("path", "name", "status", "text", "error", "lang", "translation", "tr_error", "tr_dst")
LAUNCHER_PATH = Path.home() / ".local" / "share" / "applications" / "slushai.desktop"
ICON_PATH = Path.home() / ".local" / "share" / "icons" / "hicolor" / "scalable" / "apps" / "slushai.svg"
ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 150 150" width="150" height="150">
<rect width="150" height="150" rx="34" fill="#3B82F6"/>
<path d="M38 28 H80 A16 16 0 0 1 96 44 V68 A16 16 0 0 1 80 84 H56 L36 102 V84 H38 A16 16 0 0 1 22 68 V44 A16 16 0 0 1 38 28 Z" fill="#FFFFFF"/>
<g fill="none" stroke="#3B82F6" stroke-width="6" stroke-linecap="round">
<path d="M44 50 V62"/>
<path d="M59 42 V70"/>
<path d="M74 50 V62"/>
</g>
<path d="M92 70 H116 A14 14 0 0 1 130 84 V98 A14 14 0 0 1 116 112 H112 L112 126 L96 112 H92 A14 14 0 0 1 78 98 V84 A14 14 0 0 1 92 70 Z" fill="#3B82F6" stroke="#FFFFFF" stroke-width="6" stroke-linejoin="round"/>
<g fill="none" stroke="#FFFFFF" stroke-width="5" stroke-linecap="round">
<path d="M92 85 H116"/>
<path d="M92 97 H108"/>
</g>
</svg>
"""

AUDIO_EXT = {".wav", ".mp3", ".m4a", ".ogg", ".opus", ".flac", ".aac", ".wma",
             ".amr", ".mp4", ".mkv", ".webm", ".mov", ".avi"}

LANGUAGES = (("ru", "Русский"), ("en", "English"), ("de", "Deutsch"), ("auto", "Определять само"))
# на какой язык переводим: русский -> English, English -> русский, Deutsch -> русский
TARGETS = {"ru": "en", "en": "ru", "de": "ru"}

DEFAULTS = {"language": "ru", "model": "small", "newline_segments": False, "target": "auto", "theme": "dark"}


# ---------- настройки и файлы ----------
def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except Exception:
            pass
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


def write_if_changed(path, text):
    try:
        if path.exists() and path.read_text() == text:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    except Exception as e:
        print("Не смог записать", path, e, file=sys.stderr)


def desktop_text():
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Слушай и переводи\n"
        "Comment=Расшифровка аудио в текст и перевод\n"
        f'Exec="{sys.executable}" "{SCRIPT}"\n'
        f"Path={os.path.dirname(SCRIPT)}\n"
        f"Icon={ICON_PATH}\n"
        "Terminal=false\n"
        "Categories=AudioVideo;Utility;\n"
    )


def collect_audio(paths):
    """Из списка файлов и папок оставляет только аудио/видео."""
    found = []
    for p in paths:
        pp = Path(p)
        if pp.is_dir():
            found += [str(f) for f in sorted(pp.iterdir())
                      if f.is_file() and f.suffix.lower() in AUDIO_EXT]
        elif pp.is_file() and pp.suffix.lower() in AUDIO_EXT:
            found.append(str(pp))
    return found


def ascii_safe_path(path):
    """Даём ffmpeg временную ссылку с латинским именем (русские буквы в имени иногда ломают чтение)."""
    try:
        path.encode("ascii")
        return path, lambda: None
    except UnicodeEncodeError:
        pass
    tmpdir = tempfile.mkdtemp(prefix="slushai-")
    ext = Path(path).suffix.lower()
    if not ext.isascii():
        ext = ""
    link = os.path.join(tmpdir, "audio" + ext)
    try:
        os.symlink(path, link)
    except OSError:
        shutil.copyfile(path, link)
    return link, lambda: shutil.rmtree(tmpdir, ignore_errors=True)


def load_audio(path):
    """Декодирует файл через системный ffmpeg в массив 16 кГц.
    Если ffmpeg нет, отдаём путь как есть, и файл прочитает сам Whisper."""
    ff = shutil.which("ffmpeg")
    if not ff:
        return path
    r = subprocess.run(
        [ff, "-nostdin", "-v", "error", "-i", path, "-vn", "-map", "0:a:0",
         "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "f32le", "-"],
        capture_output=True)
    if r.returncode != 0 or not r.stdout:
        msg = r.stderr.decode("utf-8", "replace").strip()[-300:]
        raise RuntimeError("ffmpeg не смог прочитать файл. " + msg)
    return np.frombuffer(r.stdout, dtype=np.float32).copy()


# ---------- перевод ----------
def chunk_text(line, limit=1200):
    """Режет длинную строку на куски по предложениям, чтобы перевод шёл по частям."""
    sentences = re.split(r"(?<=[.!?…])\s+", line)
    chunks, cur = [], ""
    for s in sentences:
        if cur and len(cur) + len(s) + 1 > limit:
            chunks.append(cur)
            cur = s
        else:
            cur = (cur + " " + s).strip()
    if cur:
        chunks.append(cur)
    return chunks


class Pivot:
    """Перевод через английский: например немецкий -> английский -> русский."""

    def __init__(self, first, second):
        self.first = first
        self.second = second

    def translate(self, text):
        return self.second.translate(self.first.translate(text))


def argos_translate(text, src, dst, on_progress, on_status):
    """Переводит текст офлайн через Argos Translate. Нужные языковые модели при первом разе скачивает."""
    import argostranslate.package as apkg
    import argostranslate.translate as atr

    def lang(code):
        return next((x for x in atr.get_installed_languages() if x.code == code), None)

    def direct(a, b):
        la, lb = lang(a), lang(b)
        return la.get_translation(lb) if la and lb else None

    def find():
        tr = direct(src, dst)
        if tr is not None:
            return tr
        t1, t2 = direct(src, "en"), direct("en", dst)
        return Pivot(t1, t2) if t1 and t2 else None

    tr = find()
    if tr is None:
        on_status("Скачиваю языковые модели для перевода (один раз, нужен интернет)…")
        apkg.update_package_index()
        avail = apkg.get_available_packages()

        def get(a, b):
            pkg = next((x for x in avail if x.from_code == a and x.to_code == b), None)
            if pkg is None:
                raise RuntimeError(f"Не нашёл языковую модель {a} → {b}")
            apkg.install_from_path(pkg.download())

        if any(x.from_code == src and x.to_code == dst for x in avail):
            get(src, dst)
        else:
            # прямой модели нет, идём через английский
            if direct(src, "en") is None:
                get(src, "en")
            if direct("en", dst) is None:
                get("en", dst)
        tr = find()
        if tr is None:
            raise RuntimeError("Модели скачались, но перевод не собрался")

    lines = text.split("\n")
    total = sum(len(chunk_text(x)) for x in lines if x.strip()) or 1
    done = 0
    out = []
    for line in lines:
        if not line.strip():
            out.append("")
            continue
        parts = []
        for ch in chunk_text(line):
            parts.append(tr.translate(ch))
            done += 1
            on_progress(done / total)
        out.append(" ".join(parts))
    return "\n".join(out)


LANG_NAMES = {"ru": "русский", "en": "English", "de": "Deutsch"}
QUALITY = (
    ("base", "Быстрое", "Работает быстро и мало грузит компьютер, но чаще ошибается (модель base)"),
    ("small", "Сбалансированное", "Золотая середина между скоростью и качеством (модель small)"),
    ("medium", "Точное", "Лучшее качество, но медленнее и сильнее грузит компьютер (модель medium)"),
)
TARGET_CHOICES = (("auto", "Авто (по записи)"), ("ru", "Русский"), ("en", "English"))

STYLE = """
* { font-family: "Inter", "Noto Sans", "Ubuntu", sans-serif; font-size: 14px; color: #e6e8f0; }
QWidget#main { background: #14151b; }
QLabel#title { font-size: 24px; font-weight: 700; }
QLabel#muted { color: #9299ae; }
QLabel#section { color: #9299ae; font-size: 12px; font-weight: 700; letter-spacing: 1px; }
QLabel#hname { font-size: 17px; font-weight: 700; }
QLabel#fname { font-size: 14px; font-weight: 600; }
QLabel#fstate, QLabel#hstate { color: #9299ae; font-size: 13px; }
QLabel#fstate[state="run"], QLabel#hstate[state="run"] { color: #6aa3ff; }
QLabel#fstate[state="done"], QLabel#hstate[state="done"] { color: #3baa78; }
QLabel#fstate[state="error"], QLabel#hstate[state="error"] { color: #e45d66; }
QLabel#fstate[state="cancel"], QLabel#hstate[state="cancel"] { color: #d6a44d; }

QFrame#drop { background: #1c1e28; border: 2px dashed #3a3e54; border-radius: 14px; }
QFrame#drop[dragging="true"] { border-color: #4387f5; background: #1d2540; }
QLabel#dropTitle { font-size: 22px; font-weight: 700; }

QListWidget { background: #1c1e28; border: 1px solid #34384a; border-radius: 10px; padding: 6px; outline: none; }
QListWidget::item { border-radius: 8px; }
QListWidget::item:hover { background: #232635; }
QListWidget::item:selected { background: #2b3a63; }
QPlainTextEdit { background: #1c1e28; border: 1px solid #34384a; border-radius: 10px; padding: 12px; font-size: 16px; selection-background-color: #4387f5; }
QSplitter::handle { background: transparent; }
QSplitter::handle:hover { background: #34384a; }

QComboBox { background: #1c1e28; border: 1px solid #34384a; border-radius: 8px; padding: 0 12px; min-height: 38px; min-width: 150px; }
QComboBox:hover, QComboBox:focus { border-color: #4387f5; }
QComboBox:disabled { color: #7a8098; }
QComboBox QAbstractItemView { background: #1c1e28; border: 1px solid #34384a; selection-background-color: #4387f5; }
QMenu { background: #1c1e28; border: 1px solid #34384a; padding: 6px; }
QMenu::item { padding: 8px 18px; border-radius: 6px; }
QMenu::item:selected { background: #2b3a63; }
QMenu::item:disabled { color: #7a8098; }

QPushButton { background: #232635; border: 1px solid #34384a; border-radius: 8px; padding: 0 16px; min-height: 38px; }
QPushButton:hover { background: #2c3044; }
QPushButton:focus { border-color: #4387f5; }
QPushButton:disabled { color: #7a8098; background: #1c1e28; border-color: #2a2d3d; }
QPushButton[accent="true"] { background: #4387f5; border-color: #4387f5; color: #ffffff; font-weight: 700; }
QPushButton[accent="true"]:hover { background: #5a98ff; }
QPushButton[accent="true"]:disabled { background: #1c1e28; border-color: #2a2d3d; color: #7a8098; }
QPushButton#small { min-height: 30px; padding: 0 12px; }
QPushButton#small::menu-indicator { image: none; }
QPushButton#tab { background: transparent; border: none; border-bottom: 2px solid transparent; border-radius: 0; color: #9299ae; padding: 0 14px; }
QPushButton#tab:hover { color: #e6e8f0; background: transparent; }
QPushButton#tab:checked { color: #e6e8f0; border-bottom: 2px solid #4387f5; }

QProgressBar { background: #232635; border: none; border-radius: 2px; min-height: 4px; max-height: 4px; }
QProgressBar::chunk { background: #4387f5; border-radius: 2px; }
QCheckBox { spacing: 10px; }
QCheckBox::indicator { width: 20px; height: 20px; border-radius: 6px; background: #1c1e28; border: 1px solid #3a3e54; }
QCheckBox::indicator:checked { background: #4387f5; border-color: #4387f5; }
"""


# светлая тема: те же стили, только цвета заменены по таблице (акцент и белый текст на акценте остаются)
LIGHT_MAP = {
    "#14151b": "#f3f4f8", "#1c1e28": "#ffffff", "#232635": "#eceef5", "#2c3044": "#e1e4ee",
    "#e6e8f0": "#1b1d27", "#9299ae": "#5f6680", "#34384a": "#cfd3e0", "#3a3e54": "#b8bdd0",
    "#2a2d3d": "#dcdfea", "#7a8098": "#9aa0b5", "#1d2540": "#e3ecfd", "#2b3a63": "#d6e4ff",
    "#6aa3ff": "#2563d1", "#3baa78": "#1f8a5b", "#e45d66": "#c8353f", "#d6a44d": "#a77617",
}
LIGHT_STYLE = re.sub(r"#[0-9a-fA-F]{6}", lambda m: LIGHT_MAP.get(m.group(0).lower(), m.group(0)), STYLE)

# иконки кнопки темы: граната-«флешка» (включить светлую) и луна (вернуть тёмную)
GRENADE_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
<rect x="7" y="9" width="10" height="13" rx="2.5" fill="{c}"/>
<rect x="9" y="6" width="6" height="3.5" rx="1" fill="{c}"/>
<path d="M14.5 6.2 L20.5 4 L21 6 L15.5 8.6 Z" fill="{c}"/>
<circle cx="9.2" cy="4.2" r="2.1" fill="none" stroke="{c}" stroke-width="1.5"/>
<rect x="7" y="13" width="10" height="1.8" fill="{cut}"/>
<rect x="7" y="17" width="10" height="1.8" fill="{cut}"/>
</svg>"""
MOON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
<path d="M20.5 14.6A8.6 8.6 0 1 1 9.4 3.5 6.9 6.9 0 0 0 20.5 14.6Z" fill="{c}"/>
</svg>"""


def svg_icon(svg, color, cut, size=24):
    r = QSvgRenderer(QByteArray(svg.format(c=color, cut=cut).encode()))
    pm = QPixmap(size * 2, size * 2)
    pm.fill(Qt.transparent)
    pt = QPainter(pm)
    r.render(pt)
    pt.end()
    return QIcon(pm)


def repolish(w):
    w.style().unpolish(w)
    w.style().polish(w)


def set_accent(btn, on):
    on = bool(on)
    if btn.property("accent") != on:
        btn.setProperty("accent", on)
        repolish(btn)


def status_info(j):
    """Что написать про файл: (текст, цвет-состояние, доля прогресса или None)."""
    if j["tr_busy"]:
        f = j.get("tr_frac", 0.0)
        return f"Переводится · {f * 100:.0f}%", "run", f
    st = j["status"]
    if st == "wait":
        return "В очереди", "wait", None
    if st == "run":
        f = j.get("frac", 0.0)
        return (f"Распознаётся · {f * 100:.0f}%" if f > 0 else "Распознаётся…"), "run", f
    if st == "done":
        if not j["text"]:
            return "Речи не найдено", "done", None
        if j["translation"]:
            return "Готово · есть перевод", "done", None
        if j["tr_error"]:
            return "Готово · ошибка перевода", "error", None
        return "Готово", "done", None
    if st == "error":
        return "Ошибка распознавания", "error", None
    return "Прервано", "cancel", None


class Bridge(QObject):
    model_loaded = Signal()
    model_failed = Signal(str)
    file_progress = Signal(int, float, str)
    file_done = Signal(int, str)
    file_failed = Signal(int, str)
    file_cancelled = Signal(int)
    tr_status = Signal(str)
    tr_progress = Signal(int, float)
    tr_done = Signal(int, str, str)
    tr_failed = Signal(int, str)


class ClickFrame(QFrame):
    clicked = Signal()

    def mousePressEvent(self, e):
        self.clicked.emit()


class ElideLabel(QLabel):
    """Подпись, которая сама сокращает длинный текст многоточием (полное имя видно в подсказке)."""

    def __init__(self, text=""):
        super().__init__()
        self._full = text
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setFullText(text)

    def setFullText(self, text):
        self._full = text
        self.setToolTip(text)
        self._apply()

    def _apply(self):
        QLabel.setText(self, self.fontMetrics().elidedText(self._full, Qt.ElideMiddle, max(20, self.width())))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._apply()


class FileRow(QWidget):
    """Строка файла в списке: имя, отдельно статус, отдельно полоска прогресса."""

    def __init__(self):
        super().__init__()
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(3)
        self.name = ElideLabel()
        self.name.setObjectName("fname")
        self.state = QLabel()
        self.state.setObjectName("fstate")
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        lay.addWidget(self.name)
        lay.addWidget(self.state)
        lay.addWidget(self.bar)

    def set_state(self, name, text, kind, frac):
        self.name.setFullText(name)
        self.state.setText(text)
        if self.state.property("state") != kind:
            self.state.setProperty("state", kind)
            repolish(self.state)
        self.bar.setVisible(frac is not None)
        if frac is not None:
            self.bar.setValue(int(frac * 1000))


class Main(QWidget):
    def __init__(self, server):
        super().__init__()
        self.cfg = load_config()
        if self.cfg["model"] not in {q[0] for q in QUALITY}:
            self.cfg["model"] = "small"
        self.bridge = Bridge()
        self.server = server
        self.model = None
        self.model_ready = False
        self.jobs = []  # {id, path, name, status, text, error, lang, translation, tr_busy, tr_error, tr_dst, frac, tr_frac}
        self.next_id = 1
        self.running = False
        self.cancel = False
        self.mode = "orig"  # что показываем: оригинал или перевод
        self.scrolls = {}  # позиция прокрутки для каждой пары (файл, вкладка)
        self.shown_key = None

        # иконка и ярлык в меню приложений
        write_if_changed(ICON_PATH, ICON_SVG)
        write_if_changed(LAUNCHER_PATH, desktop_text())
        save_config(self.cfg)

        self.setObjectName("main")
        self.setWindowTitle("Слушай и переводи")
        self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.setAcceptDrops(True)
        self.setMinimumSize(960, 600)
        w, h = (self.cfg.get("win") or [1040, 740])[:2]
        self.resize(max(960, int(w)), max(600, int(h)))

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 18, 24, 14)
        root.setSpacing(12)

        title = QLabel("Слушай и переводи")
        title.setObjectName("title")
        sub = QLabel("Расшифровка аудио в текст и перевод. Всё работает на твоём компьютере.")
        sub.setObjectName("muted")
        top = QHBoxLayout()
        tv = QVBoxLayout()
        tv.setSpacing(2)
        tv.addWidget(title)
        tv.addWidget(sub)
        self.theme_btn = QPushButton()
        self.theme_btn.setFixedSize(44, 44)
        self.theme_btn.setIconSize(QSize(24, 24))
        self.theme_btn.setCursor(Qt.PointingHandCursor)
        self.theme_btn.clicked.connect(self.toggle_theme)
        top.addLayout(tv, 1)
        top.addWidget(self.theme_btn, 0, Qt.AlignTop)
        root.addLayout(top)

        # --- параметры
        opts = QHBoxLayout()
        opts.setSpacing(16)
        self.lang = QComboBox()
        for code, name in LANGUAGES:
            self.lang.addItem(name, code)
        self.lang.setCurrentIndex(max(0, self.lang.findData(self.cfg["language"])))
        self.lang.currentIndexChanged.connect(self.on_lang)
        self.model_box = QComboBox()
        for i, (code, name, tip) in enumerate(QUALITY):
            self.model_box.addItem(name, code)
            self.model_box.setItemData(i, tip, Qt.ToolTipRole)
        self.model_box.setCurrentIndex(max(0, self.model_box.findData(self.cfg["model"])))
        self.model_box.currentIndexChanged.connect(self.on_model)
        self.target_box = QComboBox()
        for code, name in TARGET_CHOICES:
            self.target_box.addItem(name, code)
        self.target_box.setCurrentIndex(max(0, self.target_box.findData(self.cfg.get("target", "auto"))))
        self.target_box.currentIndexChanged.connect(self.on_target)
        self.newline = QCheckBox("Каждая фраза с новой строки")
        self.newline.setChecked(self.cfg["newline_segments"])
        self.newline.toggled.connect(self.on_newline)
        opts.addLayout(self.labeled("Язык записи", self.lang))
        opts.addLayout(self.labeled("Качество распознавания", self.model_box))
        opts.addLayout(self.labeled("Перевести на", self.target_box))
        opts.addStretch(1)
        nl = QVBoxLayout()
        nl.addStretch(1)
        nl.addWidget(self.newline)
        nl.addSpacing(8)
        opts.addLayout(nl)
        root.addLayout(opts)

        # --- страницы: пусто / работа
        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)
        self.pages.addWidget(self.build_empty_page())
        self.pages.addWidget(self.build_work_page())

        self.status = QLabel("Загружаю модель…")
        self.status.setObjectName("muted")
        root.addWidget(self.status)

        QShortcut(QKeySequence("Ctrl+V"), self, activated=self.paste)
        QShortcut(QKeySequence("Ctrl+O"), self, activated=self.choose_files)
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self.save)
        QShortcut(QKeySequence(Qt.Key_Delete), self, activated=self.delete_selected)

        b = self.bridge
        b.model_loaded.connect(self.on_model_loaded)
        b.model_failed.connect(self.on_model_failed)
        b.file_progress.connect(self.on_progress)
        b.file_done.connect(self.on_done)
        b.file_failed.connect(self.on_failed)
        b.file_cancelled.connect(self.on_cancelled)
        b.tr_status.connect(self.set_status)
        b.tr_progress.connect(self.on_tr_progress)
        b.tr_done.connect(self.on_tr_done)
        b.tr_failed.connect(self.on_tr_failed)

        # второй запуск (например из меню) просто выводит это окно вперёд
        server.newConnection.connect(self.on_ipc)

        self.apply_theme()
        self.load_history()
        self.update_pages()
        self.refresh_view()
        threading.Thread(target=self.load_model, args=(self.cfg["model"],), daemon=True).start()

    # --- тема ---
    def apply_theme(self):
        light = self.cfg.get("theme") == "light"
        self.setStyleSheet(LIGHT_STYLE if light else STYLE)
        if light:
            self.theme_btn.setIcon(svg_icon(MOON_SVG, "#1b1d27", "#ffffff"))
            self.theme_btn.setToolTip("Вернуть тёмную тему")
        else:
            self.theme_btn.setIcon(svg_icon(GRENADE_SVG, "#e6e8f0", "#232635"))
            self.theme_btn.setToolTip("Включить светлую тему (флешка)")

    def toggle_theme(self):
        self.cfg["theme"] = "dark" if self.cfg.get("theme") == "light" else "light"
        save_config(self.cfg)
        self.apply_theme()

    # --- сборка окна ---
    def build_empty_page(self):
        self.drop = ClickFrame()
        self.drop.setObjectName("drop")
        self.drop.setCursor(Qt.PointingHandCursor)
        self.drop.clicked.connect(self.choose_files)
        dl = QVBoxLayout(self.drop)
        dl.setAlignment(Qt.AlignCenter)
        dl.setSpacing(8)
        t = QLabel("🎧  Добавь аудио или видео")
        t.setObjectName("dropTitle")
        t.setAlignment(Qt.AlignCenter)
        s = QLabel("Перетащи файлы сюда или выбери их на компьютере")
        s.setAlignment(Qt.AlignCenter)
        h = QLabel("Подойдут WAV, MP3, M4A, OGG, FLAC и видео. Можно сразу несколько файлов.")
        h.setObjectName("muted")
        h.setAlignment(Qt.AlignCenter)
        row = QHBoxLayout()
        row.setAlignment(Qt.AlignCenter)
        b_pick = QPushButton("Выбрать файлы…")
        set_accent(b_pick, True)
        b_pick.clicked.connect(self.choose_files)
        b_paste = QPushButton("Вставить из буфера (Ctrl+V)")
        b_paste.clicked.connect(self.paste)
        row.addWidget(b_pick)
        row.addWidget(b_paste)
        dl.addStretch(1)
        dl.addWidget(t)
        dl.addWidget(s)
        dl.addWidget(h)
        dl.addSpacing(10)
        dl.addLayout(row)
        dl.addStretch(1)
        return self.drop

    def build_work_page(self):
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)

        # левая панель: файлы
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(8)
        lh = QHBoxLayout()
        lab = QLabel("ФАЙЛЫ")
        lab.setObjectName("section")
        b_add = QPushButton("+ Добавить")
        b_add.setObjectName("small")
        b_add.clicked.connect(self.choose_files)
        b_more = QPushButton("⋯")
        b_more.setObjectName("small")
        b_more.setToolTip("Удалить выбранное или очистить список")
        menu = QMenu(b_more)
        menu.addAction("Удалить выбранное", self.delete_selected)
        menu.addAction("Очистить список", self.clear)
        b_more.setMenu(menu)
        lh.addWidget(lab)
        lh.addStretch(1)
        lh.addWidget(b_add)
        lh.addWidget(b_more)
        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.ExtendedSelection)
        self.list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self.list_menu)
        self.list.currentRowChanged.connect(self.on_select)
        ll.addLayout(lh)
        ll.addWidget(self.list, 1)
        left.setMinimumWidth(250)

        # правая панель: заголовок, вкладки, текст, действия
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(6, 0, 0, 0)
        rl.setSpacing(8)

        head = QHBoxLayout()
        hv = QVBoxLayout()
        hv.setSpacing(2)
        self.h_name = ElideLabel()
        self.h_name.setObjectName("hname")
        self.h_state = QLabel()
        self.h_state.setObjectName("hstate")
        hv.addWidget(self.h_name)
        hv.addWidget(self.h_state)
        self.stop_btn = QPushButton("Отменить обработку")
        self.stop_btn.clicked.connect(self.stop)
        self.stop_btn.setVisible(False)
        head.addLayout(hv, 1)
        head.addWidget(self.stop_btn)
        rl.addLayout(head)
        self.h_bar = QProgressBar()
        self.h_bar.setRange(0, 1000)
        self.h_bar.setTextVisible(False)
        rl.addWidget(self.h_bar)

        tabs = QHBoxLayout()
        tabs.setSpacing(4)
        self.b_orig = QPushButton("Оригинал")
        self.b_tr = QPushButton("Перевод")
        for btn, mode in ((self.b_orig, "orig"), (self.b_tr, "tr")):
            btn.setObjectName("tab")
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setMinimumWidth(150)
            btn.clicked.connect(lambda _=False, m=mode: self.set_mode(m))
            tabs.addWidget(btn)
        self.b_orig.setChecked(True)
        tabs.addStretch(1)
        rl.addLayout(tabs)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        rl.addWidget(self.view, 1)

        acts = QHBoxLayout()
        acts.setSpacing(8)
        self.reason = QLabel()
        self.reason.setObjectName("muted")
        self.reason.setWordWrap(True)
        self.reason.setVisible(False)
        self.b_translate = QPushButton("Перевести")
        self.b_translate.clicked.connect(self.translate)
        self.b_copy = QPushButton("Копировать оригинал")
        self.b_copy.clicked.connect(self.copy)
        self.b_save = QPushButton("Сохранить оригинал")
        self.b_save.clicked.connect(self.save)
        acts.addStretch(1)
        acts.addWidget(self.b_translate)
        acts.addWidget(self.b_copy)
        acts.addWidget(self.b_save)
        rl.addWidget(self.reason)
        rl.addLayout(acts)

        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([300, 700])
        return split

    def labeled(self, text, widget):
        box = QVBoxLayout()
        box.setSpacing(6)
        label = QLabel(text)
        label.setObjectName("muted")
        box.addWidget(label)
        box.addWidget(widget)
        return box

    def update_pages(self):
        self.pages.setCurrentIndex(1 if self.jobs else 0)

    def closeEvent(self, e):
        self.cfg["win"] = [self.width(), self.height()]
        save_config(self.cfg)
        self.save_history()
        super().closeEvent(e)

    def on_ipc(self):
        s = self.server.nextPendingConnection()
        if s:
            s.disconnectFromServer()
        self.show()
        self.raise_()
        self.activateWindow()

    # --- перетаскивание на всё окно ---
    def _drag(self, on):
        self.drop.setProperty("dragging", on)
        repolish(self.drop)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._drag(True)

    def dragLeaveEvent(self, e):
        self._drag(False)

    def dropEvent(self, e):
        self._drag(False)
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.add_files(paths)

    # --- настройки ---
    def on_lang(self):
        self.cfg["language"] = self.lang.currentData()
        save_config(self.cfg)
        self.refresh_view()

    def on_model(self):
        self.cfg["model"] = self.model_box.currentData()
        save_config(self.cfg)
        self.model_ready = False
        self.set_status("Загружаю другую модель…")
        threading.Thread(target=self.load_model, args=(self.cfg["model"],), daemon=True).start()

    def on_target(self):
        self.cfg["target"] = self.target_box.currentData()
        save_config(self.cfg)
        self.refresh_view()

    def on_newline(self, v):
        self.cfg["newline_segments"] = v
        save_config(self.cfg)

    def sync_running_ui(self):
        """Кнопка отмены видна только во время работы, а настройки распознавания на это время заблокированы."""
        self.stop_btn.setVisible(self.running)
        tip = "Во время обработки менять нельзя" if self.running else ""
        for w in (self.lang, self.model_box, self.newline):
            w.setEnabled(not self.running)
            w.setToolTip(tip)

    # --- модель ---
    def load_model(self, size):
        try:
            from faster_whisper import WhisperModel
            model = WhisperModel(model_source(size), device="cpu", compute_type="int8")
            self.model = model
            self.bridge.model_loaded.emit()
        except Exception as e:
            self.bridge.model_failed.emit(str(e))

    def on_model_loaded(self):
        self.model_ready = True
        self.set_status("Готов")
        self.maybe_start()

    def on_model_failed(self, err):
        self.set_status("Не загрузилась модель (в первый раз нужен интернет)")
        print("Ошибка загрузки модели:", err, file=sys.stderr)

    # --- добавление файлов ---
    def choose_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Выбери аудио или видео", str(Path.home()),
            "Аудио и видео (" + " ".join("*" + x for x in sorted(AUDIO_EXT)) + ");;Все файлы (*)")
        if files:
            self.add_files(files)

    def paste(self):
        md = QApplication.clipboard().mimeData()
        paths = []
        if md.hasUrls():
            paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
        if not paths and md.hasFormat("x-special/gnome-copied-files"):
            raw = bytes(md.data("x-special/gnome-copied-files")).decode(errors="ignore")
            paths = [QUrl(line).toLocalFile() for line in raw.splitlines()[1:] if line.startswith("file:")]
        if not paths:
            self.set_status("В буфере нет файлов. Скопируй аудиофайл в файловом менеджере (Ctrl+C) и нажми сюда")
            return
        self.add_files(paths)

    def add_files(self, paths):
        audio = collect_audio(paths)
        if not audio:
            self.set_status("Это не аудио. Подойдут wav, mp3, m4a, ogg, flac и видеофайлы")
            return
        first_new = len(self.jobs)
        for p in audio:
            job = {
                "id": self.next_id, "path": p, "name": Path(p).name, "status": "wait",
                "text": "", "error": "", "lang": "", "translation": "",
                "tr_busy": False, "tr_error": "", "tr_dst": "", "frac": 0.0, "tr_frac": 0.0,
            }
            self.jobs.append(job)
            self.next_id += 1
            self.add_row(job)
        self.save_history()
        self.update_pages()
        if self.list.currentRow() < 0:
            self.list.setCurrentRow(first_new)
        self.maybe_start()

    def add_row(self, job):
        item = QListWidgetItem()
        item.setSizeHint(QSize(0, 70))
        self.list.addItem(item)
        self.list.setItemWidget(item, FileRow())
        self.refresh_item(job["id"])

    # --- история: список файлов с текстами и переводами переживает закрытие ---
    def save_history(self):
        try:
            data = [{k: j[k] for k in HISTORY_KEYS} for j in self.jobs]
            HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = HISTORY_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(HISTORY_PATH)
        except Exception as e:
            print("Не смог сохранить историю:", e, file=sys.stderr)

    def load_history(self):
        try:
            data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except Exception:
            return
        for d in data:
            try:
                job = {
                    "id": self.next_id, "path": d["path"], "name": d["name"], "status": d["status"],
                    "text": d.get("text", ""), "error": d.get("error", ""), "lang": d.get("lang", ""),
                    "translation": d.get("translation", ""), "tr_busy": False,
                    "tr_error": d.get("tr_error", ""), "tr_dst": d.get("tr_dst", ""),
                    "frac": 1.0, "tr_frac": 0.0,
                }
            except KeyError:
                continue
            if job["status"] in ("wait", "run"):  # программу закрыли посреди работы
                job["status"] = "cancel"
            self.next_id += 1
            self.jobs.append(job)
            self.add_row(job)
        if self.jobs:
            self.list.setCurrentRow(0)

    # --- поиск заданий по номеру (список может меняться, пока что-то считается) ---
    def row_of(self, jid):
        return next((i for i, j in enumerate(self.jobs) if j["id"] == jid), -1)

    def job_of(self, jid):
        i = self.row_of(jid)
        return (i, self.jobs[i]) if i >= 0 else (-1, None)

    def refresh_item(self, jid):
        i, j = self.job_of(jid)
        if i < 0:
            return
        row = self.list.itemWidget(self.list.item(i))
        if row is not None:
            text, kind, frac = status_info(j)
            row.set_state(j["name"], text, kind, frac)

    # --- очередь расшифровки ---
    def maybe_start(self):
        if self.running:
            return
        job = next((j for j in self.jobs if j["status"] == "wait"), None)
        if job is None:
            return
        if not self.model_ready:
            self.set_status("Модель ещё грузится, расшифровка начнётся сама")
            return
        self.running = True
        self.cancel = False
        job["status"] = "run"
        job["frac"] = 0.0
        self.sync_running_ui()
        self.refresh_item(job["id"])
        self.refresh_view()
        self.set_status(f"Расшифровываю: {job['name']}")
        threading.Thread(target=self.work, args=(job,), daemon=True).start()

    def work(self, job):
        br = self.bridge
        jid = job["id"]
        cleanup = lambda: None  # noqa: E731
        try:
            path, cleanup = ascii_safe_path(job["path"])
            audio = load_audio(path)
            lang = self.cfg["language"]
            lang = None if lang == "auto" else lang
            segments, info = self.model.transcribe(
                audio, language=lang, vad_filter=True, beam_size=5,
                initial_prompt=PROMPT if lang == "ru" else None)
            job["lang"] = getattr(info, "language", "") or ""
            total = max(float(info.duration or 0), 0.1)
            sep = "\n" if self.cfg["newline_segments"] else " "
            parts = []
            for seg in segments:
                if self.cancel:
                    br.file_cancelled.emit(jid)
                    return
                t = seg.text.strip()
                if t:
                    parts.append(t)
                br.file_progress.emit(jid, min(seg.end / total, 1.0), sep.join(parts))
            br.file_done.emit(jid, sep.join(parts))
        except Exception as e:
            traceback.print_exc()
            br.file_failed.emit(jid, str(e))
        finally:
            cleanup()

    def on_progress(self, jid, frac, partial):
        i, j = self.job_of(jid)
        if j is None:
            return
        j["text"] = partial
        j["frac"] = frac
        self.refresh_item(jid)
        if self.list.currentRow() == i:
            self.update_header(j)
            if self.mode == "orig":
                sb = self.view.verticalScrollBar()
                at_bottom = sb.value() >= sb.maximum() - 4
                if self.view.toPlainText() != partial:
                    self.view.setPlainText(partial)
                if at_bottom:  # если пользователь читает выше, не дёргаем текст
                    sb.setValue(sb.maximum())
            self.update_actions(j)

    def on_done(self, jid, text):
        i, j = self.job_of(jid)
        if j is not None:
            j["status"] = "done"
            j["text"] = text
            j["frac"] = 1.0
            self.refresh_item(jid)
            self.save_history()
            if self.list.currentRow() == i:
                self.refresh_view()
        self.next_job()

    def on_failed(self, jid, err):
        i, j = self.job_of(jid)
        if j is not None:
            j["status"] = "error"
            j["error"] = err
            self.refresh_item(jid)
            self.save_history()
            if self.list.currentRow() == i:
                self.refresh_view()
        self.next_job()

    def on_cancelled(self, jid):
        _, j = self.job_of(jid)
        if j is not None:
            j["status"] = "cancel"
            self.refresh_item(jid)
        for other in self.jobs:
            if other["status"] == "wait":
                other["status"] = "cancel"
                self.refresh_item(other["id"])
        self.save_history()
        self.finish("Остановлено")
        self.refresh_view()

    def next_job(self):
        self.running = False
        if any(j["status"] == "wait" for j in self.jobs):
            self.maybe_start()
        else:
            self.finish("Готово")
            self.refresh_view()
            if not self.isActiveWindow() and shutil.which("notify-send"):
                subprocess.Popen(["notify-send", "Слушай и переводи", "Расшифровка готова"])

    def finish(self, text):
        self.running = False
        self.sync_running_ui()
        self.set_status(text)

    def stop(self):
        self.cancel = True
        self.set_status("Останавливаю…")

    def retry(self, jid):
        _, j = self.job_of(jid)
        if j is None or j["status"] not in ("error", "cancel") or j["tr_busy"]:
            return
        j.update(status="wait", text="", error="", lang="", translation="", tr_error="", tr_dst="", frac=0.0)
        self.refresh_item(jid)
        self.save_history()
        self.refresh_view()
        self.maybe_start()

    # --- показ ---
    def current_job(self):
        row = self.list.currentRow()
        return (row, self.jobs[row]) if 0 <= row < len(self.jobs) else (-1, None)

    def target_info(self, j):
        """На какой язык переводить: (код, "") или (None, причина, почему нельзя)."""
        cfg_lang = self.cfg["language"]
        src = j["lang"] or (cfg_lang if cfg_lang in TARGETS else "")
        if src not in TARGETS:
            return None, "Перевод есть для русского, английского и немецкого"
        pick = self.cfg.get("target", "auto")
        dst = TARGETS[src] if pick == "auto" else pick
        if dst == src:
            return None, "Запись уже на этом языке. Выбери другой язык в «Перевести на»"
        return dst, ""

    def on_select(self, _row):
        self.refresh_view()

    def set_mode(self, mode):
        self.mode = mode
        self.refresh_view()

    def update_header(self, j):
        if j is None:
            self.h_name.setFullText("Файл не выбран")
            self.h_state.setText("")
            self.h_bar.setVisible(False)
            return
        text, kind, frac = status_info(j)
        self.h_name.setFullText(j["name"])
        self.h_state.setText(text)
        if self.h_state.property("state") != kind:
            self.h_state.setProperty("state", kind)
            repolish(self.h_state)
        self.h_bar.setVisible(frac is not None)
        if frac is not None:
            self.h_bar.setValue(int(frac * 1000))

    def update_actions(self, j):
        txt = self.shown_text()
        which = "перевод" if self.mode == "tr" else "оригинал"
        self.b_copy.setText(f"Копировать {which}")
        self.b_save.setText(f"Сохранить {which}")
        self.b_copy.setEnabled(bool(txt))
        self.b_save.setEnabled(bool(txt))

        enabled, label, reason = False, "Перевести", ""
        if j is not None:
            if j["tr_busy"]:
                label, reason = "Переводится…", "Идёт перевод. Пока можно читать оригинал."
            elif j["status"] == "wait":
                reason = "Файл ждёт очереди на расшифровку"
            elif j["status"] == "run":
                reason = "Перевод станет доступен, когда закончится расшифровка"
            elif j["status"] == "cancel":
                reason = "Расшифровка была прервана, перевод недоступен"
            elif j["status"] == "error":
                reason = "Расшифровка не удалась, переводить нечего"
            elif not j["text"]:
                reason = "В записи нет речи, переводить нечего"
            else:
                dst, why = self.target_info(j)
                if dst is None:
                    reason = why
                else:
                    enabled = True
                    again = bool(j["translation"]) and j["tr_dst"] == dst
                    label = "Перевести заново" if again else f"Перевести на {LANG_NAMES[dst]}"
        self.b_translate.setText(label)
        self.b_translate.setEnabled(enabled)
        self.b_translate.setToolTip(reason)
        self.reason.setText(reason)
        self.reason.setVisible(bool(reason))
        primary_translate = enabled and not (j and j["translation"])
        set_accent(self.b_translate, primary_translate)
        set_accent(self.b_copy, bool(txt) and not primary_translate)

    def content_for(self, j):
        """Что показать в текстовом поле: (текст, подсказка-заглушка, если текста нет)."""
        if self.mode == "tr":
            if j["status"] == "cancel":
                return "", "Расшифровка была прервана, поэтому перевода нет."
            if j["tr_busy"]:
                return "", "Перевожу… Пока можно открыть вкладку «Оригинал» и читать."
            if j["tr_error"]:
                return "", ("Не удалось получить перевод. Оригинал на месте, можно повторить.\n\n"
                            "Подробности: " + j["tr_error"])
            if j["translation"]:
                return j["translation"], ""
            return "", "Перевод ещё не создан.\nВыбери язык в «Перевести на» и нажми «Перевести»."
        st = j["status"]
        if st == "error":
            return "", "Не удалось расшифровать файл. Проверь, что он не битый, или попробуй другое качество.\n\nПодробности: " + j["error"]
        if st == "wait":
            return "", "Файл в очереди. Расшифровка начнётся сама."
        if st == "cancel":
            note = "[Расшифровка была прервана, показана только готовая часть]"
            return (note + "\n\n" + j["text"], "") if j["text"] else ("", note)
        if st == "run":
            return j["text"], "Идёт расшифровка… Текст дописывается по ходу."
        return j["text"], "Речи не найдено."

    def refresh_view(self):
        sb = self.view.verticalScrollBar()
        if self.shown_key:
            self.scrolls[self.shown_key] = sb.value()
        _, j = self.current_job()
        self.b_orig.setChecked(self.mode == "orig")
        self.b_tr.setChecked(self.mode == "tr")
        self.update_header(j)
        self.update_actions(j)
        if j is None:
            self.b_tr.setText("Перевод")
            self.shown_key = None
            self.view.setPlainText("")
            self.view.setPlaceholderText("Выбери файл в списке слева.")
            return

        if j["tr_busy"]:
            self.b_tr.setText("Перевод · идёт")
        elif j["tr_error"]:
            self.b_tr.setText("Перевод · ошибка")
        elif j["translation"]:
            self.b_tr.setText("Перевод · готов")
        else:
            self.b_tr.setText("Перевод")

        text, hint = self.content_for(j)
        key = (j["id"], self.mode)
        if self.view.toPlainText() != text:
            self.view.setPlainText(text)
        self.view.setPlaceholderText(hint)
        if key != self.shown_key:
            pos = self.scrolls.get(key, 0)
            if j["status"] == "run" and self.mode == "orig":
                pos = None  # идущую расшифровку показываем с конца
            QTimer.singleShot(0, lambda p=pos: sb.setValue(sb.maximum() if p is None else p))
        self.shown_key = key

    # --- перевод ---
    def translate(self):
        _, j = self.current_job()
        if j is None or j["tr_busy"] or j["status"] != "done" or not j["text"]:
            return
        dst, why = self.target_info(j)
        if dst is None:
            self.set_status(why)
            return
        src = j["lang"] or self.cfg["language"]
        j["tr_busy"] = True
        j["tr_error"] = ""
        j["tr_dst"] = dst
        j["tr_frac"] = 0.0
        self.mode = "tr"
        self.refresh_item(j["id"])
        self.refresh_view()
        self.set_status("Перевожу…")
        threading.Thread(target=self.tr_work, args=(j["id"], j["text"], src, dst), daemon=True).start()

    def tr_work(self, jid, text, src, dst):
        br = self.bridge
        try:
            out = argos_translate(text, src, dst,
                                  lambda f: br.tr_progress.emit(jid, f),
                                  lambda m: br.tr_status.emit(m))
            br.tr_done.emit(jid, out, dst)
        except ImportError:
            br.tr_failed.emit(jid, "Не установлена библиотека перевода. Вбей в терминале: "
                                   "~/apps/govori/venv/bin/pip install argostranslate")
        except Exception as e:
            traceback.print_exc()
            br.tr_failed.emit(jid, str(e))

    def on_tr_progress(self, jid, frac):
        i, j = self.job_of(jid)
        if j is None:
            return
        j["tr_frac"] = frac
        self.refresh_item(jid)
        if self.list.currentRow() == i:
            self.update_header(j)
        self.set_status(f"Перевожу… {frac * 100:.0f}%")

    def on_tr_done(self, jid, text, dst):
        i, j = self.job_of(jid)
        if j is None:
            return
        j["translation"] = text
        j["tr_busy"] = False
        j["tr_error"] = ""
        j["tr_dst"] = dst
        self.refresh_item(jid)
        self.set_status("Перевод готов")
        self.save_history()
        if self.list.currentRow() == i:
            self.refresh_view()

    def on_tr_failed(self, jid, err):
        _, j = self.job_of(jid)
        if j is None:
            return
        j["tr_busy"] = False
        j["tr_error"] = err
        self.refresh_item(jid)
        self.set_status("Ошибка перевода")
        self.save_history()
        self.refresh_view()

    # --- список: меню, удаление, очистка ---
    def list_menu(self, pos):
        it = self.list.itemAt(pos)
        if it is None:
            return
        if not it.isSelected():
            self.list.clearSelection()
            it.setSelected(True)
            self.list.setCurrentItem(it)
        j = self.jobs[self.list.row(it)]
        m = QMenu(self)
        if j["status"] in ("error", "cancel") and not j["tr_busy"]:
            m.addAction("Повторить распознавание", lambda jid=j["id"]: self.retry(jid))
        m.addAction("Удалить из списка", self.delete_selected)
        m.exec(self.list.viewport().mapToGlobal(pos))

    def delete_selected(self):
        rows = sorted({self.list.row(i) for i in self.list.selectedItems()}, reverse=True)
        if not rows:
            self.set_status("Выбери в списке, что удалить (можно несколько, с зажатым Ctrl или Shift)")
            return
        deleted = skipped = 0
        for r in rows:
            j = self.jobs[r]
            if j["status"] == "run" or j["tr_busy"]:
                skipped += 1
                continue
            self.jobs.pop(r)
            self.list.takeItem(r)
            deleted += 1
        self.save_history()
        self.update_pages()
        self.refresh_view()
        msg = f"Удалено из списка: {deleted}. Сами файлы на диске не тронуты"
        if skipped:
            msg += ". Идущую расшифровку или перевод удалить нельзя, дождись конца или нажми «Отменить обработку»"
        self.set_status(msg)

    def clear(self):
        if self.running:
            self.set_status("Идёт расшифровка. Сначала дождись конца или нажми «Отменить обработку»")
            return
        if any(j["tr_busy"] for j in self.jobs):
            self.set_status("Идёт перевод, подожди")
            return
        self.jobs.clear()
        self.list.clear()
        self.scrolls.clear()
        self.shown_key = None
        self.save_history()
        self.update_pages()
        self.refresh_view()
        self.set_status("Список очищен. Файлы на диске не тронуты")

    # --- копирование и сохранение ---
    def shown_text(self):
        _, j = self.current_job()
        if j is None:
            return ""
        return j["translation"] if self.mode == "tr" else j["text"]

    def copy(self):
        text = self.shown_text()
        if not text:
            self.set_status("Нечего копировать")
            return
        QApplication.clipboard().setText(text)
        self.set_status("Перевод скопирован" if self.mode == "tr" else "Оригинал скопирован")

    def save(self):
        text = self.shown_text()
        _, j = self.current_job()
        if not text:
            self.set_status("Нечего сохранять")
            return
        stem = Path(j["path"]).with_suffix("")
        suffix = f".{j['tr_dst'] or 'tr'}.txt" if self.mode == "tr" else ".txt"
        path, _f = QFileDialog.getSaveFileName(self, "Сохранить текст", str(stem) + suffix, "Текст (*.txt)")
        if path:
            Path(path).write_text(text, encoding="utf-8")
            self.set_status(f"Сохранено: {Path(path).name}")

    def set_status(self, text):
        self.status.setText(text)


def main():
    app = QApplication(sys.argv)
    app.setDesktopFileName("slushai")

    # если уже запущено, просим то окно выйти вперёд и выходим
    sock = QLocalSocket()
    sock.connectToServer(IPC_NAME)
    if sock.waitForConnected(300):
        sock.write(b"window")
        sock.flush()
        return 0

    QLocalServer.removeServer(IPC_NAME)
    server = QLocalServer()
    server.listen(IPC_NAME)

    win = Main(server)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
