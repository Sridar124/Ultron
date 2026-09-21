# Ultron (Modular)

A safe, always-on Windows voice assistant built for low-risk browser automation. 

It handles YouTube, Google, and Spotify securely without a heavy LLM backend. It intentionally refuses arbitrary commands, file operations, security changes, and messaging to guarantee safety.

## Key Features

- **Push-to-Talk (PTT):** Hold `Ctrl+Space` anytime to capture a command instantly.
- **System Tray Icon:** Live status indicator with right-click control menu.
- **Continuous Background Mode:** Runs silently at Windows startup, wakes on "Activate Ultron".
- **Fuzzy NLP Matching:** Understands synonyms ("find" -> "search", "skip" -> "next").
- **Robust YouTube Control:** Auto-caches video IDs, supports sequential queueing, handles volume/fullscreen.
- **Context-Aware:** Remembers if you are on YouTube or Google.
- **Built-in Extras:** Live weather (`weather Chennai`), spoken timers (`set timer 5 minutes`), and command history.

---

## 1. Installation

1. Install Python (Windows) and ensure **Add Python to PATH** is checked.
2. Open this folder in PowerShell and create the environment:
   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
3. *(Optional but recommended)* Store your YouTube Data API key securely in the Windows Credential Manager so it isn't kept in plain-text config:
   ```powershell
   python ultron.py --set-api-key
   ```
4. Configure your microphone input level. To let Ultron automatically measure your room's ambient noise and set the threshold:
   ```powershell
   python ultron.py --calibrate
   ```

## 2. Usage

### Interactive Mode
To run Ultron with a visible console window (best for testing):
```powershell
python ultron.py
```
You can type commands, or type `voice` to enter the always-listening mode.

### Background Mode (Always-Ready)
To run silently with a system tray icon:
```powershell
python ultron.py --background
```

### Windows Startup Integration
To have Ultron run silently every time you log in, open PowerShell as Administrator and run:
```powershell
.\register_startup_task.ps1
```
Or, you can simply run `control_ultron.ps1 -Action Start`.

## 3. Supported Commands

*You can speak or type these.*

**Navigation:**
- `open youtube`
- `open google`
- `open spotify`
- `go back`

**Search:**
- `search youtube <topic>`
- `search google <topic>`
- `search <topic>` (context-aware based on your current site)

**Media Control (YouTube):**
- `play <song>` (uses YouTube API to directly open the top match)
- `pause` / `resume` / `next`
- `volume up` / `volume down` / `set volume 35`
- `fullscreen`

**Queue (YouTube):**
- `queue add <song>`
- `queue next`
- `queue list`
- `queue clear`

**Spotify (Navigation only):**
- `search spotify <artist>`
- `spotify liked songs`
- `spotify new releases`

**System Extras:**
- `weather <city>`
- `set timer 5 minutes`
- `status` (reports uptime and browser state)
- `history 10` (lists the last 10 commands)
- `help`
- `exit` (deactivates voice mode)

## 4. Configuration

Edit `ultron_config.json` to customize behavior. Changes take effect on the next start.

- `wake_word`: Phrase to enter active mode (default: `"activate ultron"`).
- `ptt_hotkey`: Keyboard shortcut for Push-to-Talk (default: `"ctrl+space"`).
- `speech_silence_rms`: Audio threshold for speech detection. Set to `0` to auto-calibrate on startup.
- `pin_phrase`: (Optional) If set, Ultron will ask for this spoken phrase before allowing commands.
- `youtube_api_key`: Leave blank if using `--set-api-key` (recommended).
- `tts_rate`: Speaking speed (default: `185`).

## 5. Troubleshooting

- **Microphone ignored?** Check your Windows Privacy Settings and ensure Desktop Apps can access the microphone.
- **Selenium browser closes instantly?** Make sure Google Chrome is installed. The `webdriver` will automatically download the matching ChromeDriver.
- **Voice is slow to recognize?** The default Google recognizer requires internet. For offline command processing, install a Vosk model (e.g. `vosk-model-small-en-us-0.15`), extract it, and set `"vosk_model_path"` in `ultron_config.json`.
- **Where are the errors?** Check the `logs/ultron.log` file.
