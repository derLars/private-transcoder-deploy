import os
import subprocess
import json
import argparse
import sys
import threading
import time
from fastapi import FastAPI, HTTPException, Response, status
from fastapi.responses import JSONResponse
import uvicorn

app = FastAPI(title="Transcoding Server")

# Global State
CURRENT_JOB = None
PREVIOUS_JOB = None
JOB_LOCK = threading.Lock()

def get_video_duration_frames(input_path):
    """
    Attempts to get the total number of frames in the video stream.
    Returns (duration_seconds, total_frames).
    """
    cmd = [
        'ffprobe', 
        '-v', 'error', 
        '-select_streams', 'v:0', 
        '-count_packets', 
        '-show_entries', 'stream=nb_read_packets,r_frame_rate,duration', 
        '-of', 'json', 
        input_path
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        streams = data.get('streams', [])
        if not streams:
            return 0, 0
            
        stream = streams[0]
        
        # Try to get frame count directly
        nb_frames = stream.get('nb_read_packets')
        if nb_frames:
            try:
                total_frames = int(nb_frames)
            except ValueError:
                total_frames = 0
        else:
            total_frames = 0
            
        # Get duration
        duration = stream.get('duration')
        if duration:
            try:
                duration_sec = float(duration)
            except ValueError:
                duration_sec = 0.0
        else:
            duration_sec = 0.0

        # Fallback calculation if frames missing but duration and fps exist
        if total_frames == 0 and duration_sec > 0:
            r_frame_rate = stream.get('r_frame_rate', '')
            if '/' in r_frame_rate:
                num, den = map(int, r_frame_rate.split('/'))
                if den > 0:
                    fps = num / den
                    total_frames = int(duration_sec * fps)
            
        return duration_sec, total_frames
        
    except Exception as e:
        print(f"Error probing for frame count: {e}")
        return 0, 0

def run_transcode(input_path, output_path):
    global CURRENT_JOB, PREVIOUS_JOB
    
    # Update status to starting
    with JOB_LOCK:
        if CURRENT_JOB:
            CURRENT_JOB['status'] = 'analyzing'
        
    try:
        # 0. Enforce MKV extension
        base, ext = os.path.splitext(output_path)
        if ext.lower() != '.mkv':
            output_path = base + '.mkv'
            print(f"Enforcing MKV container. Output file changed to: {output_path}")
            with JOB_LOCK:
                if CURRENT_JOB:
                    CURRENT_JOB['output'] = output_path

        # 1. Validation
        if not os.path.exists(input_path):
            raise FileNotFoundError(f"Input file not found: {input_path}")

        # 1b. Ensure output directory exists
        output_dir = os.path.dirname(output_path)
        if output_dir and not os.path.exists(output_dir):
            os.makedirs(output_dir, exist_ok=True)

        # Get total frames for progress
        _, total_frames = get_video_duration_frames(input_path)
        with JOB_LOCK:
            if CURRENT_JOB:
                CURRENT_JOB['total_frames'] = total_frames

        # 2. Probe the file using ffprobe (original logic)
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

        print(f"Transcoding {input_path} to {output_path}...")
        
        # Start FFmpeg process
        with JOB_LOCK:
            if CURRENT_JOB:
                CURRENT_JOB['status'] = 'transcoding'
            
        process = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )

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
        
        if process.returncode != 0:
            stderr_output = process.stderr.read()
            raise subprocess.CalledProcessError(process.returncode, ffmpeg_cmd, output=None, stderr=stderr_output)

        # Success
        with JOB_LOCK:
            PREVIOUS_JOB = {
                'input': input_path,
                'output': output_path,
                'status': 'success',
                'timestamp': time.time()
            }
            CURRENT_JOB = None
            
        print(f"Finished transcoding: {output_path}")

    except Exception as e:
        print(f"Transcoding failed: {e}")
        with JOB_LOCK:
            PREVIOUS_JOB = {
                'input': input_path,
                'output': output_path,
                'status': 'failed',
                'error': str(e),
                'timestamp': time.time()
            }
            CURRENT_JOB = None

@app.get("/transcode", status_code=status.HTTP_202_ACCEPTED)
def start_transcode(input: str, output: str):
    global CURRENT_JOB
    
    if not input or not output:
        raise HTTPException(status_code=400, detail="Missing input or output parameters")

    with JOB_LOCK:
        if CURRENT_JOB is not None:
            raise HTTPException(status_code=409, detail="Server is busy with another transcoding request")
        
        # Initialize job
        CURRENT_JOB = {
            'input': input,
            'output': output,
            'status': 'starting',
            'fps': 0.0,
            'frames_processed': 0,
            'total_frames': 0
        }

    # Start thread
    thread = threading.Thread(target=run_transcode, args=(input, output))
    thread.daemon = True
    thread.start()

    return {"message": "Transcoding started"}

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
