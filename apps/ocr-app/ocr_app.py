#!/usr/bin/env python3
"""Re Scan: локальное распознавание (Tesseract), русский + английский.

Что умеет:
- перетащить картинки мышкой в окно
- Ctrl+V: вставить картинку (скриншот) из буфера обмена
- кнопка "Открыть файлы"
- история сканирований слева (хранится в ~/.local/share/ocr-app/history.db)
"""
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

LANGS = "rus+eng"
DATA_DIR = Path.home() / ".local/share/ocr-app"
DB_PATH = DATA_DIR / "history.db"
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")
HINT = (
    "Кинь сюда картинки мышкой,\n"
    "нажми Ctrl+V, если картинка в буфере обмена,\n"
    "или нажми «Открыть файлы».\n\n"
    "Слева будет история сканирований."
)

CSS = b"""
window.ocr { background-color: #14151c; }
.ocr label { color: #e8e9f0; }
.ocr .muted { color: #8b8fa3; }
.ocr .section { color: #8b8fa3; font-weight: 700; font-size: 12px; }

.ocr button {
    background-image: none; background-color: #262940; color: #e8e9f0;
    border: none; border-radius: 10px; padding: 9px 16px;
    box-shadow: none; text-shadow: none;
}
.ocr button:hover { background-color: #30344f; }
.ocr button:active { background-color: #3a3e5c; }
.ocr button.primary { background-color: #14b8a6; }
.ocr button.primary:hover { background-color: #2dd4bf; }
.ocr button.primary label { color: #06201d; font-weight: 700; }

.ocr scrolledwindow { border: 1px solid #2c2f42; border-radius: 10px; background-color: #1c1e28; }
.ocr scrolledwindow.dropping { border: 2px solid #14b8a6; }

.ocr textview, .ocr textview text { background-color: #1c1e28; color: #e8e9f0; font-size: 15px; }
.ocr textview text selection, .ocr textview selection { background-color: #14b8a6; color: #06201d; }

.ocr treeview.view { background-color: #1c1e28; color: #e8e9f0; }
.ocr treeview.view:hover { background-color: #262940; }
.ocr treeview.view:selected { background-color: #14b8a6; color: #06201d; }

.ocr paned > separator { background-color: transparent; background-image: none; min-width: 10px; }
.ocr scrollbar { background-color: transparent; }
.ocr scrollbar slider { background-color: #3a3e54; border-radius: 6px; min-width: 6px; min-height: 6px; }
.ocr scrollbar slider:hover { background-color: #4a4f6c; }
"""


def db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "CREATE TABLE IF NOT EXISTS scans ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, source TEXT, text TEXT)"
    )
    return con


def uris_to_paths(uris):
    paths = []
    for u in uris or []:
        p = urlparse(u)
        if p.scheme == "file":
            path = unquote(p.path)
            if path.lower().endswith(IMG_EXT):
                paths.append(path)
    return paths


def btn(label, cb, primary=False):
    b = Gtk.Button(label=label)
    b.connect("clicked", cb)
    if primary:
        b.get_style_context().add_class("primary")
    return b


