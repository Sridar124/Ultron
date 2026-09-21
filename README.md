# Ultron MVP

A small, safe Windows voice assistant. It can open YouTube, search YouTube or Google, play the top matching YouTube video when configured, open Google, and open Spotify in a browser.

It intentionally refuses arbitrary commands, deleting files, purchases, passwords, security changes, and sending messages.

## Always-ready Windows startup

ULTRON now has two voice states:

- **IDLE** — only `Activate ULTRON` or `Open ULTRON` is considered. Idle audio is never passed to command handling.
- **ACTIVE** — after the wake phrase, ULTRON says “Online, I’m listening,” keeps listening for approved commands, and retains the current browser context.

Say `Deactivate ULTRON`, `ULTRON, deactivate`, `Go to sleep`, or `ULTRON, exit` while active to return to silent IDLE mode. ULTRON explicitly speaks `ULTRON activated.` and `ULTRON deactivated.` for those state changes. Recognition punctuation is ignored, so `ULTRON, exit.` works too. These phrases do not terminate the process. By default, five minutes without an approved command also returns ULTRON to IDLE. Set `"active_timeout_seconds": null` in [ultron_config.json](ultron_config.json) to disable the timeout, or use another positive number of seconds.

A hidden per-user Startup entry was added at Windows’ Startup folder. At your next sign-in it runs [ultron_startup.vbs](ultron_startup.vbs), which launches `pythonw.exe` (no console window). It restarts an unexpected crash after 30 seconds, up to three times, then stops to avoid a restart loop. See `logs/ultron.log` for Python errors and `logs/launcher.log` for launcher events.

The scheduled-task mechanism was attempted but is unavailable on this Windows installation: even a read-only `schtasks /Query` fails with “The system cannot find the path specified.” The Startup-folder entry is the active fallback.

Manually manage it without opening the project folder:

```powershell
& "C:\Users\grida\Documents\Codex\2026-09-17\h\outputs\ultron_mvp\control_ultron.ps1" -Action Status
& "C:\Users\grida\Documents\Codex\2026-09-17\h\outputs\ultron_mvp\control_ultron.ps1" -Action Stop
& "C:\Users\grida\Documents\Codex\2026-09-17\h\outputs\ultron_mvp\control_ultron.ps1" -Action Restart
```

`Stop` leaves a local `ultron.disabled` marker so the crash-recovery launcher does not immediately bring the process back. Use `Start` or `Restart` to remove that marker and enable it again.

### Wake-word reliability

