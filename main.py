from fastapi import FastAPI, UploadFile, File
from fastapi.responses import Response
from starlette.responses import StreamingResponse
import uuid
import shutil
import os
from app.video_process import process_video
import json
import mimetypes

app = FastAPI()


@app.post("/api/v1/process_video")
async def process_video_endpoint(file: UploadFile = File(...)):
    temp_input = f"/tmp/input_{uuid.uuid4()}.mp4"
    temp_output = f"/tmp/output_{uuid.uuid4()}.mp4"

    with open(temp_input, "wb") as f:
        shutil.copyfileobj(file.file, f)

    result, output_path = process_video(temp_input, temp_output)

    boundary = "video-json-boundary"

    def generate():
        yield f"--{boundary}\r\n"
        yield "Content-Type: application/json\r\n\r\n"
        yield json.dumps({"data": result}) + "\r\n"

        yield f"--{boundary}\r\n"
        yield "Content-Type: video/mp4\r\n"
        yield f"Content-Disposition: attachment; filename=processed.mp4\r\n\r\n"
        with open(output_path, "rb") as f:
            yield from f
        yield f"\r\n--{boundary}--\r\n"

    headers = {
        "Content-Type": f"multipart/mixed; boundary={boundary}"
    }

    return StreamingResponse(generate(), headers=headers)