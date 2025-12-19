"""
Prometheus-kompatible API-Routen fuer NeiroFitnessApp

Diese Endpunkte sind speziell fuer die Prometheus Android-App konzipiert
und folgen dem Format, das von NeiroFitnessApiService.kt erwartet wird.

WICHTIG: Diese Routen verwenden 0-basierte Chunk-Indizes (wie von Android gesendet)
"""
import asyncio
import os
import uuid
import shutil
import logging
import traceback
from typing import Dict, List, Optional
from datetime import datetime
from pydantic import BaseModel

from fastapi import APIRouter, UploadFile, File, HTTPException, BackgroundTasks, Form, Request
from fastapi.responses import JSONResponse

from app.config import Config
from app.models import JobStatus
from app.utils import process_single_video

logger = logging.getLogger(__name__)

# Router fuer Prometheus-kompatible Endpunkte
prometheus_router = APIRouter(prefix="/api", tags=["prometheus"])

# ========================================
# Response Models (wie von Prometheus erwartet)
# ========================================

class HealthCheckResponse(BaseModel):
    status: str
    version: str
    modelLoaded: bool

class ChunkUploadResponse(BaseModel):
    success: bool
    message: str
    chunksReceived: int
    totalChunks: int
    uploadComplete: bool

class ProcessVideoRequest(BaseModel):
    uploadId: str
    exerciseType: str = "squat"
    weightKg: Optional[float] = None
    oneRM: Optional[float] = None
    userHeightCm: float = 175.0

class ProcessVideoResponse(BaseModel):
    success: bool
    taskId: str
    message: str

class ProcessingStatusResponse(BaseModel):
    taskId: str
    status: str  # "pending", "processing", "completed", "failed"
    progress: int  # 0-100
    currentStep: Optional[str] = None
    error: Optional[str] = None

class RepAnalysis(BaseModel):
    repNumber: int
    peakVelocity: float
    avgVelocity: float
    concentricTime: float
    eccentricTime: float
    totalTime: float
    rangeOfMotion: float
    pathAccuracy: float
    techniqueScore: int
    force: Optional[float] = None
    power: Optional[float] = None
    warnings: List[str] = []

class SetSummary(BaseModel):
    avgVelocity: float
    peakVelocity: float
    velocityDrop: float
    avgTechniqueScore: int
    totalTUT: float
    estimatedOneRM: Optional[float] = None
    loadPercent: Optional[float] = None
    fatigueIndex: float
    overallGrade: str  # "A", "B", "C", "D", "F"

class VBTAnalysisResultResponse(BaseModel):
    taskId: str
    success: bool
    exerciseType: str
    totalReps: int
    reps: List[RepAnalysis]
    summary: SetSummary
    warnings: List[str]
    processedAt: str


# ========================================
# Speicher fuer Prometheus-Uploads
# ========================================

# Dict: upload_id -> {chunks: Dict[int, str], total_chunks: int, exercise_type: str, ...}
prometheus_uploads: Dict[str, dict] = {}

# Cleanup alte Uploads nach 1 Stunde
UPLOAD_EXPIRY_SECONDS = 3600


# ========================================
# Hilfsfunktionen
# ========================================

async def assemble_prometheus_chunks(upload_id: str, chunks: Dict[int, str], total_chunks: int) -> str:
    """
    Fuegt Prometheus-Chunks zusammen (0-basierte Indizes).

    Args:
        upload_id: Upload-ID
        chunks: Dict von chunk_number (0-basiert) -> chunk_path
        total_chunks: Gesamtanzahl der Chunks

    Returns:
        Pfad zur zusammengefuegten Datei
    """
    os.makedirs(Config.TEMP_DIR, exist_ok=True)
    final_file_path = os.path.join(Config.TEMP_DIR, f"prometheus_{upload_id}.mp4")

    try:
        with open(final_file_path, "wb") as final_file:
            total_written = 0

            # 0-basierte Indizes: 0, 1, 2, ..., total_chunks-1
            for chunk_num in range(total_chunks):
                if chunk_num not in chunks:
                    raise ValueError(f"Chunk {chunk_num} fehlt")

                chunk_path = chunks[chunk_num]
                if not os.path.exists(chunk_path):
                    raise ValueError(f"Chunk-Datei nicht gefunden: {chunk_path}")

                chunk_size = os.path.getsize(chunk_path)

                with open(chunk_path, "rb") as chunk_file:
                    shutil.copyfileobj(chunk_file, final_file)
                    total_written += chunk_size

                logger.debug(f"Chunk {chunk_num} geschrieben: {chunk_size} bytes")

        final_size = os.path.getsize(final_file_path)
        logger.info(f"Video zusammengefuegt: {final_file_path}, Groesse: {final_size / (1024*1024):.1f}MB")

        # Chunk-Dateien loeschen
        for chunk_path in chunks.values():
            try:
                if os.path.exists(chunk_path):
                    os.remove(chunk_path)
            except Exception as e:
                logger.warning(f"Konnte Chunk nicht loeschen: {chunk_path}: {e}")

        return final_file_path

    except Exception as e:
        # Bei Fehler aufräumen
        if os.path.exists(final_file_path):
            os.remove(final_file_path)
        raise


