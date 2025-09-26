// Тест загрузки видео по кусочкам для NeiroFitnessApp
const fs = require('fs');
const path = require('path');
const FormData = require('form-data');
const axios = require('axios');

// Конфигурация
const API_BASE_URL = 'http://localhost:8000';
const CHUNK_SIZE = 5 * 1024 * 1024; // 5MB чанки
const TEST_VIDEO_PATH = 'test-video.mp4'; // Путь к тестовому видео

class ChunkedUploadTester {
    constructor() {
        this.uploadId = null;
        this.jobId = null;
        this.totalChunks = 0;
        this.uploadedChunks = 0;
    }

    // Создание тестового видео файла (если не существует)
    async createTestVideo() {
        if (!fs.existsSync(TEST_VIDEO_PATH)) {
            console.log('⚠️  Тестовое видео не найдено. Создайте файл test_video.mp4 в корне проекта');
            console.log('   Или измените TEST_VIDEO_PATH на путь к существующему видео файлу');
            return false;
        }
        return true;
    }

    // Инициализация загрузки
    async initUpload() {
        try {
            const stats = fs.statSync(TEST_VIDEO_PATH);
            const fileSize = stats.size;
            this.totalChunks = Math.ceil(fileSize / CHUNK_SIZE);

            console.log(`📁 Файл: ${TEST_VIDEO_PATH}`);
            console.log(`📏 Размер: ${(fileSize / 1024 / 1024).toFixed(2)} MB`);
            console.log(`🧩 Частей: ${this.totalChunks}`);

            const formData = new FormData();
            formData.append('filename', path.basename(TEST_VIDEO_PATH));
            formData.append('total_size', fileSize.toString());
            formData.append('total_chunks', this.totalChunks.toString());

            const response = await axios.post(`${API_BASE_URL}/api/v1/upload/init`, formData, {
                headers: {
                    ...formData.getHeaders(),
                },
                timeout: 30000
            });

            this.uploadId = response.data.upload_id;
            console.log(`✅ Инициализация успешна. Upload ID: ${this.uploadId}`);
            return true;
        } catch (error) {
            console.log(JSON.stringify(error.response?.data, null, 2));
            console.error('❌ Ошибка инициализации:', error.response?.data || error.message);
            return false;
        }
    }

    // Загрузка чанка
    async uploadChunk(chunkIndex) {
        try {
            const start = chunkIndex * CHUNK_SIZE;
            const end = Math.min(start + CHUNK_SIZE, fs.statSync(TEST_VIDEO_PATH).size);
            
            // Читаем только нужную часть файла
            const chunk = fs.readFileSync(TEST_VIDEO_PATH).slice(start, end);

            const formData = new FormData();
            formData.append('upload_id', this.uploadId);
            formData.append('chunk_number', chunkIndex + 1);
            formData.append('chunk', chunk, {
                filename: `chunk_${chunkIndex}`,
                contentType: 'application/octet-stream'
            });

            const response = await axios.post(
                `${API_BASE_URL}/api/v1/upload/chunk`,
                formData,
                {
                    headers: {
                        ...formData.getHeaders(),
                        'Content-Type': 'multipart/form-data'
                    },
                    timeout: 30000
                }
            );

            this.uploadedChunks++;
            const progress = ((this.uploadedChunks / this.totalChunks) * 100).toFixed(1);
            console.log(`📤 Чанк ${chunkIndex + 1}/${this.totalChunks} загружен (${progress}%)`);
            
            return true;
        } catch (error) {
            console.log(JSON.stringify(error.response?.data, null, 2));
            console.error(`❌ Ошибка загрузки чанка ${chunkIndex}:`, error.response?.data || error.message);
            return false;
        }
    }

