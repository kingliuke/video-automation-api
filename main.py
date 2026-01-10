from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
    return {
        "status": "online",
        "message": "Video automation API is running!",
        "version": "0.1.0"
    }

@app.post("/process-video")
def process_video(video_url: str, cut_instructions: dict):
    return {
        "status": "received",
        "video_url": video_url,
        "cuts_count": len(cut_instructions.get("cuts", [])),
        "message": "Video processing logic coming soon!"
    }

@app.get("/health")
def health_check():
    return {"status": "healthy"}
