from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import subprocess

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def home():
    return {"status": "online", "message": "Video automation API is running!", "version": "0.1.0"}

@app.post("/process-video")
def process_video(video_url: str, cut_instructions: dict):
    return {"status": "received", "video_url": video_url, "message": "Processing!"}

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
