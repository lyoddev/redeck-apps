"""downloader.py — всё, что связано с yt-dlp: метаданные, пресеты качества, фоновая загрузка.

Модуль ничего не знает про GTK. Рабочий поток только вызывает переданный
callback `emit(событие)`, а доставкой в главный поток занимается queue.py.
Ни URL, ни история на диск не пишутся: yt-dlp работает в тихом режиме
с логгером, который хранит только последнюю ошибку в памяти.
"""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional

import yt_dlp
from yt_dlp.utils import DownloadCancelled


# --------------------------------------------------------------------------
# Пресеты качества
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Preset:
    id: str          # стабильный идентификатор (хранится в настройках)
    label: str       # подпись на кнопке
    kind: str        # 'video' или 'audio'
    height: int = 0  # для видео: максимальная высота кадра
    bitrate: int = 0  # для аудио: битрейт MP3, кбит/с


_PRESET_LIST = (
    Preset('v1080', '1080p', 'video', height=1080),
    Preset('v720', '720p', 'video', height=720),
    Preset('v480', '480p', 'video', height=480),
    Preset('v360', '360p', 'video', height=360),
    Preset('best', 'Максимум', 'video'),
    Preset('a320', 'MP3 · 320 kbps', 'audio', bitrate=320),
    Preset('a192', 'MP3 · 192 kbps', 'audio', bitrate=192),
    Preset('a128', 'MP3 · 128 kbps', 'audio', bitrate=128),
)
PRESETS = {p.id: p for p in _PRESET_LIST}
# То, что можно выбрать как «формат по умолчанию» в настройках
DEFAULT_CHOICES = [p for p in _PRESET_LIST if p.id != 'best']

VIDEO_TIERS = (1080, 720, 480, 360)


def available_presets(heights: set[int], has_audio: bool) -> list[Preset]:
    """Оставляем только те пресеты, которые реально есть у этого ролика.

    Пресет «720p» показываем, если есть формат с высотой в диапазоне
    (480, 720]. Так «720p» гарантированно найдёт что скачать.
    """
    result: list[Preset] = []
    for i, h in enumerate(VIDEO_TIERS):
        lower = VIDEO_TIERS[i + 1] if i + 1 < len(VIDEO_TIERS) else 0
        if any(lower < x <= h for x in heights):
            result.append(PRESETS[f'v{h}'])
    # Ролик только в 1440p/4K (или нестандартный размер) — даём «Максимум»
    if heights and (not result or max(heights) > VIDEO_TIERS[0]):
        result.append(PRESETS['best'])
    if has_audio:
        result += [PRESETS[i] for i in ('a320', 'a192', 'a128')]
    return result


def pick_default(available: list[Preset], default_id: str) -> str:
    """Выбираем предпочтительный пресет из доступных (ближайший к настройке)."""
    ids = [p.id for p in available]
    if default_id in ids:
        return default_id
    want = PRESETS.get(default_id)
    if want and want.kind == 'video':
        videos = [p for p in available if p.kind == 'video' and p.height]
        lower = [p for p in videos if p.height <= want.height]
        if lower:
            return max(lower, key=lambda p: p.height).id
        if videos:
            return min(videos, key=lambda p: p.height).id
    return ids[0] if ids else ''


# --------------------------------------------------------------------------
# Утилиты форматирования (для интерфейса)
# --------------------------------------------------------------------------

def format_size(n: Optional[float]) -> str:
    if n is None:
        return '—'
    n = float(n)
    for unit in ('Б', 'КБ', 'МБ', 'ГБ'):
        if n < 1024 or unit == 'ГБ':
            text = f'{n:.0f} {unit}' if unit == 'Б' else f'{n:.1f} {unit}'
            return text.replace('.', ',')
        n /= 1024
    return '—'


def format_speed(bps: Optional[float]) -> str:
    return f'{format_size(bps)}/с' if bps else ''


def format_duration(seconds: Optional[float]) -> str:
    if not seconds:
        return ''
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f'{h}:{m:02d}:{sec:02d}' if h else f'{m}:{sec:02d}'


def format_eta(seconds: Optional[float]) -> str:
    return format_duration(seconds) or '0:00'


