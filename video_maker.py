# video_maker.py
# ============================================================
# Серверная сборка видео из фото папки (адаптация reels_maker.py).
#
# - Работает без GUI.
# - Использует moviepy + СИСТЕМНЫЙ ffmpeg (FFMPEG_BINARY=/usr/bin/ffmpeg).
# - imageio-ffmpeg НЕ используется (на Bothost падает с execstack).
# - Zoom-эффект + размытие фона + опционально логотип/музыка/сжатие.
# - После успешной сборки УДАЛЯЕТ исходные фото из папки.
# - Возвращает отчёт для админки.
# ============================================================

import os
import shutil
import logging
import tempfile
import traceback
from typing import Optional, Dict, List, Tuple, Callable

logger = logging.getLogger(__name__)

# === Ленивый импорт moviepy (чтобы app.py не падал, если пакета нет) ===
MOVIEPY_AVAILABLE = False
MOVIEPY_IMPORT_ERROR = None
MOVIEPY_VERSION = None

# === Принудительно используем системный ffmpeg ===
# imageio-ffmpeg (который тянет свой бинарник) на Bothost падает с execstack.
# Через переменную окружения FFMPEG_BINARY moviepy будет искать системный ffmpeg.
os.environ.setdefault('FFMPEG_BINARY', '/usr/bin/ffmpeg')
os.environ.setdefault('IMAGEIO_FFMPEG_EXE', '/usr/bin/ffmpeg')

try:
    from moviepy.editor import (
        ImageClip, AudioFileClip, CompositeVideoClip,
        concatenate_videoclips, VideoFileClip,
    )
    from moviepy.video.fx.all import gaussian_blur
    MOVIEPY_AVAILABLE = True
    try:
        import moviepy
        MOVIEPY_VERSION = getattr(moviepy, '__version__', '?')
    except Exception:
        MOVIEPY_VERSION = '?'
    logger.info(f'✅ moviepy загружен (v{MOVIEPY_VERSION})')
except Exception as e:
    MOVIEPY_IMPORT_ERROR = f"{type(e).__name__}: {e}"
    logger.warning(f'⚠️ moviepy недоступен: {MOVIEPY_IMPORT_ERROR}. Кнопка «Создать видео» не будет работать.')
    logger.debug(traceback.format_exc())

from PIL import Image


# ============================================================
# Настройки по умолчанию
# ============================================================

DEFAULT_DURATION = 3.0
DEFAULT_TRANSITION = 0.5
DEFAULT_FPS = 30
DEFAULT_MAX_SIZE_MB = 2.5
DEFAULT_MAX_DIMENSION = 1280
DEFAULT_TARGET_SIZE = (1080, 1920)
DEFAULT_BLUR_RADIUS = 30
DEFAULT_OUTPUT_NAME = 'video_ad.mp4'
QUALITY_PRESET = 'Сбалансированное'

IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp')


# ============================================================
# Утилиты
# ============================================================

def _list_photos(folder: str) -> List[str]:
    photos = []
    try:
        for f in sorted(os.listdir(folder)):
            if '.orig.' in f:
                continue
            if f.lower().endswith(IMAGE_EXTS):
                photos.append(os.path.join(folder, f))
    except Exception as e:
        logger.error(f'❌ Не прочитать папку {folder}: {e}')
    return photos


def _compress_image(image_path: str,
                    max_dimension: int = DEFAULT_MAX_DIMENSION,
                    quality: int = 75) -> str:
    try:
        with Image.open(image_path) as img:
            if img.mode in ('RGBA', 'LA', 'P'):
                rgb = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                if img.mode == 'RGBA':
                    rgb.paste(img, mask=img.split()[-1])
                img = rgb
            elif img.mode != 'RGB':
                img = img.convert('RGB')

            w, h = img.size
            if w > max_dimension or h > max_dimension:
                ratio = min(max_dimension / w, max_dimension / h)
                new_w, new_h = int(w * ratio), int(h * ratio)
                img = img.resize((new_w, new_h), Image.LANCZOS)

            temp_dir = tempfile.gettempdir()
            temp_path = os.path.join(
                temp_dir,
                f"vm_compressed_{os.getpid()}_{os.path.basename(image_path).rsplit('.', 1)[0]}.jpg"
            )
            img.save(temp_path, 'JPEG', quality=quality, optimize=True, progressive=True)
            return temp_path
    except Exception as e:
        logger.warning(f'⚠️ Не сжать {image_path}: {e}')
        return image_path


