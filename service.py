"""Windows service host for proxy.py (built into PortkeyImageProxy.exe by build.ps1).

Run by the Service Control Manager without arguments. `PortkeyImageProxy.exe debug`
[port] runs the proxy in the console instead, for troubleshooting.
"""
import os, sys, threading
import servicemanager, win32event, win32service, win32serviceutil
import proxy

SERVICE_NAME = 'PortkeyImageProxy'
LOG_DIR = os.path.join(os.environ.get('ProgramData', r'C:\ProgramData'), 'PortkeyImageProxy')


class ProxyService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = 'Portkey Image Proxy (Claude Desktop)'
    _svc_description_ = ('Local proxy between Claude Desktop and Portkey on 127.0.0.1:8787. Moves images out of '
                         'tool_result blocks for non-Anthropic (Gemini) models.')

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.server = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        if self.server:
            self.server.shutdown()
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        proxy.setup_logging(os.path.join(LOG_DIR, 'proxy.log'))
        try:
            self.server = proxy.make_server()
        except OSError as e:  # port taken: let SCM recovery retry later
            proxy.log.error(f'cannot listen on 127.0.0.1:{proxy.DEFAULT_PORT}: {e}')
            raise
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)
        self.server.server_close()
        proxy.log.info('service stopped')


if __name__ == '__main__':
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(ProxyService)
        servicemanager.StartServiceCtrlDispatcher()
    elif sys.argv[1] == 'debug':  # debug [port]
        proxy.setup_logging()
        proxy.make_server(int(sys.argv[2]) if len(sys.argv) > 2 else proxy.DEFAULT_PORT).serve_forever()
    else:
        win32serviceutil.HandleCommandLine(ProxyService)
