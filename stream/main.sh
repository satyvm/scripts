#!/bin/bash

# ==========================================
# DYNAMIC PATH RESOLUTION (PORTABILITY)
# ==========================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"

# ==========================================
# LOAD ENVIRONMENT VARIABLES
# ==========================================
ENV_FILE="$SCRIPT_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
else
    echo -e "\e[31m❌ Fatal Error: .env file not found!\e[0m"
    echo "Expected location: $ENV_FILE"
    exit 1
fi

# ==========================================
# CONFIGURATION (edit these to tune behavior)
# ==========================================

# --- Paths ---
AUDIO_FILE="$SCRIPT_DIR/lofi.m4a"
VIDEO_DEVICE="/dev/video0"
DATA_FILE="$SCRIPT_DIR/.stream_focus.dat"

# --- Temp files (RAM disk to avoid SD card wear) ---
FOCUS_PNG_TMP="/dev/shm/stream_focus_time.tmp.png"
FOCUS_PNG_FILE="/dev/shm/stream_focus_time.png"
FFMPEG_LOG="/dev/shm/ffmpeg_stream.log"

# --- Stream Resolution (default: 480p, change with [1] [2] [3] in TUI) ---
# 480p = raw YUYV, lowest CPU | 720p = MJPEG, moderate | 1080p = MJPEG, heavy
STREAM_RES="480p"

# --- Health Thresholds ---
# vcgencmd measure_temp reads the SoC (CPU+GPU on same die) temperature
TEMP_WARN=65        # °C — SoC warning
TEMP_EXTREME=75     # °C — SoC extreme (Pi throttles hard at 80°C)
CPU_WARN=85         # % — CPU usage warning
CPU_EXTREME=95      # % — CPU usage extreme
RAM_WARN=85         # % — RAM usage warning
RAM_EXTREME=95      # % — RAM usage extreme

# --- Discord Webhook ---
WEBHOOK_COOLDOWN=300    # seconds between webhook alerts (5 minutes)

# --- Overlay Style ---
OVERLAY_BG='#000000C0'  # semi-transparent black background
OVERLAY_FG='white'      # text color
OVERLAY_SIZE=20         # font pointsize

# ==========================================
# AUTO-DETECTION
# ==========================================

# Auto-detect a usable font (search common Pi OS paths)
FONT_FILE=""
for f in \
    /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf \
    /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf \
    /usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf \
    /usr/share/fonts/truetype/freefont/FreeSansBold.ttf \
    /usr/share/fonts/truetype/piboto/Piboto-Bold.ttf \
    /usr/share/fonts/truetype/noto/NotoSans-Bold.ttf; do
    if [ -f "$f" ]; then
        FONT_FILE="$f"
        break
    fi
done

# ==========================================
# PRE-FLIGHT ERROR CHECKS
# ==========================================
for cmd in ffmpeg vcgencmd curl; do
    if ! command -v "$cmd" &> /dev/null; then
        echo -e "\e[31m❌ Fatal Error: '$cmd' is not installed or not in PATH.\e[0m"
        exit 1
    fi
done

# Auto-detect ImageMagick: prefer 'magick' (v7), fall back to 'convert' (v6)
if command -v magick &> /dev/null; then
    IM_CMD="magick"
elif command -v convert &> /dev/null; then
    IM_CMD="convert"
else
    echo -e "\e[31m❌ Fatal Error: ImageMagick is not installed (neither 'magick' nor 'convert' found).\e[0m"
    echo "Install it with: sudo apt install imagemagick"
    exit 1
fi

if [ -z "$TWITCH_KEY" ] || [ "$TWITCH_KEY" == "YOUR_TWITCH_KEY_HERE" ]; then
    echo -e "\e[31m❌ Fatal Error: TWITCH_KEY is missing or default in the .env file.\e[0m"
    exit 1
fi

if [ ! -f "$AUDIO_FILE" ]; then
    echo -e "\e[31m❌ Fatal Error: Audio file not found at: $AUDIO_FILE\e[0m"
    exit 1
fi

