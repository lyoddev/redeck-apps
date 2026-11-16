#!/usr/bin/env python3
"""
Re QR — достаёт содержимое из битых, мутных, мелких, перекошенных
QR-кодов и рисует из него новый, чистый QR.

Всё работает локально, без интернета.

Установка (Pop!_OS):
    sudo apt install python3-venv
    python3 -m venv ~/qr-restore/venv
    ~/qr-restore/venv/bin/pip install PySide6 opencv-python-headless zxing-cpp "qrcode[pil]" numpy
    ~/qr-restore/venv/bin/python qr_restore.py [картинка.png]
"""
import io
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import qrcode
import qrcode.image.svg
import zxingcpp
from qrcode.constants import (ERROR_CORRECT_H, ERROR_CORRECT_L,
                              ERROR_CORRECT_M, ERROR_CORRECT_Q)
from PySide6.QtCore import QObject, QPoint, QRect, QSize, Qt, QThread, Signal
from PySide6.QtGui import (QColor, QGuiApplication, QIcon, QImage, QKeySequence,
                           QPainter, QPen, QPixmap, QShortcut)
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QFrame, QHBoxLayout,
                               QLabel, QMessageBox, QPlainTextEdit, QPushButton,
                               QVBoxLayout, QWidget)

# ───────────────────────────── ядро: восстановление ─────────────────────────────

QR_FORMATS = zxingcpp.BarcodeFormat.QRCode | zxingcpp.BarcodeFormat.MicroQRCode


@dataclass
class Found:
    text: str
    data: bytes
    method: str


def _zxing(img, binarizer=None):
    kw = dict(formats=QR_FORMATS, try_rotate=True, try_downscale=True, try_invert=True)
    if binarizer is not None:
        kw["binarizer"] = binarizer
    try:
        return zxingcpp.read_barcodes(img, **kw)
    except Exception:
        return []


def _opencv(img):
    """Запасной декодер от OpenCV: иногда ловит то, что zxing не смог."""
    out = []
    dets = [cv2.QRCodeDetector()]
    if hasattr(cv2, "QRCodeDetectorAruco"):
        dets.append(cv2.QRCodeDetectorAruco())
    for d in dets:
        try:
            txt, _pts, _ = d.detectAndDecode(img)
            if txt:
                out.append(txt)
        except Exception:
            pass
    return out


def _gray(img):
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img


def _pad(g, frac=0.12):
    """Тихая зона: белая рамка вокруг кода. Многие обрезанные коды без неё не читаются."""
    p = max(20, int(max(g.shape[:2]) * frac))
    return cv2.copyMakeBorder(g, p, p, p, p, cv2.BORDER_CONSTANT, value=255)


def _fit(g, target=900):
    """Приводим к удобному размеру: мелкое увеличиваем, огромное уменьшаем."""
    h, w = g.shape[:2]
    m = max(h, w)
    if m == 0:
        return g
    s = target / m
    if 0.8 < s < 1.25:
        return g
    interp = cv2.INTER_CUBIC if s > 1 else cv2.INTER_AREA
    return cv2.resize(g, None, fx=s, fy=s, interpolation=interp)


def _order_pts(pts):
    pts = pts.reshape(4, 2).astype(np.float32)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)],
                     pts[np.argmax(s)], pts[np.argmax(d)]], dtype=np.float32)


def _warp(g, quad, size=700):
    dst = np.array([[0, 0], [size - 1, 0], [size - 1, size - 1], [0, size - 1]], np.float32)
    M = cv2.getPerspectiveTransform(_order_pts(quad), dst)
    return cv2.warpPerspective(g, M, (size, size), flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_REPLICATE)


def _quads(g):
    """Ищем четырёхугольник, в котором сидит код (для выпрямления перекоса)."""
    quads = []
    try:
        ok, pts = cv2.QRCodeDetector().detect(g)
        if ok and pts is not None:
            quads.append(pts.reshape(-1, 2)[:4])
    except Exception:
        pass
    try:
        small = _fit(g, 800)
        k = small.shape[1] / g.shape[1]
        blur = cv2.GaussianBlur(small, (5, 5), 0)
        _, th = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
        cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cnts = sorted(cnts, key=cv2.contourArea, reverse=True)[:3]
        for c in cnts:
            if cv2.contourArea(c) < 0.02 * small.shape[0] * small.shape[1]:
                continue
            peri = cv2.arcLength(c, True)
            ap = cv2.approxPolyDP(c, 0.03 * peri, True)
            if len(ap) == 4:
                quads.append(ap.reshape(4, 2) / k)
            else:
                box = cv2.boxPoints(cv2.minAreaRect(c))
                quads.append(box / k)
    except Exception:
        pass
    return quads