def _get_video_size(folder: str, images: List[str],
                    target_size: Optional[Tuple[int, int]] = None) -> Tuple[int, int]:
    if target_size:
        return target_size
    try:
        sizes = []
        for p in images[:20]:
            try:
                with Image.open(p) as img:
                    sizes.append(img.size)
            except Exception:
                continue
        if not sizes:
            return (1080, 1920)

        vertical = sum(1 for w, h in sizes if h > w)
        horizontal = sum(1 for w, h in sizes if w > h)

        widths = sorted(s[0] for s in sizes)
        heights = sorted(s[1] for s in sizes)
        mw = widths[len(widths) // 2]
        mh = heights[len(heights) // 2]

        if vertical >= horizontal:
            return (min(mw, DEFAULT_MAX_DIMENSION), min(mh, DEFAULT_MAX_DIMENSION))
        else:
            return (min(mw, DEFAULT_MAX_DIMENSION), min(mh, DEFAULT_MAX_DIMENSION))
    except Exception as e:
        logger.warning(f'⚠️ Не определить размер: {e}')
        return (1080, 1920)


def _apply_blur(clip, radius: int = DEFAULT_BLUR_RADIUS):
    try:
        return clip.fx(gaussian_blur, radius)
    except Exception:
        try:
            small = clip.resize(newsize=(max(2, clip.w // 10), max(2, clip.h // 10)))
            return small.resize(newsize=(clip.w, clip.h))
        except Exception:
            return clip


def _resize_with_blur(clip, target_size: Tuple[int, int],
                      blur_radius: int = DEFAULT_BLUR_RADIUS):
    try:
        cw, ch = clip.size
        tw, th = target_size

        scale = max(tw / cw, th / ch)
        nw, nh = int(cw * scale), int(ch * scale)
        nw = nw if nw % 2 == 0 else nw + 1
        nh = nh if nh % 2 == 0 else nh + 1

        resized = clip.resize(newsize=(nw, nh))
        bg = clip.resize(newsize=target_size)
        bg = _apply_blur(bg, blur_radius)

        return CompositeVideoClip([bg, resized.set_position('center')], size=target_size)
    except Exception as e:
        logger.warning(f'⚠️ resize_with_blur: {e}')
        return clip.resize(target_size)


def _apply_zoom(clip, duration: float, zoom_factor: float = 1.15):
    def zoom_func(t):
        progress = min(1.0, t / duration) if duration > 0 else 1.0
        return 1.0 + (zoom_factor - 1.0) * progress
    try:
        return clip.resize(zoom_func)
    except Exception:
        return clip


def _add_logo(clip, logo_path: str, target_size: Tuple[int, int],
              opacity: float = 0.6, scale: float = 0.15):
    if not logo_path or not os.path.exists(logo_path):
        return clip
    try:
        logo_clip = ImageClip(logo_path)
        logo_w = int(target_size[0] * scale)
        logo_h = int(logo_clip.size[1] * (logo_w / logo_clip.size[0]))
        logo_clip = logo_clip.resize((logo_w, logo_h)).set_opacity(opacity)
        margin = 20
        pos = (target_size[0] - logo_w - margin, target_size[1] - logo_h - margin)
        logo_clip = logo_clip.set_duration(clip.duration)
        return CompositeVideoClip([clip, logo_clip.set_position(pos)])
    except Exception as e:
        logger.warning(f'⚠️ Не добавить логотип: {e}')
        return clip


def _compress_video(input_path: str, output_path: str,
                    target_size_mb: float = DEFAULT_MAX_SIZE_MB) -> bool:
    try:
        video = VideoFileClip(input_path)
        current_size = os.path.getsize(input_path) / (1024 * 1024)

        if current_size <= target_size_mb:
            video.close()
            shutil.copy2(input_path, output_path)
            return True

        duration = video.duration or 1
        target_bitrate = int((target_size_mb * 1024 * 1024 * 8) / duration * 0.9)
        target_bitrate = max(500 * 1000, min(target_bitrate, 8000 * 1000))

        video.write_videofile(
            output_path,
            fps=video.fps or DEFAULT_FPS,
            codec='libx264',
            audio_codec='aac',
            bitrate=f'{target_bitrate}',
            preset='medium',
            ffmpeg_params=['-crf', '26', '-pix_fmt', 'yuv420p', '-movflags', '+faststart'],
            verbose=False,
            logger=None,
        )
        video.close()
        return True
    except Exception as e:
        logger.error(f'❌ Ошибка сжатия видео: {e}')
        try:
            shutil.copy2(input_path, output_path)
        except Exception:
            pass
        return False


# ============================================================
# Обработка одной папки
# ============================================================

def process_one_folder(
    folder_path: str,
    *,
    duration: float = DEFAULT_DURATION,
    transition: float = DEFAULT_TRANSITION,
    fps: int = DEFAULT_FPS,
    target_size: Optional[Tuple[int, int]] = None,
    max_size_mb: float = DEFAULT_MAX_SIZE_MB,
    blur_radius: int = DEFAULT_BLUR_RADIUS,
    logo_path: Optional[str] = None,
    logo_opacity: float = 0.6,
    logo_scale: float = 0.15,
    music_file: Optional[str] = None,
    compress: bool = True,
    delete_photos_after: bool = True,
    output_name: str = DEFAULT_OUTPUT_NAME,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, str]:
    if not MOVIEPY_AVAILABLE:
        return False, f'moviepy не установлен: {MOVIEPY_IMPORT_ERROR or "unknown"}'

    if progress_cb is None:
        progress_cb = lambda m: logger.info(m)

    photos = _list_photos(folder_path)
    if len(photos) < 1:
        return False, 'Нет фото в папке'
    if len(photos) == 1:
        progress_cb(f'  ⚠️ Только 1 фото — сделаю «видео» из одного кадра')

    progress_cb(f'  📸 Фото: {len(photos)}')

    temp_photos: List[str] = []
    use_photos = []
    for p in photos:
        cp = _compress_image(p, DEFAULT_MAX_DIMENSION, 75)
        temp_photos.append(cp)
        use_photos.append(cp)

    size = _get_video_size(folder_path, use_photos, target_size)
    progress_cb(f'  📐 Размер видео: {size[0]}x{size[1]}')

    audio_clip = None
    music_duration = None
    if music_file and os.path.exists(music_file):
        try:
            audio_clip = AudioFileClip(music_file)
            music_duration = audio_clip.duration
            progress_cb(f'  🎵 Музыка: {os.path.basename(music_file)} ({music_duration:.1f} сек)')
        except Exception as e:
            progress_cb(f'  ⚠️ Ошибка загрузки музыки: {e}')
            audio_clip = None

    total_photos_needed = len(use_photos)
    if music_duration and music_duration > 0:
        effective = duration + transition * 0.5
        total_photos_needed = max(len(use_photos), int(music_duration / effective) + 2)
        progress_cb(f'  🔄 Для музыки {music_duration:.1f}с нужно {total_photos_needed} кадров')

    sequence = []
    idx = 0
    while len(sequence) < total_photos_needed:
        sequence.append(use_photos[idx % len(use_photos)])
        idx += 1

    clips = []
    for i, path in enumerate(sequence):
        try:
            clip = ImageClip(path).set_duration(duration)
            clip = _resize_with_blur(clip, size, blur_radius)
            clip = _apply_zoom(clip, duration, zoom_factor=1.15)
            clips.append(clip)
        except Exception as e:
            progress_cb(f'  ⚠️ Фото {i+1}: {e}')
            continue

    if not clips:
        for t in temp_photos:
            try:
                if os.path.exists(t):
                    os.remove(t)
            except Exception:
                pass
        return False, 'Не удалось обработать ни одного фото'

    if transition > 0 and len(clips) > 1:
        faded = []
        for i, c in enumerate(clips):
            if i == 0:
                faded.append(c.crossfadein(transition))
            elif i == len(clips) - 1:
                faded.append(c.crossfadeout(transition))
            else:
                faded.append(c.crossfadein(transition).crossfadeout(transition))
        final_video = concatenate_videoclips(faded, method="compose")
    else:
        final_video = concatenate_videoclips(clips, method="compose")

    if logo_path:
        final_video = _add_logo(final_video, logo_path, size, logo_opacity, logo_scale)

    if audio_clip:
        try:
            if final_video.duration < audio_clip.duration:
                looped = final_video
                while looped.duration < audio_clip.duration:
                    looped = concatenate_videoclips([looped, final_video], method="compose")
                final_video = looped.subclip(0, audio_clip.duration)
            elif final_video.duration > audio_clip.duration:
                final_video = final_video.subclip(0, audio_clip.duration)
            audio_clip = audio_clip.volumex(0.8)
            final_video = final_video.set_audio(audio_clip)
        except Exception as e:
            progress_cb(f'  ⚠️ Музыка: {e}')

    if not output_name.lower().endswith('.mp4'):
        output_name += '.mp4'
    output_path = os.path.join(folder_path, output_name)
    counter = 1
    while os.path.exists(output_path):
        base, ext = os.path.splitext(output_name)
        output_path = os.path.join(folder_path, f'{base}_{counter}{ext}')
        counter += 1

    tmp_video = tempfile.NamedTemporaryFile(delete=False, suffix='.mp4')
    tmp_video_path = tmp_video.name
    tmp_video.close()

    try:
        progress_cb(f'  💾 Рендер видео → {os.path.basename(output_path)}')
        final_video.write_videofile(
            tmp_video_path,
            fps=fps,
            codec='libx264',
            audio_codec='aac',
            threads=4,
            preset='medium',
            bitrate='4000k',
            ffmpeg_params=['-crf', '23', '-pix_fmt', 'yuv420p', '-movflags', '+faststart'],
            verbose=False,
            logger=None,
        )

        if compress:
            progress_cb(f'  📦 Сжатие до {max_size_mb} МБ')
            _compress_video(tmp_video_path, output_path, max_size_mb)
        else:
            shutil.copy2(tmp_video_path, output_path)

        final_size = os.path.getsize(output_path) / (1024 * 1024)
        progress_cb(f'  ✅ Готово: {os.path.basename(output_path)} ({final_size:.1f} МБ)')

        if delete_photos_after:
            deleted = 0
            for p in photos:
                try:
                    if os.path.exists(p):
                        os.remove(p)
                        deleted += 1
                except Exception:
                    pass
            for f in list(os.listdir(folder_path)):
                if '.orig.' in f:
                    try:
                        os.remove(os.path.join(folder_path, f))
                    except Exception:
                        pass
            progress_cb(f'  🗑️ Удалено исходных фото: {deleted}')

        return True, f'✅ {os.path.basename(output_path)} ({final_size:.1f} МБ)'

    except Exception as e:
        logger.exception(f'❌ Ошибка рендера {folder_path}: {e}')
        progress_cb(f'  ❌ Ошибка рендера: {e}')
        return False, f'Ошибка рендера: {e}'

    finally:
        try:
            final_video.close()
        except Exception:
            pass
        for c in clips:
            try:
                c.close()
            except Exception:
                pass
        if audio_clip:
            try:
                audio_clip.close()
            except Exception:
                pass
        for t in temp_photos:
            try:
                if os.path.exists(t):
                    os.remove(t)
            except Exception:
                pass
        try:
            if os.path.exists(tmp_video_path):
                os.remove(tmp_video_path)
        except Exception:
            pass


# ============================================================
# Пакетная обработка всей очереди
# ============================================================

def process_queue_videos(
    bot_db,
    *,
    only_ids: Optional[List[int]] = None,
    stop_flag: Optional[Callable[[], bool]] = None,
    progress_cb: Optional[Callable[[Dict], None]] = None,
    **kwargs,
) -> Dict:
    report = {'total': 0, 'ok': 0, 'failed': 0, 'skipped': 0, 'details': []}

    if not MOVIEPY_AVAILABLE:
        report['details'].append({
            'id': None, 'folder': '(все)',
            'status': 'error',
            'reason': f'moviepy не установлен: {MOVIEPY_IMPORT_ERROR or "unknown"}'
        })
        return report

    if only_ids is not None:
        ads = []
        for ad_id in only_ids:
            ad = bot_db.get_ad_by_id(ad_id)
            if ad:
                ads.append(ad)
    else:
        ads = bot_db.get_all_pending_ads()

    report['total'] = len(ads)
    if not ads:
        return report

    if progress_cb:
        progress_cb({'total': report['total'], 'done': 0, 'ok': 0, 'failed': 0,
                     'current': None, 'log': '▶️ Старт'})

    for i, ad in enumerate(ads, 1):
        if stop_flag and stop_flag():
            report['details'].append({
                'id': None, 'folder': '(прервано)',
                'status': 'skip', 'reason': 'Остановлено пользователем'
            })
            break

        ad_id = ad['id']
        media_path = ad.get('media_path') or ''
        folder_name = ad.get('folder_name') or os.path.basename(media_path) or f'#{ad_id}'

        current = {'id': ad_id, 'folder': folder_name, 'index': i}
        if progress_cb:
            progress_cb({'total': report['total'], 'done': i - 1,
                         'ok': report['ok'], 'failed': report['failed'],
                         'current': current,
                         'log': f'📁 [{i}/{report["total"]}] {folder_name}'})

        if not media_path or not os.path.isdir(media_path):
            report['failed'] += 1
            report['details'].append({
                'id': ad_id, 'folder': folder_name,
                'status': 'error', 'reason': 'Папка не найдена'
            })
            continue

        photos = _list_photos(media_path)
        if not photos:
            has_video = any(f.lower().endswith(('.mp4', '.mov', '.webm'))
                            for f in os.listdir(media_path))
            if has_video:
                report['skipped'] += 1
                report['details'].append({
                    'id': ad_id, 'folder': folder_name,
                    'status': 'skip', 'reason': 'Видео уже есть — пропуск'
                })
            else:
                report['failed'] += 1
                report['details'].append({
                    'id': ad_id, 'folder': folder_name,
                    'status': 'error', 'reason': 'Нет ни фото, ни видео'
                })
            continue

        def _log(msg: str):
            logger.info(msg)
            if progress_cb:
                progress_cb({'total': report['total'], 'done': i - 1,
                             'ok': report['ok'], 'failed': report['failed'],
                             'current': current, 'log': msg})

        try:
            ok, message = process_one_folder(
                media_path,
                duration=kwargs.get('duration', DEFAULT_DURATION),
                transition=kwargs.get('transition', DEFAULT_TRANSITION),
                fps=kwargs.get('fps', DEFAULT_FPS),
                target_size=kwargs.get('target_size'),
                max_size_mb=kwargs.get('max_size_mb', DEFAULT_MAX_SIZE_MB),
                blur_radius=kwargs.get('blur_radius', DEFAULT_BLUR_RADIUS),
                logo_path=kwargs.get('logo_path'),
                logo_opacity=kwargs.get('logo_opacity', 0.6),
                logo_scale=kwargs.get('logo_scale', 0.15),
                music_file=kwargs.get('music_file'),
                compress=kwargs.get('compress', True),
                delete_photos_after=kwargs.get('delete_photos_after', True),
                output_name=kwargs.get('output_name', DEFAULT_OUTPUT_NAME),
                progress_cb=_log,
            )
            if ok:
                report['ok'] += 1
                report['details'].append({
                    'id': ad_id, 'folder': folder_name,
                    'status': 'ok', 'reason': message
                })
            else:
                report['failed'] += 1
                report['details'].append({
                    'id': ad_id, 'folder': folder_name,
                    'status': 'error', 'reason': message
                })
        except Exception as e:
            logger.exception(f'❌ Ошибка обработки {folder_name}: {e}')
            report['failed'] += 1
            report['details'].append({
                'id': ad_id, 'folder': folder_name,
                'status': 'error', 'reason': str(e)[:300]
            })

        if progress_cb:
            progress_cb({'total': report['total'], 'done': i,
                         'ok': report['ok'], 'failed': report['failed'],
                         'current': current, 'log': '—'})

    if progress_cb:
        progress_cb({'total': report['total'], 'done': report['total'],
                     'ok': report['ok'], 'failed': report['failed'],
                     'current': None,
                     'log': f'🏁 Готово. OK: {report["ok"]}, ошибок: {report["failed"]}, пропущено: {report["skipped"]}'})

    return report