if [ -z "$FONT_FILE" ]; then
    echo -e "\e[33m⚠ Warning: No TTF font found. Using ImageMagick built-in font.\e[0m"
fi

# ==========================================
# STATE & VARIABLES
# ==========================================
STREAM_RUNNING=false
FOCUS_RUNNING=false
FFMPEG_PID=""

# Timekeeping
TODAY=$(date +%Y-%m-%d)
TOTAL_FOCUS_SECONDS=0
FOCUS_START_EPOCH=0
DISPLAY_SECONDS=0
LAST_FFMPEG_STR=""

# Hardware Polling & Health Monitoring
POLL_TICK=0
TEMP_STR="0.0"
CPU_PCT="0"
RAM_PCT="0"
THROTTLE_HEX="0x0"
CLOCK_MHZ="0"
HEALTH_LEVEL="NORMAL"
LAST_WEBHOOK_EPOCH=0
PREV_CPU_IDLE=0
PREV_CPU_TOTAL=0
LOG_MSG="Ready to start."

# ==========================================
# INITIALIZATION & DAILY LOAD
# ==========================================
if [ -f "$DATA_FILE" ]; then
    read -r FILE_DATE FILE_SECS < "$DATA_FILE"
    if [ "$FILE_DATE" = "$TODAY" ]; then
        TOTAL_FOCUS_SECONDS=$FILE_SECS
        DISPLAY_SECONDS=$FILE_SECS
    fi
fi

# Build the font argument conditionally (empty string = use ImageMagick built-in)
IM_FONT_ARG=()
if [ -n "$FONT_FILE" ]; then
    IM_FONT_ARG=(-font "$FONT_FILE")
fi

# Generate the initial overlay image so FFmpeg doesn't crash on startup
$IM_CMD -background "$OVERLAY_BG" -fill "$OVERLAY_FG" "${IM_FONT_ARG[@]}" -pointsize "$OVERLAY_SIZE" \
       label:"Focus: 00H:00M" \
       "PNG32:$FOCUS_PNG_FILE" 2>/dev/shm/im_error.log

if [ ! -f "$FOCUS_PNG_FILE" ]; then
    echo -e "\e[31m❌ Fatal Error: ImageMagick failed to create overlay PNG.\e[0m"
    echo "Command: $IM_CMD"
    [ -f /dev/shm/im_error.log ] && cat /dev/shm/im_error.log
    exit 1
fi

# ==========================================
# RESOLUTION HELPER
# ==========================================
get_res_settings() {
    case $STREAM_RES in
        "480p")
            RES_SIZE="640x480"
            RES_FMT="yuyv422"    # raw — no CPU decode overhead
            RES_FPS=24
            RES_BITRATE="1500k"
            ;;
        "720p")
            RES_SIZE="1280x720"
            RES_FMT="mjpeg"      # compressed — needed for USB bandwidth
            RES_FPS=24
            RES_BITRATE="2500k"
            ;;
        "1080p")
            RES_SIZE="1920x1080"
            RES_FMT="mjpeg"      # compressed — heavy on Pi 3B
            RES_FPS=15
            RES_BITRATE="4000k"
            ;;
    esac
}

# ==========================================
# CLEANUP & TRAP
# ==========================================
cleanup() {
    if [ -n "$FFMPEG_PID" ] && kill -0 "$FFMPEG_PID" 2>/dev/null; then
        kill "$FFMPEG_PID"
    fi

    if [ "$FOCUS_RUNNING" = true ]; then
        NOW=$(date +%s)
        CURRENT_SESSION_SECONDS=$((NOW - FOCUS_START_EPOCH))
        DISPLAY_SECONDS=$((TOTAL_FOCUS_SECONDS + CURRENT_SESSION_SECONDS))
    fi
    echo "$TODAY $DISPLAY_SECONDS" > "$DATA_FILE"

    rm -f "$FOCUS_PNG_FILE" "$FOCUS_PNG_TMP"
    tput cnorm
    # Restore original terminal settings (fixes "commands not visible" after exit)
    stty "$ORIG_STTY"
    echo ""
    echo "Stream manager closed gracefully."
    exit 0
}
trap cleanup INT TERM

