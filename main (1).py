from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import subprocess
import requests
import os
from pathlib import Path
import json
from typing import List, Dict, Optional
import base64
import cv2

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Create temp directory for video processing
TEMP_DIR = Path("/tmp/videos")
TEMP_DIR.mkdir(exist_ok=True)

# Pydantic models for request validation
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

@app.get("/")
def home():
    return {"status": "online", "message": "Video automation API is running!", "version": "0.6.0"}

@app.get("/health")
def health_check():
    return {"status": "healthy"}

@app.get("/test-ffmpeg")
def test_ffmpeg():
    try:
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
        return {
            "ffmpeg_installed": True,
            "version": result.stdout.split('\n')[0]
        }
    except FileNotFoundError:
        return {
            "ffmpeg_installed": False,
            "error": "FFmpeg not found"
        }

def download_video(video_url: str, output_path: str) -> bool:
    """Download video from URL to specified path"""
    try:
        response = requests.get(video_url, stream=True, timeout=300)
        response.raise_for_status()
        
        with open(output_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        return True
    except Exception as e:
        print(f"Download error: {e}")
        return False

@app.get("/download-video")
def test_download(video_url: str):
    """Test endpoint to download a video"""
    video_id = "test_video"
    video_path = TEMP_DIR / f"{video_id}.mp4"
    
    success = download_video(video_url, str(video_path))
    
    if success:
        file_size = os.path.getsize(video_path)
        return {
            "status": "success",
            "video_path": str(video_path),
            "file_size_mb": round(file_size / (1024 * 1024), 2)
        }
    else:
        raise HTTPException(status_code=400, detail="Failed to download video")

def time_to_seconds(time_str: str) -> float:
    """Convert time string (HH:MM:SS or MM:SS or SS) to seconds"""
    parts = time_str.strip().split(':')
    if len(parts) == 3:  # HH:MM:SS
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    elif len(parts) == 2:  # MM:SS
        return int(parts[0]) * 60 + float(parts[1])
    else:  # Just seconds
        return float(parts[0])

def create_keep_segments(cuts: List[Dict], video_duration: float) -> List[Dict]:
    """
    Given a list of cuts (segments to remove),
    create a list of segments to keep
    """
    if not cuts:
        return [{"start": 0, "end": video_duration}]
    
    sorted_cuts = sorted(cuts, key=lambda x: time_to_seconds(x['start']))
    
    keep_segments = []
    current_time = 0
    
    for cut in sorted_cuts:
        cut_start = time_to_seconds(cut['start'])
        cut_end = time_to_seconds(cut['end'])
        
        if current_time < cut_start:
            keep_segments.append({
                "start": current_time,
                "end": cut_start
            })
        
        current_time = max(current_time, cut_end)
    
    if current_time < video_duration:
        keep_segments.append({
            "start": current_time,
            "end": video_duration
        })
    
    return keep_segments

def get_video_duration(video_path: str) -> float:
    """Get video duration in seconds using FFmpeg"""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries',
             'format=duration', '-of',
             'default=noprint_wrappers=1:nokey=1', video_path],
            capture_output=True,
            text=True
        )
        return float(result.stdout.strip())
    except:
        return 0

