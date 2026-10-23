# claude-airs-image-bridge (PL)

> **[English version (README.md)](README.md)** | **PL**

Wieloplatformowa (**macOS** oraz **Windows 11**) lokalna usługa działająca w tle oraz aplikacja w pasku menu (macOS Menu Bar) / zasobniku systemowym (Windows System Tray), pełniąca rolę mostu pomiędzy **Claude Desktop (w trybie 3P Gateway)** a bramką AI Gateway (`https://aigw.portkey.ai`) podczas korzystania z modeli innych niż Anthropic (takich jak **Google Gemini**, **Gemma**, **GLM** czy **Llama** na Vertex AI).

---

## Opis problemu

Gdy Claude Desktop działa w trybie **3P Gateway** z modelami innymi niż Anthropic udostępnianymi przez bramkę AI Gateway:

1. **Wymóg nazewnictwa modeli w Claude Desktop**: Claude Desktop filtruje listę modeli i wyświetla wyłącznie identyfikatory zaczynające się od `anthropic.` lub `claude-`. Aby udostępnić modele Gemini lub inne modele zewnętrzne w menu wyboru, w lokalnej konfiguracji definiuje się aliasy `claude-*` (np. `claude-argon`, `claude-pro-3-1`, `claude-flash-3-8`), które po stronie bramki AI Gateway są mapowane na właściwe identyfikatory modeli (`gemini-4-argon`, `gemini-3.1-pro`, `gemini-3.8-flash` itd.) za pomocą konfiguracji routingu ([`configs/aigw-override-config.json`](configs/aigw-override-config.json)).
2. **Błąd serializacji obrazów w blokach `tool_result`**: Gdy Claude Code odczytuje plik graficzny narzędziem `Read` lub robi zrzut ekranu, przesyła blok `image` (lub `document`) wewnątrz bloku `tool_result` (format Anthropic Messages API).
   - Dla natywnych modeli `anthropic.*` obrazy w `tool_result` są przetwarzane natywnie.
   - Dla modeli Gemini adapter Anthropic->Gemini w bramce AI Gateway serializuje obrazy wewnątrz `tool_result` jako **surowy tekst base64** (~1,47 znaku na token — ponad 52 000 tokenów dla małego obrazka 160x120 PNG i ponad 300 000 tokenów dla pełnego zrzutu ekranu). Po kilku zrzutach ekranu sesja przekracza limit 1 048 576 tokenów kontekstu i zawiesza się na stałe błędem `API Error: 400 (no body)`.
   - Umieszczenie obrazu obok `tool_result` w tej samej turze użytkownika powoduje jego ciche pominięcie przez adapter.
   - Umieszczenie obrazu w **osobnej, kolejnej turze użytkownika (`user`)** pozwala modelowi Gemini przetworzyć obraz natywnie (~1 280 tokenów).
3. **Brak obsługi `count_tokens` w Vertex AI na Portkey**: Claude Desktop stale odpytuje endpoint `/v1/messages/count_tokens`, który na bramce Portkey dla Vertex AI zwraca `HTTP 500: messagesCountTokens is not supported by vertex-ai`. Proxy przechwytuje ten endpoint lokalnie i zwraca natychmiast `200 OK` z oszacowaną liczbą tokenów.
4. **Limit rozmiaru zapytania i timeouty przy długich sesjach (`MAX_RECENT_MEDIA = 6`)**: W długich sesjach (np. czytanie dokumentów PDF przez MCP) kumulują się dziesiątki obrazów w base64 (payload rzędu 30–50 MB). Przekracza to limit REST Vertex AI (20 MB), a czas odpowiedzi przekracza 36-sekundowy timeout bramki Portkey (błędy 500/502). Proxy automatycznie przycina historyczne multimedia z dawnych tur, pozostawiając w pełnej formie 6 najświeższych obrazów (aktywny kontekst).

### Porównanie zużycia tokenów (obraz 160x120 PNG, 77 072 znaków base64)

| Scenariusz | Bezpośrednio do bramki | Przez `claude-airs-image-bridge` | Czy model widzi obraz? |
|---|---:|---:|:---:|
| Sam tekst (baza odniesienia) | 156 tokenów | 156 tokenów | N/D |
| Obraz wewnątrz `tool_result` | **52 645 tokenów** (tekst base64) | **1 280 tokenów** | Tak |
| Obraz obok `tool_result` (ta sama tura) | 156 tokenów (pominięty) | **1 280 tokenów** | Tak |

---

## Zawartość repozytorium

