import os
import subprocess
import json
import argparse
import sys
import threading
import time
import logging
from fastapi import FastAPI, HTTPException, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import uvicorn

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI(title="Transcoding Server")

# Global State
CURRENT_JOB = None
PREVIOUS_JOB = None
JOB_LOCK = threading.Lock()

class TranscodeRequest(BaseModel):
    input: str
    output: str

def get_video_duration_frames(input_path):
    """
    Attempts to get the total number of frames in the video stream.
    Returns (duration_seconds, total_frames).
    """
    cmd = [
        'ffprobe', 
        '-v', 'error', 
        '-select_streams', 'v:0', 
        '-show_entries', 'stream=r_frame_rate,duration,nb_frames', 
        '-of', 'json', 
        input_path
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        streams = data.get('streams', [])
        if not streams:
            logger.warning("No video streams found")
            return 0, 0
            
        stream = streams[0]
        
        # Get duration
        duration = stream.get('duration')
        if duration:
            try:
                duration_sec = float(duration)
            except ValueError:
                duration_sec = 0.0
        else:
            duration_sec = 0.0

        # Try to get frame count from metadata (fast, but not always present)
        nb_frames = stream.get('nb_frames')
        total_frames = 0
        if nb_frames:
            try:
                total_frames = int(nb_frames)
                logger.info(f"Frame count from metadata: {total_frames}")
            except ValueError:
                pass

        # Calculate from duration * fps if not available
        if total_frames == 0 and duration_sec > 0:
            r_frame_rate = stream.get('r_frame_rate', '')
            if '/' in r_frame_rate:
                try:
                    num, den = map(int, r_frame_rate.split('/'))
                    if den > 0:
                        fps = num / den
                        total_frames = int(duration_sec * fps)
                        logger.info(f"Frame count calculated: {total_frames} (duration: {duration_sec}s, fps: {fps})")
                except ValueError:
                    logger.warning("Could not parse frame rate")
            
        return duration_sec, total_frames
        
    except Exception as e:
        logger.error(f"Error probing for frame count: {e}")
        return 0, 0

def stderr_reader(process):
    """Read stderr in a separate thread to prevent blocking."""
    try:
        for line in process.stderr:
            if line:
                logger.warning(f"FFmpeg: {line.rstrip()}")
    except Exception as e:
        logger.error(f"Error reading stderr: {e}")

def run_transcode(input_path, output_path):
    global CURRENT_JOB, PREVIOUS_JOB
    
    logger.info(f"Starting transcode job: {input_path} -> {output_path}")
    
    # Update status to starting
    with JOB_LOCK:
        if CURRENT_JOB:
            CURRENT_JOB['status'] = 'analyzing'
        
    try:
        # 0. Enforce MKV extension
        base, ext = os.path.splitext(output_path)
        if ext.lower() != '.mkv':
            output_path = base + '.mkv'
            logger.info(f"Enforcing MKV container. Output file changed to: {output_path}")
            with JOB_LOCK:
                if CURRENT_JOB:
                    CURRENT_JOB['output'] = output_path

        # 1. Validation
        if not os.path.exists(input_path):
            raise FileNotFoundError(f"Input file not found: {input_path}")

        logger.info(f"Input file validated: {input_path}")

        # 1b. Ensure output directory exists
        output_dir = os.path.dirname(output_path)
        if output_dir and not os.path.exists(output_dir):
            os.makedirs(output_dir, exist_ok=True)
            logger.info(f"Created output directory: {output_dir}")

        # Get total frames for progress
        logger.info("Probing video for frame count...")
        _, total_frames = get_video_duration_frames(input_path)
        with JOB_LOCK:
            if CURRENT_JOB:
                CURRENT_JOB['total_frames'] = total_frames
        logger.info(f"Total frames: {total_frames}")

        # 2. Probe the file using ffprobe (original logic)
        logger.info("Analyzing video streams...")
        probe_cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', input_path]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
        media_info = json.loads(result.stdout)

        # 3. Identify Audio Streams
        streams = media_info.get('streams', [])
        audio_streams = [s for s in streams if s['codec_type'] == 'audio']
        
        english_audio_index = None

        if audio_streams:
            for stream in audio_streams:
                tags = stream.get('tags', {})
                lang = tags.get('language', '').lower()
                if lang in ['eng', 'en', 'english']:
                    english_audio_index = stream['index']
                    break

        # 4. Construct FFmpeg Command
        ffmpeg_cmd = [
            'ffmpeg',
            '-y',
            '-i', input_path,
            '-map_metadata', '0',
            '-map', '0:v:0',
            '-progress', '-',  # Output progress to stdout
            '-nostats'         # Reduce verbosity
        ]

        if english_audio_index is not None:
            ffmpeg_cmd.extend(['-map', f'0:{english_audio_index}'])
        elif audio_streams:
            ffmpeg_cmd.extend(['-map', '0:a'])

        subtitle_streams = [s for s in streams if s['codec_type'] == 'subtitle']
        if subtitle_streams:
            needs_conversion = any(s.get('codec_name') == 'mov_text' for s in subtitle_streams)
            if needs_conversion:
                ffmpeg_cmd.extend(['-map', '0:s', '-c:s', 'srt'])
            else:
                ffmpeg_cmd.extend(['-map', '0:s', '-c:s', 'copy'])

        ffmpeg_cmd.extend([
            '-c:v', 'libx265',
            '-preset', 'slow',
            '-tag:v', 'hvc1',
            '-pix_fmt', 'yuv420p10le',
            '-vf', 'scale=-2:1080',
            '-c:a', 'aac',
            '-b:a', '192k',
            '-ac', '2',
            output_path
        ])

        logger.info(f"Starting FFmpeg transcode: {input_path} -> {output_path}")
        logger.info(f"FFmpeg command: {' '.join(ffmpeg_cmd)}")
        
        # Start FFmpeg process
        with JOB_LOCK:
            if CURRENT_JOB:
                CURRENT_JOB['status'] = 'transcoding'
        
        logger.info("Launching FFmpeg process...")
        process = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )
        
        logger.info(f"FFmpeg process started (PID: {process.pid})")

        # Start stderr reader thread to prevent blocking
        stderr_thread = threading.Thread(target=stderr_reader, args=(process,))
        stderr_thread.daemon = True
        stderr_thread.start()

        # Read progress from stdout (because of -progress -)
        while True:
            line = process.stdout.readline()
            if not line:
                break
            
            line = line.strip()
            if not line:
                continue

            # Parse key=value lines
            if '=' in line:
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip()
                
                with JOB_LOCK:
                    if CURRENT_JOB:
                        if key == 'frame':
                            try:
                                CURRENT_JOB['frames_processed'] = int(value)
                            except ValueError:
                                pass
                        elif key == 'fps':
                            try:
                                CURRENT_JOB['fps'] = float(value)
                            except ValueError:
                                pass
                        elif key == 'progress':
                            if value == 'end':
                                break

        process.wait()
        logger.info(f"FFmpeg process completed with return code: {process.returncode}")
        
        if process.returncode != 0:
            # stderr already consumed by stderr_thread, so just raise the error
            raise subprocess.CalledProcessError(process.returncode, ffmpeg_cmd)

        # Success
        with JOB_LOCK:
            PREVIOUS_JOB = {
                'input': input_path,
                'output': output_path,
                'status': 'success',
                'timestamp': time.time()
            }
            CURRENT_JOB = None
            
        logger.info(f"Transcoding completed successfully: {output_path}")

    except Exception as e:
        logger.error(f"Transcoding failed: {e}", exc_info=True)
        with JOB_LOCK:
            PREVIOUS_JOB = {
                'input': input_path,
                'output': output_path,
                'status': 'failed',
                'error': str(e),
                'timestamp': time.time()
            }
            CURRENT_JOB = None