    // Завершение загрузки
    async completeUpload() {
        try {
            const formData = new FormData();
            formData.append('upload_id', this.uploadId);

            const response = await axios.post(`${API_BASE_URL}/api/v1/upload/complete`, formData, {
                headers: {
                    ...formData.getHeaders(),
                },
                timeout: 30000
            });

            console.log('✅ Загрузка завершена успешно!');
            console.log('📊 Результат:', response.data);
            
            // Сохраняем job_id для дальнейшего отслеживания
            if (response.data.job_id) {
                this.jobId = response.data.job_id;
                console.log(`🎯 Job ID: ${this.jobId}`);
            }
            
            return response.data;
        } catch (error) {
            console.error('❌ Ошибка завершения загрузки:', error.response?.data || error.message);
            return null;
        }
    }

    

    // Проверка статуса загрузки
    async checkUploadStatus() {
        try {
            const response = await axios.get(`${API_BASE_URL}/api/v1/upload/${this.uploadId}`);
            console.log('📊 Статус загрузки:', response.data);
            return response.data;
        } catch (error) {
            console.error('❌ Ошибка проверки статуса:', error.response?.data || error.message);
            return null;
        }
    }

    // Получение данных о задаче по job_id
    async getJobData() {
        try {
            if (!this.jobId) {
                console.error('❌ Job ID не найден');
                return null;
            }

            const response = await axios.get(`${API_BASE_URL}/api/v1/job/${this.jobId}`, {
                timeout: 10000, // 10 секунд таймаут
                headers: {
                    'Content-Type': 'application/json'
                }
            });
            console.log(`📊 Статус задачи ${this.jobId}:`, response.data);
            return response.data;
        } catch (error) {
            if (error.code === 'ECONNRESET' || error.message.includes('socket hang up')) {
                console.log('⚠️  Сервер занят, попробуем позже...');
                return null;
            }
            console.error('❌ Ошибка получения данных задачи:', error.response?.data || error.message);
            return null;
        }
    }

    // Ожидание завершения обработки
    async waitForJobCompletion(maxWaitTime = 300000) { // 5 минут по умолчанию
        if (!this.jobId) {
            console.error('❌ Job ID не найден');
            return null;
        }

        const startTime = Date.now();
        console.log(`⏳ Ожидаем завершения обработки задачи ${this.jobId}...`);

        while (Date.now() - startTime < maxWaitTime) {
            const status = await this.getJobData();
            
            if (!status) {
                console.error('❌ Не удалось получить статус задачи');
                return null;
            }

            if (status.status === 'completed') {
                console.log('✅ Обработка завершена!');
                return status;
            } else if (status.status === 'failed') {
                console.error('❌ Обработка завершилась с ошибкой');
                return status;
            }

            // Показываем прогресс
            const progress = status.progress || 0;
            console.log(`🔄 Обработка в процессе... Прогресс: ${progress}%`);

            // Ждем 5 секунд перед следующей проверкой
            await new Promise(resolve => setTimeout(resolve, 5000));
        }

        console.error('⏰ Время ожидания истекло');
        return null;
    }

    // Основной тест
    async runTest() {
        console.log('🚀 Начинаем тест загрузки по кусочкам...\n');

        // Проверяем наличие тестового файла
        if (!(await this.createTestVideo())) {
            return;
        }

        // Инициализация
        if (!(await this.initUpload())) {
            return;
        }

        // Загружаем чанки
        console.log('\n📤 Загружаем чанки...');
        for (let i = 0; i < this.totalChunks; i++) {
            const success = await this.uploadChunk(i);
            if (!success) {
                console.error(`❌ Не удалось загрузить чанк ${i}`);
                return;
            }
            
            // Небольшая задержка между чанками
            await new Promise(resolve => setTimeout(resolve, 100));
        }

        // Проверяем статус перед завершением
        console.log('\n📊 Проверяем статус...');
        await this.checkUploadStatus();

        // Завершаем загрузку
        console.log('\n🏁 Завершаем загрузку...');
        const result = await this.completeUpload();

        if (result && this.jobId) {
            console.log(`🎯 Job ID получен: ${this.jobId}`);
            
            // Задержка для начала обработки
            console.log('\n⏳ Ждем начала обработки...');
            await new Promise(resolve => setTimeout(resolve, 10000)); // 10 секунд
            
            // Проверяем статус задачи
            console.log('\n📊 Проверяем статус задачи...');
            const jobStatus = await this.getJobData();
            
            if (jobStatus) {
                // Ожидаем завершения обработки (2 минуты для теста)
                console.log('\n⏳ Ожидаем завершения обработки видео...');
                const finalStatus = await this.waitForJobCompletion(120000); // 2 минуты
                
                if (finalStatus && finalStatus.status === 'completed') {
                    console.log('\n🎉 Тест завершен успешно!');
                    console.log('📊 Финальный результат:', finalStatus);
                } else if (finalStatus && finalStatus.status === 'failed') {
                    console.log('\n❌ Обработка завершилась с ошибкой');
                    console.log('📊 Детали ошибки:', finalStatus);
                } else {
                    console.log('\n⏰ Обработка еще не завершена (это нормально для больших файлов)');
                }
            }
        } else {
            console.log('\n❌ Не удалось получить job_id или завершить загрузку');
        }
    }

