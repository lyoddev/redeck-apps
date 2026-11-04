"""updater.py — обновление yt-dlp без прав администратора.

Как это работает (по-простому):
  * свежий yt-dlp ставится через pip в личную папку приложения
    (~/.local/share/mediavault/ytdlp-lib), а не в систему;
  * при следующем запуске main.py вызывает use_private_lib() ДО импорта
    yt_dlp, и Python берёт версию из этой папки, если она новее;
  * так не мешает PEP 668 («externally managed environment») и не нужен sudo.

Обновлённая версия начинает работать после перезапуска приложения:
уже загруженный в память yt-dlp «на лету» не подменить без риска.

Этот модуль не импортирует yt_dlp на уровне модуля — иначе use_private_lib()
не успел бы повлиять на выбор версии.
"""
from __future__ import annotations

import importlib
import importlib.metadata
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable

from gi.repository import GLib

LIB_DIR = os.path.join(GLib.get_user_data_dir(), 'mediavault', 'ytdlp-lib')
CHECK_INTERVAL = 12 * 3600  # автопроверка не чаще раза в 12 часов
_lock = threading.Lock()


@dataclass
class UpdateResult:
    status: str        # 'updated' | 'up_to_date' | 'failed' | 'busy'
    message: str
    old: str = ''
    new: str = ''


# --------------------------------------------------------------------------
# Версии
# --------------------------------------------------------------------------

def _vkey(version: str) -> tuple[int, ...]:
    """'2025.01.15' → (2025, 1, 15): годится для сравнения версий yt-dlp."""
    return tuple(int(x) for x in re.findall(r'\d+', version or ''))


def _dir_version(path: str) -> str:
    """Версия yt-dlp, лежащего в папке (по имени *.dist-info)."""
    try:
        for name in os.listdir(path):
            m = re.match(r'(?i)^yt[_-]dlp-(.+)\.dist-info$', name)
            if m:
                return m.group(1)
    except OSError:
        pass
    return ''


def installed_version() -> str:
    """Версия yt-dlp на диске (через importlib.metadata)."""
    try:
        return importlib.metadata.version('yt-dlp')
    except importlib.metadata.PackageNotFoundError:
        return ''


def running_version() -> str:
    """Версия yt-dlp, которая реально загружена в программу сейчас."""
    try:
        from yt_dlp.version import __version__
        return __version__
    except Exception:  # noqa: BLE001
        return ''


def use_private_lib() -> None:
    """Подключает личную папку с yt-dlp, если там версия не старее системной.

    Вызывать в самом начале main.py, до любого `import yt_dlp`.
    """
    private = _dir_version(LIB_DIR)
    if not private:
        return
    current = installed_version()
    if not current or _vkey(private) >= _vkey(current):
        sys.path.insert(0, LIB_DIR)
        importlib.invalidate_caches()


# --------------------------------------------------------------------------
# Само обновление
# --------------------------------------------------------------------------

def _explain(stderr: str) -> str:
    low = (stderr or '').lower()
    if 'no module named pip' in low:
        return 'Не найден pip. Установите: sudo apt install python3-pip'
    if any(s in low for s in ('connection', 'resolution', 'timed out', 'max retries', 'network')):
        return 'Не удалось связаться с PyPI. Проверьте интернет и повторите.'
    return 'Не удалось обновить yt-dlp.'


def _do_update() -> UpdateResult:
    old = installed_version()
    tmp = LIB_DIR + '.new'
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        os.makedirs(os.path.dirname(LIB_DIR), exist_ok=True)
    except OSError:
        return UpdateResult('failed', 'Нет доступа к папке данных приложения.', old)

    cmd = [sys.executable, '-m', 'pip', 'install', '--upgrade', '--quiet', '--no-input',
           '--disable-pip-version-check', '--only-binary=:all:', '--target', tmp, 'yt-dlp']
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmp, ignore_errors=True)
        return UpdateResult('failed', 'Обновление заняло слишком много времени.', old)
    except OSError:
        return UpdateResult('failed', 'Не удалось запустить pip.', old)

    if proc.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        return UpdateResult('failed', _explain(proc.stderr), old)

    new = _dir_version(tmp)
    if not new:
        shutil.rmtree(tmp, ignore_errors=True)
        return UpdateResult('failed', 'pip отработал, но yt-dlp не найден в результате.', old)

    if _vkey(new) <= _vkey(old):
        shutil.rmtree(tmp, ignore_errors=True)
        return UpdateResult('up_to_date', f'Уже установлена последняя версия ({old}).', old, old)

    # Подменяем папку целиком: сначала откладываем старую, потом ставим новую
    backup = LIB_DIR + '.old'
    try:
        shutil.rmtree(backup, ignore_errors=True)
        if os.path.isdir(LIB_DIR):
            os.replace(LIB_DIR, backup)
        os.replace(tmp, LIB_DIR)
        shutil.rmtree(backup, ignore_errors=True)
    except OSError:
        if os.path.isdir(backup) and not os.path.isdir(LIB_DIR):
            os.replace(backup, LIB_DIR)  # откат
        shutil.rmtree(tmp, ignore_errors=True)
        return UpdateResult('failed', 'Не удалось заменить файлы yt-dlp.', old)

    importlib.invalidate_caches()
    return UpdateResult('updated', f'yt-dlp обновлён до {new}. Перезапустите приложение.', old, new)


def run_update() -> UpdateResult:
    """Блокирующее обновление. Вызывать из рабочего потока."""
    if not _lock.acquire(blocking=False):
        return UpdateResult('busy', 'Обновление уже идёт.')
    try:
        return _do_update()
    finally:
        _lock.release()


def update_in_background(on_done: Callable[[UpdateResult], None]) -> None:
    """Запускает обновление в потоке; on_done вызовется в главном потоке GTK."""
    def worker():
        result = run_update()

        def deliver():
            on_done(result)
            return GLib.SOURCE_REMOVE
        GLib.idle_add(deliver)

    threading.Thread(target=worker, daemon=True).start()


def auto_update(settings, on_done: Callable[[UpdateResult], None]) -> None:
    """Тихая проверка при старте (не чаще раза в CHECK_INTERVAL).

    `settings` — объект с полем last_update_check и методом save().
    """
    now = time.time()
    if now - settings.last_update_check < CHECK_INTERVAL:
        return
    settings.last_update_check = now  # ставим сразу, чтобы не долбить сеть при сбоях
    settings.save()
    update_in_background(on_done)
