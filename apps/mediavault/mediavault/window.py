"""window.py — главное окно: ввод ссылки, превью, выбор качества и список загрузок."""
from __future__ import annotations

import os
import re
import threading

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import downloader as dl
from . import updater
from .queue import DownloadQueue, QueueItem, State, TERMINAL, call_in_ui
from .settings import PreferencesWindow, choose_folder

CSS = """
.mv-hero      { font-size: 34px; font-weight: 800; letter-spacing: -0.8px; }
.mv-lead      { font-size: 15px; opacity: 0.6; }
.mv-section   { font-size: 20px; font-weight: 700; letter-spacing: -0.3px; }
.mv-caption   { font-size: 12px; font-weight: 600; opacity: 0.55; letter-spacing: 0.4px; }

.mv-card {
  background-color: @card_bg_color;
  border-radius: 20px;
  padding: 20px;
  box-shadow: 0 0 0 1px alpha(currentColor, 0.07), 0 8px 24px alpha(black, 0.10);
}

entry.url-entry {
  min-height: 48px; padding: 0 16px; border-radius: 14px; font-size: 16px;
}

button.pill-btn {
  border-radius: 999px; padding: 10px 26px; min-height: 24px; font-weight: 600;
  transition: all 180ms ease;
}
button.pill-btn:hover { box-shadow: 0 4px 14px alpha(@accent_bg_color, 0.45); }

button.quality-btn {
  border-radius: 12px; padding: 8px 16px; font-weight: 600;
  transition: all 150ms ease;
}
button.quality-btn:hover   { background-color: alpha(@accent_bg_color, 0.18); }
button.quality-btn:checked { background-color: @accent_bg_color; color: @accent_fg_color; }

.preview-thumb, .thumb {
  overflow: hidden; background-color: alpha(currentColor, 0.08);
}
.preview-thumb { border-radius: 14px; }
.thumb         { border-radius: 10px; }

.mv-banner { background-color: alpha(@warning_bg_color, 0.25); }

.duration-badge {
  background-color: alpha(black, 0.68); color: white;
  border-radius: 7px; padding: 2px 8px; font-size: 12px; font-weight: 700;
}

row.queue-row { padding: 6px 4px; transition: background-color 150ms ease; }
row.queue-row:hover { background-color: alpha(currentColor, 0.04); }

progressbar.thin trough,
progressbar.thin progress { min-height: 6px; border-radius: 3px; }
"""

_css_loaded = False


def load_css() -> None:
    global _css_loaded
    if _css_loaded:
        return
    provider = Gtk.CssProvider()
    if hasattr(provider, 'load_from_string'):   # GTK ≥ 4.12
        provider.load_from_string(CSS)
    else:
        provider.load_from_data(CSS.encode())
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _css_loaded = True


def cover_picture(**kwargs) -> Gtk.Picture:
    """Картинка, заполняющая рамку. На GTK < 4.8 режима COVER нет — работает и без него."""
    pic = Gtk.Picture(can_shrink=True, **kwargs)
    if hasattr(Gtk, 'ContentFit'):
        pic.set_content_fit(Gtk.ContentFit.COVER)
    return pic


class InfoBanner(Gtk.Revealer):
    """Простой баннер с кнопкой (Adw.Banner появился только в libadwaita 1.3)."""

    def __init__(self, text: str, button_label: str, on_click) -> None:
        super().__init__(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        box = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8, margin_start=14, margin_end=14)
        box.add_css_class('mv-banner')
        label = Gtk.Label(label=text, xalign=0, hexpand=True, wrap=True)
        button = Gtk.Button(label=button_label, valign=Gtk.Align.CENTER)
        button.connect('clicked', lambda *_: on_click())
        box.append(label)
        box.append(button)
        self.set_child(box)

    def set_revealed(self, revealed: bool) -> None:
        self.set_reveal_child(revealed)


def make_texture(data: bytes):
    """Байты картинки → Gdk.Texture (или None, если формат не поддерживается)."""
    try:
        return Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
    except GLib.Error:
        return None


def open_folder(path: str) -> bool:
    try:
        Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(path).get_uri(), None)
        return True
    except GLib.Error:
        return False


# ==========================================================================
# Строка очереди
# ==========================================================================

