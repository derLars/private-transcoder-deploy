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

The server listens on port **9009** by default.

### 1. Start Transcoding

You can use either **GET** or **POST** (recommended).

**Option A: POST (Recommended for complex paths)**
**Endpoint:** `POST /transcode`
**Body (JSON):**
```json
{
  "input": "/mnt/media/movie.mp4",
  "output": "/mnt/media/movie.mkv"
}
```

**Example:**
```bash
curl -X POST "http://<container-ip>:9009/transcode" \
     -H "Content-Type: application/json" \
     -d '{"input": "/mnt/media/movie.mp4", "output": "/mnt/media/movie.mkv"}'
```

**Option B: GET (Query Parameters)**
**Endpoint:** `GET /transcode`
**Parameters:**
- `input`: Absolute path to the source file.
- `output`: Absolute path to the destination file.

**Example:**
```bash
curl -g "http://<container-ip>:9009/transcode?input=/mnt/media/movie.mp4&output=/mnt/media/movie.mkv"
```
*Note: Use `-g` with curl if your filenames contain square brackets `[]`.*

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

## Manual Update

To update the server, simply run the installation script again. It will detect the existing installation and pull the latest changes from the repository only if needed.

## Service Management & Logs

You can manage the service using standard `systemctl` commands:

- **Check Service Status**:
    ```bash
    systemctl status transcode
    ```

- **View Real-time Logs**:
    ```bash
    journalctl -u transcode -f
    ```

- **Restart Service**:
    ```bash
    systemctl restart transcode
    ```

- **Stop Service**:
    ```bash
    systemctl stop transcode
    ```