# --------------------------------------------------------------------------
# Ошибки
# --------------------------------------------------------------------------

class DownloaderError(Exception):
    """Ошибка с готовым русским текстом для пользователя."""


_ANSI = re.compile(r'\x1b\[[0-9;]*m')

_NETWORK_MARKERS = (
    'urlopen error', 'temporary failure in name resolution', 'name or service not known',
    'timed out', 'connection reset', 'connection refused', 'network is unreachable',
    'remote end closed', 'transporterror', 'failed to resolve', 'max retries',
    'unable to download', 'getaddrinfo', 'ssl:',
)
_UNAVAILABLE_MARKERS = (
    'video unavailable', 'private video', 'this video is not available', 'has been removed',
    'sign in to confirm', 'members-only', 'members only', 'age-restricted', 'confirm your age',
    'not available in your country', 'geo restrict', 'this video is unavailable',
)


def humanize_error(exc: BaseException) -> str:
    """Превращаем техническую ошибку yt-dlp в понятное сообщение."""
    raw = _ANSI.sub('', str(exc)).strip()
    low = raw.lower()

    if 'ffmpeg' in low or 'ffprobe' in low:
        if any(s in low for s in ('not found', 'not installed', 'no such file')):
            return 'Не найден ffmpeg. Установите его: sudo apt install ffmpeg'
    if 'no space left' in low:
        return 'На диске закончилось место.'
    if 'permission denied' in low:
        return 'Нет прав на запись в выбранную папку.'
    if 'http error 429' in low:
        return 'Сайт просит подождать (слишком много запросов). Попробуйте позже.'
    if 'http error 403' in low:
        return 'Сайт отказал в доступе (403). Попробуйте обновить yt-dlp.'
    if 'http error 404' in low:
        return 'Ничего не найдено по этой ссылке (404).'
    if 'unsupported url' in low:
        return 'Эта ссылка не поддерживается.'
    if any(s in low for s in _UNAVAILABLE_MARKERS):
        return 'Видео недоступно: оно приватное, удалено или ограничено по региону/возрасту.'
    if any(s in low for s in _NETWORK_MARKERS):
        return 'Проблема с сетью. Проверьте подключение и попробуйте снова.'

    first = raw.splitlines()[0] if raw else 'неизвестная ошибка'
    first = re.sub(r'^ERROR:\s*', '', first)
    return f'Ошибка yt-dlp: {first[:160]}'


class _MemoryLogger:
    """Логгер для yt-dlp: ничего не пишет на диск и не печатает в консоль."""

    def __init__(self) -> None:
        self.last_error = ''

    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass

    def error(self, msg):
        self.last_error = str(msg)


# --------------------------------------------------------------------------
# ffmpeg
# --------------------------------------------------------------------------

def ffmpeg_available() -> bool:
    """yt-dlp нужны оба: ffmpeg (склейка/конвертация) и ffprobe (анализ)."""
    return bool(shutil.which('ffmpeg')) and bool(shutil.which('ffprobe'))


# --------------------------------------------------------------------------
# Метаданные
# --------------------------------------------------------------------------

@dataclass
class MediaInfo:
    url: str
    title: str
    channel: str
    duration: Optional[int]
    thumbnail_urls: list[str]
    presets: list[Preset]


def _thumbnail_candidates(info: dict) -> list[str]:
    """Список адресов обложки от лучшего к запасному.

    Берём только jpg/png: webp без доп. библиотек GTK может не показать.
    """
    urls: list[str] = []
    thumbs = [t for t in (info.get('thumbnails') or []) if t.get('url')]
    good = [t for t in thumbs
            if t['url'].split('?')[0].lower().endswith(('.jpg', '.jpeg', '.png'))]
    for t in reversed(good):  # yt-dlp сортирует от худшего к лучшему
        w = t.get('width')
        if not w or w <= 1280:
            urls.append(t['url'])
    urls += [t['url'] for t in reversed(good) if t['url'] not in urls]
    if info.get('thumbnail') and info['thumbnail'] not in urls:
        urls.append(info['thumbnail'])
    return urls[:4]


