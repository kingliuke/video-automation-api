from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import subprocess
import requests
import os
import threading
import json
import uuid
import base64
import cv2
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TEMP_DIR = Path("/tmp/videos")
TEMP_DIR.mkdir(exist_ok=True)

JOBS_DIR = Path("/tmp/jobs")
JOBS_DIR.mkdir(exist_ok=True)

MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2GB


# ─────────────────────────────────────────────
# PYDANTIC MODELS
# ─────────────────────────────────────────────

class CutInstruction(BaseModel):
    start: str
    end: str
    reason: str = ""

class CutVideoRequest(BaseModel):
    video_url: str
    cuts: List[CutInstruction]

class KeepInstruction(BaseModel):
    start: str
    end: str
    label: str = ""

class KeepVideoRequest(BaseModel):
    video_url: str
    keep: List[KeepInstruction]

class ExtractFramesRequest(BaseModel):
    video_url: str
    fps: float = 1.0
    max_frames: int = 100


# ─────────────────────────────────────────────
# JOB STATE HELPERS
# ─────────────────────────────────────────────

def job_path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"

def write_job(job_id: str, data: dict):
    with open(job_path(job_id), "w") as f:
        json.dump(data, f)

def read_job(job_id: str) -> Optional[dict]:
    p = job_path(job_id)
    if not p.exists():
        return None
    with open(p) as f:
        return json.load(f)

def set_job_status(job_id: str, status: str, extra: dict = {}):
    job = read_job(job_id) or {}
    job["status"] = status
    job["updated_at"] = datetime.utcnow().isoformat()
    job.update(extra)
    write_job(job_id, job)


# ─────────────────────────────────────────────
# CORE HELPERS
# ─────────────────────────────────────────────

def cleanup_files(*paths):
    for path in paths:
        try:
            if path and os.path.exists(str(path)):
                os.remove(str(path))
        except Exception:
            pass

def download_video(video_url: str, output_path: str) -> bool:
    try:
        response = requests.get(video_url, stream=True, timeout=(10, 600))
        response.raise_for_status()
        downloaded = 0
        with open(output_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                downloaded += len(chunk)
                if downloaded > MAX_DOWNLOAD_BYTES:
                    print("Download aborted: exceeded 2GB limit")
                    return False
                f.write(chunk)
        return True
    except Exception as e:
        print(f"Download error: {e}")
        return False

def time_to_seconds(time_str: str) -> float:
    parts = time_str.strip().split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    elif len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    else:
        return float(parts[0])

def get_video_duration(video_path: str) -> float:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration", "-of",
             "default=noprint_wrappers=1:nokey=1", video_path],
            capture_output=True, text=True
        )
        return float(result.stdout.strip())
    except:
        return 0

def create_keep_segments(cuts: List[Dict], video_duration: float) -> List[Dict]:
    if not cuts:
        return [{"start": 0, "end": video_duration}]
    sorted_cuts = sorted(cuts, key=lambda x: time_to_seconds(x["start"]))
    keep_segments = []
    current_time = 0
    for cut in sorted_cuts:
        cut_start = time_to_seconds(cut["start"])
        cut_end = time_to_seconds(cut["end"])
        if current_time < cut_start:
            keep_segments.append({"start": current_time, "end": cut_start})
        current_time = max(current_time, cut_end)
    if current_time < video_duration:
        keep_segments.append({"start": current_time, "end": video_duration})
    return keep_segments

def cut_video(video_path: str, keep_segments: List[Dict], output_path: str) -> bool:
    try:
        seg_job_id = uuid.uuid4().hex
        segment_dir = TEMP_DIR / f"segments_{seg_job_id}"
        segment_dir.mkdir(exist_ok=True)
        segment_files = []

        for i, segment in enumerate(keep_segments):
            segment_file = segment_dir / f"segment_{i}.mp4"
            start = segment["start"]
            duration = segment["end"] - segment["start"]

            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start),
                "-i", video_path,
                "-t", str(duration),
                "-c:v", "libx264",
                "-c:a", "aac",
                "-preset", "fast",
                "-movflags", "+faststart",
                str(segment_file)
            ]

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                print(f"Error extracting segment {i}: {result.stderr}")
                return False

            segment_files.append(str(segment_file))

        concat_file = segment_dir / "concat.txt"
        with open(concat_file, "w") as f:
            for seg_file in segment_files:
                f.write(f"file '{seg_file}'\n")

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            output_path
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)

        for seg_file in segment_files:
            cleanup_files(seg_file)
        cleanup_files(str(concat_file))
        try:
            segment_dir.rmdir()
        except Exception:
            pass

        return result.returncode == 0

    except Exception as e:
        print(f"Cut video error: {e}")
        return False