# ==========================================
# MINIMALIST UI RENDERER
# ==========================================
update_ui() {
    tput cup 0 0

    echo -e "\e[1m  Stream & Focus Manager\e[0m                           "
    echo -e "\e[90m--------------------------------------------------\e[0m"

    echo -n "  [S] Stream : "
    if [ "$STREAM_RUNNING" = true ]; then
        echo -e "\e[32mRUNNING\e[0m        "
    else
        echo -e "\e[31mSTOPPED\e[0m        "
    fi
    tput cup 2 26
    echo -e "SoC : \e[33m${TEMP_STR}°C\e[0m    "

    tput cup 3 0
    echo -n "  [F] Focus  : "
    if [ "$FOCUS_RUNNING" = true ]; then
        echo -e "\e[32mACTIVE \e[0m        "
    else
        echo -e "\e[33mPAUSED \e[0m        "
    fi

    TUI_H=$((DISPLAY_SECONDS / 3600))
    TUI_M=$(((DISPLAY_SECONDS % 3600) / 60))
    TUI_S=$((DISPLAY_SECONDS % 60))
    tput cup 3 26
    printf "Time : \e[36m%02d:%02d:%02d\e[0m   \n" $TUI_H $TUI_M $TUI_S

    echo -n "  [Q] Quit                 "
    tput cup 4 26
    printf "CPU : \e[36m%3s%%\e[0m RAM : \e[36m%3s%%\e[0m  \n" "$CPU_PCT" "$RAM_PCT"

    echo -n "  [1/2/3] Res : "
    echo -e "\e[35m${STREAM_RES}\e[0m          "
    tput cup 5 26
    printf "Clk : \e[36m%s\e[0m MHz  \n" "$CLOCK_MHZ"

    echo -e "\e[90m--------------------------------------------------\e[0m"
    tput el
    echo "  Log: $LOG_MSG"
}

