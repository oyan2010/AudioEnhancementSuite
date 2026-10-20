# Audio Enhancement Automation Suite

A desktop application that automates the full audio enhancement pipeline using Adobe Podcast Enhance, FFmpeg post-processing, and PyQt6.

---

## Quick Start

Double-click **`launch.bat`** — it installs dependencies and launches the app.

Or manually:
```
A:\Python-MainEXE\python.exe -m pip install -r requirements.txt
A:\Python-MainEXE\python.exe main.py
```

---

## Requirements

| Package | Purpose |
|---|---|
| PyQt6 | Desktop UI |
| seleniumbase | Chrome automation (Adobe Podcast) |
| pydub | Audio utilities |
| watchdog | File system monitoring |
| FFmpeg | Audio extraction / processing / export |

**FFmpeg must be installed separately** and available on PATH, or set the full path in Settings.

---

## First-Time Setup

1. **Set FFmpeg path** via `Settings` (if not on PATH)
2. **Add Chrome Profiles**: Click `+ Add Profile` in the sidebar
   - Open Chrome manually with that profile
   - Log in to [Adobe Podcast Enhance](https://podcast.adobe.com/en/enhance)
   - Come back to the app and add the profile's User Data directory + profile folder name
3. **Select your input file** (MP4, MOV, M4V, MP3, WAV, AAC, FLAC, M4A)
4. Click **▶ Start**

---

## Pipeline Stages

```
Import → Extract Audio → Smart Chunk Split → Adobe Enhancement
       → Post-Processing → Merge → Export MP3
```

| Stage | Description |
|---|---|
| Extract | Converts video/audio to 48kHz stereo WAV |
| Split | Splits into ≤29 min chunks at silence points |
| Enhance | Uploads each chunk to Adobe Podcast via SeleniumBase |
| Post-Process | FFmpeg compressor → EQ → pitch shift → limiter → normalize |
| Merge | Concatenates processed chunks in order |
| Export | Encodes final MP3 at 192kbps / 48kHz / stereo |

---

## Folder Structure (per project)

```
~/AudioEnhancementProjects/
└── <filename>_YYYYMMDD_HHMMSS/
    ├── input/          audio_master.wav
    ├── chunks/         chunk_001.wav …
    ├── downloads/      raw Adobe downloads
    ├── enhanced/       chunk_001_enhanced.wav …
    ├── processed/      chunk_001_processed.wav …
    ├── merged/         merged.wav
    ├── output/         final_output.mp3  ← final result
    ├── logs/
    └── temp/
```

---

## Profile Quota Management

- Each Adobe free account = **60 min/day**
- The app tracks usage per profile, resets daily
- Chunks are auto-assigned to a profile with remaining quota
- If all profiles are exhausted, chunks are marked **SKIPPED**

---

## Post-Processing Chain (FFmpeg)

Mirrors the Audacity macro:

| Step | Parameters |
|---|---|
| Compressor | Attack 0.2s · Release 1s · Threshold -41dB · Ratio 2:1 |
| EQ Low | 100–500 Hz → +9 dB |
| EQ High | 4000–5000 Hz → +9 dB |
| Pitch Shift | -1% |
| Hard Limiter | Threshold -2.1 dB |
| Normalize | Peak -2 dB |

---

## Error Handling

| Error | Recovery |
|---|---|
| Upload failure | Retry up to 3 times |
| Download not found | Continue scanning (5s interval, 90 min timeout) |
| Post-proc failure | Retry once, then use source as fallback |
| No quota | Mark chunk SKIPPED, continue |
| Browser crash | Worker catches and retries |
