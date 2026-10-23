# claude-airs-image-bridge

> **EN** | **[Wersja polska (README.pl.md)](README.pl.md)**

Cross-platform (**macOS** & **Windows 11**) local background service and menu-bar / system-tray utility that bridges **Claude Desktop (3P Gateway mode)** and an AI Gateway (`https://aigw.portkey.ai`) when using non-Anthropic models (such as **Google Gemini**, **Gemma**, **GLM**, or **Llama** on Vertex AI).

---

## Problem Statement

When Claude Desktop runs in **3P Gateway mode** with non-Anthropic models exposed via an Anthropic-compatible AI Gateway:

1. **Model naming in Claude Desktop**: Claude Desktop filters out model identifiers that do not begin with `anthropic.` or `claude-`. Non-Anthropic models are therefore declared with `claude-*` aliases in the local config (e.g., `claude-argon`, `claude-pro-3-1`, `claude-flash-3-8`) and mapped to their upstream model IDs (`gemini-4-argon`, `gemini-3.1-pro`, `gemini-3.8-flash`, etc.) via a gateway routing configuration ([`configs/aigw-override-config.json`](configs/aigw-override-config.json)).
2. **`tool_result` image serialization bug**: When Claude Code reads an image (`Read` tool) or captures a screenshot, it sends an `image` (or `document`) block nested inside a `tool_result` block.
   - For native `anthropic.*` models, images inside `tool_result` are processed natively as vision tokens.
   - For Gemini / non-Anthropic models, the Anthropic-to-Gemini gateway adapter serializes images inside `tool_result` as **raw base64 text** (~1.47 characters per token — over 52,000 tokens for a tiny 160x120 PNG, and 300,000+ tokens for a full screenshot). After a few screenshots, the session exceeds the 1,048,576-token context window and fails permanently with `API Error: 400 (no body)`.
   - Placing an image alongside `tool_result` in the same user turn causes the adapter to drop the image silently.
   - Placing the image in a **separate follow-up user turn** works natively with Gemini vision (~1,280 tokens).

### Token Comparison (160x120 PNG, 77,072 base64 chars)

| Scenario | Direct to Gateway | Through `claude-airs-image-bridge` | Model sees image? |
|---|---:|---:|:---:|
| Text-only baseline | 156 tokens | 156 tokens | N/A |
| Image inside `tool_result` | **52,645 tokens** (base64 text) | **1,280 tokens** | Yes |
| Image next to `tool_result` (same turn) | 156 tokens (dropped) | **1,280 tokens** | Yes |

---

## How It Works

```text
+-----------------------+       http://127.0.0.1:8787       +------------------------------+
| Claude Desktop (3P)   | --------------------------------> | claude-airs-image-bridge     |
| - anthropic.claude-*  |                                   | - macOS: launchd + Menu Bar  |
| - claude-argon        | <-------------------------------- | - Win11: Service + Sys Tray  |
+-----------------------+          SSE Stream               +------------------------------+
                                                                           |
                                                                           | HTTPS
                                                                           v
                                                            +------------------------------+
                                                            | AI Gateway                   |
                                                            | (https://aigw.portkey.ai)    |
                                                            +------------------------------+
```

- Listens locally on `http://127.0.0.1:8787` and forwards requests to `https://aigw.portkey.ai`.
- Requests for models starting with `anthropic.` are forwarded **byte-for-byte untouched**.
- **Historical media pruning (`MAX_RECENT_MEDIA = 6`)**: In long-running sessions (e.g. reading PDFs with MCP tools), conversations accumulate dozens of base64 images, causing request bodies to balloon (30–50MB) and latency to exceed upstream gateway timeouts (36s+). The bridge retains only the most recent 6 media items (active context) and replaces older historical media with concise text placeholders.
- Requests for non-`anthropic.` models containing `image` or `document` blocks inside (or next to) a `tool_result` are rewritten on the fly:
  1. The media block inside `tool_result` is replaced with a short text placeholder: `[1 image(s)/document(s) from this tool result are attached in the next user message.]`
  2. A synthetic assistant bridge turn is appended: `"Please share the images returned by the tool call above."`
  3. A synthetic user turn is appended with the `tool_use_id` label and the original `image`/`document` blocks.
  4. Consecutive identical turns (`user` followed by `user`) are merged to guarantee strict turn alternation required by Gemini / Vertex AI.
- **Local `count_tokens` interception**: Google Vertex AI on Portkey does not support `/v1/messages/count_tokens` (returns `HTTP 500: messagesCountTokens is not supported by vertex-ai`). The bridge intercepts this endpoint locally, approximates tokens (~4 chars/token + ~1600/image), and returns immediate `200 OK`, preventing error loops in Claude Desktop.
- Streaming responses (SSE) are forwarded back in real time without buffering. Upstream errors (HTTP >= 400) are fully logged with error payloads.

---

## Repository Structure

