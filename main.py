from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import subprocess
import requests
import os
from pathlib import Path
import json
from typing import List, Dict

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

@app.get("/download-video")
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

def time_to_seconds(time_str: str) -> float:
    """Convert time string (HH:MM:SS or MM:SS) to seconds"""
    parts = time_str.split(':')
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
    
    # Sort cuts by start time
    sorted_cuts = sorted(cuts, key=lambda x: time_to_seconds(x['start']))
    
    keep_segments = []
    current_time = 0
    
    for cut in sorted_cuts:
        cut_start = time_to_seconds(cut['start'])
        cut_end = time_to_seconds(cut['end'])
        
        # Add segment before this cut
        if current_time < cut_start:
            keep_segments.append({
                "start": current_time,
                "end": cut_start
            })
        
        current_time = max(current_time, cut_end)
    
    # Add final segment after last cut
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
    Cut video based on keep segments using FFmpeg
    Creates a list of segments to keep and concatenates them
    """
    try:
        # Create temp directory for segments
        segment_dir = TEMP_DIR / "segments"
        segment_dir.mkdir(exist_ok=True)
        
        segment_files = []
        
        # Extract each segment
        for i, segment in enumerate(keep_segments):
            segment_file = segment_dir / f"segment_{i}.mp4"
            start = segment['start']
            duration = segment['end'] - segment['start']
            
            # FFmpeg command to extract segment
            cmd = [
                'ffmpeg', '-y',
                '-i', video_path,
                '-ss', str(start),
                '-t', str(duration),
                '-c', 'copy',  # Copy codec (fast, no re-encoding)
                str(segment_file)
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                print(f"Error extracting segment {i}: {result.stderr}")
                return False
            
            segment_files.append(str(segment_file))
        
        # Create concat file
        concat_file = segment_dir / "concat.txt"
        with open(concat_file, 'w') as f:
            for seg_file in segment_files:
                f.write(f"file '{seg_file}'\n")
        
        # Concatenate segments
        cmd = [
            'ffmpeg', '-y',
            '-f', 'concat',
            '-safe', '0',
            '-i', str(concat_file),
            '-c', 'copy',
            output_path
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        # Cleanup segments
        for seg_file in segment_files:
            os.remove(seg_file)
        os.remove(concat_file)
        
        return result.returncode == 0
        
    except Exception as e:
        print(f"Cut video error: {e}")
        return False

@app.post("/cut-video")
async def execute_cut(video_url: str, cuts: List[Dict]):
    """
    Main endpoint to cut video based on cut instructions
    
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
        # Download video
        video_id = "input_video"
        input_path = TEMP_DIR / f"{video_id}.mp4"
        
        success = download_video(video_url, str(input_path))
        if not success:
            raise HTTPException(status_code=400, detail="Failed to download video")
        
        # Get video duration
        duration = get_video_duration(str(input_path))
        if duration == 0:
            raise HTTPException(status_code=400, detail="Could not determine video duration")
        
        # Calculate segments to keep
        keep_segments = create_keep_segments(cuts, duration)
        
        # Cut video
        output_path = TEMP_DIR / f"output_{video_id}.mp4"
        success = cut_video(str(input_path), keep_segments, str(output_path))
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to cut video")
        
        # Get output file size
        output_size = os.path.getsize(output_path)
        
        return {
            "status": "success",
            "original_duration": round(duration, 2),
            "cuts_applied": len(cuts),
            "segments_kept": len(keep_segments),
            "output_path": str(output_path),
            "output_size_mb": round(output_size / (1024 * 1024), 2),
            "message": "Video cut successfully! File is ready for download."
        }
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")

@app.post("/process-video")
def process_video(video_url: str, cut_instructions: dict):
    return {"status": "received", "video_url": video_url, "message": "Processing!"}
