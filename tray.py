"""Tray icon for the PortkeyImageProxy service (run with pythonw, no console).

Green = service running and proxy answers, amber = running but no answer,
red = stopped / not installed. Menu: service control, log, Desktop gateway switch.
"""
import os, sys, threading, time, urllib.request

if sys.stderr is None:  # pythonw: keep tracebacks somewhere
    _log = os.path.join(os.environ['LOCALAPPDATA'], 'PortkeyImageProxy', 'tray_error.log')
    os.makedirs(os.path.dirname(_log), exist_ok=True)
    sys.stderr = sys.stdout = open(_log, 'a', encoding='utf-8', buffering=1)

import pystray, pywintypes, win32api, win32event, winerror, win32service
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import switch_gateway

SERVICE = 'PortkeyImageProxy'
HEALTH_URL = 'http://127.0.0.1:8787/__proxy_health'
LOG_FILE = os.path.join(os.environ.get('ProgramData', r'C:\ProgramData'), SERVICE, 'proxy.log')
COLORS = {'ok': (52, 211, 153), 'degraded': (254, 165, 33), 'down': (239, 68, 68)}
STATE_TEXT = {'ok': 'running', 'degraded': 'running, not answering', 'down': 'stopped'}


def make_icon(color):
    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 60, 60), 14, fill=(30, 30, 36, 255))
    d.ellipse((14, 14, 50, 50), fill=color + (255,))
    d.ellipse((24, 24, 40, 40), fill=(30, 30, 36, 255))
    return img


ICONS = {k: make_icon(c) for k, c in COLORS.items()}


def _open_service(access):
    # win32serviceutil opens the SCM with ALL_ACCESS (admin only); ask only for what the service ACL grants.
    scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    try:
        return win32service.OpenService(scm, SERVICE, access)
    finally:
        win32service.CloseServiceHandle(scm)


def service_state():
    try:
        h = _open_service(win32service.SERVICE_QUERY_STATUS)
    except pywintypes.error:
        return None  # not installed
    try:
        return win32service.QueryServiceStatus(h)[1]
    finally:
        win32service.CloseServiceHandle(h)


def wait_state(state, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if service_state() == state:
            return
        time.sleep(0.5)
    raise TimeoutError(f'service did not reach state {state}')


def service_start():
    h = _open_service(win32service.SERVICE_START | win32service.SERVICE_QUERY_STATUS)
    try:
        win32service.StartService(h, None)
    finally:
        win32service.CloseServiceHandle(h)
    wait_state(win32service.SERVICE_RUNNING)


def service_stop():
    h = _open_service(win32service.SERVICE_STOP | win32service.SERVICE_QUERY_STATUS)
    try:
        win32service.ControlService(h, win32service.SERVICE_CONTROL_STOP)
    finally:
        win32service.CloseServiceHandle(h)
    wait_state(win32service.SERVICE_STOPPED)


def healthy():
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=2) as r:
            return r.read() == b'ok'
    except OSError:
        return False


class Tray:
    def __init__(self):
        self.state, self.svc, self.gateway = 'down', None, '?'
        self.icon = pystray.Icon(SERVICE, ICONS['down'], 'Portkey Image Proxy', menu=pystray.Menu(
            pystray.MenuItem(lambda _: f'Proxy: {self.status_text()}', None, enabled=False),
            pystray.MenuItem(lambda _: f'Claude Desktop → {self.gateway}', None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Restart service', lambda: self.control('restart')),
            pystray.MenuItem('Start service', lambda: self.control('start'), visible=lambda _: self.svc != win32service.SERVICE_RUNNING),
            pystray.MenuItem('Stop service', lambda: self.control('stop'), visible=lambda _: self.svc == win32service.SERVICE_RUNNING),
            pystray.MenuItem('Open log', self.open_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Desktop: use proxy', lambda: self.switch('proxy'), checked=lambda _: self.gateway == 'proxy', radio=True),
            pystray.MenuItem('Desktop: direct to Portkey', lambda: self.switch('direct'), checked=lambda _: self.gateway == 'direct', radio=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Quit tray icon', lambda: self.icon.stop())))

    def status_text(self):
        return 'service not installed' if self.svc is None else STATE_TEXT[self.state]

    def refresh(self):
        self.svc = service_state()
        ok = healthy()
        state = 'ok' if ok else ('degraded' if self.svc == win32service.SERVICE_RUNNING else 'down')
        try:
            self.gateway = 'proxy' if '127.0.0.1' in (switch_gateway.current_gateway() or '') else 'direct'
        except (OSError, ValueError, KeyError):
            self.gateway = '?'
        if state != self.state:
            if self.state == 'ok' and self.gateway == 'proxy':
                self.icon.notify('Proxy is not answering — Claude Desktop requests will fail.', 'Portkey Image Proxy')
            self.state = state
        self.icon.icon = ICONS[state]
        self.icon.title = f'Portkey Image Proxy: {self.status_text()} (Desktop → {self.gateway})'
        self.icon.update_menu()

    def poll(self):
        while True:
            self.refresh()
            time.sleep(5)

    def control(self, action):
        try:
            if action in ('stop', 'restart') and self.svc == win32service.SERVICE_RUNNING:
                service_stop()
            if action in ('start', 'restart'):
                service_start()
        except TimeoutError as e:
            self.icon.notify(f'{action}: {e}', 'Portkey Image Proxy')
        except pywintypes.error as e:
            if e.winerror == winerror.ERROR_ACCESS_DENIED:  # no delegated rights: ask for UAC
                cmd = {'stop': f'stop {SERVICE}', 'start': f'start {SERVICE}'}.get(action)
                args = cmd if cmd else f'/c sc stop {SERVICE} & timeout /t 3 & sc start {SERVICE}'
                exe = 'sc.exe' if cmd else 'cmd.exe'
                win32api.ShellExecute(0, 'runas', exe, args, None, 0)
            else:
                self.icon.notify(f'{action} failed: {e.strerror}', 'Portkey Image Proxy')
        self.refresh()

    def open_log(self):
        if os.path.exists(LOG_FILE):
            os.startfile(LOG_FILE)
        else:
            self.icon.notify(f'No log yet: {LOG_FILE}', 'Portkey Image Proxy')

    def switch(self, mode):
        if mode == self.gateway:
            return
        if mode == 'proxy' and self.state != 'ok':
            self.icon.notify('Proxy is not running — start the service first.', 'Portkey Image Proxy')
            return
        switch_gateway.set_gateway(mode)
        self.refresh()
        self.icon.notify(f'Claude Desktop now points {"at the proxy" if mode == "proxy" else "directly at Portkey"}. Restart Claude Desktop to apply.', 'Portkey Image Proxy')

    def run(self):
        threading.Thread(target=self.poll, daemon=True).start()
        self.icon.run()


if __name__ == '__main__':
    mutex = win32event.CreateMutex(None, False, 'Local\\PortkeyImageProxyTray')
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        sys.exit(0)  # already running
    Tray().run()
