# modules/file_manager.py
import os
import shutil
import re
import logging
import time
from datetime import datetime

logger = logging.getLogger(__name__)

class FileManager:
    def __init__(self, data_dir="/app/data"):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        logger.info(f"✅ FileManager инициализирован: {data_dir}")
    
    def get_user_folder(self, user_id):
        user_folder = os.path.join(self.data_dir, f"user_{user_id}")
        os.makedirs(user_folder, exist_ok=True)
        return user_folder
    
    def get_ads_folder(self, user_id):
        user_folder = self.get_user_folder(user_id)
        ads_folder = os.path.join(user_folder, "ads")
        os.makedirs(ads_folder, exist_ok=True)
        return ads_folder
    
    def get_temp_folder(self, user_id):
        user_folder = self.get_user_folder(user_id)
        temp_folder = os.path.join(user_folder, "temp")
        os.makedirs(temp_folder, exist_ok=True)
        return temp_folder
    
    def get_report_folder(self, user_id):
        user_folder = self.get_user_folder(user_id)
        report_folder = os.path.join(user_folder, "reports")
        os.makedirs(report_folder, exist_ok=True)
        return report_folder
    
    def save_file(self, user_id, filename, content, subfolder=None):
        try:
            if subfolder:
                folder = os.path.join(self.get_user_folder(user_id), subfolder)
                os.makedirs(folder, exist_ok=True)
            else:
                folder = self.get_user_folder(user_id)
            
            filepath = os.path.join(folder, filename)
            
            if isinstance(content, str):
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(content)
            else:
                with open(filepath, 'wb') as f:
                    f.write(content)
            
            logger.info(f"💾 Файл сохранен: {filepath}")
            return filepath
            
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения файла {filename}: {e}")
            return None
    
    def read_file(self, user_id, filename, subfolder=None):
        try:
            if subfolder:
                filepath = os.path.join(self.get_user_folder(user_id), subfolder, filename)
            else:
                filepath = os.path.join(self.get_user_folder(user_id), filename)
            
            if not os.path.exists(filepath):
                logger.warning(f"⚠️ Файл не найден: {filepath}")
                return None
            
            with open(filepath, 'r', encoding='utf-8') as f:
                return f.read()
                
        except Exception as e:
            logger.error(f"❌ Ошибка чтения файла {filename}: {e}")
            return None
    
    def delete_file(self, user_id, filename, subfolder=None):
        try:
            if subfolder:
                filepath = os.path.join(self.get_user_folder(user_id), subfolder, filename)
            else:
                filepath = os.path.join(self.get_user_folder(user_id), filename)
            
            if os.path.exists(filepath):
                os.remove(filepath)
                logger.info(f"🗑️ Файл удален: {filepath}")
                return True
            else:
                logger.warning(f"⚠️ Файл не найден для удаления: {filepath}")
                return False
                
        except Exception as e:
            logger.error(f"❌ Ошибка удаления файла {filename}: {e}")
            return False
    
    def delete_user_folder(self, user_id):
        try:
            user_folder = self.get_user_folder(user_id)
            if os.path.exists(user_folder):
                shutil.rmtree(user_folder)
                logger.info(f"🗑️ Папка пользователя {user_id} удалена")
                return True
            return False
        except Exception as e:
            logger.error(f"❌ Ошибка удаления папки пользователя {user_id}: {e}")
            return False
    
    def clear_temp_folder(self, user_id):
        try:
            temp_folder = self.get_temp_folder(user_id)
            if os.path.exists(temp_folder):
                shutil.rmtree(temp_folder)
                os.makedirs(temp_folder, exist_ok=True)
                logger.info(f"🧹 Временная папка пользователя {user_id} очищена")
                return True
            return False
        except Exception as e:
            logger.error(f"❌ Ошибка очистки временной папки {user_id}: {e}")
            return False
    
    def list_user_files(self, user_id, subfolder=None):
        try:
            if subfolder:
                folder = os.path.join(self.get_user_folder(user_id), subfolder)
            else:
                folder = self.get_user_folder(user_id)
            
            if not os.path.exists(folder):
                return []
            
            files = []
            for item in os.listdir(folder):
                item_path = os.path.join(folder, item)
                if os.path.isfile(item_path):
                    stat = os.stat(item_path)
                    files.append({
                        'name': item,
                        'size': stat.st_size,
                        'modified': datetime.fromtimestamp(stat.st_mtime).isoformat()
                    })
            
            return sorted(files, key=lambda x: x['modified'], reverse=True)
            
        except Exception as e:
            logger.error(f"❌ Ошибка получения списка файлов: {e}")
            return []
    
    def get_file_info(self, user_id, filename, subfolder=None):
        try:
            if subfolder:
                filepath = os.path.join(self.get_user_folder(user_id), subfolder, filename)
            else:
                filepath = os.path.join(self.get_user_folder(user_id), filename)
            
            if not os.path.exists(filepath):
                return None
            
            stat = os.stat(filepath)
            return {
                'name': filename,
                'path': filepath,
                'size': stat.st_size,
                'created': datetime.fromtimestamp(stat.st_ctime).isoformat(),
                'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                'is_file': os.path.isfile(filepath)
            }
            
        except Exception as e:
            logger.error(f"❌ Ошибка получения информации о файле: {e}")
            return None
    
    def cleanup_old_files(self, max_age_hours=24):
        try:
            current_time = time.time()
            max_age_seconds = max_age_hours * 3600
            deleted_count = 0
            
            for item in os.listdir(self.data_dir):
                item_path = os.path.join(self.data_dir, item)
                
                if not os.path.isdir(item_path) or not item.startswith('user_'):
                    continue
                
                dir_mtime = os.path.getmtime(item_path)
                if current_time - dir_mtime > max_age_seconds:
                    shutil.rmtree(item_path)
                    deleted_count += 1
                    logger.info(f"🗑️ Удалена старая папка: {item}")
            
            if deleted_count > 0:
                logger.info(f"🧹 Очищено {deleted_count} старых папок")
            
        except Exception as e:
            logger.error(f"❌ Ошибка очистки старых файлов: {e}")
    
    def get_user_size(self, user_id):
        try:
            user_folder = self.get_user_folder(user_id)
            total_size = 0
            
            for dirpath, dirnames, filenames in os.walk(user_folder):
                for filename in filenames:
                    filepath = os.path.join(dirpath, filename)
                    if os.path.exists(filepath):
                        total_size += os.path.getsize(filepath)
            
            return total_size
            
        except Exception as e:
            logger.error(f"❌ Ошибка подсчета размера: {e}")
            return 0
    
    def ensure_folder_exists(self, folder_path):
        try:
            os.makedirs(folder_path, exist_ok=True)
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка создания папки {folder_path}: {e}")
            return False