def fetch_info(url: str) -> MediaInfo:
    """Блокирующий вызов — запускать только из рабочего потока."""
    opts = {
        'quiet': True, 'no_warnings': True, 'skip_download': True,
        'noplaylist': True, 'socket_timeout': 20, 'logger': _MemoryLogger(),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001 — yt-dlp бросает много разных исключений
        raise DownloaderError(humanize_error(exc)) from None

    if not info:
        raise DownloaderError('Не удалось получить данные по ссылке.')
    if info.get('_type') == 'playlist':
        raise DownloaderError('Это плейлист. Вставьте ссылку на одно видео.')
    if info.get('is_live'):
        raise DownloaderError('Это прямая трансляция — её нельзя скачать целиком.')

    heights: set[int] = set()
    has_audio = False
    formats = info.get('formats') or []
    for f in formats:
        if f.get('ext') == 'mhtml':  # раскадровки — не медиа
            continue
        if f.get('vcodec') != 'none' and f.get('height'):
            heights.add(int(f['height']))
        if f.get('acodec') != 'none':
            has_audio = True
    if not formats:  # некоторые сайты отдают один прямой файл без списка форматов
        if info.get('height'):
            heights.add(int(info['height']))
        has_audio = True

    presets = available_presets(heights, has_audio)
    if not presets:
        raise DownloaderError('Не нашлось подходящих форматов для скачивания.')

    return MediaInfo(
        url=url,
        title=info.get('title') or 'Без названия',
        channel=info.get('channel') or info.get('uploader') or 'Неизвестный автор',
        duration=int(info['duration']) if info.get('duration') else None,
        thumbnail_urls=_thumbnail_candidates(info),
        presets=presets,
    )


def fetch_thumbnail(urls: list[str]) -> Optional[bytes]:
    """Скачивает обложку (первый рабочий адрес). Блокирующий вызов."""
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read(6 * 1024 * 1024)
            if data:
                return data
        except Exception:  # noqa: BLE001 — обложка не критична
            continue
    return None


# --------------------------------------------------------------------------
# Параметры yt-dlp
# --------------------------------------------------------------------------

def build_options(preset: Preset, dest_dir: str, logger, progress_hook, pp_hook) -> dict:
    opts: dict = {
        'outtmpl': os.path.join(dest_dir, '%(title)s [%(id)s].%(ext)s'),
        'trim_file_name': 200,
        'noplaylist': True,
        'quiet': True, 'no_warnings': True, 'noprogress': True,
        'logger': logger,
        'progress_hooks': [progress_hook],
        'postprocessor_hooks': [pp_hook],
        # .part-файлы остаются на диске — благодаря этому работает «пауза»
        'continuedl': True,
        'retries': 5, 'fragment_retries': 5, 'socket_timeout': 20,
        'overwrites': False,
    }
    if preset.kind == 'audio':
        opts['format'] = 'bestaudio/best'
        opts['postprocessors'] = [
            {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3',
             'preferredquality': str(preset.bitrate)},
            {'key': 'FFmpegMetadata', 'add_metadata': True},
        ]
    else:
        if preset.height:
            h = preset.height
            opts['format'] = f'bv*[height<={h}]+ba/b[height<={h}]/b'
        else:
            opts['format'] = 'bv*+ba/b'
        # Сначала разрешение, затем предпочитаем h264+aac — они играют везде
        opts['format_sort'] = ['res', 'vcodec:h264', 'acodec:aac']
        opts['merge_output_format'] = 'mp4'
        opts['postprocessors'] = [{'key': 'FFmpegMetadata', 'add_metadata': True}]
    return opts


def remove_partial_files(paths) -> None:
    """Удаляет недокачанные файлы (.part, .ytdl, фрагменты) после отмены."""
    suffixes = ('.part', '.ytdl', '-Frag', '.temp')
    for path in set(paths):
        folder, base = os.path.split(path)
        try:
            for entry in os.scandir(folder):
                tail = entry.name[len(base):]
                if entry.is_file() and entry.name.startswith(base) and (not tail or tail.startswith(suffixes)):
                    try:
                        os.remove(entry.path)
                    except OSError:
                        pass
        except OSError:
            pass


# --------------------------------------------------------------------------
# Задача загрузки (один поток на одну загрузку)
# --------------------------------------------------------------------------

class _StopDownload(DownloadCancelled):
    """Бросаем из хука, чтобы прервать загрузку (пауза или отмена)."""


class DownloadJob:
    """Одна загрузка в отдельном потоке.

    Пауза сделана через остановку с сохранением .part-файлов: при «Продолжить»
    создаётся новая задача, и yt-dlp докачивает файл с того же места.
    Так соединение не висит часами в ожидании и не рвётся по таймауту.
    """

    def __init__(self, url: str, preset: Preset, dest_dir: str, emit: Callable[[dict], None]):
        self.url = url
        self.preset = preset
        self.dest_dir = dest_dir
        self._emit = emit
        self._pause = threading.Event()
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seen: set[str] = set()      # файлы, которые начали качаться в этой задаче
        self._finished: set[str] = set()  # потоки (видео/аудио), докачанные до конца
        self._last_emit = 0.0

    # -- управление --------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def pause(self) -> None:
        self._pause.set()

    def cancel(self) -> None:
        self._cancel.set()

    # -- рабочий поток -----------------------------------------------------
    def _run(self) -> None:
        if not ffmpeg_available():
            self._emit({'type': 'error',
                        'message': 'Не найден ffmpeg. Установите его: sudo apt install ffmpeg'})
            return
        try:
            os.makedirs(self.dest_dir, exist_ok=True)
        except OSError:
            self._emit({'type': 'error', 'message': 'Не удалось создать папку для загрузки.'})
            return

        logger = _MemoryLogger()
        opts = build_options(self.preset, self.dest_dir, logger,
                             self._on_progress, self._on_postprocess)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(self.url, download=True)
                if not info:
                    raise DownloaderError('Загрузка не началась: нет данных.')
                downloads = info.get('requested_downloads') or []
                path = (downloads[0].get('filepath') if downloads else None) \
                    or info.get('filepath') or ydl.prepare_filename(info)
            self._emit({'type': 'done', 'path': path})
        except Exception as exc:  # noqa: BLE001
            if self._cancel.is_set():
                self._emit({'type': 'cancelled'})
            elif self._pause.is_set():
                self._emit({'type': 'paused'})
            elif isinstance(exc, DownloaderError):
                self._emit({'type': 'error', 'message': str(exc)})
            else:
                self._emit({'type': 'error', 'message': humanize_error(exc)})

    def _on_progress(self, d: dict) -> None:
        # Проверка флагов на каждом вызове хука — так пауза/отмена срабатывают быстро
        if self._cancel.is_set():
            raise _StopDownload('Отменено')
        if self._pause.is_set():
            raise _StopDownload('Пауза')

        status = d.get('status')
        fname = d.get('filename') or ''
        info_dict = d.get('info_dict') or {}
        streams = len(info_dict.get('requested_formats') or []) or 1

        if status == 'finished':
            if fname:
                self._finished.add(fname)
            frac = min(len(self._finished) / streams, 0.99)
            self._emit({'type': 'progress', 'fraction': frac, 'speed': None, 'eta': None})
            return
        if status != 'downloading':
            return

        if fname and fname not in self._seen:
            self._seen.add(fname)
            self._emit({'type': 'file', 'path': fname})

        now = time.monotonic()
        if now - self._last_emit < 0.2:  # не заваливаем интерфейс событиями
            return
        self._last_emit = now

        done = d.get('downloaded_bytes') or 0
        total = d.get('total_bytes') or d.get('total_bytes_estimate')
        if total:
            part = done / total
        elif d.get('fragment_count'):
            part = (d.get('fragment_index') or 0) / d['fragment_count']
        else:
            part = 0.0
        frac = min((len(self._finished) + min(part, 1.0)) / streams, 0.99)
        self._emit({'type': 'progress', 'fraction': frac,
                    'speed': d.get('speed'), 'eta': d.get('eta')})

    def _on_postprocess(self, d: dict) -> None:
        if d.get('status') != 'started':
            return
        name = str(d.get('postprocessor') or '')
        if 'Merge' in name:
            label = 'Склейка видео и звука…'
        elif 'ExtractAudio' in name:
            label = 'Конвертация в MP3…'
        else:
            return
        self._emit({'type': 'processing', 'label': label})