For reliable, low-CPU wake detection, install the `vosk` dependency and download a local English Vosk model (for example `vosk-model-small-en-us-0.15`) from the [official Vosk models page](https://alphacephei.com/vosk/models). Extract it inside this project, then set `"vosk_model_path"` in [ultron_config.json](ultron_config.json) to that model folder. ULTRON then uses offline grammar-restricted recognition for `Activate ULTRON` and `Open ULTRON`; idle-mode speech cannot run commands.

Until a local Vosk model is configured, ULTRON falls back to strict online SpeechRecognition matching for the exact phrase. It is functional but less resistant to false positives and uses more network/CPU than the offline model.

## Setup and run

1. Install the current Windows version of Python from [python.org](https://www.python.org/downloads/windows/). During setup, select **Add Python to PATH**.
2. Open this project folder in File Explorer, then right-click its empty area and select **Open in Terminal**.
3. Install the required dependencies:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

For the always-ready startup launcher, keep this `.venv` folder in the project. It uses `.venv\Scripts\pythonw.exe` so no console window appears.

4. Read this `README.md` file so you understand the approved commands and safety limits.
5. Run the assistant:

```powershell
python ultron.py
```

6. Enable always-ready voice commands by allowing microphone access if Windows asks, then type `voice` once. ULTRON starts in **IDLE**: say `Activate ULTRON`, then continue with approved commands such as `open YouTube` and `search GTA 6` without repeating the wake phrase. Each capture is a short clip recorded through `sounddevice`, turned into an in-memory WAV with `soundfile`, and sent to SpeechRecognition for transcription.

While active, say `Deactivate ULTRON`, `Go to sleep`, or `ULTRON, exit` to return to IDLE. You can also press `Ctrl+C` in the terminal to stop the foreground assistant.

### Voice-mode requirements

Voice mode uses `sounddevice` and `soundfile`; it does **not** use PyAudio, so it works with Python versions where PyAudio has no compatible wheel. `SpeechRecognition` is still responsible for converting your recorded speech to text, and its Google recognizer needs an internet connection.

When you type `voice`, ULTRON waits for audio above the configured speech level, keeps a 0.3-second pre-roll, then captures until it hears 0.8 seconds of silence. This avoids cutting an activation phrase or command at the edge of a fixed recording window. It prints capture and WAV details (`16 kHz`, `PCM_16`, `mono`), the `SpeechRecognition` `AudioData` properties, every successful `SpeechRecognition transcript (en-US): '...'` result, and its normalized active phrase before it is filtered or executed. If transcription fails, it also prints the exact exception type and message. This diagnostic output contains no recording audio; copy those diagnostic lines when reporting a problem.

On Windows, ULTRON uses the built-in SAPI5 speech engine for its spoken replies. Startup logs show either `Speech output ready.` or a precise `Speech output disabled/error:` reason, so a text-only response is diagnosable instead of silent.

All behavior is persisted in [ultron_config.json](ultron_config.json): wake and deactivation phrases, active timeout, recording lengths, speech/noise thresholds, sample rate, microphone device, recognition language, optional Vosk model path, and text-to-speech rate. You never need to enter these in PowerShell. Increase `speech_silence_rms` when background noise starts captures; decrease it only if normal speech is being ignored. `speech_recognition_language` defaults to `en-US`; set it to a supported Google recognition language such as `en-IN` if that better matches how you speak. Changes take effect on the next Windows sign-in or background restart.

### Startup microphone behavior

ULTRON starts from the per-user Windows Startup folder, so it runs in the interactive signed-in session—not as a Windows service—and receives normal desktop microphone permissions. The launcher waits 15 seconds after sign-in before starting ULTRON so Windows can finish initializing audio devices. On each start, `logs/ultron.log` records `version=2026.09.19-startup-audio` and the selected `Audio input device:`. `microphone_device` is `null` by default, meaning Windows' current default input device. If the log identifies the wrong device, set this value to the logged device name or index and restart Windows; the choice then persists.

If voice mode says it cannot access the microphone, go to **Settings → Privacy & security → Microphone** and allow microphone access for desktop apps. Ensure a working input device is selected in **Settings → System → Sound → Input**, then restart Ultron.

Type commands directly at any time. Try `help` first.

Examples: `search Google Chennai weather`, `search latest football news`, and `search YouTube A. R. Rahman songs`. Google searches open Chrome when it is installed; otherwise they open the default browser.

### Controlled YouTube session

YouTube commands (`open YouTube`, `search YouTube <topic>`, and `play <song>`) start or reuse a Selenium-controlled Chrome session. Ultron therefore keeps a browser driver and page handle instead of merely opening a URL; that is the foundation required for future playback controls such as play, pause, next, and volume.

Install the updated dependencies before running Ultron:

```powershell
pip install -r requirements.txt
```

Selenium uses an installed Google Chrome and its browser driver. On first start, Selenium Manager may obtain a compatible driver automatically. If a controlled session cannot start, check that Google Chrome is installed and read the `Controlled YouTube browser error:` line in the terminal. Ultron closes the controlled Chrome session when you exit.

### Low-risk multi-step voice commands

Ultron now keeps short-term browser context in its controlled Chrome session. It understands a small, explicit set of natural-language steps and runs them in order, verifying each one before it proceeds. If a step fails, it announces the failure and stops the remaining steps instead of continuing silently.

Examples:

```text
open YouTube and play my DSA playlist
search YouTube lo-fi music and open first result
pause
volume up
set volume 35
next
go back
fullscreen
```

`open YouTube and play my DSA playlist` first opens YouTube, then locates a matching result (using the configured YouTube API when available, otherwise the first visible YouTube result), opens it, and verifies that the video player starts. Follow-up media commands use that same controlled page, so you do not need to repeat the site or video name. The terminal and speech output say `Confirmed:` for verified actions or `Step failed:` when an action could not be confirmed.

After an action on YouTube, an unqualified `search <topic>` is treated as a YouTube search; say `search Google <topic>` when you want to switch search engines.

Only the low-risk browser scope is enabled in this phase: Chrome/Google, YouTube, Spotify, Google and YouTube searches, YouTube result navigation, and YouTube play/pause/next/volume/fullscreen controls. It does **not** run terminal commands, install software, delete or modify files, alter Windows settings or security, change passwords, access credentials, send messages, make purchases, or control arbitrary applications. Native Spotify playback and arbitrary app launching are also not included.

Successful or failed browser steps use a small rotating set of casual acknowledgement phrases. This is a local random template layer only—there is no LLM integration or new intent/context service.

### Music behavior

`play <song>` uses the official YouTube Data API's `search.list` endpoint to retrieve a real video ID, then opens `youtube.com/watch?v=VIDEO_ID&autoplay=1` in the controlled session. Browser or YouTube autoplay rules can still block sound until you click **Play** once; this is controlled by the website/browser, not Ultron.

To enable top-result playback, set `youtube_api_key` in the local [ultron_config.json](ultron_config.json) file. ULTRON reads it automatically at startup, so no PowerShell command or per-session setup is needed. If the key ever changes, replace only that value and restart Windows (or the background assistant). Keep this local configuration file private and never commit it to a repository or share it.

For a one-off override, an existing `YOUTUBE_API_KEY` environment variable takes precedence over the configuration value.

Without a key, `play <song>` safely falls back to normal YouTube search results.

If `play <song>` falls back even though a key is set, look in the same terminal window for a `YouTube API HTTP ... response:` message. It will show Google's returned reason (for example: API not enabled, quota exhausted, or an invalid/restricted key) while redacting the key itself. The code does not use `videoEmbeddable=true`; it requests a normal video and opens its regular `watch` URL.

For direct playback in the native Spotify app, a later integration needs a Spotify Premium account, Spotify OAuth authorization, and an active Spotify Connect device. Do not put Spotify credentials into this project.

## Next safe features

- A push-to-talk desktop button instead of terminal input
- Spotify app integration after account authorization
- An approved-app launcher with a visible confirmation prompt
- Local command history and a microphone on/off indicator

Do not add unrestricted shell execution or automatic permission grants. Keep a confirmation gate for any action that can spend money, alter files, change settings, send content, or access credentials.
