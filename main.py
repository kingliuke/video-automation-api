from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import subprocess
import requests
import os
from pathlib import Path

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

@app.get("/")
def home():
    return {"status": "online", "message": "Video automation API is running!", "version": "0.2.0"}

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

@app.post("/download-video")
def test_download(video_url: str):
    """Test endpoint to download a video"""
    video_id = "test_video"
    video_path = TEMP_DIR / f"{video_id}.mp4"
    
    success = download_video(video_url, str(video_path))
    
    if success:
        # Check file size
        file_size = os.path.getsize(video_path)
        return {
            "status": "success",
            "video_path": str(video_path),
            "file_size_mb": round(file_size / (1024 * 1024), 2)
        }
    else:
        raise HTTPException(status_code=400, detail="Failed to download video")

@app.post("/process-video")
def process_video(video_url: str, cut_instructions: dict):
    return {"status": "received", "video_url": video_url, "message": "Processing!"}
