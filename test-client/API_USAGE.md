# NeiroFitnessApp API - Руководство по использованию

## 🚀 Возможности

### Chunked Upload (Загрузка по частям)
Загрузка больших видео файлов по частям с автоматической сборкой:
- ✅ **Надежность** - при обрыве соединения можно перезагрузить только потерянную часть
- ✅ **Экономия памяти** - файл не загружается целиком в память
- ✅ **Прогресс загрузки** - можно отслеживать прогресс загрузки каждой части
- ✅ **Возможность паузы** - можно приостановить и возобновить загрузку
- ✅ **Параллельная загрузка** - можно загружать части параллельно
- ✅ **Автоматическая сборка** - части автоматически собираются в один файл

## 📋 API Endpoints

### 1. Chunked Upload (Загрузка по частям)

#### 1.1 Инициализация загрузки
```http
POST /api/v1/upload/init
Content-Type: multipart/form-data

filename: video.mp4
total_size: 104857600
total_chunks: 10
```

**Ответ:**
```json
{
  "upload_id": "uuid",
  "message": "Сессия загрузки создана",
  "total_chunks": 10,
  "chunk_size": 8192
}
```

#### 1.2 Загрузка части файла
```http
POST /api/v1/upload/chunk
Content-Type: multipart/form-data

upload_id: uuid
chunk_number: 1
chunk: [binary_data]
```

**Ответ:**
```json
{
  "upload_id": "uuid",
  "chunk_number": 1,
  "total_chunks": 10,
  "uploaded_size": 10485760,
  "message": "Часть 1 загружена успешно"
}
```

#### 1.3 Завершение загрузки
```http
POST /api/v1/upload/complete
Content-Type: multipart/form-data

upload_id: uuid
```

**Ответ:**
```json
{
  "upload_id": "uuid",
  "job_id": "job_uuid",
  "message": "Файл собран и обработка начата",
  "filename": "video.mp4",
  "total_size": 104857600
}
```

#### 1.4 Статус загрузки
```http
GET /api/v1/upload/{upload_id}
```

**Ответ:**
```json
{
  "upload_id": "uuid",
  "filename": "video.mp4",
  "status": "uploading",
  "total_size": 104857600,
  "uploaded_size": 52428800,
  "total_chunks": 10,
  "uploaded_chunks": 5,
  "progress_percent": 50.0,
  "created_at": "2024-01-01T12:00:00",
  "completed_at": null
}
```

#### 1.5 Отмена загрузки
```http
DELETE /api/v1/upload/{upload_id}
```

### 2. Проверка статуса задачи
```http
GET /api/v1/job/{job_id}
```

**Ответ:**
```json
{
  "job_id": "uuid",
  "status": "processing", // pending, processing, completed, failed
  "progress": 45,
  "created_at": "2024-01-01T12:00:00",
  "completed_at": null,
  "error_message": null,
  "result": null,
  "output_file": null
}
```

### 3. Скачивание обработанного видео
```http
GET /api/v1/download/{job_id}
```

### 4. Получение результатов анализа
```http
GET /api/v1/result/{job_id}
```

**Ответ:**
```json
{
  "job_id": "uuid",
  "result": {
    "trajectory": [[x1, y1], [x2, y2], ...],
    "reps": 10,
    "velocity": 2.5,
    "load_percent": 66.67,
    "1rm": 90,
    "tut": 45.2,
    "bar_path": "2m 15cm",
    "bar_path_accuracy_percent": 95.5,
    "fatigue": "Without alert",
    "force_per_rep": 150.0
  }
}
```

### 5. Статистика системы
```http
GET /api/v1/stats
```

**Ответ:**
```json
{
  "total_jobs": 25,
  "completed_jobs": 20,
  "failed_jobs": 2,
  "processing_jobs": 3,
  "active_jobs": 3,
  "active_uploads": 2,
  "max_concurrent_jobs": 3,
  "max_file_size_mb": 500
}
```

### 6. Удаление задачи
```http
DELETE /api/v1/job/{job_id}
```

## ⚙️ Конфигурация

### Ограничения системы:
- **Максимальный размер файла:** 500MB
- **Максимум одновременных задач:** 3
- **Размер чанка для загрузки:** 8KB
- **Максимум частей файла:** 1000