class App(Gtk.Window):
    def __init__(self):
        super().__init__(title="Re Scan")
        self.set_default_size(1000, 640)
        self.set_icon_name("ocr-app")
        self.busy = False
        self.current_text = ""

        # тёмная тема для всего приложения и наш CSS
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self.get_style_context().add_class("ocr")

        accel = Gtk.AccelGroup()
        self.add_accel_group(accel)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(root)

        # --- верхняя панель кнопок
        bar = Gtk.Box(spacing=8)
        bar.set_margin_start(16)
        bar.set_margin_end(16)
        bar.set_margin_top(16)
        bar.set_margin_bottom(10)
        root.pack_start(bar, False, False, 0)

        b_open = btn("Открыть файлы…", self.on_open, primary=True)
        bar.pack_start(b_open, False, False, 0)

        b_paste = btn("Вставить из буфера (Ctrl+V)", self.on_paste)
        b_paste.add_accelerator(
            "clicked", accel, Gdk.KEY_v, Gdk.ModifierType.CONTROL_MASK, Gtk.AccelFlags.VISIBLE
        )
        bar.pack_start(b_paste, False, False, 0)

        bar.pack_end(btn("Скопировать текст", self.on_copy), False, False, 0)
        bar.pack_end(btn("Сохранить в .txt", self.on_save), False, False, 0)

        # --- основная часть: история слева, текст справа
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(290)
        root.pack_start(paned, True, True, 0)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left.set_margin_start(16)
        left.set_margin_bottom(8)
        paned.pack1(left, False, False)

        lbl = Gtk.Label(label="ИСТОРИЯ", xalign=0)
        lbl.get_style_context().add_class("section")
        left.pack_start(lbl, False, False, 0)

        self.store = Gtk.ListStore(int, str)
        self.tree = Gtk.TreeView(model=self.store)
        self.tree.set_headers_visible(False)
        col = Gtk.TreeViewColumn(
            "", Gtk.CellRendererText(ellipsize=3, xpad=10, ypad=8), text=1
        )
        self.tree.append_column(col)
        self.tree.get_selection().connect("changed", self.on_select)
        sw = Gtk.ScrolledWindow()
        sw.set_shadow_type(Gtk.ShadowType.NONE)
        sw.add(self.tree)
        left.pack_start(sw, True, True, 0)

        hb = Gtk.Box(spacing=8)
        hb.pack_start(btn("Удалить", self.on_delete), True, True, 0)
        hb.pack_start(btn("Очистить всё", self.on_clear), True, True, 0)
        left.pack_start(hb, False, False, 0)

        self.view = Gtk.TextView()
        self.view.set_editable(False)
        self.view.set_wrap_mode(Gtk.WrapMode.WORD)
        self.view.set_left_margin(14)
        self.view.set_right_margin(14)
        buf = self.view.get_buffer()
        buf.create_tag(
            "hint",
            foreground="#8b8fa3",
            justification=Gtk.Justification.CENTER,
            size_points=15,
        )
        self.sw2 = Gtk.ScrolledWindow()
        self.sw2.set_shadow_type(Gtk.ShadowType.NONE)
        self.sw2.set_margin_start(8)
        self.sw2.set_margin_end(16)
        self.sw2.set_margin_top(26)
        self.sw2.set_margin_bottom(8)
        self.sw2.add(self.view)
        paned.pack2(self.sw2, True, False)

        self.status = Gtk.Label(label="Готов", xalign=0)
        self.status.get_style_context().add_class("muted")
        self.status.set_margin_start(16)
        self.status.set_margin_bottom(12)
        root.pack_start(self.status, False, False, 0)

        # --- драг-н-дроп: принимаем файлы в любое место окна
        for w in (self, self.view, self.tree):
            self.setup_dnd(w)

        self.load_history()
        self.show_text("")
        self.connect("destroy", Gtk.main_quit)

    # ---------- история
    def load_history(self):
        self.store.clear()
        with db() as con:
            rows = con.execute(
                "SELECT id, created, source FROM scans ORDER BY id DESC"
            ).fetchall()
        for sid, created, source in rows:
            self.store.append([sid, f"{created}  {source}"])

    def selected_id(self):
        model, it = self.tree.get_selection().get_selected()
        return model[it][0] if it else None

    def on_select(self, _sel):
        sid = self.selected_id()
        if sid is None:
            self.show_text("")
            return
        with db() as con:
            row = con.execute("SELECT text FROM scans WHERE id=?", (sid,)).fetchone()
        self.show_text(row[0] if row else "", from_scan=True)

    def show_text(self, text, from_scan=False):
        self.current_text = text if from_scan else ""
        buf = self.view.get_buffer()
        if from_scan:
            self.view.set_top_margin(12)
            buf.set_text(text if text else "(текст не найден)")
        else:
            # подсказка по центру и приглушённым цветом
            self.view.set_top_margin(120)
            buf.set_text(HINT)
            buf.apply_tag_by_name("hint", buf.get_start_iter(), buf.get_end_iter())

    def on_delete(self, _b):
        sid = self.selected_id()
        if sid is None:
            return
        with db() as con:
            con.execute("DELETE FROM scans WHERE id=?", (sid,))
        self.load_history()
        self.show_text("")

    def on_clear(self, _b):
        dlg = Gtk.MessageDialog(
            transient_for=self,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text="Удалить всю историю?",
        )
        ok = dlg.run() == Gtk.ResponseType.YES
        dlg.destroy()
        if ok:
            with db() as con:
                con.execute("DELETE FROM scans")
            self.load_history()
            self.show_text("")

    # ---------- ввод картинок
    def setup_dnd(self, w):
        # Вручную принимаем drag-n-drop, чтобы текстовое поле и список
        # не перехватывали перетаскивание у окна
        w.drag_dest_set(
            0,
            [Gtk.TargetEntry.new("text/uri-list", 0, 0)],
            Gdk.DragAction.COPY,
        )
        w.connect("drag-motion", self.on_drag_motion)
        w.connect("drag-leave", self.on_drag_leave)
        w.connect("drag-drop", self.on_drag_drop)
        w.connect("drag-data-received", self.on_drop)

    def on_drag_motion(self, _w, ctx, _x, _y, time_):
        self.sw2.get_style_context().add_class("dropping")
        Gdk.drag_status(ctx, Gdk.DragAction.COPY, time_)
        return True

    def on_drag_leave(self, _w, _ctx, _time):
        self.sw2.get_style_context().remove_class("dropping")

    def on_drag_drop(self, w, ctx, _x, _y, time_):
        w.drag_get_data(ctx, Gdk.Atom.intern("text/uri-list", False), time_)
        return True

    def on_drop(self, w, ctx, _x, _y, data, _info, time_):
        w.stop_emission_by_name("drag-data-received")
        self.sw2.get_style_context().remove_class("dropping")
        paths = uris_to_paths(data.get_uris())
        Gtk.drag_finish(ctx, True, False, time_)
        if paths:
            self.process([(p, Path(p).name, False) for p in paths])
        else:
            self.set_status("Это не картинка. Нужны png, jpg, webp, bmp, tiff")

    def on_paste(self, _b):
        cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        pix = cb.wait_for_image()
        if pix is not None:
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            tmp.close()
            pix.savev(tmp.name, "png", [], [])
            self.process([(tmp.name, "из буфера", True)])
            return
        paths = uris_to_paths(cb.wait_for_uris())
        if paths:
            self.process([(p, Path(p).name, False) for p in paths])
        else:
            self.set_status("В буфере обмена нет картинки")

    def on_open(self, _b):
        dlg = Gtk.FileChooserDialog(
            title="Выбери картинки",
            parent=self,
            action=Gtk.FileChooserAction.OPEN,
        )
        dlg.add_buttons("Отмена", Gtk.ResponseType.CANCEL, "Открыть", Gtk.ResponseType.OK)
        dlg.set_select_multiple(True)
        flt = Gtk.FileFilter()
        flt.set_name("Картинки")
        for ext in IMG_EXT:
            flt.add_pattern(f"*{ext}")
            flt.add_pattern(f"*{ext.upper()}")
        dlg.add_filter(flt)
        if dlg.run() == Gtk.ResponseType.OK:
            files = dlg.get_filenames()
            dlg.destroy()
            self.process([(p, Path(p).name, False) for p in files])
        else:
            dlg.destroy()

    # ---------- распознавание
    def set_status(self, text):
        self.status.set_text(text)
        return False

    def process(self, items):
        if self.busy:
            self.set_status("Подожди, ещё распознаю предыдущее")
            return
        self.busy = True
        threading.Thread(target=self.worker, args=(items,), daemon=True).start()

    def worker(self, items):
        total = len(items)
        for i, (path, label, is_tmp) in enumerate(items, 1):
            GLib.idle_add(self.set_status, f"Распознаю {i}/{total}: {label}…")
            text, error = "", ""
            try:
                r = subprocess.run(
                    ["tesseract", path, "stdout", "-l", LANGS],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                )
                text = r.stdout.strip()
                if r.returncode != 0:
                    error = r.stderr.strip() or "tesseract вернул ошибку"
            except FileNotFoundError:
                error = "Tesseract не установлен (sudo apt install tesseract-ocr)"
            finally:
                if is_tmp:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
            if error:
                GLib.idle_add(self.set_status, f"Ошибка: {error}")
                continue
            created = time.strftime("%d.%m %H:%M")
            with db() as con:
                cur = con.execute(
                    "INSERT INTO scans (created, source, text) VALUES (?,?,?)",
                    (created, label, text),
                )
                sid = cur.lastrowid
            GLib.idle_add(self.on_done, sid, f"{created}  {label}", total, i, bool(text))
        GLib.idle_add(self.finish)

    def on_done(self, sid, label, total, i, has_text):
        it = self.store.prepend([sid, label])
        self.tree.get_selection().select_iter(it)
        self.tree.scroll_to_cell(self.store.get_path(it))
        if not has_text:
            self.set_status("Текст не найден на картинке")
        return False

    def finish(self):
        self.busy = False
        if self.status.get_text().startswith("Распознаю"):
            self.set_status("Готово")
        return False

    # ---------- копирование / сохранение
    def on_copy(self, _b):
        if not self.current_text:
            self.set_status("Нечего копировать")
            return
        cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        cb.set_text(self.current_text, -1)
        cb.store()
        self.set_status("Текст скопирован")

    def on_save(self, _b):
        if not self.current_text:
            self.set_status("Нечего сохранять")
            return
        dlg = Gtk.FileChooserDialog(
            title="Сохранить текст",
            parent=self,
            action=Gtk.FileChooserAction.SAVE,
        )
        dlg.add_buttons("Отмена", Gtk.ResponseType.CANCEL, "Сохранить", Gtk.ResponseType.OK)
        dlg.set_do_overwrite_confirmation(True)
        dlg.set_current_name("текст.txt")
        if dlg.run() == Gtk.ResponseType.OK:
            Path(dlg.get_filename()).write_text(self.current_text, encoding="utf-8")
            self.set_status("Сохранено")
        dlg.destroy()


if __name__ == "__main__":
    GLib.set_prgname("ocr-app")
    win = App()
    win.show_all()
    Gtk.main()