def start_job(input_path: str, output_path: str):
    global CURRENT_JOB
    
    # Strip quotes if present
    input_path = input_path.strip("'").strip('"')
    output_path = output_path.strip("'").strip('"')

    if not input_path or not output_path:
        raise HTTPException(status_code=400, detail="Missing input or output parameters")
        
    # Validation check before starting thread to give immediate feedback
    if not os.path.exists(input_path):
        raise HTTPException(status_code=400, detail=f"Input file not found: {input_path}")

    with JOB_LOCK:
        if CURRENT_JOB is not None:
            raise HTTPException(status_code=409, detail="Server is busy with another transcoding request")
        
        # Initialize job
        CURRENT_JOB = {
            'input': input_path,
            'output': output_path,
            'status': 'starting',
            'fps': 0.0,
            'frames_processed': 0,
            'total_frames': 0
        }

    # Start thread
    thread = threading.Thread(target=run_transcode, args=(input_path, output_path))
    thread.daemon = True
    thread.start()

    return {"message": "Transcoding started"}

@app.get("/transcode", status_code=status.HTTP_202_ACCEPTED)
def start_transcode_get(input: str, output: str):
    return start_job(input, output)

@app.post("/transcode", status_code=status.HTTP_202_ACCEPTED)
def start_transcode_post(request: TranscodeRequest):
    return start_job(request.input, request.output)

@app.get("/status")
def get_status():
    with JOB_LOCK:
        if CURRENT_JOB:
            return {
                'busy': True,
                'input': CURRENT_JOB['input'],
                'output': CURRENT_JOB['output'],
                'fps': CURRENT_JOB['fps'],
                'frames_processed': CURRENT_JOB['frames_processed'],
                'total_frames': CURRENT_JOB['total_frames'],
                'status': CURRENT_JOB['status']
            }
        else:
            return {
                'busy': False,
                'status': 'idle'
            }

@app.get("/previous")
def get_previous():
    with JOB_LOCK:
        if PREVIOUS_JOB:
            return PREVIOUS_JOB
        else:
            return {
                'status': 'none',
                'message': 'No previous jobs recorded'
            }

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Transcoding FastAPI Server")
    parser.add_argument("--port", type=int, default=9009, help="Port to listen on")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to listen on")
    args = parser.parse_args()
    
    uvicorn.run(app, host=args.host, port=args.port)