| File | Platform | Purpose |
|---|---|---|
| [`proxy.py`](proxy.py) | macOS & Windows | HTTP/1.1 streaming proxy and `tool_result` image/document rewriter |
| [`switch_gateway.py`](switch_gateway.py) | macOS & Windows | CLI and helper module to switch `inferenceGatewayBaseUrl` in Claude Desktop's `configLibrary/<appliedId>.json` between `http://127.0.0.1:8787` and `https://aigw.portkey.ai` (creates a `.json.bak` backup) |
| [`menubar_macos.py`](menubar_macos.py) | **macOS** | Windowless macOS menu-bar controller (`rumps`, Dock icon hidden via `LSUIElement`) with 5-second health polling (`🟢 P` / `🟠 P` / `🔴 P`), `launchd` service start/stop/restart, log viewer, and 1-click proxy/direct mode toggle |
| [`install_macos.sh`](install_macos.sh) | **macOS** | One-step installer & uninstaller for macOS: sets up a dedicated virtualenv in `~/Library/Application Support/PortkeyImageProxy`, installs `rumps`, registers and starts `launchd` LaunchAgents (`com.portkey.imageproxy` with `KeepAlive=true` and `com.portkey.imageproxy.menubar`) |
| [`requirements-macos.txt`](requirements-macos.txt) | **macOS** | Python dependencies for the macOS menu-bar app (`rumps`, `pyobjc-framework-Cocoa`) |
| [`service.py`](service.py) | **Windows** | Windows Service wrapper (`PortkeyImageProxy`, runs as `NT AUTHORITY\LocalService`, automatic startup, automatic restart on failure) |
| [`tray.py`](tray.py) | **Windows** | Windowless Windows system-tray application (`pythonw.exe`) with 5-second health polling (green/amber/red icon), service start/stop/restart without UAC prompts, log viewer, and 1-click proxy/direct mode toggle |
| [`build.ps1`](build.ps1) | **Windows** | Builds the standalone Windows Service executable (`dist\PortkeyImageProxy\PortkeyImageProxy.exe`) using PyInstaller |
| [`install.cmd`](install.cmd) / [`install.ps1`](install.ps1) | **Windows** | Installer & updater for Windows: installs dependencies, builds the `.exe` if missing, elevates [`install_service.ps1`](install_service.ps1) via UAC, grants non-admin service start/stop SDDL permissions, creates the Startup shortcut, and starts the tray icon |
| [`requirements.txt`](requirements.txt) | **Windows** | Python dependencies for the Windows service and system tray (`pywin32`, `pystray`, `Pillow`, `pyinstaller`) |
| [`configs/aigw-override-config.json`](configs/aigw-override-config.json) | All | Conditional routing configuration for the AI Gateway (`claude-*` aliases -> upstream model IDs) |
| [`configs/claude-desktop-3p-config.example.json`](configs/claude-desktop-3p-config.example.json) | All | Sanitized example configuration for `configLibrary/<appliedId>.json` |
| [`test_rewrite.py`](test_rewrite.py) / [`smoke_test.py`](smoke_test.py) | All | Unit and end-to-end HTTP proxy tests |

---

## Installation on macOS (Native)

### Prerequisites
- macOS (Apple Silicon M1/M2/M3/M4 or Intel)
- Python 3.10+ (`brew install python` or official installer from `python.org`)
- Claude Desktop (3P Gateway build), which stores its gateway configuration in:
  `~/Library/Application Support/Claude-3p/configLibrary/<appliedId>.json`

### 1. Clone the repository

```bash
git clone https://github.com/mzalewski87/claude-airs-image-bridge.git ~/claude-airs-image-bridge
cd ~/claude-airs-image-bridge
```

### 2. Run unit & smoke tests (optional)

```bash
python3 test_rewrite.py
python3 smoke_test.py
```

### 3. Install the `launchd` Background Service & Menu-Bar Icon

```bash
chmod +x install_macos.sh
./install_macos.sh
```
`install_macos.sh` creates an isolated Python virtual environment under `~/Library/Application Support/PortkeyImageProxy/venv`, installs `rumps`, creates and bootstraps two `launchd` LaunchAgents (`~/Library/LaunchAgents/com.portkey.imageproxy.plist` with `RunAtLoad=true` & `KeepAlive=true`, and `~/Library/LaunchAgents/com.portkey.imageproxy.menubar.plist`), and verifies `http://127.0.0.1:8787/__proxy_health`. Both survive reboots and run with zero Terminal windows.

### 4. Point Claude Desktop at the Local Proxy

```bash
python3 switch_gateway.py proxy
```
*(Or click the **`🟢 P`** icon in the macOS top menu bar -> **Desktop: use proxy (127.0.0.1:8787)**).*

Restart **Claude Desktop** (`Cmd + Q` and reopen).

### 5. macOS Operations & Uninstall
- **Log file**: `~/Library/Logs/PortkeyImageProxy/proxy.log` (or click **Open log** in the `🟢 P` menu bar dropdown).
- **Uninstall**:
  ```bash
  python3 ~/claude-airs-image-bridge/switch_gateway.py direct
  ~/claude-airs-image-bridge/install_macos.sh uninstall
  ```

---

## Installation on Windows 11 (Native or VMware Fusion VM)

### Prerequisites
- Windows 11 with **Python 3.11 or 3.12** installed and added to `PATH`
- Claude Desktop (3P Gateway build), which stores its gateway configuration in:
  `%LOCALAPPDATA%\Claude-3p\configLibrary\<appliedId>.json`

### 1. Clone the repository

```powershell
git clone https://github.com/mzalewski87/claude-airs-image-bridge.git C:\_claude\claude-airs-image-bridge
cd C:\_claude\claude-airs-image-bridge
```

### 2. Install the Windows Service & System Tray Icon

```powershell
.\install.cmd
```

### 3. Point Claude Desktop at the Local Proxy

```powershell
python switch_gateway.py proxy
```
*(Or right-click the green tray icon -> **Desktop: use proxy (127.0.0.1:8787)**).*

Restart **Claude Desktop**.

### 4. Windows Operations & Uninstall
- **Log file**: `C:\ProgramData\PortkeyImageProxy\proxy.log` (accessible via **Open log** in the tray menu).
- **Uninstall**:
  ```powershell
  python switch_gateway.py direct
  .\install.cmd uninstall
  ```
