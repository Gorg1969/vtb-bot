# plate_mask.py 3
# ============================================================
# Закраска номерных знаков через OpenCV DNN (YOLOv8 ONNX)
# - НЕ требует onnxruntime (он падает на Bothost из-за execstack)
# - Использует cv2.dnn.readNetFromONNX()
# ============================================================

import os
import logging
from typing import Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger(__name__)

_net = None
_session_loaded = False


def get_session(model_path: str = '/app/yolov8_plate_fp16.onnx'):
    """Загружает ONNX-модель через OpenCV DNN. Кэширует."""
    global _net, _session_loaded

    if _session_loaded:
        return _net

    _session_loaded = True

    # === ДИАГНОСТИКА ===
    logger.warning('=' * 60)
    logger.warning('🔍 ДИАГНОСТИКА OpenCV DNN:')
    logger.warning(f'   model_path = {model_path}')
    logger.warning(f'   exists = {os.path.exists(model_path)}')
    try:
        files = os.listdir('/app')
        onnx_files = [f for f in files if f.endswith('.onnx')]
        logger.warning(f'   ONNX в /app/: {onnx_files}')
    except Exception as e:
        logger.warning(f'   /app/ ошибка: {e}')
    logger.warning('=' * 60)

    if not os.path.exists(model_path):
        for alt in ['yolov8_plate_fp16.onnx', './yolov8_plate_fp16.onnx',
                    '/app/data/yolov8_plate_fp16.onnx']:
            if os.path.exists(alt):
                model_path = alt
                logger.warning(f'✅ Модель найдена: {alt}')
                break
        else:
            logger.error(f'❌ Модель не найдена: {model_path}')
            return None

    try:
        logger.warning(f'🤖 Загрузка через cv2.dnn: {model_path}')
        _net = cv2.dnn.readNetFromONNX(model_path)
        # Только CPU
        _net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        _net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        logger.warning('✅ OpenCV DNN модель загружена')
        return _net
    except Exception as e:
        logger.exception(f'❌ Ошибка загрузки модели: {e}')
        return None


def letterbox(img: np.ndarray, new_shape: Tuple[int, int] = (640, 640),
              color: Tuple[int, int, int] = (114, 114, 114)):
    shape = img.shape[:2]
    r = min(new_shape[0] / shape[0], new_shape[1] / shape[1])
    new_unpad = (int(round(shape[1] * r)), int(round(shape[0] * r)))
    dw = new_shape[1] - new_unpad[0]
    dh = new_shape[0] - new_unpad[1]
    dw //= 2
    dh //= 2

    if shape[::-1] != new_unpad:
        img = cv2.resize(img, new_unpad, interpolation=cv2.INTER_LINEAR)

    img = cv2.copyMakeBorder(img, dh, dh, dw, dw,
                             cv2.BORDER_CONSTANT, value=color)
    return img, r, (dw, dh)


def mask_plate(image_bytes: bytes,
               model_path: str = '/app/yolov8_plate_fp16.onnx',
               confidence: float = 0.4,
               padding: int = 3,
               imgsz: int = 640) -> bytes:
    net = get_session(model_path)
    if net is None:
        logger.warning('⚠️ OpenCV DNN модель недоступна, возвращаю оригинал')
        return image_bytes

    try:
        # Декодируем
        arr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return image_bytes

        orig_h, orig_w = img.shape[:2]

        # Letterbox 640x640
        img_lb, ratio, (dw, dh) = letterbox(img, (imgsz, imgsz))

        # BGR → RGB, нормализация, HWC → CHW
        img_rgb = cv2.cvtColor(img_lb, cv2.COLOR_BGR2RGB)
        img_norm = img_rgb.astype(np.float32) / 255.0

        # Создаём blob для OpenCV DNN
        # blobFromImage принимает HWC, возвращает NCHW
        blob = cv2.dnn.blobFromImage(
            img_rgb, scalefactor=1/255.0, size=(imgsz, imgsz),
            mean=(0, 0, 0), swapRB=False, crop=False
        )

        # Inference
        net.setInput(blob)
        outputs = net.forward()

        # YOLOv8: outputs shape (1, 5, 8400) — [x, y, w, h, conf] × 8400
        predictions = outputs[0]  # (5, 8400)
        preds = predictions.T      # (8400, 5)

        masked_count = 0
        for pred in preds:
            x, y, w, h, conf = pred[0], pred[1], pred[2], pred[3], pred[4]
            if conf < confidence:
                continue

            # xywh (центр) → xyxy
            x1_lb = x - w / 2
            y1_lb = y - h / 2
            x2_lb = x + w / 2
            y2_lb = y + h / 2

            # Обратно из letterbox в оригинал
            x1 = int((x1_lb - dw) / ratio)
            y1 = int((y1_lb - dh) / ratio)
            x2 = int((x2_lb - dw) / ratio)
            y2 = int((y2_lb - dh) / ratio)

            x1 = max(0, x1 - padding)
            y1 = max(0, y1 - padding)
            x2 = min(orig_w, x2 + padding)
            y2 = min(orig_h, y2 + padding)

            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 0, 0), -1)
            masked_count += 1

        if masked_count == 0:
            logger.info('  ⏭️ Номера не найдены — фото без изменений')
            return image_bytes

        logger.info(f'  🎭 Закрашено номеров: {masked_count}')

        _, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return buf.tobytes()

    except Exception as e:
        logger.exception(f'❌ Ошибка закраски: {e}')
        return image_bytes
