"""Offline end-to-end check: fake upstream <- proxy <- client, all on 127.0.0.1."""
import http.client, json, os, sys, threading, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import proxy

seen = {}


class FakeUpstream(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def do_POST(self):
        seen['body'] = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        seen['headers'] = dict(self.headers)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()
        for i in range(3):
            data = f'event: ping\ndata: {{"n": {i}}}\n\n'.encode()
            self.wfile.write(b'%x\r\n%s\r\n' % (len(data), data))
            self.wfile.flush()
        self.wfile.write(b'0\r\n\r\n')


up = ThreadingHTTPServer(('127.0.0.1', 0), FakeUpstream)
threading.Thread(target=up.serve_forever, daemon=True).start()
proxy.Handler.upstream = urllib.parse.urlsplit(f'http://127.0.0.1:{up.server_port}')
px = ThreadingHTTPServer(('127.0.0.1', 0), proxy.Handler)
threading.Thread(target=px.serve_forever, daemon=True).start()

IMG = {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'AAAA'}}
body = {'model': 'claude-argon', 'stream': True, 'messages': [
    {'role': 'user', 'content': 'read'},
    {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 't1', 'name': 'Read', 'input': {}}]},
    {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': [IMG]}]}]}
c = http.client.HTTPConnection('127.0.0.1', px.server_port, timeout=10)
c.request('POST', '/v1/messages?beta=true', json.dumps(body), {'Content-Type': 'application/json', 'x-portkey-provider': '@p'})
r = c.getresponse()
stream = r.read().decode()
assert r.status == 200 and stream.count('event: ping') == 3, (r.status, stream)
assert [m['role'] for m in seen['body']['messages']] == ['user', 'assistant', 'user', 'assistant', 'user']
assert seen['body']['messages'][4]['content'][2] == IMG
assert seen['headers'].get('x-portkey-provider') == '@p'
c.request('GET', '/__proxy_health')
assert c.getresponse().read() == b'ok'
print('smoke test passed: body rewritten, headers forwarded, SSE streamed, keep-alive OK')