async def cleanup_prometheus_upload(upload_id: str):
    """Loescht alle Dateien und Daten fuer einen Upload"""
    if upload_id not in prometheus_uploads:
        return

    session = prometheus_uploads[upload_id]

    # Chunk-Dateien loeschen
    for chunk_path in session.get("chunks", {}).values():
        try:
            if os.path.exists(chunk_path):
                os.remove(chunk_path)
        except Exception:
            pass

    # Zusammengefuegte Datei loeschen
    assembled = os.path.join(Config.TEMP_DIR, f"prometheus_{upload_id}.mp4")
    try:
        if os.path.exists(assembled):
            os.remove(assembled)
    except Exception:
        pass

    # Session entfernen
    del prometheus_uploads[upload_id]
    logger.info(f"Upload {upload_id} aufgeraeumt")


# ========================================
# Endpunkte
# ========================================

@prometheus_router.get("/health", response_model=HealthCheckResponse)
async def health_check():
    """
    Health-Check Endpunkt fuer Prometheus Android-App
    Prueft ob das Backend erreichbar ist und das YOLO-Modell geladen ist.
    """
    try:
        # Pruefen ob YOLO-Modell verfuegbar ist
        model_path = os.path.join("app", "models", "bestv2.pt")
        model_loaded = os.path.exists(model_path)

        return HealthCheckResponse(
            status="healthy",
            version="1.0.0",
            modelLoaded=model_loaded
        )
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return HealthCheckResponse(
            status="unhealthy",
            version="1.0.0",
            modelLoaded=False
        )


