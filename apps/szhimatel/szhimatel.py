#!/usr/bin/env python3
"""Re Shrink: перетащил файлы, выбрал качество, нажал кнопку. Всё делает ffmpeg."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QProcess, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QMessageBox, QProgressBar, QPushButton,
    QVBoxLayout, QWidget,
)

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".flv",
             ".wmv", ".mts", ".m2ts", ".3gp", ".ts"}

# (название, описание, crf, макс. высота, битрейт звука)
PRESETS = [
    ("Лёгкое", "почти не видно разницы", 23, None, "128k"),
    ("Среднее", "хороший баланс, ужимает сильно", 28, None, "128k"),
    ("Сильное", "для мессенджеров и почты, до 720p", 32, 720, "96k"),
    ("Максимум", "самый маленький файл, до 480p", 35, 480, "64k"),
]

CODECS = [
    ("H.264", "libx264", 0, "работает везде"),
    ("H.265", "libx265", 4, "файл меньше, но не везде играет"),
]

STYLE = """
* { font-family: "Inter", "Noto Sans", "Ubuntu", sans-serif; font-size: 14px; color: #e8e9f0; }
QWidget#root { background: #14151c; }
QLabel#title { font-size: 24px; font-weight: 700; }
QLabel#subtitle { color: #8b8fa3; }
QLabel#muted { color: #8b8fa3; }
QFrame#drop { background: #1c1e28; border: 2px dashed #3a3e54; border-radius: 18px; }
QFrame#drop[dragging="true"] { border-color: #7c6cff; background: #232540; }
QLabel#dropTitle { font-size: 17px; font-weight: 600; }
QListWidget { background: transparent; border: none; outline: none; }
QListWidget::item { background: transparent; border: none; }
QFrame#row { background: #1c1e28; border-radius: 12px; }
QComboBox { background: #1c1e28; border: 1px solid #2c2f42; border-radius: 10px; padding: 8px 12px; min-height: 22px; }
QComboBox:hover { border-color: #7c6cff; }
QComboBox QAbstractItemView { background: #1c1e28; border: 1px solid #2c2f42; selection-background-color: #7c6cff; }
QPushButton { background: #262940; border: none; border-radius: 10px; padding: 10px 16px; }
QPushButton:hover { background: #30344f; }
QPushButton:disabled { color: #5d6079; background: #1c1e28; }
QPushButton#primary { background: #7c6cff; font-weight: 700; font-size: 15px; padding: 12px 28px; }
QPushButton#primary:hover { background: #8e80ff; }
QPushButton#primary:disabled { background: #2c2f42; color: #5d6079; }
QPushButton#danger { background: #e5484d; font-weight: 700; font-size: 15px; padding: 12px 28px; }
QPushButton#danger:hover { background: #ef5e63; }
QPushButton#x { background: transparent; color: #8b8fa3; padding: 4px 10px; font-size: 16px; }
QPushButton#x:hover { color: #ff6b70; background: #262940; }
QProgressBar { background: #2c2f42; border: none; border-radius: 4px; max-height: 8px; min-height: 8px; }
QProgressBar::chunk { background: #7c6cff; border-radius: 4px; }
"""


SCRIPT = os.path.abspath(__file__)
ICON_PATH = Path.home() / ".local" / "share" / "icons" / "hicolor" / "scalable" / "apps" / "szhimatel.svg"
LAUNCHER_PATH = Path.home() / ".local" / "share" / "applications" / "szhimatel.desktop"
ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 150 150" width="150" height="150">
<rect width="150" height="150" rx="34" fill="#7C6CFF"/>
<path d="M57 52 L57 98 L93 75 Z" fill="#FFFFFF" stroke="#FFFFFF" stroke-width="6" stroke-linejoin="round"/>
<g fill="none" stroke="#FFFFFF" stroke-width="6" stroke-linecap="round" stroke-linejoin="round">
<path d="M23 58 L35 75 L23 92"/>
<path d="M127 58 L115 75 L127 92"/>
</g>
</svg>
"""


def write_if_changed(path, text):
    try:
        if path.exists() and path.read_text() == text:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    except Exception as e:
        print("Не смог записать", path, e, file=sys.stderr)


def install_launcher():
    """Кладёт иконку и ярлык в меню приложений (обновляет при каждом запуске)."""
    write_if_changed(ICON_PATH, ICON_SVG)
    write_if_changed(LAUNCHER_PATH, (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Re Shrink\n"
        "Comment=Сжать видео через ffmpeg\n"
        f'Exec="{sys.executable}" "{SCRIPT}"\n'
        f"Path={os.path.dirname(SCRIPT)}\n"
        f"Icon={ICON_PATH}\n"
        "Terminal=false\n"
        "Categories=AudioVideo;Video;\n"
    ))


def human(n):
    n = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit in ("Б", "КБ") else f"{n:.1f} {unit}"
        n /= 1024


def get_duration(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=20).stdout.strip()
        return float(out)
    except Exception:
        return 0.0


def unique_path(folder, stem):
    p = Path(folder) / f"{stem}_сжато.mp4"
    i = 2
    while p.exists():
        p = Path(folder) / f"{stem}_сжато_{i}.mp4"
        i += 1
    return str(p)


class DropZone(QFrame):
    files_dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setObjectName("drop")
        self.setAcceptDrops(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(150)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignCenter)
        icon = QLabel("🎬")
        icon.setStyleSheet("font-size: 38px;")
        icon.setAlignment(Qt.AlignCenter)
        t = QLabel("Перетащи видео сюда")
        t.setObjectName("dropTitle")
        t.setAlignment(Qt.AlignCenter)
        s = QLabel("или нажми, чтобы выбрать файлы")
        s.setObjectName("muted")
        s.setAlignment(Qt.AlignCenter)
        for w in (icon, t, s):
            lay.addWidget(w)

    def _set_drag(self, on):
        self.setProperty("dragging", on)
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, e):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Выбери видео", str(Path.home()),
            "Видео (" + " ".join("*" + x for x in sorted(VIDEO_EXT)) + ");;Все файлы (*)")
        if files:
            self.files_dropped.emit(files)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._set_drag(True)

    def dragLeaveEvent(self, e):
        self._set_drag(False)

    def dropEvent(self, e):
        self._set_drag(False)
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.files_dropped.emit(paths)


class Row(QFrame):
    removed = Signal(object)

    def __init__(self, path):
        super().__init__()
        self.setObjectName("row")
        self.path = path
        self.status = "wait"  # wait / run / done / error / cancel
        self.out_path = None
        self.duration = 0.0
        self.src_size = os.path.getsize(path)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 10, 12)
        lay.setSpacing(6)
        top = QHBoxLayout()
        self.name = QLabel(Path(path).name)
        self.name.setStyleSheet("font-weight: 600;")
        self.info = QLabel(f"{human(self.src_size)} · ждёт очереди")
        self.info.setObjectName("muted")
        self.x = QPushButton("✕")
        self.x.setObjectName("x")
        self.x.setCursor(Qt.PointingHandCursor)
        self.x.clicked.connect(lambda: self.removed.emit(self))
        top.addWidget(self.name, 1)
        top.addWidget(self.info)
        top.addWidget(self.x)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        lay.addLayout(top)
        lay.addWidget(self.bar)

    def set_status(self, status, text):
        self.status = status
        self.info.setText(text)
        self.x.setEnabled(status != "run")


class Main(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        self.setWindowTitle("Re Shrink")
        self.resize(760, 780)
        self.out_dir = None  # None = рядом с оригиналом
        self.proc = None
        self.current = None
        self.cancelled = False
        self.output_buf = []

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 24)
        root.setSpacing(14)

        title = QLabel("Re Shrink")
        title.setObjectName("title")
        sub = QLabel("Кинул файлы, выбрал качество, нажал «Сжать». Оригиналы не трогаются.")
        sub.setObjectName("subtitle")
        root.addWidget(title)
        root.addWidget(sub)

        self.drop = DropZone()
        self.drop.files_dropped.connect(self.add_files)
        root.addWidget(self.drop)

        self.list = QListWidget()
        self.list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.list.setSpacing(6)
        root.addWidget(self.list, 1)

        opts = QHBoxLayout()
        opts.setSpacing(10)
        self.preset = QComboBox()
        for name, desc, *_ in PRESETS:
            self.preset.addItem(f"{name} — {desc}")
        self.preset.setCurrentIndex(1)
        self.codec = QComboBox()
        for name, _, _, desc in CODECS:
            self.codec.addItem(f"{name} — {desc}")
        opts.addWidget(self.preset, 3)
        opts.addWidget(self.codec, 3)
        root.addLayout(opts)

        folder = QHBoxLayout()
        self.folder_label = QLabel()
        self.folder_label.setObjectName("muted")
        self._update_folder_label()
        b1 = QPushButton("Папка…")
        b1.clicked.connect(self.choose_folder)
        b2 = QPushButton("Рядом с оригиналом")
        b2.clicked.connect(self.reset_folder)
        folder.addWidget(self.folder_label, 1)
        folder.addWidget(b1)
        folder.addWidget(b2)
        root.addLayout(folder)

        bottom = QHBoxLayout()
        self.clear_btn = QPushButton("Очистить список")
        self.clear_btn.clicked.connect(self.clear_list)
        self.open_btn = QPushButton("Открыть папку")
        self.open_btn.clicked.connect(self.open_folder)
        self.go = QPushButton("Сжать")
        self.go.setObjectName("primary")
        self.go.setCursor(Qt.PointingHandCursor)
        self.go.clicked.connect(self.toggle)
        bottom.addWidget(self.clear_btn)
        bottom.addWidget(self.open_btn)
        bottom.addStretch(1)
        bottom.addWidget(self.go)
        root.addLayout(bottom)

        self.setAcceptDrops(True)

    # --- перетаскивание на всё окно ---
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.add_files(paths)

    # --- список ---
    def add_files(self, paths):
        found = []
        for p in paths:
            pp = Path(p)
            if pp.is_dir():
                found += [str(f) for f in sorted(pp.iterdir())
                          if f.is_file() and f.suffix.lower() in VIDEO_EXT]
            elif pp.is_file():
                found.append(str(pp))
        existing = {self.list.itemWidget(self.list.item(i)).path
                    for i in range(self.list.count())}
        for f in found:
            if f in existing:
                continue
            row = Row(f)
            row.removed.connect(self.remove_row)
            item = QListWidgetItem()
            item.setSizeHint(row.sizeHint())
            self.list.addItem(item)
            self.list.setItemWidget(item, row)

    def rows(self):
        return [self.list.itemWidget(self.list.item(i)) for i in range(self.list.count())]

    def remove_row(self, row):
        for i in range(self.list.count()):
            if self.list.itemWidget(self.list.item(i)) is row:
                self.list.takeItem(i)
                break

    def clear_list(self):
        if self.proc:
            return
        self.list.clear()

    # --- папка ---
    def _update_folder_label(self):
        self.folder_label.setText(
            "Сохраняю: рядом с оригиналом" if not self.out_dir else f"Сохраняю в: {self.out_dir}")

    def choose_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Куда сохранять", str(Path.home()))
        if d:
            self.out_dir = d
            self._update_folder_label()

    def reset_folder(self):
        self.out_dir = None
        self._update_folder_label()

    def open_folder(self):
        target = self.out_dir
        if not target:
            done = [r for r in self.rows() if r.out_path and r.status == "done"]
            target = str(Path(done[-1].out_path).parent) if done else str(Path.home())
        QDesktopServices.openUrl(QUrl.fromLocalFile(target))

    # --- сжатие ---
    def toggle(self):
        if self.proc:
            self.cancelled = True
            self.proc.kill()
            return
        pending = [r for r in self.rows() if r.status in ("wait", "error", "cancel")]
        if not pending:
            QMessageBox.information(self, "Пусто", "Добавь видео, потом жми «Сжать».")
            return
        for r in pending:
            r.set_status("wait", f"{human(r.src_size)} · ждёт очереди")
            r.bar.setValue(0)
        self.cancelled = False
        self.go.setText("Остановить")
        self.go.setObjectName("danger")
        self.go.setStyleSheet("")  # переприменить стиль
        self.style().unpolish(self.go)
        self.style().polish(self.go)
        self.preset.setEnabled(False)
        self.codec.setEnabled(False)
        self.next_job()

    def next_job(self):
        if self.cancelled:
            return self.finish_all()
        row = next((r for r in self.rows() if r.status == "wait"), None)
        if not row:
            return self.finish_all()

        _, _, crf, height, abr = PRESETS[self.preset.currentIndex()]
        _, vcodec, crf_bonus, _ = CODECS[self.codec.currentIndex()]
        row.duration = get_duration(row.path)
        folder = self.out_dir or str(Path(row.path).parent)
        row.out_path = unique_path(folder, Path(row.path).stem)

        args = ["-hide_banner", "-nostdin", "-i", row.path,
                "-map", "0:v:0", "-map", "0:a:0?",
                "-c:v", vcodec, "-crf", str(crf + crf_bonus), "-preset", "medium",
                "-pix_fmt", "yuv420p"]
        if vcodec == "libx265":
            args += ["-tag:v", "hvc1"]
        if height:
            args += ["-vf", f"scale=-2:'min({height},ih)'"]
        args += ["-c:a", "aac", "-b:a", abr, "-movflags", "+faststart",
                 "-progress", "pipe:1", "-nostats", row.out_path]

        self.current = row
        self.output_buf = []
        row.set_status("run", "сжимаю…")
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self.on_output)
        self.proc.finished.connect(self.on_finished)
        self.proc.start("ffmpeg", args)

    def on_output(self):
        data = bytes(self.proc.readAllStandardOutput()).decode(errors="ignore")
        for line in data.splitlines():
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    sec = int(line.split("=")[1]) / 1_000_000
                except ValueError:
                    continue
                row = self.current
                if row and row.duration > 0:
                    frac = min(sec / row.duration, 1.0)
                    row.bar.setValue(int(frac * 1000))
                    row.info.setText(f"{human(row.src_size)} · {frac * 100:.0f}%")
            elif "=" not in line[:20]:
                self.output_buf.append(line)
                self.output_buf = self.output_buf[-15:]

    def on_finished(self, code, _status):
        row = self.current
        self.proc.deleteLater()
        self.proc = None
        if self.cancelled:
            if row.out_path and os.path.exists(row.out_path):
                os.remove(row.out_path)
            row.set_status("cancel", "остановлено")
            row.bar.setValue(0)
        elif code == 0 and row.out_path and os.path.exists(row.out_path):
            new = os.path.getsize(row.out_path)
            diff = (1 - new / row.src_size) * 100 if row.src_size else 0
            note = f"{human(row.src_size)} → {human(new)}  (−{diff:.0f}%)" if diff > 0 \
                else f"{human(row.src_size)} → {human(new)}  (стало больше, лучше оставь оригинал)"
            row.set_status("done", "✓ " + note)
            row.bar.setValue(1000)
        else:
            if row.out_path and os.path.exists(row.out_path):
                os.remove(row.out_path)
            row.set_status("error", "ошибка (наведи мышкой)")
            row.info.setToolTip("\n".join(self.output_buf))
            row.bar.setValue(0)
        self.next_job()

    def finish_all(self):
        self.proc = None
        self.current = None
        self.go.setText("Сжать")
        self.go.setObjectName("primary")
        self.style().unpolish(self.go)
        self.style().polish(self.go)
        self.preset.setEnabled(True)
        self.codec.setEnabled(True)
        if not self.cancelled and shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "Re Shrink", "Всё готово"])

    def closeEvent(self, e):
        if self.proc:
            self.cancelled = True
            self.proc.kill()
            self.proc.waitForFinished(2000)
        e.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    install_launcher()
    app.setWindowIcon(QIcon(str(ICON_PATH)))
    app.setDesktopFileName("szhimatel")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        QMessageBox.critical(None, "Нет ffmpeg",
                             "Поставь его командой:\n\nsudo apt install ffmpeg")
        return 1
    w = Main()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
