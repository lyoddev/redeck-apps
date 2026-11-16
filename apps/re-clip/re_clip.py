#!/usr/bin/env python3
"""Re Clip: история буфера обмена с поиском (GTK3, X11).

Запуск:
  re-clip               показать окно с историей
  re-clip --background  тихо работать в фоне и записывать историю
  re-clip --quit        остановить фоновую работу

История хранится в ~/.local/share/re-clip/history.json (только для тебя).
Копии из менеджеров паролей, которые помечают свои данные, не записываются.
"""
import json
import os
import sys
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

try:  # значок в трее: нужен пакет gir1.2-ayatanaappindicator3-0.1
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402
except (ValueError, ImportError):
    AppIndicator = None

APP_ID = "io.redeck.ReClip"
DATA_DIR = Path.home() / ".local" / "share" / "re-clip"
HISTORY = DATA_DIR / "history.json"
MAX_ITEMS = 300        # сколько записей хранить (закреплённые не считаются)
MAX_LEN = 50000        # слишком длинные тексты не записываем
SECRET_HINTS = {"x-kde-passwordmanagerhint"}

CSS = b"""
window.reclip { background: #0c0a14; }
window.reclip label { color: #e8e4f5; }
window.reclip label.dim { color: #8d84ad; font-size: 11px; }
window.reclip entry { background: #141024; color: #e8e4f5; border: 1px solid #2a2245;
                      border-radius: 4px; padding: 6px 8px; }
window.reclip entry:focus { border-color: #3ee8e0; }
window.reclip list, window.reclip scrolledwindow { background: transparent; }
window.reclip row { padding: 6px 8px; border-bottom: 1px dashed #2a2245; }
window.reclip row:selected { background: #1f1836; }
window.reclip row:selected label { color: #f5e642; }
window.reclip button { background: #141024; color: #e8e4f5; border: 1px solid #2a2245;
                       border-radius: 4px; padding: 4px 10px; }
window.reclip button:hover { border-color: #ff3d8b; }
window.reclip button:checked { border-color: #ff3d8b; color: #ff3d8b; }
"""


class History:
    """Список записей: {"t": текст, "pin": закреплено, "ts": время}."""

    def __init__(self):
        self.items = []
        self.load()

    def load(self):
        try:
            data = json.loads(HISTORY.read_text(encoding="utf-8"))
            self.items = [i for i in data
                          if isinstance(i, dict) and isinstance(i.get("t"), str)]
        except Exception:
            self.items = []

    def save(self):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            os.chmod(DATA_DIR, 0o700)
            tmp = HISTORY.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.items, f, ensure_ascii=False)
            os.replace(tmp, HISTORY)
        except Exception as exc:
            print("Не удалось сохранить историю:", exc, file=sys.stderr)

    def add(self, text):
        for i, it in enumerate(self.items):
            if it["t"] == text:
                it["ts"] = time.time()
                self.items.insert(0, self.items.pop(i))
                self.save()
                return
        self.items.insert(0, {"t": text, "pin": False, "ts": time.time()})
        self.trim()
        self.save()

    def trim(self):
        unpinned = [i for i in self.items if not i.get("pin")]
        extra = len(unpinned) - MAX_ITEMS
        if extra > 0:
            drop = {id(x) for x in unpinned[-extra:]}
            self.items = [i for i in self.items if id(i) not in drop]

    def view(self):
        pinned = [i for i in self.items if i.get("pin")]
        rest = [i for i in self.items if not i.get("pin")]
        return pinned + rest

    def toggle_pin(self, item):
        item["pin"] = not item.get("pin", False)
        self.save()

    def remove(self, item):
        self.items = [i for i in self.items if i is not item]
        self.save()

    def clear_unpinned(self):
        self.items = [i for i in self.items if i.get("pin")]
        self.save()


class ClipWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Re Clip")
        self.app = app
        self.set_default_size(520, 580)
        self.set_position(Gtk.WindowPosition.MOUSE)
        self.set_keep_above(True)
        self.get_style_context().add_class("reclip")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(10)
        self.add(box)

        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Поиск по истории…")
        self.search.connect("search-changed", self.on_search_changed)
        self.search.connect("activate", lambda *_: self.copy_selected())
        self.search.connect("key-press-event", self.on_search_key)
        box.pack_start(self.search, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.set_activate_on_single_click(False)  # копируем по Enter / двойному клику
        self.listbox.set_filter_func(self.filter_row)
        self.listbox.connect("row-activated", lambda _lb, row: self.copy_row(row))
        scroller.add(self.listbox)
        box.pack_start(scroller, True, True, 0)

        hint = Gtk.Label(label="Enter или двойной клик: копировать · Ctrl+P: закрепить · "
                               "Ctrl+D: удалить · Esc: закрыть")
        hint.get_style_context().add_class("dim")
        hint.set_xalign(0)
        box.pack_start(hint, False, False, 0)

        bar = Gtk.Box(spacing=8)
        self.pause_btn = Gtk.ToggleButton(label="Пауза записи")
        self.pause_btn.connect("toggled", self.on_pause)
        clear_btn = Gtk.Button(label="Очистить")
        clear_btn.connect("clicked", self.on_clear)
        self.count = Gtk.Label(label="")
        self.count.get_style_context().add_class("dim")
        bar.pack_start(self.pause_btn, False, False, 0)
        bar.pack_start(clear_btn, False, False, 0)
        bar.pack_end(self.count, False, False, 0)
        box.pack_start(bar, False, False, 0)

        self.connect("delete-event", lambda *_: (self.hide() or True))
        self.connect("key-press-event", self.on_key)

    # --- список ---
    def refresh(self):
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        for item in self.app.hist.view():
            self.listbox.add(self.make_row(item))
        self.listbox.show_all()
        self.listbox.invalidate_filter()
        self.select_first()
        n = len(self.app.hist.items)
        self.count.set_text(f"Записей: {n}")

    def make_row(self, item):
        row = Gtk.ListBoxRow()
        row.item = item
        text = item["t"].strip()
        shown = (text[:300] + "…") if len(text) > 300 else text
        prefix = "★ " if item.get("pin") else ""
        label = Gtk.Label(label=prefix + shown)
        label.set_xalign(0)
        label.set_line_wrap(True)
        label.set_lines(3)
        label.set_ellipsize(3)  # Pango.EllipsizeMode.END
        label.set_max_width_chars(60)
        label.set_tooltip_text(text[:1000])
        row.add(label)
        return row

    def filter_row(self, row):
        q = self.search.get_text().strip().lower()
        return (not q) or (q in row.item["t"].lower())

    def visible_rows(self):
        return [r for r in self.listbox.get_children() if r.get_child_visible()]

    def select_first(self):
        rows = self.visible_rows()
        if rows:
            self.listbox.select_row(rows[0])

    def selected_row(self):
        row = self.listbox.get_selected_row()
        if row is not None and row.get_child_visible():
            return row
        rows = self.visible_rows()
        return rows[0] if rows else None

    def move(self, delta):
        rows = self.visible_rows()
        if not rows:
            return
        cur = self.listbox.get_selected_row()
        idx = rows.index(cur) if cur in rows else -1
        idx = max(0, min(len(rows) - 1, idx + delta))
        self.listbox.select_row(rows[idx])
        rows[idx].grab_focus()
        self.search.grab_focus()

    # --- действия ---
    def copy_row(self, row):
        if row is None:
            return
        self.app.set_clipboard(row.item["t"])
        self.hide()

    def copy_selected(self):
        self.copy_row(self.selected_row())

    def pin_selected(self):
        row = self.selected_row()
        if row is not None:
            self.app.hist.toggle_pin(row.item)
            self.refresh()

    def delete_selected(self):
        row = self.selected_row()
        if row is not None:
            self.app.hist.remove(row.item)
            self.refresh()

    # --- события ---
    def on_search_changed(self, *_):
        self.listbox.invalidate_filter()
        self.select_first()

    def on_search_key(self, _widget, event):
        if event.keyval == Gdk.KEY_Down:
            self.move(1)
            return True
        if event.keyval == Gdk.KEY_Up:
            self.move(-1)
            return True
        return False

    def on_key(self, _widget, event):
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        if event.keyval == Gdk.KEY_Escape:
            self.hide()
            return True
        if ctrl and event.keyval in (Gdk.KEY_p, Gdk.KEY_P):
            self.pin_selected()
            return True
        if ctrl and event.keyval in (Gdk.KEY_d, Gdk.KEY_D):
            self.delete_selected()
            return True
        return False

    def on_pause(self, button):
        if not self.app.syncing:
            self.app.set_paused(button.get_active())

    def on_clear(self, _button):
        dlg = Gtk.MessageDialog(transient_for=self, modal=True,
                                message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.YES_NO,
                                text="Очистить историю?")
        dlg.format_secondary_text("Закреплённые записи останутся.")
        answer = dlg.run()
        dlg.destroy()
        if answer == Gtk.ResponseType.YES:
            self.app.hist.clear_unpinned()
            self.refresh()


class ReClip(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.hist = None
        self.win = None
        self.clip = None
        self.paused = False
        self.syncing = False
        self.pause_item = None
        self.indicator = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.hist = History()
        self.clip = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        self.clip.connect("owner-change", self.on_owner_change)
        self.setup_tray()
        self.hold()

    def setup_tray(self):
        if AppIndicator is None:
            return
        try:
            self.indicator = AppIndicator.Indicator.new(
                APP_ID, APP_ID, AppIndicator.IndicatorCategory.APPLICATION_STATUS)
            self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
            self.indicator.set_title("Re Clip")
            menu = Gtk.Menu()
            show = Gtk.MenuItem(label="Показать историю")
            show.connect("activate", lambda *_: self.show_window())
            self.pause_item = Gtk.CheckMenuItem(label="Пауза записи")
            self.pause_item.connect(
                "toggled", lambda it: None if self.syncing else self.set_paused(it.get_active()))
            quit_item = Gtk.MenuItem(label="Выход")
            quit_item.connect("activate", lambda *_: self.quit())
            for item in (show, self.pause_item, Gtk.SeparatorMenuItem(), quit_item):
                menu.append(item)
            menu.show_all()
            self.indicator.set_menu(menu)
            self.indicator.set_secondary_activate_target(show)
        except Exception as exc:
            print("Значок в трее недоступен:", exc, file=sys.stderr)
            self.indicator = None

    def set_paused(self, value):
        self.paused = bool(value)
        self.syncing = True
        try:
            if self.pause_item is not None:
                self.pause_item.set_active(self.paused)
            if self.win is not None:
                self.win.pause_btn.set_active(self.paused)
                self.win.pause_btn.set_label(
                    "Запись на паузе" if self.paused else "Пауза записи")
        finally:
            self.syncing = False

    def do_command_line(self, cmdline):
        args = cmdline.get_arguments()[1:]
        if "--quit" in args:
            self.quit()
            return 0
        if "--background" not in args:
            self.show_window()
        return 0

    def show_window(self):
        if self.win is None:
            self.win = ClipWindow(self)
        self.win.search.set_text("")
        self.win.refresh()
        self.win.show_all()
        self.win.present()
        self.win.search.grab_focus()

    def set_clipboard(self, text):
        self.clip.set_text(text, -1)

    def on_owner_change(self, clip, _event):
        if self.paused:
            return
        clip.request_targets(self.on_targets)

    def on_targets(self, clip, atoms, _data=None):
        names = {a.name().lower() for a in atoms} if atoms else set()
        if names & SECRET_HINTS:
            return
        clip.request_text(self.on_text)

    def on_text(self, _clip, text, _data=None):
        if not text or not text.strip() or len(text) > MAX_LEN:
            return
        self.hist.add(text)
        if self.win is not None and self.win.get_visible():
            self.win.refresh()


if __name__ == "__main__":
    GLib.set_prgname("re-clip")
    GLib.set_application_name("Re Clip")
    sys.exit(ReClip().run(sys.argv))
