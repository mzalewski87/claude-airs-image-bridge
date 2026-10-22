"""macOS menu-bar controller for the Portkey Image Proxy launchd LaunchAgent.

Runs windowless in the macOS menu bar (uses rumps; hides the Dock icon via LSUIElement),
polls launchd service state and /__proxy_health every 5 s, and lets the user start/stop/restart
the service, open the log in Console/TextEdit, or switch Claude Desktop between proxy and direct.
"""
import os, subprocess, sys, threading, urllib.request
import switch_gateway

SERVICE_LABEL = 'com.portkey.imageproxy'
HEALTH_URL = 'http://127.0.0.1:8787/__proxy_health'
LOG_DIR = os.path.expanduser('~/Library/Logs/PortkeyImageProxy')
LOG_FILE = os.path.join(LOG_DIR, 'proxy.log')
PLIST_PATH = os.path.expanduser(f'~/Library/LaunchAgents/{SERVICE_LABEL}.plist')

# Redirect stdout/stderr to a log file when launched from launchd/Finder without a terminal.
os.makedirs(LOG_DIR, exist_ok=True)
if sys.stderr is None or not sys.stderr.isatty():
    _err = open(os.path.join(LOG_DIR, 'menubar_error.log'), 'a', encoding='utf-8', buffering=1)
    sys.stderr = sys.stdout = _err

try:
    import AppKit
    # Hide the Python rocket icon from the macOS Dock (run strictly as a menu-bar agent).
    info = AppKit.NSBundle.mainBundle().infoDictionary()
    if info is not None:
        info['LSUIElement'] = '1'
except Exception:
    pass

import rumps


def _uid():
    return str(os.getuid())


def service_state():
    """Return 'Running', 'Stopped', or 'Not installed'."""
    if not os.path.exists(PLIST_PATH):
        return 'Not installed'
    res = subprocess.run(
        ['launchctl', 'print', f'gui/{_uid()}/{SERVICE_LABEL}'],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return 'Stopped'
    if 'state = running' in res.stdout:
        return 'Running'
    return 'Stopped'


def health_ok():
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=1.5) as r:
            return r.status == 200 and r.read() == b'ok'
    except Exception:
        return False


def gateway_mode():
    try:
        url = switch_gateway.current_gateway() or ''
        return 'proxy' if '127.0.0.1' in url or 'localhost' in url else 'direct'
    except Exception:
        return 'unknown'


def service_start():
    uid = _uid()
    subprocess.run(['launchctl', 'bootstrap', f'gui/{uid}', PLIST_PATH], capture_output=True)
    subprocess.run(['launchctl', 'enable', f'gui/{uid}/{SERVICE_LABEL}'], capture_output=True)
    subprocess.run(['launchctl', 'kickstart', '-k', f'gui/{uid}/{SERVICE_LABEL}'], capture_output=True)


def service_stop():
    uid = _uid()
    subprocess.run(['launchctl', 'bootout', f'gui/{uid}/{SERVICE_LABEL}'], capture_output=True)


def service_restart():
    uid = _uid()
    res = subprocess.run(['launchctl', 'kickstart', '-k', f'gui/{uid}/{SERVICE_LABEL}'], capture_output=True)
    if res.returncode != 0:
        service_start()


class ProxyMenuBarApp(rumps.App):
    def __init__(self):
        super().__init__('🟢 P', quit_button=None)
        self.status_item = rumps.MenuItem('Service: checking...')
        self.gw_item = rumps.MenuItem('Claude Desktop -> checking...')
        self.restart_item = rumps.MenuItem('Restart service', callback=self.on_restart)
        self.start_item = rumps.MenuItem('Start service', callback=self.on_start)
        self.stop_item = rumps.MenuItem('Stop service', callback=self.on_stop)
        self.log_item = rumps.MenuItem('Open log', callback=self.on_open_log)
        self.use_proxy_item = rumps.MenuItem('Desktop: use proxy (127.0.0.1:8787)', callback=self.on_use_proxy)
        self.use_direct_item = rumps.MenuItem('Desktop: direct to Portkey', callback=self.on_use_direct)
        self.quit_item = rumps.MenuItem('Quit menu bar icon (service keeps running)', callback=self.on_quit)

        self.menu = [
            self.status_item,
            self.gw_item,
            None,
            self.restart_item,
            self.start_item,
            self.stop_item,
            self.log_item,
            None,
            self.use_proxy_item,
            self.use_direct_item,
            None,
            self.quit_item,
        ]
        self.was_healthy = True
        self.timer = rumps.Timer(self.refresh, 5)
        self.timer.start()
        self.refresh(None)

    def refresh(self, _):
        svc = service_state()
        ok = health_ok() if svc == 'Running' else False
        gw = gateway_mode()

        if ok:
            self.title = '🟢 P'
            detail = 'running, healthy'
        elif svc == 'Running':
            self.title = '🟠 P'
            detail = 'running, health check failed'
        else:
            self.title = '🔴 P'
            detail = svc.lower()

        self.status_item.title = f'Service: {detail}'
        self.gw_item.title = f'Claude Desktop -> {gw}'
        self.use_proxy_item.state = 1 if gw == 'proxy' else 0
        self.use_direct_item.state = 1 if gw == 'direct' else 0

        if self.was_healthy and not ok and gw == 'proxy':
            rumps.notification(
                'Portkey Image Proxy',
                'Proxy is down',
                'Service is not responding on 127.0.0.1:8787 while Claude Desktop is set to use it.',
            )
        self.was_healthy = ok

    def on_start(self, _):
        threading.Thread(target=self._run_and_refresh, args=(service_start,), daemon=True).start()

    def on_stop(self, _):
        threading.Thread(target=self._run_and_refresh, args=(service_stop,), daemon=True).start()

    def on_restart(self, _):
        threading.Thread(target=self._run_and_refresh, args=(service_restart,), daemon=True).start()

    def _run_and_refresh(self, fn):
        fn()
        self.refresh(None)

    def on_open_log(self, _):
        os.makedirs(LOG_DIR, exist_ok=True)
        if not os.path.exists(LOG_FILE):
            open(LOG_FILE, 'a', encoding='utf-8').close()
        subprocess.Popen(['open', '-a', 'Console', LOG_FILE])

    def on_use_proxy(self, _):
        switch_gateway.set_gateway('proxy')
        self.refresh(None)
        rumps.notification('Portkey Image Proxy', 'Switched to proxy', 'Claude Desktop -> 127.0.0.1:8787. Restart Claude Desktop to apply.')

    def on_use_direct(self, _):
        switch_gateway.set_gateway('direct')
        self.refresh(None)
        rumps.notification('Portkey Image Proxy', 'Switched to direct', 'Claude Desktop -> aigw.portkey.ai. Restart Claude Desktop to apply.')

    def on_quit(self, _):
        rumps.quit_application()


if __name__ == '__main__':
    ProxyMenuBarApp().run()
