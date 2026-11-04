"""queue.py — очередь загрузок: состояния, параллельность, пауза/продолжение/отмена.

Файл лежит внутри пакета mediavault, поэтому не конфликтует со стандартным
модулем `queue` (запускать приложение нужно как `python3 -m mediavault`).

Очередь живёт только в памяти: ссылки и история на диск не пишутся.
Все методы вызываются из главного потока GTK. События из рабочих потоков
доезжают сюда через GLib.idle_add.
"""
from __future__ import annotations

import itertools
from enum import Enum
from typing import Callable, Optional

from gi.repository import GLib

from .downloader import DownloadJob, Preset, remove_partial_files


def call_in_ui(fn: Callable, *args) -> None:
    """Выполнить fn(*args) в главном потоке GTK (один раз)."""
    def _run():
        fn(*args)
        return GLib.SOURCE_REMOVE
    GLib.idle_add(_run)


class State(Enum):
    WAITING = 'waiting'          # ждёт свободный слот
    DOWNLOADING = 'downloading'
    PROCESSING = 'processing'    # склейка / конвертация
    PAUSED = 'paused'            # пауза или «ещё не запускали»
    DONE = 'done'
    ERROR = 'error'
    CANCELLED = 'cancelled'


TERMINAL = (State.DONE, State.ERROR, State.CANCELLED)


class QueueItem:
    def __init__(self, uid: int, url: str, title: str, channel: str,
                 preset: Preset, dest_dir: str, thumbnail) -> None:
        self.uid = uid
        self.url = url              # только в памяти!
        self.title = title
        self.channel = channel
        self.preset = preset
        self.dest_dir = dest_dir
        self.thumbnail = thumbnail  # Gdk.Texture или None

        self.state = State.WAITING
        self.fraction = 0.0
        self.speed: Optional[float] = None
        self.eta: Optional[float] = None
        self.error = ''
        self.path = ''
        self.process_label = ''
        self.touched = False        # загрузка хоть раз стартовала
        self.stopping = ''          # '' | 'pause' | 'cancel' — ждём остановки потока
        self.partial_files: set[str] = set()
        self.job: Optional[DownloadJob] = None
        self.job_alive = False


class DownloadQueue:
    def __init__(self,
                 on_added: Callable[[QueueItem], None],
                 on_changed: Callable[[QueueItem, State], None],
                 on_removed: Callable[[QueueItem], None],
                 max_parallel: int = 2) -> None:
        self._on_added = on_added
        self._on_changed = on_changed
        self._on_removed = on_removed
        self._max_parallel = max_parallel
        self._counter = itertools.count(1)
        self.items: list[QueueItem] = []
        self._by_id: dict[int, QueueItem] = {}

    # ---------------------------------------------------------------- добавление
    def add(self, url: str, title: str, channel: str, preset: Preset,
            dest_dir: str, thumbnail, autostart: bool) -> QueueItem:
        item = QueueItem(next(self._counter), url, title, channel, preset, dest_dir, thumbnail)
        if not autostart:
            item.state = State.PAUSED  # ждёт, пока пользователь нажмёт «Старт»
        self.items.append(item)
        self._by_id[item.uid] = item
        self._on_added(item)
        self._pump()
        return item

    # ---------------------------------------------------------------- управление
    def pause(self, item: QueueItem) -> None:
        if item.state == State.WAITING:
            self._set_state(item, State.PAUSED)
        elif item.state == State.DOWNLOADING and item.job_alive and not item.stopping:
            item.stopping = 'pause'
            item.job.pause()
            self._on_changed(item, item.state)

    def resume(self, item: QueueItem) -> None:
        """Продолжить с паузы или повторить после ошибки/отмены."""
        if item.job_alive or item.state not in (State.PAUSED, State.ERROR, State.CANCELLED):
            return
        if item.state in (State.ERROR, State.CANCELLED):
            item.fraction = 0.0
            item.error = ''
        self._set_state(item, State.WAITING)
        self._pump()

    def cancel(self, item: QueueItem) -> None:
        if item.job_alive:
            if item.state == State.DOWNLOADING and item.stopping != 'cancel':
                item.stopping = 'cancel'
                item.job.cancel()
                self._on_changed(item, item.state)
        elif item.state in (State.WAITING, State.PAUSED):
            remove_partial_files(item.partial_files)
            item.partial_files.clear()
            item.fraction = 0.0
            self._set_state(item, State.CANCELLED)
            self._pump()

    def remove(self, item: QueueItem) -> None:
        if item.job_alive or item.state not in TERMINAL:
            return
        self.items.remove(item)
        self._by_id.pop(item.uid, None)
        self._on_removed(item)

    def clear_finished(self) -> None:
        for item in [i for i in self.items if i.state in TERMINAL]:
            self.remove(item)

    def has_active(self) -> bool:
        return any(i.job_alive or i.state == State.WAITING for i in self.items)

    def shutdown(self) -> None:
        """При выходе мягко останавливаем потоки (файлы .part сохраняются)."""
        for item in self.items:
            if item.job_alive and item.job:
                item.job.pause()

    # ---------------------------------------------------------------- внутреннее
    def _set_state(self, item: QueueItem, new: State) -> None:
        old = item.state
        item.state = new
        self._on_changed(item, old)

    def _pump(self) -> None:
        """Запускает ожидающие загрузки, пока есть свободные слоты."""
        running = sum(1 for i in self.items if i.job_alive)
        for item in self.items:
            if running >= self._max_parallel:
                break
            if item.state == State.WAITING and not item.job_alive:
                self._launch(item)
                running += 1

    def _launch(self, item: QueueItem) -> None:
        item.touched = True
        item.stopping = ''
        item.speed = None
        item.eta = None
        item.job_alive = True
        item.job = DownloadJob(
            item.url, item.preset, item.dest_dir,
            emit=lambda ev, uid=item.uid: call_in_ui(self._on_event, uid, ev))
        self._set_state(item, State.DOWNLOADING)
        item.job.start()

    def _on_event(self, uid: int, ev: dict) -> None:
        item = self._by_id.get(uid)
        if item is None:
            return
        kind = ev.get('type')

        if kind == 'progress':
            if item.state == State.DOWNLOADING:
                item.fraction = ev['fraction']
                item.speed = ev.get('speed')
                item.eta = ev.get('eta')
                self._on_changed(item, item.state)
        elif kind == 'file':
            item.partial_files.add(ev['path'])
        elif kind == 'processing':
            item.process_label = ev.get('label', '')
            item.speed = item.eta = None
            self._set_state(item, State.PROCESSING)
        elif kind == 'done':
            item.job_alive = False
            item.stopping = ''
            item.fraction = 1.0
            item.path = ev.get('path', '')
            item.partial_files.clear()
            self._set_state(item, State.DONE)
            self._pump()
        elif kind == 'paused':
            item.job_alive = False
            item.stopping = ''
            item.speed = item.eta = None
            self._set_state(item, State.PAUSED)
            self._pump()
        elif kind == 'cancelled':
            item.job_alive = False
            item.stopping = ''
            remove_partial_files(item.partial_files)
            item.partial_files.clear()
            item.fraction = 0.0
            self._set_state(item, State.CANCELLED)
            self._pump()
        elif kind == 'error':
            item.job_alive = False
            item.stopping = ''
            item.error = ev.get('message', 'Неизвестная ошибка')
            item.speed = item.eta = None
            self._set_state(item, State.ERROR)
            self._pump()
