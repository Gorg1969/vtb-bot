# modules/publisher.py - финальная версия с поддержкой видео
import logging
import os
import time
import re
import requests
import threading
import json
import uuid
import base64
from datetime import datetime
import pytz

logger = logging.getLogger(__name__)

class Publisher:
    def __init__(self, api, file_manager, db):
        self.api = api
        self.fm = file_manager
        self.db = db
        self.active_publishes = {}
        self.publish_threads = {}
        self.FOLDER_TIMEOUT = 120
        self.STOP_FLAG = {}
        self.moscow_tz = pytz.timezone('Europe/Moscow')
        self.pending_messages = {}
        self.diagnostic_log = []

    def extract_chat_id_from_folder(self, folder_name):
        if not folder_name:
            return None
        
        match = re.search(r'(-?\d{10,})', folder_name)
        if match:
            chat_id = match.group(1)
            if not chat_id.startswith('-') and len(chat_id) >= 10:
                chat_id = f"-{chat_id}"
            return chat_id
        
        match = re.search(r'(\d{10,})', folder_name)
        if match:
            return f"-{match.group(1)}"
        
        return None

    def _seq_to_max_id(self, seq: int) -> str:
        try:
            seq_bytes = int(seq).to_bytes(8, byteorder='big')
            encoded = base64.urlsafe_b64encode(seq_bytes).decode('utf-8').rstrip('=')
            return encoded
        except Exception as e:
            logger.error(f"❌ Ошибка конвертации seq в MAX ID: {e}")
            return str(seq)

    def _send_and_get_id(self, chat_id, text, media_tokens, media_types=None):
        """
        Отправка сообщения с медиа (фото и/или видео)
        """
        try:
            if not self.api.token:
                return False, None
            
            if media_types is None:
                media_types = ['image'] * len(media_tokens)
            
            attachments = []
            for i, token in enumerate(media_tokens[:10]):
                media_type = media_types[i] if i < len(media_types) else 'image'
                attachments.append({
                    "type": media_type,
                    "payload": {"token": token}
                })
            
            payload = {
                "text": text,
                "format": "markdown"
            }
            
            if attachments:
                payload["attachments"] = attachments
            
            chat_id_str = str(chat_id)
            chat_id_for_api = chat_id_str if chat_id_str.startswith('-') else f"-{chat_id_str}"
            
            logger.info(f"📤 Отправка в чат {chat_id_for_api} с {len(attachments)} медиа")
            
            response = requests.post(
                f"{self.api.base_url}/messages?chat_id={chat_id_for_api}",
                headers={
                    "Authorization": self.api.token,
                    "Content-Type": "application/json"
                },
                json=payload,
                timeout=60,
                verify=False
            )
            
            logger.info(f"📨 СТАТУС ОТВЕТА: {response.status_code}")
            
            if response.status_code == 200:
                try:
                    result = response.json()
                    logger.info(f"📨 ПОЛНЫЙ JSON ОТВЕТА: {json.dumps(result, indent=2, ensure_ascii=False)}")
                    
                    seq = None
                    if isinstance(result, dict):
                        if 'message' in result and isinstance(result['message'], dict):
                            msg = result['message']
                            if 'body' in msg and isinstance(msg['body'], dict):
                                if 'seq' in msg['body']:
                                    seq = msg['body']['seq']
                                    logger.info(f"✅ Найден seq: {seq}")
                        
                        if not seq and 'seq' in result:
                            seq = result['seq']
                            logger.info(f"✅ Найден seq в корне: {seq}")
                    
                    if seq:
                        encoded_id = self._seq_to_max_id(seq)
                        post_link = f"https://max.ru/c/{chat_id_str}/{encoded_id}"
                        logger.info(f"🔗 Ссылка на пост создана: {post_link}")
                        return True, post_link
                    else:
                        logger.warning(f"⚠️ seq не найден в ответе")
                        return True, None
                        
                except json.JSONDecodeError as e:
                    logger.error(f"❌ Ошибка парсинга JSON: {e}")
                    return True, None
                except Exception as e:
                    logger.error(f"❌ Ошибка обработки ответа: {e}")
                    import traceback
                    traceback.print_exc()
                    return True, None
            else:
                logger.error(f"❌ Ошибка API: {response.status_code}")
                return False, None
                
        except Exception as e:
            logger.error(f"❌ Критическая ошибка: {e}")
            import traceback
            traceback.print_exc()
            return False, None

    def _send_to_user(self, user_id, text, media_tokens, media_types=None):
        try:
            if not self.api.token:
                return False, None
            
            if media_types is None:
                media_types = ['image'] * len(media_tokens)
            
            attachments = []
            for i, token in enumerate(media_tokens[:10]):
                media_type = media_types[i] if i < len(media_types) else 'image'
                attachments.append({
                    "type": media_type,
                    "payload": {"token": token}
                })
            
            payload = {
                "user_id": user_id,
                "text": text,
                "format": "markdown"
            }
            
            if attachments:
                payload["attachments"] = attachments
            
            logger.info(f"📤 Отправка пользователю {user_id} с {len(attachments)} медиа")
            
            response = requests.post(
                f"{self.api.base_url}/messages",
                headers={
                    "Authorization": self.api.token,
                    "Content-Type": "application/json"
                },
                json=payload,
                timeout=60,
                verify=False
            )
            
            if response.status_code == 200:
                seq = None
                post_link = None
                try:
                    result = response.json()
                    if 'message' in result and isinstance(result['message'], dict):
                        msg = result['message']
                        if 'body' in msg and isinstance(msg['body'], dict):
                            if 'seq' in msg['body']:
                                seq = msg['body']['seq']
                    elif 'seq' in result:
                        seq = result['seq']
                    
                    if seq:
                        encoded_id = self._seq_to_max_id(seq)
                        post_link = f"https://max.ru/c/{user_id}/{encoded_id}"
                    else:
                        post_link = None
                except:
                    post_link = None
                
                if not post_link:
                    post_link = f"https://max.ru/c/{user_id}"
                
                logger.info(f"✅ Отправлено пользователю {user_id}, ссылка: {post_link}")
                return True, post_link
            else:
                logger.error(f"❌ Ошибка: {response.status_code}")
                return False, None
                
        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
            return False, None

    def _parse_metadata(self, metadata_text):
        metadata = {}
        if not metadata_text:
            return metadata
        
        fields = {
            'Название': r'Название:\s*(.+)',
            'Ссылка': r'Ссылка:\s*(.+)',
            'Код предложения': r'Код предложения:\s*(.+)',
            'Цена в лизинге': r'Цена\s*[вВ]\s*лизинге:\s*(.+)',
        }
        
        for key, pattern in fields.items():
            match = re.search(pattern, metadata_text, re.IGNORECASE)
            if match:
                metadata[key] = match.group(1).strip()
        
        return metadata

    def publish_folder_with_tokens(self, user_id, folder_name, ad_text, metadata_text, image_tokens):
        """
        Устаревший метод для совместимости
        """
        return self.publish_folder_with_media(user_id, folder_name, ad_text, metadata_text, image_tokens)

    def publish_folder_with_media(self, user_id, folder_name, ad_text, metadata_text, media_tokens, media_types=None):
        """
        Публикация папки с медиа (фото и видео)
        """
        try:
            if self.STOP_FLAG.get(user_id, False):
                return False, "Остановка пользователем"
            
            chat_id = self.extract_chat_id_from_folder(folder_name)
            
            if not chat_id:
                return False, f"Не удалось извлечь chat_id из {folder_name}"
            
            logger.info(f"📤 Извлечен chat_id: {chat_id}")
            logger.info(f"📦 Медиа: {len(media_tokens)} файлов")
            
            metadata = self._parse_metadata(metadata_text)
            metadata['chat_id'] = chat_id
            
            if media_types:
                has_video = any(t == 'video' for t in media_types)
                if has_video:
                    logger.info("🎬 Обнаружено видео, проверяем поддержку чатом...")
            
            if media_types and any(t == 'video' for t in media_types):
                logger.info("⏳ Ожидание 3 секунды перед отправкой видео...")
                time.sleep(3)
            
            success, post_link = self._send_and_get_id(chat_id, ad_text, media_tokens, media_types)
            
            if not success:
                logger.warning("⚠️ Отправка в чат не удалась, пробуем в личные сообщения...")
                success, post_link = self._send_to_user(user_id, ad_text, media_tokens, media_types)
            
            if not success:
                return False, "Не удалось отправить сообщение"
            
            if post_link:
                metadata['post_link'] = post_link
                logger.info(f"🔗 Ссылка на пост сохранена: {post_link}")
            else:
                metadata['post_link'] = ''
                logger.warning(f"⚠️ Ссылка на пост не получена")
            
            if metadata['post_link'] and 'https://max.ru/u/' in metadata['post_link']:
                logger.error(f"❌ ОШИБКА: В post_link попала ссылка-источник! Очищаем.")
                metadata['post_link'] = ''
            
            now = datetime.now()
            timestamp = now.timestamp()
            self.db.save_ad_metadata(user_id, folder_name, chat_id, metadata, timestamp)
            self.db.add_publication(user_id, folder_name, chat_id, status='success')
            
            logger.info(f"✅ Папка {folder_name} опубликована")
            
            if post_link:
                return True, f"✅ Папка {folder_name} опубликована, ссылка: {post_link}"
            else:
                return True, f"✅ Папка {folder_name} опубликована"
            
        except Exception as e:
            logger.error(f"❌ Ошибка публикации {folder_name}: {e}")
            import traceback
            traceback.print_exc()
            return False, str(e)

    def handle_message_created(self, chat_id, message_id, user_id=None):
        try:
            if not chat_id or not message_id:
                return False
            
            chat_id_str = str(chat_id)
            logger.info(f"📨 ВЕБХУК: chat_id={chat_id_str}, message_id={message_id}")
            logger.info(f"📊 Всего pending записей: {len(self.pending_messages)}")
            
            found = False
            matching_keys = []
            
            for key, data in self.pending_messages.items():
                if data['chat_id'] == chat_id_str:
                    matching_keys.append(key)
                    found = True
            
            if not found:
                logger.warning(f"⚠️ Нет pending записи для chat_id {chat_id_str}")
                return False
            
            for key in matching_keys:
                data = self.pending_messages[key]
                folder_name = data['folder_name']
                user_id_from_pending = data['user_id']
                
                post_link = f"https://max.ru/c/{chat_id_str}/{message_id}"
                
                self.db.update_post_link(user_id_from_pending, folder_name, post_link)
                self.db.update_publication_status(user_id_from_pending, folder_name, 'success')
                
                del self.pending_messages[key]
                logger.info(f"✅ Обновлена ссылка для {folder_name}: {post_link}")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Ошибка обработки вебхука: {e}")
            return False

    def publish_single_folder(self, user_id, folder_name, ad_text, metadata_text, images_data):
        try:
            if self.STOP_FLAG.get(user_id, False):
                return False, "Остановка пользователем"
            
            image_tokens = []
            max_images = min(len(images_data), 10) if isinstance(images_data, list) else 0
            
            for i in range(max_images):
                if self.STOP_FLAG.get(user_id, False):
                    return False, "Остановка пользователем"
                
                img_data = images_data[i]
                if not img_data:
                    continue
                
                if isinstance(img_data, dict):
                    data = img_data.get('data')
                    if isinstance(data, list):
                        image_bytes = bytes(data)
                    elif isinstance(data, bytes):
                        image_bytes = data
                    else:
                        continue
                else:
                    image_bytes = img_data
                
                token = self.api.upload_file(image_bytes, f"image_{i}.jpg", 'image')
                if token:
                    image_tokens.append(token)
                    time.sleep(0.3)
            
            return self.publish_folder_with_media(
                user_id, folder_name, ad_text, metadata_text, image_tokens
            )
            
        except Exception as e:
            logger.error(f"❌ Ошибка: {e}")
            return False, str(e)

    def stop(self, user_id):
        logger.info(f"⏹️ Остановка для пользователя {user_id}")
        self.STOP_FLAG[user_id] = True
        
        try:
            user_folder = self.fm.get_user_folder(user_id)
            if os.path.exists(user_folder):
                import shutil
                shutil.rmtree(user_folder)
                os.makedirs(user_folder, exist_ok=True)
        except Exception as e:
            logger.error(f"❌ Ошибка удаления: {e}")
        
        def reset_stop_flag():
            time.sleep(5)
            self.STOP_FLAG[user_id] = False
        
        threading.Thread(target=reset_stop_flag, daemon=True).start()
        return True

    def is_running(self, user_id):
        return self.STOP_FLAG.get(user_id, False)
    
    def get_diagnostic_log(self):
        return self.diagnostic_log
    
    def clear_diagnostic_log(self):
        self.diagnostic_log = []
        logger.info("🧹 Диагностический журнал очищен")