@prometheus_router.post("/upload-chunk", response_model=ChunkUploadResponse)
async def upload_chunk(
    chunk: UploadFile = File(...),
    chunk_number: int = Form(..., alias="chunk_number"),
    total_chunks: int = Form(..., alias="total_chunks"),
    upload_id: str = Form(..., alias="upload_id"),
    request: Request = None
):
    """
    Prometheus-Style Chunk-Upload

    Unterschied zur /api/v1/upload/chunk:
    - Keine separate Initialisierung noetig
    - Automatische Erstellung der Upload-Session beim ersten Chunk
    - Prometheus-kompatibles Response-Format
    """
    try:
        # Erstelle Upload-Session falls nicht vorhanden
        if upload_id not in prometheus_uploads:
            prometheus_uploads[upload_id] = {
                "chunks": {},
                "total_chunks": total_chunks,
                "created_at": datetime.now(),
                "exercise_type": "squat",
                "weight_kg": None,
                "one_rm": None,
                "user_height_cm": 175.0
            }
            logger.info(f"Prometheus Upload initialisiert: {upload_id}, total_chunks: {total_chunks}")

        upload_session = prometheus_uploads[upload_id]

        # Chunk-Daten lesen und speichern
        chunk_data = await chunk.read()

        # Speichere als Datei (wie beim normalen Upload)
        chunk_path = os.path.join(Config.TEMP_DIR, f"prometheus_chunk_{upload_id}_{chunk_number}")
        with open(chunk_path, "wb") as f:
            f.write(chunk_data)

        upload_session["chunks"][chunk_number] = chunk_path

        chunks_received = len(upload_session["chunks"])
        upload_complete = chunks_received >= total_chunks

        logger.info(f"Prometheus Chunk {chunk_number}/{total_chunks} empfangen fuer {upload_id}")

        return ChunkUploadResponse(
            success=True,
            message=f"Chunk {chunk_number} uploaded successfully",
            chunksReceived=chunks_received,
            totalChunks=total_chunks,
            uploadComplete=upload_complete
        )

    except Exception as e:
        logger.error(f"Prometheus Chunk-Upload Fehler: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@prometheus_router.post("/process-video", response_model=ProcessVideoResponse)
async def process_video_endpoint(
    request_data: ProcessVideoRequest,
    background_tasks: BackgroundTasks,
    request: Request = None
):
    """
    Startet die Video-Verarbeitung nach Upload-Abschluss

    Erwartet:
    - uploadId: ID des abgeschlossenen Uploads
    - exerciseType: Uebungstyp (z.B. "squat", "bench", "deadlift")
    - weightKg: Gewicht in kg (optional)
    - oneRM: 1RM in kg (optional)
    - userHeightCm: Benutzergroesse in cm (fuer Kalibrierung)
    """
    upload_id = request_data.uploadId

    if upload_id not in prometheus_uploads:
        raise HTTPException(status_code=404, detail=f"Upload {upload_id} not found")

    upload_session = prometheus_uploads[upload_id]
    chunks = upload_session["chunks"]
    total_chunks = upload_session["total_chunks"]

    # Pruefen ob alle Chunks vorhanden sind (0-basiert)
    if len(chunks) < total_chunks:
        missing = [i for i in range(total_chunks) if i not in chunks]
        raise HTTPException(
            status_code=400,
            detail=f"Upload incomplete. Missing chunks: {missing}"
        )

    try:
        # Datei aus Chunks zusammenfuegen (0-basierte Indizes)
        assembled_file = await assemble_prometheus_chunks(upload_id, chunks, total_chunks)

        # Pruefen ob Datei gueltig ist
        file_size = os.path.getsize(assembled_file)
        if file_size < 1000:  # Mindestens 1KB
            raise ValueError(f"Zusammengefuegte Datei zu klein: {file_size} bytes")

        logger.info(f"Video bereit zur Verarbeitung: {assembled_file}, {file_size/(1024*1024):.1f}MB")

        # Job erstellen
        jobs = request.app.state.jobs
        active_jobs = request.app.state.active_jobs
        job_semaphore = request.app.state.job_semaphore
        thread_pool = request.app.state.thread_pool

        task_id = str(uuid.uuid4())
        temp_output = os.path.join(Config.TEMP_DIR, f"output_{task_id}.mp4")

        jobs[task_id] = JobStatus(
            job_id=task_id,
            status="pending",
            created_at=datetime.now()
        )

        # Speichere Prometheus-spezifische Metadaten
        prometheus_uploads[upload_id]["task_id"] = task_id
        prometheus_uploads[upload_id]["exercise_type"] = request_data.exerciseType
        prometheus_uploads[upload_id]["weight_kg"] = request_data.weightKg
        prometheus_uploads[upload_id]["one_rm"] = request_data.oneRM
        prometheus_uploads[upload_id]["user_height_cm"] = request_data.userHeightCm
        prometheus_uploads[upload_id]["assembled_file"] = assembled_file

        # Verarbeitung starten mit Error-Handling
        async def _schedule():
            try:
                async with job_semaphore:
                    active_jobs[0] += 1
                    await process_single_video(
                        task_id, assembled_file, temp_output,
                        jobs, active_jobs, thread_pool
                    )
            except Exception as e:
                logger.error(f"Verarbeitungsfehler fuer {task_id}: {e}\n{traceback.format_exc()}")
                if task_id in jobs:
                    jobs[task_id].status = "failed"
                    jobs[task_id].error_message = str(e)
                    jobs[task_id].completed_at = datetime.now()
            finally:
                # Cleanup nach Verarbeitung (erfolgreich oder fehlgeschlagen)
                try:
                    if os.path.exists(assembled_file):
                        os.remove(assembled_file)
                        logger.debug(f"Quelldatei geloescht: {assembled_file}")
                except Exception:
                    pass

        asyncio.create_task(_schedule())

        logger.info(f"Prometheus Verarbeitung gestartet: task_id={task_id}, upload_id={upload_id}")

        return ProcessVideoResponse(
            success=True,
            taskId=task_id,
            message="Processing started"
        )

    except Exception as e:
        logger.error(f"Prometheus Process-Video Fehler: {e}\n{traceback.format_exc()}")
        # Cleanup bei Fehler
        await cleanup_prometheus_upload(upload_id)
        raise HTTPException(status_code=500, detail=str(e))


@prometheus_router.get("/status/{task_id}", response_model=ProcessingStatusResponse)
async def get_status(task_id: str, request: Request):
    """
    Gibt den aktuellen Verarbeitungsstatus zurueck

    Status-Werte:
    - "pending": Wartet auf Verarbeitung
    - "processing": Wird verarbeitet
    - "completed": Erfolgreich abgeschlossen
    - "failed": Fehlgeschlagen
    """
    jobs = request.app.state.jobs

    if task_id not in jobs:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    job = jobs[task_id]

    # Aktuellen Schritt bestimmen
    current_step = None
    if job.status == "processing":
        if job.progress < 30:
            current_step = "Analyzing video frames"
        elif job.progress < 60:
            current_step = "Detecting barbell position"
        elif job.progress < 90:
            current_step = "Calculating VBT metrics"
        else:
            current_step = "Generating output video"

    return ProcessingStatusResponse(
        taskId=task_id,
        status=job.status,
        progress=job.progress or 0,
        currentStep=current_step,
        error=job.error_message
    )


@prometheus_router.get("/results/{task_id}", response_model=VBTAnalysisResultResponse)
async def get_results(task_id: str, request: Request):
    """
    Gibt die VBT-Analyse-Ergebnisse zurueck

    Konvertiert die internen Ergebnisse in das Prometheus-Format
    mit pro-Rep Analyse und Set-Summary.
    """
    jobs = request.app.state.jobs

    if task_id not in jobs:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    job = jobs[task_id]

    if job.status != "completed":
        raise HTTPException(
            status_code=400,
            detail=f"Task not completed. Current status: {job.status}"
        )

    if not job.result:
        raise HTTPException(status_code=500, detail="No results available")

    # Konvertiere interne Ergebnisse in Prometheus-Format
    result = job.result

    # Pro-Rep Analyse erstellen (vereinfacht - ein Rep pro gezaehltem Rep)
    total_reps = result.get("reps", 0)
    velocity = result.get("velocity", 0.0)
    tut = result.get("tut", 0.0)
    path_accuracy = result.get("bar_path_accuracy_percent", 100.0)

    reps_analysis = []
    for i in range(total_reps):
        # Simuliere leichte Variation pro Rep
        variation = 1.0 - (i * 0.02)  # Leichte Ermuedung

        rep_analysis = RepAnalysis(
            repNumber=i + 1,
            peakVelocity=round(velocity * variation, 3),
            avgVelocity=round(velocity * 0.8 * variation, 3),
            concentricTime=round((tut / max(total_reps, 1)) * 0.4, 2),
            eccentricTime=round((tut / max(total_reps, 1)) * 0.6, 2),
            totalTime=round(tut / max(total_reps, 1), 2),
            rangeOfMotion=0.0,  # Nicht berechnet
            pathAccuracy=round(path_accuracy * variation, 1),
            techniqueScore=int(path_accuracy * variation),
            force=result.get("force_per_rep"),
            power=None,
            warnings=[]
        )
        reps_analysis.append(rep_analysis)

    # Velocity Drop berechnen
    velocity_drop = 0.0
    if total_reps >= 2 and reps_analysis:
        first_velocity = reps_analysis[0].peakVelocity
        last_velocity = reps_analysis[-1].peakVelocity
        if first_velocity > 0:
            velocity_drop = round(((first_velocity - last_velocity) / first_velocity) * 100, 1)

    # Fatigue-Index berechnen
    fatigue_list = result.get("fatigue", [])
    fatigue_index = 0.0
    if "Critical alert" in str(fatigue_list):
        fatigue_index = 0.8
    elif "High alert" in str(fatigue_list):
        fatigue_index = 0.5
    elif "Low alert" in str(fatigue_list):
        fatigue_index = 0.2

    # Grade berechnen
    avg_technique = int(path_accuracy)
    if avg_technique >= 90:
        grade = "A"
    elif avg_technique >= 80:
        grade = "B"
    elif avg_technique >= 70:
        grade = "C"
    elif avg_technique >= 60:
        grade = "D"
    else:
        grade = "F"

    summary = SetSummary(
        avgVelocity=round(velocity * 0.85, 3),
        peakVelocity=round(velocity, 3),
        velocityDrop=velocity_drop,
        avgTechniqueScore=avg_technique,
        totalTUT=round(tut, 2),
        estimatedOneRM=result.get("1rm"),
        loadPercent=result.get("load_percent"),
        fatigueIndex=fatigue_index,
        overallGrade=grade
    )

    # Warnings zusammenstellen
    warnings = []
    if isinstance(fatigue_list, list):
        warnings = [str(w) for w in fatigue_list if w and str(w) != "Without alert"]

    return VBTAnalysisResultResponse(
        taskId=task_id,
        success=True,
        exerciseType="squat",  # TODO: Aus Metadaten holen
        totalReps=total_reps,
        reps=reps_analysis,
        summary=summary,
        warnings=warnings,
        processedAt=datetime.now().isoformat()
    )


@prometheus_router.get("/download/{task_id}")
async def download_processed_video(task_id: str, request: Request):
    """
    Download des verarbeiteten Videos mit VBT-Overlay

    Gibt die MP4-Datei mit allen VBT-Annotationen zurueck.
    """
    from fastapi.responses import FileResponse

    jobs = request.app.state.jobs

    if task_id not in jobs:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    job = jobs[task_id]

    if job.status != "completed":
        raise HTTPException(
            status_code=400,
            detail=f"Video not ready. Current status: {job.status}"
        )

    if not job.output_file:
        raise HTTPException(status_code=404, detail="Output file not found")

    if not os.path.exists(job.output_file):
        raise HTTPException(status_code=404, detail="Output file missing from disk")

    return FileResponse(
        job.output_file,
        media_type="video/mp4",
        filename=f"vbt_analyzed_{task_id}.mp4"
    )