def variants(img):
    """Генератор картинок: от простых к хитрым. Останавливаемся на первой удачной."""
    g0 = _gray(img)
    yield "как есть", img
    yield "серый + белая рамка", _pad(g0)

    g = _fit(g0, 900)
    gp = _pad(g)
    yield "подогнан размер", gp

    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    yield "контраст", _pad(clahe.apply(g))

    _, otsu = cv2.threshold(cv2.GaussianBlur(g, (3, 3), 0), 0, 255,
                            cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    yield "чёрно-белое (Оцу)", _pad(otsu)

    for bs in (31, 51, 91):
        ad = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY, bs | 1, 8)
        yield f"адаптивный порог {bs}", _pad(ad)

    blur = cv2.GaussianBlur(g, (0, 0), 3)
    sharp = cv2.addWeighted(g, 2.2, blur, -1.2, 0)
    yield "резкость", _pad(sharp)
    _, sh_o = cv2.threshold(sharp, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    yield "резкость + ч/б", _pad(sh_o)

    med = cv2.medianBlur(g, 5)
    _, med_o = cv2.threshold(med, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    yield "убрали шум", _pad(med_o)

    k3 = np.ones((3, 3), np.uint8)
    yield "залатали дыры", _pad(cv2.morphologyEx(otsu, cv2.MORPH_OPEN, k3))
    yield "стёрли мусор", _pad(cv2.morphologyEx(otsu, cv2.MORPH_CLOSE, k3))
    yield "жирнее", _pad(cv2.erode(otsu, k3))
    yield "тоньше", _pad(cv2.dilate(otsu, k3))

    # мелкий размер -> потом крупно: сглаживает мутный шум
    for scale in (0.5, 0.33):
        sm = cv2.resize(g, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        big = cv2.resize(sm, None, fx=1 / scale, fy=1 / scale, interpolation=cv2.INTER_CUBIC)
        _, b = cv2.threshold(big, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        yield f"сгладили (x{scale})", _pad(b)

    # выпрямление перекоса
    for qi, q in enumerate(_quads(g0)):
        try:
            w = _warp(g0, q)
        except Exception:
            continue
        yield f"выпрямили #{qi + 1}", _pad(w)
        _, wo = cv2.threshold(cv2.GaussianBlur(w, (3, 3), 0), 0, 255,
                              cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        yield f"выпрямили #{qi + 1} + ч/б", _pad(wo)
        wa = cv2.adaptiveThreshold(w, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY, 41, 6)
        yield f"выпрямили #{qi + 1} + порог", _pad(wa)

    # последняя надежда: разные способы бинаризации у zxing
    yield "__binarizers__", gp


def restore(img, progress=None):
    """Возвращает (список Found, картинка-победитель или None)."""
    n = 0
    for name, v in variants(img):
        n += 1
        if progress:
            progress(f"Пробую: {name}")
        if name == "__binarizers__":
            for bn in ("GlobalHistogram", "FixedThreshold", "LocalAverage"):
                b = getattr(zxingcpp.Binarizer, bn, None)
                if b is None:
                    continue
                res = _zxing(v, b)
                if res:
                    return [Found(r.text, bytes(r.bytes), f"zxing / {bn}") for r in res], v
            continue
        res = _zxing(v)
        if res:
            return [Found(r.text, bytes(r.bytes), f"{name}") for r in res], v
        for t in _opencv(v):
            return [Found(t, t.encode("utf-8"), f"{name} (OpenCV)")], v
    return [], None


def make_qr(data, border=4):
    """Рисуем чистый QR. Берём максимальную защиту, а если данных много — снижаем."""
    last = None
    for ec in (ERROR_CORRECT_H, ERROR_CORRECT_Q, ERROR_CORRECT_M, ERROR_CORRECT_L):
        try:
            qr = qrcode.QRCode(error_correction=ec, box_size=12, border=border)
            qr.add_data(data)
            qr.make(fit=True)
            return qr
        except Exception as e:  # слишком много данных для этого уровня
            last = e
    raise last


def qr_to_pil(qr):
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")


def qr_to_svg_bytes(qr):
    img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue()


def load_image(path):
    data = np.fromfile(str(path), dtype=np.uint8)  # работает и с кириллицей в пути
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Не получилось открыть этот файл как картинку")
    return img


def qimage_to_bgr(qimg):
    qimg = qimg.convertToFormat(QImage.Format_RGB888)
    w, h, bpl = qimg.width(), qimg.height(), qimg.bytesPerLine()
    arr = np.frombuffer(qimg.constBits(), np.uint8).reshape(h, bpl)[:, : w * 3]
    return cv2.cvtColor(arr.reshape(h, w, 3).copy(), cv2.COLOR_RGB2BGR)


def bgr_to_pixmap(img):
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
    else:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    h, w = img.shape[:2]
    q = QImage(img.data, w, h, w * 3, QImage.Format_RGB888).copy()
    return QPixmap.fromImage(q)


def pil_to_pixmap(pil):
    buf = io.BytesIO()
    pil.save(buf, "PNG")
    pm = QPixmap()
    pm.loadFromData(buf.getvalue(), "PNG")
    return pm


# ───────────────────────────── поток, чтобы окно не вешалось ─────────────────────────────

class Worker(QObject):
    progress = Signal(str)
    done = Signal(object, object)

    def __init__(self, img):
        super().__init__()
        self.img = img

    def run(self):
        try:
            found, win = restore(self.img, self.progress.emit)
        except Exception:
            found, win = [], None
        self.done.emit(found, win)


# ───────────────────────────── окно ─────────────────────────────

ACCENT = "#6C8CFF"
STYLE = f"""
QWidget {{ background:#14161c; color:#e8eaf0; font-size:14px; }}
QLabel#title {{ font-size:20px; font-weight:700; }}
QLabel#hint {{ color:#8a91a3; }}
QFrame#card {{ background:#1c1f28; border:1px solid #2a2e3a; border-radius:14px; }}
QLabel#drop {{ background:#181b23; border:2px dashed #3a4050; border-radius:14px;
              color:#8a91a3; font-size:15px; }}
QPushButton {{ background:#262b38; border:none; border-radius:10px; padding:10px 14px; color:#e8eaf0; }}
QPushButton:hover {{ background:#30364a; }}
QPushButton:disabled {{ color:#5b6172; background:#1f232d; }}
QPushButton#main {{ background:{ACCENT}; color:#0d1020; font-weight:700; }}
QPushButton#main:hover {{ background:#8aa3ff; }}
QPlainTextEdit {{ background:#181b23; border:1px solid #2a2e3a; border-radius:10px; padding:8px; color:#e8eaf0; }}
"""



class CropCanvas(QWidget):
    """Картинка, на которой мышкой рисуется рамка обрезки."""

    def __init__(self, pm):
        super().__init__()
        self.pm = pm
        self.setMinimumSize(560, 420)
        self.setCursor(Qt.CrossCursor)
        self.a = None
        self.b = None
        self._scaled = None
        self._off = QPoint(0, 0)

    def _layout(self):
        sc = self.pm.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._scaled = sc
        self._off = QPoint((self.width() - sc.width()) // 2, (self.height() - sc.height()) // 2)

    def _clamp(self, p):
        x = min(max(p.x(), self._off.x()), self._off.x() + self._scaled.width() - 1)
        y = min(max(p.y(), self._off.y()), self._off.y() + self._scaled.height() - 1)
        return QPoint(x, y)

    def mousePressEvent(self, e):
        self._layout()
        self.a = self._clamp(e.position().toPoint())
        self.b = self.a
        self.update()

    def mouseMoveEvent(self, e):
        if self.a is not None:
            self.b = self._clamp(e.position().toPoint())
            self.update()

    def rect_img(self):
        """Рамка в координатах настоящей картинки или None."""
        if self.a is None or self.b is None or self._scaled is None:
            return None
        r = QRect(self.a, self.b).normalized()
        if r.width() < 8 or r.height() < 8:
            return None
        k = self.pm.width() / self._scaled.width()
        x = int((r.x() - self._off.x()) * k)
        y = int((r.y() - self._off.y()) * k)
        return x, y, int(r.width() * k), int(r.height() * k)

    def paintEvent(self, _e):
        self._layout()
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0f1116"))
        p.drawPixmap(self._off, self._scaled)
        if self.a is not None and self.b is not None:
            r = QRect(self.a, self.b).normalized()
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 140))
            full = QRect(self._off, self._scaled.size())
            for part in (QRect(full.left(), full.top(), full.width(), r.top() - full.top()),
                         QRect(full.left(), r.bottom() + 1, full.width(), full.bottom() - r.bottom()),
                         QRect(full.left(), r.top(), r.left() - full.left(), r.height()),
                         QRect(r.right() + 1, r.top(), full.right() - r.right(), r.height())):
                p.drawRect(part)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(ACCENT), 2))
            p.drawRect(r)


class CropDialog(QDialog):
    def __init__(self, img, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Обрезка")
        self.setStyleSheet(STYLE)
        self.img = img
        self.result_img = None
        lay = QVBoxLayout(self)
        hint = QLabel("Зажми мышку и обведи только сам QR-код. Лишнее вокруг мешает.")
        hint.setObjectName("hint")
        lay.addWidget(hint)
        self.canvas = CropCanvas(bgr_to_pixmap(img))
        lay.addWidget(self.canvas, 1)
        row = QHBoxLayout()
        b_cancel = QPushButton("Отмена")
        b_cancel.clicked.connect(self.reject)
        b_ok = QPushButton("Обрезать")
        b_ok.setObjectName("main")
        b_ok.clicked.connect(self.accept_crop)
        row.addStretch(1)
        row.addWidget(b_cancel)
        row.addWidget(b_ok)
        lay.addLayout(row)
        self.resize(900, 680)

    def accept_crop(self):
        r = self.canvas.rect_img()
        if r is None:
            QMessageBox.information(self, "Обрезка", "Сначала обведи область мышкой")
            return
        x, y, w, h = r
        H, W = self.img.shape[:2]
        x, y = max(0, x), max(0, y)
        w, h = min(w, W - x), min(h, H - y)
        self.result_img = self.img[y:y + h, x:x + w].copy()
        self.accept()


class ImageBox(QLabel):
    """Область под картинку: принимает файлы перетаскиванием."""
    dropped = Signal(str)

    def __init__(self, placeholder, droppable=False):
        super().__init__(placeholder)
        self.placeholder = placeholder
        self.setObjectName("drop")
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(320, 320)
        self.setAcceptDrops(droppable)
        self._pm = None

    def set_pixmap(self, pm):
        self._pm = pm
        self._rescale()

    def clear_pixmap(self):
        self._pm = None
        self.setPixmap(QPixmap())
        self.setText(self.placeholder)

    def _rescale(self):
        if self._pm:
            self.setPixmap(self._pm.scaled(self.size() - QSize(12, 12),
                                           Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._rescale()

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasImage():
            e.acceptProposedAction()

    def dropEvent(self, e):
        md = e.mimeData()
        if md.hasUrls():
            p = md.urls()[0].toLocalFile()
            if p:
                self.dropped.emit(p)
                return
        if md.hasImage():
            self.window().load_qimage(QImage(md.imageData()))


class Main(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Re QR")
        self.resize(1000, 640)
        self.setStyleSheet(STYLE)
        self.setAcceptDrops(True)
        self.current_img = None
        self.original_img = None
        self.qr = None
        self.thread = None
        self.worker = None

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        t = QLabel("Re QR")
        t.setObjectName("title")
        h = QLabel("Кинь картинку в окно, вставь через Ctrl+V или выбери файл. "
                   "Программа вытащит содержимое и нарисует чистый код.")
        h.setObjectName("hint")
        h.setWordWrap(True)
        root.addWidget(t)
        root.addWidget(h)

        row = QHBoxLayout()
        row.setSpacing(14)
        root.addLayout(row, 1)

        # левая колонка: исходник
        left = QVBoxLayout()
        self.src_box = ImageBox("Сюда перетащи картинку\nс битым QR", droppable=True)
        self.src_box.dropped.connect(self.load_path)
        left.addWidget(self.src_box, 1)
        lb = QHBoxLayout()
        b_open = QPushButton("Выбрать файл…")
        b_open.clicked.connect(self.pick_file)
        b_paste = QPushButton("Вставить (Ctrl+V)")
        b_paste.clicked.connect(self.paste)
        lb.addWidget(b_open)
        lb.addWidget(b_paste)
        left.addLayout(lb)
        cb = QHBoxLayout()
        self.b_crop = QPushButton("Обрезать вручную…")
        self.b_crop.clicked.connect(self.crop_manual)
        self.b_orig = QPushButton("Вернуть оригинал")
        self.b_orig.clicked.connect(self.restore_original)
        cb.addWidget(self.b_crop)
        cb.addWidget(self.b_orig)
        left.addLayout(cb)
        self.b_go = QPushButton("Восстановить")
        self.b_go.setObjectName("main")
        self.b_go.setEnabled(False)
        self.b_go.clicked.connect(self.start)
        left.addWidget(self.b_go)
        row.addLayout(left, 1)

        # правая колонка: результат
        card = QFrame()
        card.setObjectName("card")
        right = QVBoxLayout(card)
        right.setContentsMargins(14, 14, 14, 14)
        self.status = QLabel("Жду картинку")
        self.status.setObjectName("hint")
        self.status.setWordWrap(True)
        right.addWidget(self.status)
        self.new_box = ImageBox("Здесь будет\nновый чистый QR")
        right.addWidget(self.new_box, 1)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFixedHeight(90)
        self.text.setPlaceholderText("Тут появится текст из кода")
        right.addWidget(self.text)

        r1 = QHBoxLayout()
        self.b_copy_text = QPushButton("Копировать текст")
        self.b_copy_text.clicked.connect(self.copy_text)
        self.b_open_url = QPushButton("Открыть ссылку")
        self.b_open_url.clicked.connect(self.open_url)
        r1.addWidget(self.b_copy_text)
        r1.addWidget(self.b_open_url)
        right.addLayout(r1)
        r2 = QHBoxLayout()
        self.b_copy_img = QPushButton("Копировать картинку")
        self.b_copy_img.clicked.connect(self.copy_image)
        self.b_save = QPushButton("Сохранить PNG / SVG…")
        self.b_save.clicked.connect(self.save)
        r2.addWidget(self.b_copy_img)
        r2.addWidget(self.b_save)
        right.addLayout(r2)
        row.addWidget(card, 1)

        self.b_crop.setEnabled(False)
        self.b_orig.setEnabled(False)
        self._result_buttons = [self.b_copy_text, self.b_open_url, self.b_copy_img, self.b_save]
        self._reset_result()

        QShortcut(QKeySequence.Paste, self, activated=self.paste)

    # ── загрузка картинки
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasImage():
            e.acceptProposedAction()

    def dropEvent(self, e):
        md = e.mimeData()
        if md.hasUrls() and md.urls()[0].toLocalFile():
            self.load_path(md.urls()[0].toLocalFile())
        elif md.hasImage():
            self.load_qimage(QImage(md.imageData()))

    def pick_file(self):
        p, _ = QFileDialog.getOpenFileName(self, "Выбери картинку", str(Path.home()),
                                           "Картинки (*.png *.jpg *.jpeg *.bmp *.webp *.gif *.tif *.tiff)")
        if p:
            self.load_path(p)

    def paste(self):
        cb = QGuiApplication.clipboard()
        md = cb.mimeData()
        if md.hasImage():
            self.load_qimage(QImage(cb.image()))
        elif md.hasUrls() and md.urls()[0].toLocalFile():
            self.load_path(md.urls()[0].toLocalFile())
        else:
            self.status.setText("В буфере нет картинки")

    def load_path(self, path):
        try:
            self.set_image(load_image(path))
        except Exception as e:
            QMessageBox.warning(self, "Не открылось", str(e))

    def load_qimage(self, qimg):
        if qimg.isNull():
            self.status.setText("В буфере нет картинки")
            return
        self.set_image(qimage_to_bgr(qimg))

    def set_image(self, img, keep_original=False):
        if not keep_original:
            self.original_img = img
        self.current_img = img
        self.src_box.set_pixmap(bgr_to_pixmap(img))
        self._reset_result()
        self.b_go.setEnabled(True)
        self.b_crop.setEnabled(True)
        self.b_orig.setEnabled(img is not self.original_img)
        self.start()  # сразу пробуем, кнопку жать не обязательно

    def crop_manual(self):
        if self.current_img is None or self.thread is not None:
            return
        dlg = CropDialog(self.original_img, self)
        if dlg.exec() == QDialog.Accepted and dlg.result_img is not None:
            self.set_image(dlg.result_img, keep_original=True)

    def restore_original(self):
        if self.original_img is not None and self.thread is None:
            self.set_image(self.original_img, keep_original=True)

    # ── восстановление
    def start(self):
        if self.current_img is None or self.thread is not None:
            return
        self._reset_result()
        self.b_go.setEnabled(False)
        self.b_crop.setEnabled(False)
        self.b_orig.setEnabled(False)
        self.status.setText("Работаю…")
        self.thread = QThread()
        self.worker = Worker(self.current_img)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.status.setText)
        self.worker.done.connect(self.finished)
        self.thread.start()

    def finished(self, found, win):
        self.thread.quit()
        self.thread.wait()
        self.thread = None
        self.worker = None
        self.b_go.setEnabled(True)
        self.b_crop.setEnabled(True)
        self.b_orig.setEnabled(self.current_img is not self.original_img)
        if not found:
            self.status.setText(
                "Не вышло. Советы: возьми картинку получше или побольше; нажми «Обрезать вручную» и "
                "обведи только код; убедись, что на фото видны все три угловых квадрата.")
            return
        if win is not None:
            self.src_box.set_pixmap(bgr_to_pixmap(self.current_img))
        f = found[0]
        extra = f" (ещё кодов на картинке: {len(found) - 1})" if len(found) > 1 else ""
        self.status.setText(f"Получилось! Способ: {f.method}{extra}")
        self.text.setPlainText(f.text)
        try:
            self.qr = make_qr(f.data)
            self.new_box.set_pixmap(pil_to_pixmap(qr_to_pil(self.qr)))
        except Exception as e:
            self.qr = None
            self.status.setText(f"Текст достали, но новый QR не нарисовался: {e}")
        for b in self._result_buttons:
            b.setEnabled(True)
        self.b_copy_img.setEnabled(self.qr is not None)
        self.b_save.setEnabled(self.qr is not None)
        self.b_open_url.setEnabled(f.text.lower().startswith(("http://", "https://")))

    def _reset_result(self):
        self.qr = None
        self.text.clear()
        self.new_box.clear_pixmap()
        for b in self._result_buttons:
            b.setEnabled(False)

    # ── кнопки результата
    def copy_text(self):
        QGuiApplication.clipboard().setText(self.text.toPlainText())
        self.status.setText("Текст скопирован")

    def open_url(self):
        webbrowser.open(self.text.toPlainText().strip())

    def copy_image(self):
        if self.qr is None:
            return
        pm = pil_to_pixmap(qr_to_pil(self.qr))
        QGuiApplication.clipboard().setPixmap(pm)
        self.status.setText("Картинка скопирована, можно вставлять")

    def save(self):
        if self.qr is None:
            return
        p, flt = QFileDialog.getSaveFileName(self, "Сохранить QR", str(Path.home() / "qr.png"),
                                             "PNG (*.png);;SVG (*.svg)")
        if not p:
            return
        try:
            if p.lower().endswith(".svg") or "SVG" in flt:
                if not p.lower().endswith(".svg"):
                    p += ".svg"
                Path(p).write_bytes(qr_to_svg_bytes(self.qr))
            else:
                if not p.lower().endswith(".png"):
                    p += ".png"
                qr_to_pil(self.qr).save(p)
            self.status.setText(f"Сохранено: {p}")
        except Exception as e:
            QMessageBox.warning(self, "Не сохранилось", str(e))


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("qr-restore")
    app.setDesktopFileName("qr-restore")
    icon = Path(__file__).with_suffix(".svg")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    w = Main()
    w.show()
    if len(sys.argv) > 1 and Path(sys.argv[1]).exists():
        w.load_path(sys.argv[1])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