| Plik | Platforma | Opis |
|---|---|---|
| [`proxy.py`](proxy.py) | macOS i Windows | Serwer proxy HTTP/1.1 ze strumieniowaniem SSE i przepisywaniem obrazów z `tool_result` |
| [`switch_gateway.py`](switch_gateway.py) | macOS i Windows | Skrypt przełączający `inferenceGatewayBaseUrl` w pliku `configLibrary/<appliedId>.json` między `http://127.0.0.1:8787` a `https://aigw.portkey.ai` (tworzy kopię `.json.bak`) |
| [`menubar_macos.py`](menubar_macos.py) | **macOS** | Aplikacja w górnym pasku menu macOS (`rumps`, ukryta ikona w Docku przez `LSUIElement`): monitoring co 5 s (`🟢 P` / `🟠 P` / `🔴 P`), start/stop/restart usługi `launchd`, podgląd logu w aplikacji Console oraz przełącznik proxy/direct |
| [`install_macos.sh`](install_macos.sh) | **macOS** | Instalator i deinstalator dla macOS: tworzy środowisko venv w `~/Library/Application Support/PortkeyImageProxy/venv`, instaluje `rumps` i rejestruje usługi `launchd` (`com.portkey.imageproxy` z `KeepAlive=true` oraz `com.portkey.imageproxy.menubar`) |
| [`requirements-macos.txt`](requirements-macos.txt) | **macOS** | Zależności Pythona dla paska menu macOS (`rumps`, `pyobjc-framework-Cocoa`) |
| [`service.py`](service.py) | **Windows** | Host usługi systemowej Windows (`PortkeyImageProxy`, konto `NT AUTHORITY\LocalService`, autostart z systemem, automatyczny restart po awarii) |
| [`tray.py`](tray.py) | **Windows** | Aplikacja w zasobniku systemowym Windows (`pythonw.exe`, bez okna konsoli): sprawdzanie stanu co 5 s, start/stop/restart usługi bez UAC, podgląd logu oraz przełącznik trybu proxy/direct |
| [`build.ps1`](build.ps1) | **Windows** | Buduje plik wykonywalny usługi `dist\PortkeyImageProxy\PortkeyImageProxy.exe` przy użyciu PyInstaller |
| [`install.cmd`](install.cmd) / [`install.ps1`](install.ps1) | **Windows** | Instalator i aktualizatora dla Windows: instaluje zależności, buduje `.exe`, uruchamia [`install_service.ps1`](install_service.ps1) z UAC, nadaje uprawnienia SDDL, tworzy skrót w Autostarcie i uruchamia ikonę w trayu |
| [`requirements.txt`](requirements.txt) | **Windows** | Zależności Pythona dla Windows (`pywin32`, `pystray`, `Pillow`, `pyinstaller`) |
| [`configs/aigw-override-config.json`](configs/aigw-override-config.json) | Wszystkie | Konfiguracja warunkowego routingu dla bramki AI Gateway (mapowanie aliasów `claude-*` na docelowe modele) |
| [`configs/claude-desktop-3p-config.example.json`](configs/claude-desktop-3p-config.example.json) | Wszystkie | Przykładowy szablon konfiguracji dla `configLibrary/<appliedId>.json` |
| [`test_rewrite.py`](test_rewrite.py) / [`smoke_test.py`](smoke_test.py) | Wszystkie | Testy jednostkowe i test end-to-end serwera proxy |

---

## Instalacja na macOS (natywnie)

### Wymagania wstępne
- macOS (Apple Silicon M1/M2/M3/M4 lub Intel)
- Python 3.10+ (`brew install python` lub instalator z `python.org`)
- Claude Desktop (build 3P Gateway), przechowujący konfigurację w:
  `~/Library/Application Support/Claude-3p/configLibrary/<appliedId>.json`

### 1. Sklonowanie repozytorium

```bash
git clone https://github.com/mzalewski87/claude-airs-image-bridge.git ~/claude-airs-image-bridge
cd ~/claude-airs-image-bridge
```

### 2. Uruchomienie testów (opcjonalnie)

```bash
python3 test_rewrite.py
python3 smoke_test.py
```

### 3. Instalacja usługi `launchd` oraz ikony w pasku menu (Menu Bar)

```bash
chmod +x install_macos.sh
./install_macos.sh
```
Skrypt `install_macos.sh` tworzy izolowane środowisko wirtualne Pythona w `~/Library/Application Support/PortkeyImageProxy/venv`, instaluje bibliotekę `rumps`, rejestruje i uruchamia dwa agenty `launchd` (`~/Library/LaunchAgents/com.portkey.imageproxy.plist` z `RunAtLoad=true` i `KeepAlive=true` oraz `~/Library/LaunchAgents/com.portkey.imageproxy.menubar.plist`) i sprawdza `http://127.0.0.1:8787/__proxy_health`. Oba procesy działają bez żadnego okna Terminala i automatycznie startują po restarcie Maca.

### 4. Przełączenie Claude Desktop na lokalne proxy

```bash
python3 switch_gateway.py proxy
```
*(Lub kliknij ikonę **`🟢 P`** w górnym pasku menu macOS -> **Desktop: use proxy (127.0.0.1:8787)**).*

Zrestartuj **Claude Desktop** (`Cmd + Q` i uruchom ponownie).

### 5. Logi i deinstalacja na macOS
- **Plik logu**: `~/Library/Logs/PortkeyImageProxy/proxy.log` (dostępny też po kliknięciu **Open log** w menu `🟢 P`).
- **Deinstalacja**:
  ```bash
  python3 ~/claude-airs-image-bridge/switch_gateway.py direct
  ~/claude-airs-image-bridge/install_macos.sh uninstall
  ```

---

## Instalacja na Windows 11 (natywnie lub w VMware Fusion)

### 1. Sklonowanie repozytorium i instalacja usługi

```powershell
git clone https://github.com/mzalewski87/claude-airs-image-bridge.git C:\_claude\claude-airs-image-bridge
cd C:\_claude\claude-airs-image-bridge
.\install.cmd
python switch_gateway.py proxy
```
Po wykonaniu powyższych poleceń zrestartuj **Claude Desktop**.