# ==========================================
# HEALTH MONITORING FUNCTIONS
# ==========================================
check_health() {
    # --- Gather Metrics ---
    TEMP_STR=$(vcgencmd measure_temp 2>/dev/null | grep -oE '[0-9.]+' || echo "0.0")
    RAM_PCT=$(free | awk '/Mem:/ {if($2>0) printf "%.0f", $3*100/$2; else print "0"}')
    THROTTLE_HEX=$(vcgencmd get_throttled 2>/dev/null | awk -F= '{print $2}')
    THROTTLE_HEX=${THROTTLE_HEX:-0x0}
    CLOCK_MHZ=$(vcgencmd measure_clock arm 2>/dev/null | awk -F= '{printf "%.0f", $2/1000000}')
    CLOCK_MHZ=${CLOCK_MHZ:-0}

    # CPU usage via /proc/stat delta (fast, no subprocess overhead)
    local cpu_line
    read -r cpu_line < /proc/stat
    set -- $cpu_line; shift
    # $1=user $2=nice $3=system $4=idle $5=iowait $6=irq $7=softirq $8=steal
    local idle=$(($4 + $5)) total=0 val
    for val in "$@"; do total=$((total + val)); done
    local d_idle=$((idle - PREV_CPU_IDLE)) d_total=$((total - PREV_CPU_TOTAL))
    [ "$d_total" -gt 0 ] && CPU_PCT=$(( (d_total - d_idle) * 100 / d_total )) || CPU_PCT=0
    PREV_CPU_IDLE=$idle
    PREV_CPU_TOTAL=$total

    # --- Determine Health Level ---
    local temp_int=${TEMP_STR%.*}
    local level="NORMAL"
    local reasons=""

    # Temperature (SoC = combined CPU+GPU)
    if [ "${temp_int:-0}" -ge "$TEMP_EXTREME" ]; then
        level="EXTREME"; reasons="${reasons}Temp:${TEMP_STR}°C "
    elif [ "${temp_int:-0}" -ge "$TEMP_WARN" ]; then
        level="WARNING"; reasons="${reasons}Temp:${TEMP_STR}°C "
    fi

    # CPU
    if [ "${CPU_PCT:-0}" -ge "$CPU_EXTREME" ]; then
        level="EXTREME"; reasons="${reasons}CPU:${CPU_PCT}% "
    elif [ "${CPU_PCT:-0}" -ge "$CPU_WARN" ]; then
        [ "$level" != "EXTREME" ] && level="WARNING"
        reasons="${reasons}CPU:${CPU_PCT}% "
    fi

    # RAM
    if [ "${RAM_PCT:-0}" -ge "$RAM_EXTREME" ]; then
        level="EXTREME"; reasons="${reasons}RAM:${RAM_PCT}% "
    elif [ "${RAM_PCT:-0}" -ge "$RAM_WARN" ]; then
        [ "$level" != "EXTREME" ] && level="WARNING"
        reasons="${reasons}RAM:${RAM_PCT}% "
    fi

    # Throttle flags (bits 0-3 = current state)
    local thr_int=$((THROTTLE_HEX))
    if [ $((thr_int & 1)) -ne 0 ]; then
        level="EXTREME"; reasons="${reasons}Undervoltage! "
    fi
    if [ $((thr_int & 4)) -ne 0 ]; then
        [ "$level" != "EXTREME" ] && level="WARNING"
        reasons="${reasons}Throttled "
    fi
    if [ $((thr_int & 2)) -ne 0 ]; then
        [ "$level" != "EXTREME" ] && level="WARNING"
        reasons="${reasons}FreqCapped "
    fi

    # --- Recovery Check ---
    if [ "$level" = "NORMAL" ] && [ "$HEALTH_LEVEL" != "NORMAL" ]; then
        LOG_MSG="✅ Health returned to normal."
    fi

    # --- Actions on Warning/Extreme ---
    if [ "$level" != "NORMAL" ]; then
        local now=$(date +%s)
        if [ $((now - LAST_WEBHOOK_EPOCH)) -ge "$WEBHOOK_COOLDOWN" ]; then
            LAST_WEBHOOK_EPOCH=$now
            send_health_webhook "$level" "$reasons"
        fi

        if [ "$level" = "EXTREME" ] && [ "$STREAM_RUNNING" = true ]; then
            kill "$FFMPEG_PID" 2>/dev/null
            STREAM_RUNNING=false
            if [ "$FOCUS_RUNNING" = true ]; then
                FOCUS_RUNNING=false
                TOTAL_FOCUS_SECONDS=$DISPLAY_SECONDS
                echo "$TODAY $TOTAL_FOCUS_SECONDS" > "$DATA_FILE"
            fi
            LOG_MSG="🛑 EXTREME: Stream and focus auto-stopped! ${reasons}"
        elif [ "$level" = "EXTREME" ]; then
            LOG_MSG="🔴 EXTREME: ${reasons}"
        else
            LOG_MSG="⚠️ WARNING: ${reasons}"
        fi
    fi

    HEALTH_LEVEL="$level"
}

