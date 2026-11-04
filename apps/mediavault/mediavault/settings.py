"""settings.py — хранение настроек (JSON) и окно AdwPreferencesWindow.

На диск пишутся только настройки. Ссылки и история загрузок не сохраняются.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from typing import Callable

from gi.repository import Adw, Gio, GLib, Gtk

from . import updater
from .downloader import DEFAULT_CHOICES, PRESETS

SETTINGS_PATH = os.path.join(GLib.get_user_config_dir(), 'mediavault', 'settings.json')

THEMES = (('dark', 'Тёмная'), ('light', 'Светлая'), ('system', 'Как в системе'))


def _default_download_dir() -> str:
    # В разных версиях GLib константа называется по-разному (DOWNLOAD / DOWNLOADS)
    kind = (getattr(GLib.UserDirectory, 'DIRECTORY_DOWNLOAD', None)
            or getattr(GLib.UserDirectory, 'DIRECTORY_DOWNLOADS', None))
    path = GLib.get_user_special_dir(kind) if kind is not None else None
    return path or os.path.expanduser('~/Downloads')


@dataclass
class Settings:
    download_dir: str = field(default_factory=_default_download_dir)
    default_format: str = 'v1080'
    autostart: bool = True        # начинать загрузку сразу после добавления в очередь
    theme: str = 'dark'
    auto_update: bool = True      # тихо обновлять yt-dlp при запуске
    last_update_check: float = 0.0

    @classmethod
    def load(cls) -> 'Settings':
        s = cls()
        try:
            with open(SETTINGS_PATH, encoding='utf-8') as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return s
        if not isinstance(raw, dict):
            return s
        for fld in fields(cls):
            if fld.name not in raw:
                continue
            value, expected = raw[fld.name], type(getattr(s, fld.name))
            if expected is float:
                ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            else:
                ok = isinstance(value, expected)
            if ok:
                setattr(s, fld.name, value)
        # Защита от испорченных значений
        if s.default_format not in PRESETS or s.default_format == 'best':
            s.default_format = 'v1080'
        if s.theme not in dict(THEMES):
            s.theme = 'dark'
        if not s.download_dir:
            s.download_dir = _default_download_dir()
        return s

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
            tmp = SETTINGS_PATH + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(asdict(self), f, ensure_ascii=False, indent=2)
            os.replace(tmp, SETTINGS_PATH)  # атомарно: файл не испортится при сбое
        except OSError:
            pass


def apply_theme(theme: str) -> None:
    scheme = {
        'dark': Adw.ColorScheme.FORCE_DARK,
        'light': Adw.ColorScheme.FORCE_LIGHT,
    }.get(theme, Adw.ColorScheme.DEFAULT)
    Adw.StyleManager.get_default().set_color_scheme(scheme)


# Ссылки на открытые диалоги, чтобы сборщик мусора их не убил раньше времени
_live_dialogs: set = set()


def choose_folder(parent: Gtk.Window, start_dir: str, on_chosen: Callable[[str], None]) -> None:
    """Системный диалог выбора папки (GTK FileChooser)."""
    chooser = Gtk.FileChooserNative.new(
        'Выберите папку', parent, Gtk.FileChooserAction.SELECT_FOLDER, 'Выбрать', 'Отмена')
    chooser.set_modal(True)
    if start_dir and os.path.isdir(start_dir):
        try:
            chooser.set_current_folder(Gio.File.new_for_path(start_dir))
        except GLib.Error:
            pass

    def on_response(dialog, response):
        _live_dialogs.discard(dialog)
        if response == Gtk.ResponseType.ACCEPT:
            gfile = dialog.get_file()
            if gfile and gfile.get_path():
                on_chosen(gfile.get_path())

    _live_dialogs.add(chooser)
    chooser.connect('response', on_response)
    chooser.show()


def _plain(row) -> None:
    """Отключаем разметку в строках, чтобы '&' и '<' в путях не ломали текст."""
    if hasattr(row, 'set_use_markup'):
        row.set_use_markup(False)


class PreferencesWindow(Adw.PreferencesWindow):
    def __init__(self, parent: Gtk.Window, settings: Settings,
                 on_changed: Callable[[str], None]) -> None:
        super().__init__(transient_for=parent, modal=True, title='Настройки')
        self._s = settings
        self._on_changed = on_changed
        self.set_search_enabled(False)

        page = Adw.PreferencesPage()
        self.add(page)

        # ---- Загрузки
        group = Adw.PreferencesGroup(title='Загрузки')
        page.add(group)

        self._folder_row = Adw.ActionRow(title='Папка для загрузок', subtitle=settings.download_dir)
        _plain(self._folder_row)
        btn = Gtk.Button(label='Изменить…', valign=Gtk.Align.CENTER)
        btn.connect('clicked', self._pick_folder)
        self._folder_row.add_suffix(btn)
        group.add(self._folder_row)

        self._format_ids = [p.id for p in DEFAULT_CHOICES]
        fmt_row = Adw.ComboRow(title='Формат по умолчанию')
        fmt_row.set_model(Gtk.StringList.new([p.label for p in DEFAULT_CHOICES]))
        if settings.default_format in self._format_ids:
            fmt_row.set_selected(self._format_ids.index(settings.default_format))
        fmt_row.connect('notify::selected', self._on_format)
        group.add(fmt_row)

        group.add(self._switch_row(
            'Начинать загрузку сразу', 'Иначе загрузка ждёт, пока вы нажмёте «Старт»',
            settings.autostart, 'autostart'))

        # ---- Оформление
        look = Adw.PreferencesGroup(title='Оформление')
        page.add(look)
        theme_row = Adw.ComboRow(title='Тема')
        theme_row.set_model(Gtk.StringList.new([label for _, label in THEMES]))
        ids = [t for t, _ in THEMES]
        theme_row.set_selected(ids.index(settings.theme) if settings.theme in ids else 0)
        theme_row.connect('notify::selected', self._on_theme)
        look.add(theme_row)

        # ---- yt-dlp
        yt = Adw.PreferencesGroup(title='yt-dlp',
                                  description='Сайты часто меняются, поэтому yt-dlp нужно обновлять.')
        page.add(yt)
        self._version_row = Adw.ActionRow(title='Версия', subtitle=self._version_text())
        _plain(self._version_row)
        yt.add(self._version_row)
        yt.add(self._switch_row(
            'Обновлять при запуске', 'Тихо, в фоне, не чаще раза в 12 часов',
            settings.auto_update, 'auto_update'))

        update_row = Adw.ActionRow(title='Обновить сейчас', subtitle='Проверить и установить свежую версию')
        self._spinner = Gtk.Spinner(valign=Gtk.Align.CENTER)
        self._update_btn = Gtk.Button(label='Обновить сейчас', valign=Gtk.Align.CENTER)
        self._update_btn.add_css_class('suggested-action')
        self._update_btn.connect('clicked', self._on_update_clicked)
        update_row.add_suffix(self._spinner)
        update_row.add_suffix(self._update_btn)
        yt.add(update_row)

    # ---------------------------------------------------------------- helpers
    def _switch_row(self, title: str, subtitle: str, active: bool, key: str) -> Adw.ActionRow:
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        sw = Gtk.Switch(valign=Gtk.Align.CENTER, active=active)

        def on_toggle(switch, _pspec):
            setattr(self._s, key, switch.get_active())
            self._s.save()
            self._on_changed(key)

        sw.connect('notify::active', on_toggle)
        row.add_suffix(sw)
        row.set_activatable_widget(sw)
        return row

    @staticmethod
    def _version_text() -> str:
        running, disk = updater.running_version(), updater.installed_version()
        if running and disk and running != disk:
            return f'{running} · после перезапуска: {disk}'
        return running or disk or 'неизвестно'

    # ---------------------------------------------------------------- handlers
    def _pick_folder(self, _btn) -> None:
        def chosen(path: str) -> None:
            self._s.download_dir = path
            self._s.save()
            self._folder_row.set_subtitle(path)
            self._on_changed('download_dir')
        choose_folder(self, self._s.download_dir, chosen)

    def _on_format(self, row, _pspec) -> None:
        idx = row.get_selected()
        if 0 <= idx < len(self._format_ids):
            self._s.default_format = self._format_ids[idx]
            self._s.save()
            self._on_changed('default_format')

    def _on_theme(self, row, _pspec) -> None:
        idx = row.get_selected()
        if 0 <= idx < len(THEMES):
            self._s.theme = THEMES[idx][0]
            self._s.save()
            apply_theme(self._s.theme)
            self._on_changed('theme')

    def _on_update_clicked(self, _btn) -> None:
        self._update_btn.set_sensitive(False)
        self._spinner.start()
        updater.update_in_background(self._on_update_done)

    def _on_update_done(self, result: updater.UpdateResult) -> None:
        self._spinner.stop()
        self._update_btn.set_sensitive(True)
        self._version_row.set_subtitle(self._version_text())
        self.add_toast(Adw.Toast.new(result.message))