class QueueRow(Gtk.ListBoxRow):
    def __init__(self, item: QueueItem, queue: DownloadQueue, on_open_folder) -> None:
        super().__init__()
        self.item = item
        self._queue = queue
        self._on_open_folder = on_open_folder
        self._pulse_id = 0
        self.set_activatable(False)
        self.add_css_class('queue-row')

        box = Gtk.Box(spacing=14, margin_top=8, margin_bottom=8, margin_start=10, margin_end=10)
        self.set_child(box)

        self._pic = cover_picture(width_request=112, height_request=63, valign=Gtk.Align.CENTER)
        self._pic.add_css_class('thumb')
        if item.thumbnail:
            self._pic.set_paintable(item.thumbnail)
        box.append(self._pic)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                       hexpand=True, valign=Gtk.Align.CENTER)
        box.append(info)

        title = Gtk.Label(label=item.title, xalign=0, ellipsize=3)  # 3 = END
        title.add_css_class('heading')
        info.append(title)

        sub = Gtk.Label(label=f'{item.channel} · {item.preset.label}', xalign=0, ellipsize=3)
        sub.add_css_class('caption')
        sub.add_css_class('dim-label')
        info.append(sub)

        self._bar = Gtk.ProgressBar()
        self._bar.add_css_class('thin')
        info.append(self._bar)

        self._status = Gtk.Label(xalign=0, ellipsize=3)
        self._status.add_css_class('caption')
        self._status.add_css_class('dim-label')
        info.append(self._status)

        actions = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        box.append(actions)
        self._btn_toggle = self._icon_button('media-playback-pause-symbolic', 'Пауза', self._on_toggle)
        self._btn_cancel = self._icon_button('process-stop-symbolic', 'Отменить', self._on_cancel)
        self._btn_folder = self._icon_button('folder-open-symbolic', 'Показать в папке', self._on_folder)
        self._btn_remove = self._icon_button('user-trash-symbolic', 'Убрать из списка', self._on_remove)
        for b in (self._btn_toggle, self._btn_cancel, self._btn_folder, self._btn_remove):
            actions.append(b)

        self.refresh()

    @staticmethod
    def _icon_button(icon: str, tooltip: str, handler) -> Gtk.Button:
        btn = Gtk.Button.new_from_icon_name(icon)
        btn.add_css_class('flat')
        btn.add_css_class('circular')
        btn.set_tooltip_text(tooltip)
        btn.set_valign(Gtk.Align.CENTER)
        btn.connect('clicked', handler)
        return btn

    # ---------------------------------------------------------------- кнопки
    def _on_toggle(self, _b) -> None:
        st = self.item.state
        if st in (State.WAITING, State.DOWNLOADING):
            self._queue.pause(self.item)
        else:  # пауза / ошибка / отмена → продолжить или повторить
            self._queue.resume(self.item)

    def _on_cancel(self, _b) -> None:
        self._queue.cancel(self.item)

    def _on_folder(self, _b) -> None:
        self._on_open_folder(os.path.dirname(self.item.path) if self.item.path else self.item.dest_dir)

    def _on_remove(self, _b) -> None:
        self._queue.remove(self.item)

    # ---------------------------------------------------------------- обновление вида
    def _status_text(self) -> str:
        it = self.item
        pct = f'{int(it.fraction * 100)}%'
        if it.state == State.WAITING:
            return 'В очереди'
        if it.state == State.DOWNLOADING:
            if it.stopping == 'pause':
                return 'Останавливаю…'
            if it.stopping == 'cancel':
                return 'Отменяю…'
            parts = [pct]
            if it.speed:
                parts.append(dl.format_speed(it.speed))
            if it.eta is not None:
                parts.append('осталось ' + dl.format_eta(it.eta))
            return ' · '.join(parts)
        if it.state == State.PROCESSING:
            return it.process_label or 'Обработка…'
        if it.state == State.PAUSED:
            return f'Приостановлено · {pct}' if it.touched else 'Ожидает запуска'
        if it.state == State.DONE:
            try:
                return 'Готово · ' + dl.format_size(os.path.getsize(it.path))
            except OSError:
                return 'Готово'
        if it.state == State.ERROR:
            return it.error
        return 'Отменено'

    def _pulse(self) -> bool:
        if self.item.state != State.PROCESSING:
            self._pulse_id = 0
            return GLib.SOURCE_REMOVE
        self._bar.pulse()
        return GLib.SOURCE_CONTINUE

    def refresh(self) -> None:
        it, st = self.item, self.item.state
        self._status.set_label(self._status_text())

        self._bar.set_visible(st not in TERMINAL)
        if st == State.PROCESSING:
            if not self._pulse_id:
                self._pulse_id = GLib.timeout_add(120, self._pulse)
        else:
            self._bar.set_fraction(it.fraction)

        # Кнопка «пауза / продолжить / повторить»
        toggle_visible = st in (State.WAITING, State.DOWNLOADING, State.PAUSED, State.ERROR, State.CANCELLED)
        self._btn_toggle.set_visible(toggle_visible)
        self._btn_toggle.set_sensitive(not it.stopping)
        if st in (State.WAITING, State.DOWNLOADING):
            self._btn_toggle.set_icon_name('media-playback-pause-symbolic')
            self._btn_toggle.set_tooltip_text('Пауза')
        elif st == State.PAUSED:
            self._btn_toggle.set_icon_name('media-playback-start-symbolic')
            self._btn_toggle.set_tooltip_text('Продолжить' if it.touched else 'Старт')
        else:
            self._btn_toggle.set_icon_name('view-refresh-symbolic')
            self._btn_toggle.set_tooltip_text('Повторить')

        self._btn_cancel.set_visible(st in (State.WAITING, State.DOWNLOADING, State.PAUSED))
        self._btn_cancel.set_sensitive(it.stopping != 'cancel')
        self._btn_folder.set_visible(st == State.DONE)
        self._btn_remove.set_visible(st in TERMINAL)