def cut_video(video_path: str, keep_segments: List[Dict], output_path: str) -> bool:
    """
    Cut video based on keep segments using FFmpeg concat demuxer.
    Uses -c copy for speed (no re-encoding).
    """
    try:
        segment_dir = TEMP_DIR / "segments"
        segment_dir.mkdir(exist_ok=True)
        
        segment_files = []
        
        for i, segment in enumerate(keep_segments):
            segment_file = segment_dir / f"segment_{i}.mp4"
            start = segment['start']
            duration = segment['end'] - segment['start']
            
            cmd = [
                'ffmpeg', '-y',
                '-i', video_path,
                '-ss', str(start),
                '-t', str(duration),
                '-c', 'copy',
                str(segment_file)
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                print(f"Error extracting segment {i}: {result.stderr}")
                return False
            
            segment_files.append(str(segment_file))
        
        concat_file = segment_dir / "concat.txt"
        with open(concat_file, 'w') as f:
            for seg_file in segment_files:
                f.write(f"file '{seg_file}'\n")
        
        cmd = [
            'ffmpeg', '-y',
            '-f', 'concat',
            '-safe', '0',
            '-i', str(concat_file),
            '-c', 'copy',
            output_path
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        for seg_file in segment_files:
            try:
                os.remove(seg_file)
            except:
                pass
        try:
            os.remove(concat_file)
        except:
            pass
        
        return result.returncode == 0
        
    except Exception as e:
        print(f"Cut video error: {e}")
        return False

def extract_frames_from_video(video_path: str, fps: float = 1.0, max_frames: int = 100) -> List[str]:
    """Extract frames from video and return as base64 strings"""
    frames = []
    cap = cv2.VideoCapture(video_path)
    
    if not cap.isOpened():
        return frames
    
    video_fps = cap.get(cv2.CAP_PROP_FPS)
    frame_interval = int(video_fps / fps)
    
    frame_count = 0
    extracted_count = 0
    
    while extracted_count < max_frames:
        ret, frame = cap.read()
        if not ret:
            break
        
        if frame_count % frame_interval == 0:
            height, width = frame.shape[:2]
            new_width = 640
            new_height = int(height * (new_width / width))
            frame_resized = cv2.resize(frame, (new_width, new_height))
            
            _, buffer = cv2.imencode('.jpg', frame_resized, [cv2.IMWRITE_JPEG_QUALITY, 85])
            frame_base64 = base64.b64encode(buffer).decode('utf-8')
            frames.append(frame_base64)
            extracted_count += 1
        
        frame_count += 1
    
    cap.release()
    return frames

# ─────────────────────────────────────────────
# DOWNLOAD ENDPOINT
# ─────────────────────────────────────────────

@app.get("/download-output/{filename}")
def download_output(filename: str):
    """
    Download a processed video file by filename.
    The filename is returned in the response of /cut-video or /keep-segments.

    Example:
        GET /download-output/output_input_video.mp4
    """
    # Security: strip any path traversal attempts
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

# ─────────────────────────────────────────────
# KEEP SEGMENTS ENDPOINT (direct — no inversion needed)
# ─────────────────────────────────────────────

@app.post("/keep-segments")
async def keep_segments_endpoint(request: KeepVideoRequest):
    """
    Keep only specific segments from a video.
    Provide the segments you WANT to keep directly — no inversion needed.

    Example request body:
    {
        "video_url": "https://example.com/video.mp4",
        "keep": [
            {"start": "31:34", "end": "33:23", "label": "intro"},
            {"start": "34:13", "end": "35:12", "label": "main point"},
            {"start": "56:28", "end": "58:30", "label": "demo"},
            {"start": "59:15", "end": "59:33", "label": "cta"},
            {"start": "01:08:55", "end": "01:09:53", "label": "outro"}
        ]
    }
    """
    try:
        video_id = "keep_video"
        input_path = TEMP_DIR / f"{video_id}.mp4"

        success = download_video(request.video_url, str(input_path))
        if not success:
            raise HTTPException(status_code=400, detail="Failed to download video")

        duration = get_video_duration(str(input_path))
        if duration == 0:
            raise HTTPException(status_code=400, detail="Could not determine video duration")

        # Build keep segments directly from request
        keep_segs = []
        for seg in request.keep:
            start_s = time_to_seconds(seg.start)
            end_s = time_to_seconds(seg.end)

            if start_s >= duration:
                raise HTTPException(
                    status_code=400,
                    detail=f"Segment start '{seg.start}' ({start_s}s) exceeds video duration ({duration:.2f}s)"
                )

            end_s = min(end_s, duration)
            keep_segs.append({"start": start_s, "end": end_s, "label": seg.label})

        # Sort by start time
        keep_segs.sort(key=lambda x: x["start"])

        output_filename = f"output_{video_id}.mp4"
        output_path = TEMP_DIR / output_filename

        success = cut_video(str(input_path), keep_segs, str(output_path))
        if not success:
            raise HTTPException(status_code=500, detail="Failed to assemble video segments")

        output_size = os.path.getsize(output_path)
        total_kept = sum(s["end"] - s["start"] for s in keep_segs)

        return {
            "status": "success",
            "original_duration_seconds": round(duration, 2),
            "segments_kept": len(keep_segs),
            "output_duration_seconds": round(total_kept, 2),
            "output_size_mb": round(output_size / (1024 * 1024), 2),
            "output_filename": output_filename,
            "download_url": f"/download-output/{output_filename}",
            "message": f"Done. Kept {len(keep_segs)} segments totalling {round(total_kept, 2)}s. Hit the download_url to retrieve the file."
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")

# ─────────────────────────────────────────────
# CUT VIDEO ENDPOINT (original — inverted logic)
# ─────────────────────────────────────────────

@app.post("/cut-video")
async def execute_cut(request: CutVideoRequest):
    """
    Remove specific segments from a video (provide segments to CUT OUT).
    For keeping specific segments directly, use /keep-segments instead.

    Example request body:
    {
        "video_url": "https://example.com/video.mp4",
        "cuts": [
            {"start": "00:00:05", "end": "00:00:08", "reason": "filler words"},
            {"start": "00:01:20", "end": "00:01:35", "reason": "looking away"}
        ]
    }
    """
    try:
        video_id = "input_video"
        input_path = TEMP_DIR / f"{video_id}.mp4"
        
        success = download_video(request.video_url, str(input_path))
        if not success:
            raise HTTPException(status_code=400, detail="Failed to download video")
        
        duration = get_video_duration(str(input_path))
        if duration == 0:
            raise HTTPException(status_code=400, detail="Could not determine video duration")
        
        cuts_dict = [cut.dict() for cut in request.cuts]
        keep_segments = create_keep_segments(cuts_dict, duration)
        
        output_filename = f"output_{video_id}.mp4"
        output_path = TEMP_DIR / output_filename

        success = cut_video(str(input_path), keep_segments, str(output_path))
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to cut video")
        
        output_size = os.path.getsize(output_path)
        
        return {
            "status": "success",
            "original_duration_seconds": round(duration, 2),
            "cuts_applied": len(request.cuts),
            "segments_kept": len(keep_segments),
            "output_size_mb": round(output_size / (1024 * 1024), 2),
            "output_filename": output_filename,
            "download_url": f"/download-output/{output_filename}",
            "message": "Video cut successfully. Hit the download_url to retrieve the file."
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")

@app.post("/extract-frames")
async def extract_frames_endpoint(request: ExtractFramesRequest):
    """
    Extract frames from video for vision model analysis.

    Example request body:
    {
        "video_url": "https://example.com/video.mp4",
        "fps": 1.0,
        "max_frames": 100
    }

    Returns base64 encoded JPEG images ready to send to a vision model.
    """
    try:
        video_id = "extract_frames_video"
        video_path = TEMP_DIR / f"{video_id}.mp4"
        
        success = download_video(request.video_url, str(video_path))
        if not success:
            raise HTTPException(status_code=400, detail="Failed to download video")
        
        frames = extract_frames_from_video(str(video_path), request.fps, request.max_frames)
        
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

@app.post("/process-video")
def process_video(video_url: str, cut_instructions: dict):
    return {"status": "received", "video_url": video_url, "message": "Processing!"}
