# video_maker.py
# ============================================================
# Сборка видео из фото через OpenCV (cv2.VideoWriter).
# - НЕ требует moviepy и ffmpeg.
# - Размер видео определяется автоматически по первому фото.
# - Ничего не обрезается, фото занимает весь кадр.
# - Для предпросмотра создаёт GIF первых секунд.
# ============================================================

import os
import logging
from typing import Optional, Dict, List, Tuple, Callable

import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

MOVIEPY_AVAILABLE = True
MOVIEPY_IMPORT_ERROR = None
MOVIEPY_VERSION = f"OpenCV {cv2.__version__}"

DEFAULT_DURATION = 3.0
DEFAULT_FPS = 25
# Оставляем None, чтобы размер определялся по фото
DEFAULT_TARGET_SIZE = None
DEFAULT_OUTPUT_NAME = 'video_ad.mp4'

IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp')


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


def _fit_to_canvas(img, target_size: Tuple[int, int]) -> np.ndarray:
    """
    Масштабирует фото так, чтобы оно заполнило весь кадр БЕЗ обрезки.
    Если пропорции не совпадают, фото немного растягивается.
    """
    tw, th = target_size
    # cv2.resize растянет картинку ровно до (tw, th)
    return cv2.resize(img, (tw, th), interpolation=cv2.INTER_AREA)


def _apply_kenburns_cv2(img, frames: int, zoom_factor: float = 1.08):
    """Плавный zoom-in (Ken Burns)."""
    th, tw = img.shape[:2]
    for i in range(frames):
        progress = i / max(1, frames - 1)
        scale = 1.0 + (zoom_factor - 1.0) * progress

        new_w = int(tw / scale)
        new_h = int(th / scale)
        x1 = (tw - new_w) // 2
        y1 = (th - new_h) // 2

        crop = img[y1:y1+new_h, x1:x1+new_w]
        frame = cv2.resize(crop, (tw, th), interpolation=cv2.INTER_LINEAR)
        yield frame


def process_one_folder(
    folder_path: str,
    *,
    duration: float = DEFAULT_DURATION,
    fps: int = DEFAULT_FPS,
    target_size: Optional[Tuple[int, int]] = None,
    output_name: str = DEFAULT_OUTPUT_NAME,
    delete_photos_after: bool = True,
    progress_cb: Optional[Callable[[str], None]] = None,
    **kwargs,
) -> Tuple[bool, str]:
    if progress_cb is None:
        progress_cb = lambda m: logger.info(m)

    photos = _list_photos(folder_path)
    if not photos:
        return False, 'Нет фото в папке'

    progress_cb(f'  📸 Фото: {len(photos)}')
    
    # === ОПРЕДЕЛЯЕМ РАЗМЕР ПО ПЕРВОМУ ФОТО ===
    if target_size is None:
        first_img = cv2.imread(photos[0])
        if first_img is None:
            return False, 'Не удалось прочитать первое фото для определения размера'
        h, w = first_img.shape[:2]
        # Для mp4 ширина и высота должны быть кратны 2
        tw = w + (w % 2)
        th = h + (h % 2)
        target_size = (tw, th)
        progress_cb(f'  📐 Размер видео (по первому фото): {tw}x{th}')
    else:
        tw, th = target_size
        tw = tw + (tw % 2)
        th = th + (th % 2)

    if not output_name.lower().endswith('.mp4'):
        output_name += '.mp4'
    output_path = os.path.join(folder_path, output_name)
    counter = 1
    while os.path.exists(output_path):
        base, ext = os.path.splitext(output_name)
        output_path = os.path.join(folder_path, f'{base}_{counter}{ext}')
        counter += 1

    tmp_video_path = output_path + '.tmp.mp4'
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(tmp_video_path, fourcc, fps, (tw, th))

    if not out.isOpened():
        return False, 'Не удалось открыть VideoWriter (возможно, нет кодека mp4v)'

    frames_per_photo = int(duration * fps)
    total_frames = len(photos) * frames_per_photo

    try:
        progress_cb(f'  💾 Рендер {len(photos)} фото ({total_frames} кадров) → {os.path.basename(output_path)}')
        processed = 0
        preview_frames = []

        for i, path in enumerate(photos):
            img = cv2.imread(path)
            if img is None:
                progress_cb(f'  ⚠️ Не прочитать: {os.path.basename(path)}')
                continue

            # Подгоняем под размер видео (растягиваем, чтобы не было обрезки)
            base_img = _fit_to_canvas(img, (tw, th))

            # Zoom-in
            for frame in _apply_kenburns_cv2(base_img, frames_per_photo, zoom_factor=1.08):
                out.write(frame)
                processed += 1
                if len(preview_frames) < fps * 3:
                    small = cv2.resize(frame, (tw // 4, th // 4))
                    preview_frames.append(small)

            # Плавный переход
            if i < len(photos) - 1:
                next_img = cv2.imread(photos[i + 1])
                if next_img is not None:
                    next_base = _fit_to_canvas(next_img, (tw, th))
                    trans_frames = int(kwargs.get('transition', 0.5) * fps)
                    for j in range(trans_frames):
                        alpha = j / max(1, trans_frames - 1)
                        blended = cv2.addWeighted(base_img, 1 - alpha, next_base, alpha, 0)
                        out.write(blended)
                        processed += 1
                        if len(preview_frames) < fps * 3:
                            small = cv2.resize(blended, (tw // 4, th // 4))
                            preview_frames.append(small)

            if (i + 1) % 5 == 0:
                progress_cb(f'  ⏳ Обработано {i + 1}/{len(photos)} фото')

        out.release()

        if processed == 0:
            if os.path.exists(tmp_video_path):
                os.remove(tmp_video_path)
            return False, 'Не удалось обработать ни одного кадра'

        if os.path.exists(output_path):
            os.remove(output_path)
        os.rename(tmp_video_path, output_path)

        # === Генерируем GIF-превью (первые 2 сек) ===
        preview_path = os.path.join(folder_path, 'preview.gif')
        try:
            if preview_frames:
                gif_frames = []
                for frame in preview_frames[:int(fps * 2)]:
                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    gif_frames.append(Image.fromarray(rgb))
                if gif_frames:
                    gif_frames[0].save(
                        preview_path,
                        save_all=True,
                        append_images=gif_frames[1:],
                        duration=int(1000 / fps),
                        loop=0,
                    )
                    progress_cb(f'  🎞️ GIF-превью: {os.path.basename(preview_path)}')
        except Exception as e:
            progress_cb(f'  ⚠️ GIF-превью не создано: {e}')

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
            progress_cb(f'  🗑️ Удалено исходных фото: {deleted}')

        return True, f'✅ {os.path.basename(output_path)} ({final_size:.1f} МБ)'

    except Exception as e:
        logger.exception(f'❌ Ошибка рендера {folder_path}: {e}')
        if os.path.exists(tmp_video_path):
            try:
                os.remove(tmp_video_path)
            except Exception:
                pass
        return False, f'Ошибка рендера: {e}'
    finally:
        try:
            out.release()
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
    """
    Проходит по pending-объявлениям и собирает видео для каждого.
    """
    report = {'total': 0, 'ok': 0, 'failed': 0, 'skipped': 0, 'details': []}

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
                fps=kwargs.get('fps', DEFAULT_FPS),
                target_size=kwargs.get('target_size'),
                output_name=kwargs.get('output_name', DEFAULT_OUTPUT_NAME),
                delete_photos_after=kwargs.get('delete_photos_after', True),
                progress_cb=_log,
                **kwargs
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