# ==========================================================================
# Главное окно
# ==========================================================================

class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, settings) -> None:
        super().__init__(application=app, title='Re Saver', default_width=760, default_height=900)
        load_css()
        self.settings = settings
        self._token = 0              # номер запроса: устаревшие ответы игнорируем
        self._info = None            # dl.MediaInfo текущего превью
        self._thumb = None           # Gdk.Texture текущего превью
        self._qbtns: dict[str, Gtk.ToggleButton] = {}
        self._rows: dict[int, QueueRow] = {}
        self._force_close = False

        self.queue = DownloadQueue(self._on_item_added, self._on_item_changed, self._on_item_removed)
        self._build_ui()
        self.connect('close-request', self._on_close_request)

        self._check_ffmpeg()
        if settings.auto_update:
            updater.auto_update(settings, self._on_auto_update_done)
        GLib.idle_add(lambda: self.url_entry.grab_focus() and False)

    # ================================================================ интерфейс
    def _build_ui(self) -> None:
        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.toasts.set_child(root)

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title='Re Saver', subtitle=''))
        prefs_btn = Gtk.Button.new_from_icon_name('preferences-system-symbolic')
        prefs_btn.set_tooltip_text('Настройки')
        prefs_btn.connect('clicked', self._on_open_prefs)
        header.pack_end(prefs_btn)
        root.append(header)

        self.banner = InfoBanner('ffmpeg не найден. Установите его: sudo apt install ffmpeg',
                                 'Проверить снова', self._check_ffmpeg)
        root.append(self.banner)

        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        root.append(scroller)
        clamp = Adw.Clamp(maximum_size=680, tightening_threshold=520)
        scroller.set_child(clamp)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=28,
                          margin_top=28, margin_bottom=40, margin_start=18, margin_end=18)
        clamp.set_child(content)

        content.append(self._build_hero())
        content.append(self._build_input())
        content.append(self._build_preview())
        content.append(self._build_queue_section())

    def _build_hero(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        title = Gtk.Label(label='Что сохраним?', xalign=0)
        title.add_css_class('mv-hero')
        lead = Gtk.Label(label='Ваш личный офлайн-архив: видео, подкасты и музыка.',
                         xalign=0, wrap=True)
        lead.add_css_class('mv-lead')
        box.append(title)
        box.append(lead)
        return box

    def _build_input(self) -> Gtk.Widget:
        box = Gtk.Box(spacing=10)
        self.url_entry = Gtk.Entry(hexpand=True, placeholder_text='Вставьте ссылку на видео или аудио')
        self.url_entry.add_css_class('url-entry')
        self.url_entry.connect('activate', self._on_find)
        self.find_btn = Gtk.Button(label='Найти')
        self.find_btn.add_css_class('suggested-action')
        self.find_btn.add_css_class('pill-btn')
        self.find_btn.connect('clicked', self._on_find)
        box.append(self.url_entry)
        box.append(self.find_btn)
        return box

    def _build_preview(self) -> Gtk.Widget:
        self.preview_revealer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN, transition_duration=250)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.add_css_class('mv-card')
        self.preview_revealer.set_child(card)

        self.preview_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        card.append(self.preview_stack)

        # --- загрузка
        loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                          valign=Gtk.Align.CENTER, height_request=180)
        self.spinner = Gtk.Spinner(width_request=32, height_request=32)
        loading.append(self.spinner)
        wait = Gtk.Label(label='Получаю информацию…')
        wait.add_css_class('dim-label')
        loading.append(wait)
        self.preview_stack.add_named(loading, 'loading')

        # --- ошибка
        err = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      valign=Gtk.Align.CENTER, height_request=140)
        err.append(Gtk.Image.new_from_icon_name('dialog-warning-symbolic'))
        self.error_label = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        err.append(self.error_label)
        self.preview_stack.add_named(err, 'error')

        # --- готово
        ready = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.preview_stack.add_named(ready, 'ready')

        overlay = Gtk.Overlay()
        self.preview_pic = cover_picture(height_request=210, hexpand=True)
        self.preview_pic.add_css_class('preview-thumb')
        overlay.set_child(self.preview_pic)
        self.duration_badge = Gtk.Label(halign=Gtk.Align.END, valign=Gtk.Align.END,
                                        margin_end=10, margin_bottom=10)
        self.duration_badge.add_css_class('duration-badge')
        overlay.add_overlay(self.duration_badge)
        ready.append(overlay)

        meta = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.title_label = Gtk.Label(xalign=0, wrap=True, lines=2, ellipsize=3)
        self.title_label.add_css_class('title-3')
        self.channel_label = Gtk.Label(xalign=0, ellipsize=3)
        self.channel_label.add_css_class('dim-label')
        meta.append(self.title_label)
        meta.append(self.channel_label)
        ready.append(meta)

        caption = Gtk.Label(label='КАЧЕСТВО', xalign=0)
        caption.add_css_class('mv-caption')
        ready.append(caption)
        self.quality_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        ready.append(self.quality_box)

        folders = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        folders.add_css_class('boxed-list')
        self.folder_row = Adw.ActionRow(title='Папка', subtitle=self.settings.download_dir)
        if hasattr(self.folder_row, 'set_use_markup'):
            self.folder_row.set_use_markup(False)
        pick = Gtk.Button(label='Изменить…', valign=Gtk.Align.CENTER)
        pick.connect('clicked', self._on_choose_folder)
        self.folder_row.add_suffix(pick)
        folders.append(self.folder_row)
        ready.append(folders)

        self.add_btn = Gtk.Button(label='Добавить в очередь', halign=Gtk.Align.CENTER)
        self.add_btn.add_css_class('suggested-action')
        self.add_btn.add_css_class('pill-btn')
        self.add_btn.connect('clicked', self._on_add)
        ready.append(self.add_btn)

        return self.preview_revealer

    def _build_queue_section(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        head = Gtk.Box(spacing=8)
        title = Gtk.Label(label='Загрузки', xalign=0, hexpand=True)
        title.add_css_class('mv-section')
        clear = Gtk.Button(label='Очистить завершённые')
        clear.add_css_class('flat')
        clear.connect('clicked', lambda *_: self.queue.clear_finished())
        head.append(title)
        head.append(clear)
        box.append(head)

        self.queue_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        empty = Adw.StatusPage(icon_name='folder-download-symbolic',
                               title='Пока пусто',
                               description='Вставьте ссылку выше — загрузки появятся здесь.')
        self.queue_stack.add_named(empty, 'empty')
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.add_css_class('boxed-list')
        self.queue_stack.add_named(self.listbox, 'list')
        box.append(self.queue_stack)
        return box

    # ================================================================ утилиты
    def toast(self, message: str, timeout: int = 3) -> Adw.Toast:
        t = Adw.Toast.new(message)
        t.set_timeout(timeout)
        if hasattr(t, 'set_use_markup'):
            t.set_use_markup(False)
        self.toasts.add_toast(t)
        return t

    def _check_ffmpeg(self) -> None:
        self.banner.set_revealed(not dl.ffmpeg_available())

    # ================================================================ поиск ссылки
    def _on_find(self, *_args) -> None:
        url = self.url_entry.get_text().strip()
        if not url:
            self.toast('Вставьте ссылку')
            return
        if not re.match(r'^https?://', url, re.I):
            url = 'https://' + url

        self._token += 1
        token = self._token
        self._info = None
        self._thumb = None
        self.preview_pic.set_paintable(None)
        self.add_btn.set_sensitive(False)
        self.find_btn.set_sensitive(False)
        self.preview_stack.set_visible_child_name('loading')
        self.spinner.start()
        self.preview_revealer.set_reveal_child(True)

        threading.Thread(target=self._fetch_worker, args=(token, url), daemon=True).start()

    def _fetch_worker(self, token: int, url: str) -> None:
        """Рабочий поток: метаданные, затем обложка. UI трогаем только через call_in_ui."""
        try:
            info = dl.fetch_info(url)
        except dl.DownloaderError as exc:
            call_in_ui(self._on_fetch_error, token, str(exc))
            return
        except Exception:  # noqa: BLE001
            call_in_ui(self._on_fetch_error, token,
                       'Не удалось получить данные. Проверьте ссылку и соединение.')
            return
        call_in_ui(self._on_fetch_done, token, info)
        data = dl.fetch_thumbnail(info.thumbnail_urls)
        if data:
            call_in_ui(self._on_thumb, token, data)

    def _on_fetch_error(self, token: int, message: str) -> None:
        if token != self._token:
            return
        self.spinner.stop()
        self.find_btn.set_sensitive(True)
        self.error_label.set_label(message)
        self.preview_stack.set_visible_child_name('error')

    def _on_fetch_done(self, token: int, info: dl.MediaInfo) -> None:
        if token != self._token:
            return
        self._info = info
        self.spinner.stop()
        self.find_btn.set_sensitive(True)
        self.title_label.set_label(info.title)
        self.channel_label.set_label(info.channel)
        duration = dl.format_duration(info.duration)
        self.duration_badge.set_label(duration)
        self.duration_badge.set_visible(bool(duration))
        self._populate_quality(info.presets)
        self.folder_row.set_subtitle(self.settings.download_dir)
        self.add_btn.set_sensitive(True)
        self.preview_stack.set_visible_child_name('ready')

    def _on_thumb(self, token: int, data: bytes) -> None:
        if token != self._token:
            return
        texture = make_texture(data)
        if texture:
            self._thumb = texture
            self.preview_pic.set_paintable(texture)

    # ================================================================ качество и папка
    def _populate_quality(self, presets) -> None:
        while (child := self.quality_box.get_first_child()) is not None:
            self.quality_box.remove(child)
        self._qbtns.clear()

        leader = None
        groups = (('Видео', [p for p in presets if p.kind == 'video']),
                  ('Только аудио', [p for p in presets if p.kind == 'audio']))
        for caption, items in groups:
            if not items:
                continue
            lbl = Gtk.Label(label=caption, xalign=0)
            lbl.add_css_class('dim-label')
            lbl.add_css_class('caption')
            row = Gtk.Box(spacing=8)
            for p in items:
                btn = Gtk.ToggleButton(label=p.label)
                btn.add_css_class('quality-btn')
                if leader is None:
                    leader = btn
                else:
                    btn.set_group(leader)  # в группе активна только одна кнопка
                row.append(btn)
                self._qbtns[p.id] = btn
            self.quality_box.append(lbl)
            self.quality_box.append(row)

        chosen = dl.pick_default(presets, self.settings.default_format)
        if chosen in self._qbtns:
            self._qbtns[chosen].set_active(True)

    def _selected_preset(self):
        for pid, btn in self._qbtns.items():
            if btn.get_active():
                return dl.PRESETS[pid]
        return None

    def _on_choose_folder(self, _btn) -> None:
        def chosen(path: str) -> None:
            self.settings.download_dir = path   # запоминаем последнюю выбранную папку
            self.settings.save()
            self.folder_row.set_subtitle(path)
        choose_folder(self, self.settings.download_dir, chosen)

    # ================================================================ добавление в очередь
    def _on_add(self, _btn) -> None:
        if not self._info:
            return
        if not dl.ffmpeg_available():
            self._check_ffmpeg()
            self.toast('Без ffmpeg скачивание невозможно')
            return
        preset = self._selected_preset()
        if preset is None:
            self.toast('Выберите качество')
            return

        dest = self.settings.download_dir
        try:
            os.makedirs(dest, exist_ok=True)
            if not os.access(dest, os.W_OK):
                raise OSError
        except OSError:
            self.toast('Нет доступа к выбранной папке — выберите другую')
            return

        info = self._info
        self.queue.add(info.url, info.title, info.channel, preset, dest,
                       self._thumb, self.settings.autostart)
        self.toast('Добавлено в очередь')

        # Готовим окно к следующей ссылке
        self._token += 1
        self._info = None
        self._thumb = None
        self.url_entry.set_text('')
        self.preview_revealer.set_reveal_child(False)
        self.url_entry.grab_focus()

    # ================================================================ события очереди
    def _on_item_added(self, item: QueueItem) -> None:
        row = QueueRow(item, self.queue, self._open_folder)
        self._rows[item.uid] = row
        self.listbox.append(row)
        self.queue_stack.set_visible_child_name('list')

    def _on_item_changed(self, item: QueueItem, old: State) -> None:
        row = self._rows.get(item.uid)
        if row:
            row.refresh()
        if item.state == old:
            return
        if item.state == State.DONE:
            t = self.toast(f'Готово: {item.title[:50]}', timeout=5)
            t.set_button_label('Показать')
            folder = os.path.dirname(item.path) if item.path else item.dest_dir
            t.connect('button-clicked', lambda *_: self._open_folder(folder))
        elif item.state == State.ERROR:
            self.toast(item.error, timeout=6)

    def _on_item_removed(self, item: QueueItem) -> None:
        row = self._rows.pop(item.uid, None)
        if row:
            self.listbox.remove(row)
        if not self._rows:
            self.queue_stack.set_visible_child_name('empty')

    def _open_folder(self, path: str) -> None:
        if not open_folder(path):
            self.toast('Не удалось открыть папку')

    # ================================================================ настройки и обновление
    def _on_open_prefs(self, _btn) -> None:
        PreferencesWindow(self, self.settings, self._on_settings_changed).present()

    def _on_settings_changed(self, key: str) -> None:
        if key == 'download_dir':
            self.folder_row.set_subtitle(self.settings.download_dir)

    def _on_auto_update_done(self, result: updater.UpdateResult) -> None:
        # Тихое обновление: сообщаем только об успехе
        if result.status == 'updated':
            self.toast(result.message, timeout=6)

    # ================================================================ закрытие окна
    def _on_close_request(self, *_args) -> bool:
        if self._force_close or not self.queue.has_active():
            self.queue.shutdown()
            return False  # закрываем

        title, body = 'Выйти из приложения?', \
            'Активные загрузки будут приостановлены, а очередь не сохраняется.'
        if hasattr(Adw, 'MessageDialog'):
            dialog = Adw.MessageDialog.new(self, title, body)
            dialog.add_response('stay', 'Остаться')
            dialog.add_response('quit', 'Выйти')
            dialog.set_response_appearance('quit', Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response('stay')
            dialog.set_close_response('stay')
            quit_value = 'quit'
        else:  # libadwaita < 1.2
            dialog = Gtk.MessageDialog(transient_for=self, modal=True, text=title,
                                       secondary_text=body)
            dialog.add_button('Остаться', Gtk.ResponseType.CANCEL)
            dialog.add_button('Выйти', Gtk.ResponseType.OK)
            quit_value = Gtk.ResponseType.OK

        def on_response(dlg, response):
            if response == quit_value:
                self._force_close = True
                self.close()
            if not hasattr(Adw, 'MessageDialog'):
                dlg.destroy()

        dialog.connect('response', on_response)
        dialog.present()
        return True  # окно пока не закрываем