# ─────────────────────────────────────────────
# BACKGROUND WORKERS
# ─────────────────────────────────────────────

def run_keep_segments(job_id: str, video_url: str, keep: List[dict]):
    input_path = TEMP_DIR / f"input_{job_id}.mp4"
    output_filename = f"output_{job_id}.mp4"
    output_path = TEMP_DIR / output_filename

    try:
        set_job_status(job_id, "downloading")

        if not download_video(video_url, str(input_path)):
            set_job_status(job_id, "failed", {"error": "Failed to download video"})
            return

        set_job_status(job_id, "processing")

        duration = get_video_duration(str(input_path))
        if duration == 0:
            set_job_status(job_id, "failed", {"error": "Could not determine video duration"})
            return

        keep_segs = []
        for seg in keep:
            start_s = time_to_seconds(seg["start"])
            end_s = time_to_seconds(seg["end"])
            if start_s >= duration:
                set_job_status(job_id, "failed", {
                    "error": f"Segment start '{seg['start']}' exceeds video duration ({duration:.2f}s)"
                })
                return
            keep_segs.append({"start": start_s, "end": min(end_s, duration), "label": seg.get("label", "")})

        keep_segs.sort(key=lambda x: x["start"])

        if not cut_video(str(input_path), keep_segs, str(output_path)):
            set_job_status(job_id, "failed", {"error": "Failed to assemble video segments"})
            return

        output_size = os.path.getsize(output_path)
        total_kept = sum(s["end"] - s["start"] for s in keep_segs)

        set_job_status(job_id, "done", {
            "output_filename": output_filename,
            "download_url": f"/download-output/{output_filename}",
            "original_duration_seconds": round(duration, 2),
            "output_duration_seconds": round(total_kept, 2),
            "segments_kept": len(keep_segs),
            "output_size_mb": round(output_size / (1024 * 1024), 2),
        })

    except Exception as e:
        set_job_status(job_id, "failed", {"error": str(e)})
    finally:
        cleanup_files(str(input_path))


def run_cut_video(job_id: str, video_url: str, cuts: List[dict]):
    input_path = TEMP_DIR / f"input_{job_id}.mp4"
    output_filename = f"output_{job_id}.mp4"
    output_path = TEMP_DIR / output_filename

    try:
        set_job_status(job_id, "downloading")

        if not download_video(video_url, str(input_path)):
            set_job_status(job_id, "failed", {"error": "Failed to download video"})
            return

        set_job_status(job_id, "processing")

        duration = get_video_duration(str(input_path))
        if duration == 0:
            set_job_status(job_id, "failed", {"error": "Could not determine video duration"})
            return

        keep_segments = create_keep_segments(cuts, duration)

        if not cut_video(str(input_path), keep_segments, str(output_path)):
            set_job_status(job_id, "failed", {"error": "Failed to cut video"})
            return

        output_size = os.path.getsize(output_path)

        set_job_status(job_id, "done", {
            "output_filename": output_filename,
            "download_url": f"/download-output/{output_filename}",
            "original_duration_seconds": round(duration, 2),
            "cuts_applied": len(cuts),
            "segments_kept": len(keep_segments),
            "output_size_mb": round(output_size / (1024 * 1024), 2),
        })

    except Exception as e:
        set_job_status(job_id, "failed", {"error": str(e)})
    finally:
        cleanup_files(str(input_path))


# ─────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────

@app.get("/")
def home():
    return {"status": "online", "message": "Video automation API is running!", "version": "0.8.0"}

@app.get("/health")
def health_check():
    return {"status": "healthy"}