send_health_webhook() {
    local level="$1" reasons="$2"
    local color emoji stream_status

    if [ "$level" = "EXTREME" ]; then
        color=16711680; emoji="🛑"
    else
        color=16744448; emoji="⚠️"
    fi
    [ "$STREAM_RUNNING" = true ] && stream_status="Running" || stream_status="Stopped"

    # Decode throttle flags for human-readable output
    local thr_desc="None" thr_int=$((THROTTLE_HEX))
    if [ "$thr_int" -ne 0 ]; then
        thr_desc=""
        [ $((thr_int & 1)) -ne 0 ] && thr_desc="${thr_desc}Under-voltage "
        [ $((thr_int & 2)) -ne 0 ] && thr_desc="${thr_desc}Freq-capped "
        [ $((thr_int & 4)) -ne 0 ] && thr_desc="${thr_desc}Throttled "
        [ $((thr_int & 8)) -ne 0 ] && thr_desc="${thr_desc}Soft-temp-limit "
        : "${thr_desc:=$THROTTLE_HEX}"
    fi

    curl -s -H "Content-Type: application/json" -X POST -d "{
        \"embeds\": [{
            \"title\": \"${emoji} Pi Health: ${level}\",
            \"color\": ${color},
            \"fields\": [
                {\"name\": \"🌡 SoC Temp\", \"value\": \"${TEMP_STR}°C\", \"inline\": true},
                {\"name\": \"💻 CPU\", \"value\": \"${CPU_PCT}%\", \"inline\": true},
                {\"name\": \"🧠 RAM\", \"value\": \"${RAM_PCT}%\", \"inline\": true},
                {\"name\": \"⚡ Throttle\", \"value\": \"${thr_desc}\", \"inline\": true},
                {\"name\": \"🔄 Clock\", \"value\": \"${CLOCK_MHZ} MHz\", \"inline\": true},
                {\"name\": \"📡 Stream\", \"value\": \"${stream_status} (${STREAM_RES})\", \"inline\": true}
            ]
        }]
    }" "$DISCORD_URL" > /dev/null 2>&1 &
}

# ==========================================
# MAIN INITIALIZATION
# ==========================================
ORIG_STTY=$(stty -g)   # save terminal state for clean restore on exit
tput civis
stty -echo
clear

# ==========================================
# MAIN LOOP
# ==========================================
while true; do
    # --- DAILY RESET CHECK ---
    CURRENT_TODAY=$(date +%Y-%m-%d)
    if [ "$CURRENT_TODAY" != "$TODAY" ]; then
        TODAY=$CURRENT_TODAY
        TOTAL_FOCUS_SECONDS=0
        DISPLAY_SECONDS=0
        if [ "$FOCUS_RUNNING" = true ]; then
            FOCUS_START_EPOCH=$(date +%s)
        fi
        LOG_MSG="Midnight reset: Focus timer cleared."
    fi

    # --- TIME CALCULATION ---
    if [ "$FOCUS_RUNNING" = true ]; then
        NOW=$(date +%s)
        CURRENT_SESSION_SECONDS=$((NOW - FOCUS_START_EPOCH))
        DISPLAY_SECONDS=$((TOTAL_FOCUS_SECONDS + CURRENT_SESSION_SECONDS))
    fi

    # --- DYNAMIC IMAGE GENERATION ---
    FF_H=$((DISPLAY_SECONDS / 3600))
    FF_M=$(((DISPLAY_SECONDS % 3600) / 60))
    CURRENT_FFMPEG_STR=$(printf "Focus: %02dH:%02dM" $FF_H $FF_M)

    if [ "$CURRENT_FFMPEG_STR" != "$LAST_FFMPEG_STR" ]; then
        # Create image to a temp file first
        $IM_CMD -background "$OVERLAY_BG" -fill "$OVERLAY_FG" "${IM_FONT_ARG[@]}" -pointsize "$OVERLAY_SIZE" \
               label:"$CURRENT_FFMPEG_STR" \
               "PNG32:$FOCUS_PNG_TMP" 2>/dev/null
        # Atomic move ensures FFmpeg doesn't try to read an incomplete file and crash
        if [ -f "$FOCUS_PNG_TMP" ]; then
            mv "$FOCUS_PNG_TMP" "$FOCUS_PNG_FILE"
        fi
        LAST_FFMPEG_STR="$CURRENT_FFMPEG_STR"
    fi

    # --- HEALTH MONITORING (every ~3 seconds) ---
    POLL_TICK=$((POLL_TICK + 1))
    if [ $POLL_TICK -ge 3 ]; then
        POLL_TICK=0
        check_health
    fi

    # --- PROCESS MONITORING ---
    if [ "$STREAM_RUNNING" = true ]; then
        if ! kill -0 "$FFMPEG_PID" 2>/dev/null; then
            STREAM_RUNNING=false
            if [ "$FOCUS_RUNNING" = true ]; then
                FOCUS_RUNNING=false
                TOTAL_FOCUS_SECONDS=$DISPLAY_SECONDS
                echo "$TODAY $TOTAL_FOCUS_SECONDS" > "$DATA_FILE"
            fi
            LOG_MSG="❌ FFmpeg crashed! Stream and focus stopped."
        fi
    fi

    # --- DRAW UI ---
    update_ui

    # --- HANDLE USER INPUT ---
    read -t 1 -n 1 key
    case $key in
        s|S)
            if [ "$STREAM_RUNNING" = true ]; then
                kill "$FFMPEG_PID" 2>/dev/null
                STREAM_RUNNING=false
                if [ "$FOCUS_RUNNING" = true ]; then
                    FOCUS_RUNNING=false
                    TOTAL_FOCUS_SECONDS=$DISPLAY_SECONDS
                    echo "$TODAY $TOTAL_FOCUS_SECONDS" > "$DATA_FILE"
                    LOG_MSG="Stream and focus stopped successfully."
                else
                    LOG_MSG="Stream stopped successfully."
                fi
            else
                if [ ! -c "$VIDEO_DEVICE" ]; then
                    LOG_MSG="❌ Error: Camera not found at $VIDEO_DEVICE. Plug it in!"
                elif [ ! -r "$VIDEO_DEVICE" ]; then
                    LOG_MSG="❌ Error: No read permission for $VIDEO_DEVICE."
                else
                    # Verify overlay PNG exists before launching
                    if [ ! -f "$FOCUS_PNG_FILE" ]; then
                        LOG_MSG="❌ Error: Overlay PNG missing. ImageMagick may have failed."
                    else
                    # Resolve resolution settings
                    get_res_settings

                    # OPTIMIZED FFMPEG PIPELINE
                    # 1. Camera input at selected resolution and format.
                    # 2. image2 demuxer with -loop 1 re-reads the PNG each cycle for live timer updates.
                    # 3. overlay composites the pre-rendered image, then format strips alpha for h264_v4l2m2m.
                    ffmpeg -thread_queue_size 512 \
                    -f v4l2 -input_format "$RES_FMT" -video_size "$RES_SIZE" -framerate "$RES_FPS" -i "$VIDEO_DEVICE" \
                    -stream_loop -1 -re -i "$AUDIO_FILE" \
                    -f image2 -loop 1 -framerate 1 -i "$FOCUS_PNG_FILE" \
                    -filter_complex "[0:v][2:v]overlay=W-w-15:15[tmp];[tmp]format=pix_fmts=yuv420p" \
                    -c:v h264_v4l2m2m -b:v "$RES_BITRATE" -g 48 \
                    -c:a copy \
                    -f flv "rtmp://live.twitch.tv/app/$TWITCH_KEY" \
                    > /dev/null 2>"$FFMPEG_LOG" &

                    FFMPEG_PID=$!
                    STREAM_RUNNING=true
                    LOG_MSG="Stream started at ${STREAM_RES}! (PID: $FFMPEG_PID)"
                    fi
                fi
            fi
            ;;
        f|F)
            if [ "$FOCUS_RUNNING" = true ]; then
                FOCUS_RUNNING=false
                TOTAL_FOCUS_SECONDS=$DISPLAY_SECONDS
                echo "$TODAY $TOTAL_FOCUS_SECONDS" > "$DATA_FILE"
                LOG_MSG="Focus time paused."
            else
                FOCUS_RUNNING=true
                FOCUS_START_EPOCH=$(date +%s)
                LOG_MSG="Focus time started."
            fi
            ;;
        1)
            if [ "$STREAM_RUNNING" = true ]; then
                LOG_MSG="⚠️ Stop stream first to change resolution."
            else
                STREAM_RES="480p"
                LOG_MSG="Resolution set to 480p (640x480, raw YUYV)"
            fi
            ;;
        2)
            if [ "$STREAM_RUNNING" = true ]; then
                LOG_MSG="⚠️ Stop stream first to change resolution."
            else
                STREAM_RES="720p"
                LOG_MSG="Resolution set to 720p (1280x720, MJPEG)"
            fi
            ;;
        3)
            if [ "$STREAM_RUNNING" = true ]; then
                LOG_MSG="⚠️ Stop stream first to change resolution."
            else
                STREAM_RES="1080p"
                LOG_MSG="Resolution set to 1080p (1920x1080, MJPEG)"
            fi
            ;;
        q|Q)
            cleanup
            ;;
    esac
done