    // Тест с ошибками
    async runErrorTest() {
        console.log('🧪 Запускаем тест с ошибками...\n');

        // Тест 1: Инициализация с неверными данными
        console.log('1️⃣ Тест неверной инициализации...');
        try {
            await axios.post(`${API_BASE_URL}/api/v1/upload/init`, {
                filename: 'test.mp4',
                total_size: -1,
                total_chunks: 0
            });
        } catch (error) {
            console.log('✅ Ошибка корректно обработана:', error.response?.data?.detail);
        }

        // Тест 2: Загрузка чанка без инициализации
        console.log('\n2️⃣ Тест загрузки без инициализации...');
        try {
            const formData = new FormData();
            formData.append('upload_id', 'invalid-id');
            formData.append('chunk_index', 0);
            formData.append('chunk', Buffer.from('test'));

            await axios.post(`${API_BASE_URL}/api/v1/upload/chunk`, formData, {
                headers: formData.getHeaders()
            });
        } catch (error) {
            console.log('✅ Ошибка корректно обработана:', error.response?.data?.detail);
        }

        // Тест 3: Завершение несуществующей загрузки
        console.log('\n3️⃣ Тест завершения несуществующей загрузки...');
        try {
            await axios.post(`${API_BASE_URL}/api/v1/upload/complete`, {
                upload_id: 'invalid-id'
            });
        } catch (error) {
            console.log('✅ Ошибка корректно обработана:', error.response?.data?.detail);
        }

        console.log('\n✅ Тесты ошибок завершены!');
    }
}

// Функция для быстрого теста API
async function quickApiTest() {
    console.log('🔍 Быстрый тест API...\n');

    try {
        // Проверяем главную страницу
        const homeResponse = await axios.get(`${API_BASE_URL}/`);
        console.log('✅ Главная страница:', homeResponse.data);

        // Проверяем документацию
        const docsResponse = await axios.get(`${API_BASE_URL}/docs`);
        console.log('✅ Документация доступна');

        // Проверяем список задач
        const jobsResponse = await axios.get(`${API_BASE_URL}/api/v1/jobs`);
        console.log('✅ Список задач:', jobsResponse.data);

        // Проверяем статистику
        const statsResponse = await axios.get(`${API_BASE_URL}/api/v1/stats`);
        console.log('✅ Статистика:', statsResponse.data);

    } catch (error) {
        console.error('❌ Ошибка API теста:', error.response?.data || error.message);
    }
}

// Главная функция
async function main() {
    const args = process.argv.slice(2);
    const tester = new ChunkedUploadTester();

    console.log('🎯 NeiroFitnessApp - Тест загрузки по кусочкам\n');

    if (args.includes('--quick')) {
        await quickApiTest();
    } else if (args.includes('--errors')) {
        await tester.runErrorTest();
    } else {
        await tester.runTest();
    }
}

// Обработка ошибок
process.on('unhandledRejection', (error) => {
    console.error('❌ Необработанная ошибка:', error);
    process.exit(1);
});

// Запуск
if (require.main === module) {
    main().catch(console.error);
}

module.exports = { ChunkedUploadTester };
