# plate_mask.py
# ============================================================
# Закраска номерных знаков на фото через YOLO
# - Модель загружается ОДИН РАЗ при первом вызове
# - Работает без EasyOCR (только детекция + закраска)
# - Если номера нет — фото не меняется
# ============================================================

import io
import os
import logging

from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)

# Флаги доступности
YOLO_AVAILABLE = False
_model = None

try:
    from ultralytics import YOLO
    YOLO_AVAILABLE = True
except ImportError:
    logger.warning('⚠️ ultralytics не установлен — закраска номеров недоступна')


def get_model(model_path: str = '/app/yolov8_plate.pt'):
    """Загружает YOLO-модель один раз и кэширует."""
    global _model
    if _model is not None:
        return _model

    if not YOLO_AVAILABLE:
        return None

    if not os.path.exists(model_path):
        logger.error(f'❌ Модель не найдена: {model_path}')
        # Пробуем альтернативные пути
        for alt in ['yolov8_plate.pt', './yolov8_plate.pt', '/app/data/yolov8_plate.pt']:
            if os.path.exists(alt):
                model_path = alt
                logger.info(f'✅ Модель найдена по пути: {alt}')
                break
        else:
            return None

    try:
        logger.info(f'🤖 Загрузка YOLO-модели: {model_path}')
        _model = YOLO(model_path)
        logger.info('✅ Модель YOLO загружена')
        return _model
    except Exception as e:
        logger.exception(f'❌ Ошибка загрузки модели: {e}')
        return None


def mask_plate(image_bytes: bytes,
               model_path: str = '/app/yolov8_plate.pt',
               confidence: float = 0.5,
               padding: int = 3) -> bytes:
    """
    Находит номера на фото и закрашивает чёрным.
    
    Args:
        image_bytes: JPEG/PNG в байтах
        model_path: путь к YOLO-модели
        confidence: порог уверенности (0..1)
        padding: отступ вокруг номера (пиксели)
    
    Returns:
        JPEG в байтах. Если номеров нет — возвращает оригинал без изменений.
    """
    model = get_model(model_path)
    if model is None:
        logger.warning('⚠️ Модель недоступна, возвращаю оригинал')
        return image_bytes

    try:
        # Загружаем изображение
        img = Image.open(io.BytesIO(image_bytes)).convert('RGB')

        # Детектим
        results = model(img, verbose=False)

        masked_count = 0
        for r in results:
            boxes = r.boxes
            if boxes is None:
                continue
            for box in boxes:
                conf = float(box.conf[0]) if box.conf is not None else 0
                if conf < confidence:
                    continue

                # Координаты
                x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())

                # Расширяем на padding
                x1 = max(0, x1 - padding)
                y1 = max(0, y1 - padding)
                x2 = min(img.width, x2 + padding)
                y2 = min(img.height, y2 + padding)

                # Закрашиваем чёрным
                draw = ImageDraw.Draw(img)
                draw.rectangle([x1, y1, x2, y2], fill='black')
                masked_count += 1

        if masked_count == 0:
            logger.info('  ⏭️ Номера не найдены — фото без изменений')
            return image_bytes

        logger.info(f'  🎭 Закрашено номеров: {masked_count}')

        # Сохраняем в JPEG
        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=85, optimize=True)
        return buf.getvalue()

    except Exception as e:
        logger.exception(f'❌ Ошибка закраски: {e}')
        return image_bytes  # fallback — оригинал
