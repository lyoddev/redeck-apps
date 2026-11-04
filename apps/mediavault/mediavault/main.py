"""main.py — точка входа MediaVault.

Запуск из корня проекта:  python3 -m mediavault
"""
import sys

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

# ВАЖНО: подключаем личную папку с обновлённым yt-dlp ДО любого `import yt_dlp`
# (а его импортирует downloader, который подтягивается через window).
from . import updater  # noqa: E402

updater.use_private_lib()

from gi.repository import Adw, Gio, GLib  # noqa: E402

from .settings import Settings, apply_theme  # noqa: E402
from .window import MainWindow  # noqa: E402

APP_ID = 'io.github.mediavault.MediaVault'


class MediaVaultApp(Adw.Application):
    def __init__(self) -> None:
        flags = getattr(Gio.ApplicationFlags, 'DEFAULT_FLAGS', Gio.ApplicationFlags.FLAGS_NONE)
        super().__init__(application_id=APP_ID, flags=flags)
        self.settings = Settings.load()

    def do_activate(self) -> None:
        win = self.props.active_window
        if win is None:
            apply_theme(self.settings.theme)  # тёмная тема по умолчанию
            win = MainWindow(self, self.settings)
        win.present()


def main() -> int:
    GLib.set_application_name('MediaVault')
    return MediaVaultApp().run(sys.argv)


if __name__ == '__main__':
    sys.exit(main())