@app.get("/test-ffmpeg")
def test_ffmpeg():
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
        return {"ffmpeg_installed": True, "version": result.stdout.split("\n")[0]}
    except FileNotFoundError:
        return {"ffmpeg_installed": False, "error": "FFmpeg not found"}


@app.post("/keep-segments", status_code=202)
async def keep_segments_endpoint(request: KeepVideoRequest):
    """
    Async. Returns a job_id immediately — poll GET /job/{job_id} until status == 'done'.

    Statuses: queued → downloading → processing → done | failed
    """
    job_id = uuid.uuid4().hex

    write_job(job_id, {
        "job_id": job_id,
        "status": "queued",
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
    })

    thread = threading.Thread(
        target=run_keep_segments,
        args=(job_id, request.video_url, [seg.dict() for seg in request.keep]),
        daemon=True
    )
    thread.start()

    return {
        "job_id": job_id,
        "status": "queued",
        "status_url": f"/job/{job_id}",
        "message": "Job queued. Poll status_url until status is 'done', then use download_url."
    }


@app.post("/cut-video", status_code=202)
async def execute_cut(request: CutVideoRequest):
    """
    Async. Returns a job_id immediately — poll GET /job/{job_id} until status == 'done'.

    Statuses: queued → downloading → processing → done | failed
    """
    job_id = uuid.uuid4().hex

    write_job(job_id, {
        "job_id": job_id,
        "status": "queued",
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
    })

    thread = threading.Thread(
        target=run_cut_video,
        args=(job_id, request.video_url, [cut.dict() for cut in request.cuts]),
        daemon=True
    )
    thread.start()

    return {
        "job_id": job_id,
        "status": "queued",
        "status_url": f"/job/{job_id}",
        "message": "Job queued. Poll status_url until status is 'done', then use download_url."
    }


@app.get("/job/{job_id}")
def get_job_status(job_id: str):
    """
    Poll this to check job progress.

    Possible statuses:
      queued       → accepted, not started yet
      downloading  → fetching video from URL
      processing   → cutting / encoding
      done         → finished, download_url is populated
      failed       → something went wrong, see 'error' field
    """
    job = read_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return job


@app.get("/download-output/{filename}")
def download_output(filename: str):
    safe_filename = Path(filename).name
    file_path = TEMP_DIR / safe_filename

    if not file_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"File '{safe_filename}' not found. It may have been cleaned up or never created."
        )

    return FileResponse(
        path=str(file_path),
        media_type="video/mp4",
        filename=safe_filename,
        headers={"Content-Disposition": f"attachment; filename={safe_filename}"}
    )


@app.post("/extract-frames")
async def extract_frames_endpoint(request: ExtractFramesRequest):
    """
    Synchronous — returns frames directly.
    Only suitable for short clips; long videos will still timeout on the client side.
    """
    job_id = uuid.uuid4().hex
    video_path = TEMP_DIR / f"frames_{job_id}.mp4"

    try:
        if not download_video(request.video_url, str(video_path)):
            raise HTTPException(status_code=400, detail="Failed to download video")

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            raise HTTPException(status_code=500, detail="Failed to open video")

        video_fps = cap.get(cv2.CAP_PROP_FPS)
        frame_interval = int(video_fps / request.fps)
        frames = []
        frame_count = 0
        extracted_count = 0

        while extracted_count < request.max_frames:
            ret, frame = cap.read()
            if not ret:
                break
            if frame_count % frame_interval == 0:
                height, width = frame.shape[:2]
                new_width = 640
                new_height = int(height * (new_width / width))
                frame_resized = cv2.resize(frame, (new_width, new_height))
                _, buffer = cv2.imencode(".jpg", frame_resized, [cv2.IMWRITE_JPEG_QUALITY, 85])
                frames.append(base64.b64encode(buffer).decode("utf-8"))
                extracted_count += 1
            frame_count += 1

        cap.release()

        if not frames:
            raise HTTPException(status_code=500, detail="Failed to extract frames from video")

        return {
            "status": "success",
            "frames_count": len(frames),
            "frames": frames,
            "fps": request.fps,
            "max_frames": request.max_frames,
            "note": "Frames are base64 encoded JPEGs (640px width)"
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")
    finally:
        cleanup_files(str(video_path))
