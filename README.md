# Private Transcoding Server

A lightweight FastAPI-based video transcoding server designed to run in an LXC container. It accepts transcoding requests via HTTP, processes them using FFmpeg (with hardware acceleration support where available), and provides real-time status updates.

## Features

- **HTTP API**: Simple REST endpoints to start jobs and check status.
- **Queue Management**: Handles one job at a time, rejecting concurrent requests.
- **Real-time Progress**: Tracks FPS and frames processed.
- **Automatic Deployment**: Self-contained installation script for LXC containers (Debian/Ubuntu recommended).

## System Requirements

- **CPU**: Transcoding is computationally expensive. Allocate as many cores as possible. 4+ cores recommended for reasonable 1080p HEVC speeds.
- **RAM**: FFmpeg is memory efficient. **2GB - 4GB** is sufficient for most transcoding tasks.
- **Storage**:
    -   **Container**: 8GB is sufficient for the OS and dependencies.
    -   **Media**: Ensure your media folders (e.g., `/mnt/media`) are mounted into the container with read/write permissions.

## Installation

Run the following command on your LXC container (must be run as root):

```bash
wget -O install.sh https://raw.githubusercontent.com/derLars/private-transcoder-deploy/main/install.sh && bash install.sh
```

This script will:
1.  Install necessary dependencies (`python3`, `ffmpeg`, `git`, `pip`).
2.  Clone the repository to `/opt/transcode`.
3.  Set up and start a systemd service (`transcode.service`).

## API Usage

The server listens on port **8000** by default.

### 1. Start Transcoding
**Endpoint:** `GET /transcode`

**Parameters:**
- `input`: Absolute path to the source file.
- `output`: Absolute path to the destination file.

**Example:**
```bash
curl "http://<container-ip>:8000/transcode?input=/mnt/media/movie.mp4&output=/mnt/media/movie.mkv"
```

### 2. Check Status
**Endpoint:** `GET /status`

Returns the current job status, including progress.

**Example Response:**
```json
{
  "busy": true,
  "input": "/mnt/media/movie.mp4",
  "output": "/mnt/media/movie.mkv",
  "fps": 24.5,
  "frames_processed": 1500,
  "total_frames": 24000,
  "status": "transcoding"
}
```

### 3. Check Previous Job
**Endpoint:** `GET /previous`

Returns the result of the last completed job.

## manual Update

To update the server, simply run the installation script again. It will detect the existing installation and pull the latest changes from the repository only if needed.