### Защита от перегрузки:
- ✅ Загрузка файлов по частям (chunked upload)
- ✅ Ограничение количества одновременных задач
- ✅ Валидация размера файлов
- ✅ Автоматическая очистка временных файлов
- ✅ Проверка целостности собранного файла


## 📝 Примеры использования

### Python клиент - Chunked Upload

```python
import requests
import os
import math

def upload_video_in_chunks(file_path, chunk_size_mb=10):
    """Загрузка видео по частям"""
    
    # Получаем информацию о файле
    file_size = os.path.getsize(file_path)
    filename = os.path.basename(file_path)
    chunk_size_bytes = chunk_size_mb * 1024 * 1024
    total_chunks = math.ceil(file_size / chunk_size_bytes)
    
    print(f"Загружаем {filename}, размер: {file_size / (1024*1024):.1f}MB, частей: {total_chunks}")
    
    # 1. Инициализируем загрузку
    init_data = {
        'filename': filename,
        'total_size': file_size,
        'total_chunks': total_chunks
    }
    
    init_response = requests.post('http://localhost:8000/api/v1/upload/init', data=init_data)
    upload_data = init_response.json()
    upload_id = upload_data['upload_id']
    
    print(f"Создана сессия загрузки: {upload_id}")
    
    # 2. Загружаем части
    with open(file_path, 'rb') as f:
        for chunk_num in range(1, total_chunks + 1):
            # Читаем часть файла
            chunk_data = f.read(chunk_size_bytes)
            
            # Загружаем часть
            chunk_files = {
                'chunk': (f'chunk_{chunk_num}', chunk_data, 'application/octet-stream')
            }
            chunk_data_form = {
                'upload_id': upload_id,
                'chunk_number': chunk_num
            }
            
            chunk_response = requests.post(
                'http://localhost:8000/api/v1/upload/chunk',
                data=chunk_data_form,
                files=chunk_files
            )
            
            chunk_result = chunk_response.json()
            print(f"Загружена часть {chunk_num}/{total_chunks}: {chunk_result['message']}")
    
    # 3. Завершаем загрузку
    complete_data = {'upload_id': upload_id}
    complete_response = requests.post('http://localhost:8000/api/v1/upload/complete', data=complete_data)
    complete_result = complete_response.json()
    
    job_id = complete_result['job_id']
    print(f"Загрузка завершена, создана задача: {job_id}")
    
    return job_id

def monitor_job_progress(job_id):
    """Отслеживание прогресса обработки"""
    while True:
        status_response = requests.get(f'http://localhost:8000/api/v1/job/{job_id}')
        status = status_response.json()
        
        print(f"Задача {job_id}: {status['status']} ({status['progress']}%)")
        
        if status['status'] in ['completed', 'failed']:
            break
            
        time.sleep(2)
    
    if status['status'] == 'completed':
        # Скачиваем результат
        download_response = requests.get(f'http://localhost:8000/api/v1/download/{job_id}')
        with open(f'processed_{job_id}.mp4', 'wb') as f:
            f.write(download_response.content)
        
        # Получаем метрики
        result_response = requests.get(f'http://localhost:8000/api/v1/result/{job_id}')
        metrics = result_response.json()
        print(f"Метрики: {metrics['result']}")
        
        return True
    else:
        print(f"Ошибка: {status['error_message']}")
        return False

# Использование
if __name__ == "__main__":
    import time
    
    # Загружаем видео по частям
    job_id = upload_video_in_chunks('large_video.mp4', chunk_size_mb=5)
    
    # Отслеживаем прогресс
    success = monitor_job_progress(job_id)
    
    if success:
        print("Обработка завершена успешно!")
    else:
        print("Обработка завершилась с ошибкой")
```


## 🔧 Настройка конфигурации

Создайте файл `.env` в корне проекта:
```env
MAX_FILE_SIZE_MB=500
MAX_BATCH_SIZE=5
MAX_CONCURRENT_JOBS=3
TEMP_DIR=/tmp/neirofitness
OUTPUT_DIR=/tmp/neirofitness/output
```

## 📊 Мониторинг

Используйте эндпоинт `/api/v1/stats` для мониторинга состояния системы:
- Количество активных задач обработки
- Количество активных загрузок
- Статистика по завершенным/неудачным задачам
- Текущая нагрузка на систему
- Максимальные ограничения системы
