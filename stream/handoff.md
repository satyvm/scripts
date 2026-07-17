# Stream Manager Handoff

## Context
We are building a lightweight, portable, TUI-driven Twitch stream manager (`main.sh`) designed to run on a Raspberry Pi 3B. It broadcasts a "lofi" focus stream, tracks daily study time, and dynamically overlays a live focus timer onto the video feed. The script uses `ffmpeg` for streaming and `ImageMagick` to generate the timer overlay as a PNG image to avoid FFmpeg's heavy TrueType font renderer (`drawtext`). The script is designed with hardware constraints in mind (RAM disks to avoid SD card wear, uncompressed video to bypass CPU decoding, hardware encoding, atomic file moves).

## Work Completed in this Session
- **Terminal UI / Tmux fix**: Removed `tput smcup`/`rmcup` and `clear` on exit so that terminal command history (and tmux scrollback) is preserved when closing the script via Ctrl+C.
- **ImageMagick Auto-detection**: Added auto-detection for ImageMagick 7 (`magick`) vs 6 (`convert`) since the Pi OS version might have either.
- **Font Auto-detection**: Replaced the hardcoded font path (`DejaVuSans-Bold.ttf`) with a loop that searches common Pi OS font paths. Falls back to ImageMagick's built-in font if no TTF is found.
- **ImageMagick Color & Syntax Fixes**:
  - Changed `rgba(0,0,0,0.6)` to hex `#00000099` for compatibility across ImageMagick versions.
  - Reordered ImageMagick 7 arguments: moved `label:` before `-border` since IM7 requires an image source to be defined before applying operators to it.
- **Error Handling & Logging**:
  - Added existence checks after generating the initial PNG with ImageMagick to fail gracefully instead of crashing FFmpeg.
  - Added a PNG file check before launching the `ffmpeg` pipeline.
  - Redirected `ffmpeg` stderr to `/dev/shm/ffmpeg_stream.log` (on a RAM disk) to diagnose crashes. IM errors are also logged to `/dev/shm/im_error.log`.
- **FFmpeg Input Fix (Round 1)**: Removed invalid `-f image2 -update 1` options. `-update` is an output-only muxer option; using it as an input flag caused `Option not found` and prevented the PNG from being opened at all, cascading into a V4L2 encoder crash.
- **FFmpeg Pixel Format Fix (Round 2)**: Three coordinated fixes:
  1. Forced ImageMagick to output RGBA PNGs (`PNG32:` prefix) instead of grayscale+alpha (`ya16be`), which the overlay filter couldn't properly handle.
  2. Restored `-f image2` demuxer (without `-update 1`) so FFmpeg can re-read the PNG from disk on each loop cycle for live timer updates.
  3. Added explicit `format=pix_fmts=yuv420p` after the overlay filter to strip alpha, since `h264_v4l2m2m` only accepts `yuv420p` and was rejecting `yuva420p`.
- **Focus Mode Auto-Stop**: Updated stream stopping logic to automatically pause the focus mode if it is running when the stream is stopped (via user input, crash, or extreme thermal limits).
- **Stream Overlay Changes**: Updated the focus timer format to `Focus: 00H:00M` and darkened the overlay background to 75% opacity (`#000000C0`) for better visibility.

## Next Steps
- Verify the stream starts and runs successfully on the Raspberry Pi.
- Confirm the overlay timer updates visually on-stream when the minute changes.
- If further crashes occur, check `/dev/shm/ffmpeg_stream.log`.

## Suggested Skills
- `diagnosing-bugs` if the streaming or performance issues persist.

## References
- Main script: [main.sh](file:///Users/s/Developer/personal/scripts/stream/main.sh)

