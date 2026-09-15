# plate_mask.py
# ============================================================
# Закраска номерных знаков на фото через ONNX (YOLOv8)
# - Модель: yolov8_plate_fp16.onnx (42.6 МБ fp32 → 21.3 МБ fp16)
# - Работает без torch, только onnxruntime + opencv
# - Если номера нет — фото не меняется
# ============================================================

import io
import os
import logging
from typing import Optional, List, Tuple

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# Флаги доступности
ONNX_AVAILABLE = False
_session = None

try:
    import onnxruntime as ort
    ONNX_AVAILABLE = True
except ImportError:
    logger.warning('⚠️ onnxruntime не установлен — закраска номеров недоступна')


def get_session(model_path: str = '/app/yolov8_plate_fp16.onnx'):
    """Загружает ONNX-модель один раз и кэширует."""
    global _session
    if _session is not None:
        return _session

    if not ONNX_AVAILABLE:
        return None

    if not os.path.exists(model_path):
        logger.error(f'❌ Модель не найдена: {model_path}')
        # Альтернативные пути
        for alt in ['yolov8_plate_fp16.onnx', './yolov8_plate_fp16.onnx',
                    '/app/data/yolov8_plate_fp16.onnx']:
            if os.path.exists(alt):
                model_path = alt
                logger.info(f'✅ Модель найдена по пути: {alt}')
                break
        else:
            return None

    try:
        logger.info(f'🤖 Загрузка ONNX-модели: {model_path}')
        # Используем CPU
        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        opts.intra_op_num_threads = 2  # Bothost Pro — 4 vCPU, оставляем запас

        _session = ort.InferenceSession(
            model_path,
            sess_options=opts,
            providers=['CPUExecutionProvider']
        )
        logger.info('✅ ONNX-модель загружена')
        return _session
    except Exception as e:
        logger.exception(f'❌ Ошибка загрузки модели: {e}')
        return None


def letterbox(img: np.ndarray, new_shape: Tuple[int, int] = (640, 640),
              color: Tuple[int, int, int] = (114, 114, 114)) -> Tuple[np.ndarray, float, Tuple[int, int]]:
    """Ресайз с сохранением пропорций (YOLO-letterbox)."""
    shape = img.shape[:2]
    r = min(new_shape[0] / shape[0], new_shape[1] / shape[1])
    new_unpad = (int(round(shape[1] * r)), int(round(shape[0] * r)))
    dw = new_shape[1] - new_unpad[0]
    dh = new_shape[0] - new_unpad[1]
    dw //= 2
    dh //= 2

    if shape[::-1] != new_unpad:
        img = cv2.resize(img, new_unpad, interpolation=cv2.INTER_LINEAR)

    top, bottom = dh, dh
    left, right = dw, dw
    img = cv2.copyMakeBorder(img, top, bottom, left, right,
                             cv2.BORDER_CONSTANT, value=color)
    return img, r, (dw, dh)


def mask_plate(image_bytes: bytes,
               model_path: str = '/app/yolov8_plate_fp16.onnx',
               confidence: float = 0.4,
               padding: int = 3,
               imgsz: int = 640) -> bytes:
    """
    Находит номера на фото и закрашивает чёрным.
    Возвращает JPEG в байтах.
    Если номеров нет — возвращает оригинал без изменений.
    """
    session = get_session(model_path)
    if session is None:
        logger.warning('⚠️ ONNX-модель недоступна, возвращаю оригинал')
        return image_bytes

    try:
        # Декодируем
        arr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return image_bytes

        orig_h, orig_w = img.shape[:2]

        # Letterbox до 640x640
        img_lb, ratio, (dw, dh) = letterbox(img, (imgsz, imgsz))

        # BGR → RGB, нормализация
        img_rgb = cv2.cvtColor(img_lb, cv2.COLOR_BGR2RGB)
        img_norm = img_rgb.astype(np.float32) / 255.0

        # HWC → CHW → NCHW
        img_input = np.transpose(img_norm, (2, 0, 1))[np.newaxis, ...]

        # Инференс
        input_name = session.get_inputs()[0].name
        outputs = session.run(None, {input_name: img_input})
        predictions = outputs[0]  # (1, 5, 8400)

        # predictions[0] — (5, 8400): [x, y, w, h, conf] × 8400 боксов
        preds = predictions[0].T  # (8400, 5)

        masked_count = 0
        for pred in preds:
            x, y, w, h, conf = pred[0], pred[1], pred[2], pred[3], pred[4]
            if conf < confidence:
                continue

            # xywh (центр) → xyxy (углы), в координатах letterbox
            x1_lb = x - w / 2
            y1_lb = y - h / 2
            x2_lb = x + w / 2
            y2_lb = y + h / 2

            # Обратно из letterbox в оригинал
            x1 = int((x1_lb - dw) / ratio)
            y1 = int((y1_lb - dh) / ratio)
            x2 = int((x2_lb - dw) / ratio)
            y2 = int((y2_lb - dh) / ratio)

            # Расширяем на padding
            x1 = max(0, x1 - padding)
            y1 = max(0, y1 - padding)
            x2 = min(orig_w, x2 + padding)
            y2 = min(orig_h, y2 + padding)

            # Закрашиваем чёрным
            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 0, 0), -1)
            masked_count += 1

        if masked_count == 0:
            logger.info('  ⏭️ Номера не найдены — фото без изменений')
            return image_bytes

        logger.info(f'  🎭 Закрашено номеров: {masked_count}')

        # Кодируем в JPEG
        _, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return buf.tobytes()

    except Exception as e:
        logger.exception(f'❌ Ошибка закраски: {e}')
        return image_bytes
