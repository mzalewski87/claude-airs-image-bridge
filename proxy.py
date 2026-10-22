"""Local proxy between Claude Desktop (3P gateway mode) and Portkey.

Portkey's Anthropic -> Gemini adapter serialises images inside tool_result blocks
as base64 text (~1.47 chars/token, so one screenshot costs 100k+ tokens) and drops
images placed next to a tool_result. Images in a separate user turn work fine.

For every /v1/messages request whose model does not start with "anthropic.", this
proxy moves image/document blocks out of tool_result messages into a synthetic
follow-up user turn. Everything else is forwarded byte-for-byte, responses are
streamed back unchanged.

Usage: python -I proxy.py [--port 8787] [--upstream https://aigw.portkey.ai]
Then set inferenceGatewayBaseUrl to http://127.0.0.1:8787 in the Claude-3p config.
"""
import argparse, copy, http.client, json, logging, logging.handlers, sys, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger('portkey_image_proxy')
DEFAULT_PORT = 8787
DEFAULT_UPSTREAM = 'https://aigw.portkey.ai'

MEDIA = ('image', 'document')
HOP_BY_HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te',
              'trailers', 'transfer-encoding', 'upgrade', 'host', 'content-length', 'accept-encoding'}
ASSISTANT_BRIDGE = 'Please share the images returned by the tool call above.'
USER_INTRO = 'Images returned by the tool call(s) above, in order:'


def needs_rewrite(model):
    return isinstance(model, str) and not model.startswith('anthropic.')


def rewrite_messages(messages):
    """Return (new_messages, moved_count). Pure function; input is not modified."""
    out, moved_total = [], 0
    for msg in messages:
        content = msg.get('content') if isinstance(msg, dict) else None
        if msg.get('role') != 'user' or not isinstance(content, list) or \
                not any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in content):
            out.append(msg)
            continue
        kept, follow = [], []
        for block in content:
            if not isinstance(block, dict):
                kept.append(block)
            elif block.get('type') == 'tool_result' and isinstance(block.get('content'), list):
                media = [b for b in block['content'] if isinstance(b, dict) and b.get('type') in MEDIA]
                if not media:
                    kept.append(block)
                    continue
                rest = [b for b in block['content'] if not (isinstance(b, dict) and b.get('type') in MEDIA)]
                note = {'type': 'text', 'text': f'[{len(media)} image(s)/document(s) from this tool result are attached in the next user message.]'}
                new_block = dict(block, content=rest + [note])
                kept.append(new_block)
                follow.append({'type': 'text', 'text': f'Tool result {block.get("tool_use_id", "")}:'})
                follow.extend(copy.deepcopy(media))
            elif block.get('type') in MEDIA:
                # Media next to a tool_result is silently dropped by the adapter.
                follow.append(copy.deepcopy(block))
            else:
                kept.append(block)
        moved = sum(1 for b in follow if b.get('type') in MEDIA)
        if not moved:
            out.append(msg)
            continue
        moved_total += moved
        # Keep sibling text (reminders, mid-turn user text) after the media so ordering stays natural.
        texts = [b for b in kept if isinstance(b, dict) and b.get('type') == 'text']
        kept = [b for b in kept if not (isinstance(b, dict) and b.get('type') == 'text')]
        out.append(dict(msg, content=kept))
        out.append({'role': 'assistant', 'content': [{'type': 'text', 'text': ASSISTANT_BRIDGE}]})
        out.append({'role': 'user', 'content': [{'type': 'text', 'text': USER_INTRO}] + follow + texts})
    return out, moved_total


def rewrite_body(raw):
    """Return (bytes, model, moved). Leaves the body untouched unless something moved."""
    try:
        body = json.loads(raw)
    except ValueError:
        return raw, None, 0
    model = body.get('model') if isinstance(body, dict) else None
    if not needs_rewrite(model) or not isinstance(body.get('messages'), list):
        return raw, model, 0
    messages, moved = rewrite_messages(body['messages'])
    if not moved:
        return raw, model, 0
    body['messages'] = messages
    return json.dumps(body, ensure_ascii=False).encode('utf-8'), model, moved


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    upstream = None  # set in main()

    def log_message(self, fmt, *args):
        pass

    def _read_body(self):
        if 'chunked' in self.headers.get('Transfer-Encoding', '').lower():
            data = b''
            while True:
                size = int(self.rfile.readline().split(b';')[0].strip() or b'0', 16)
                if size == 0:
                    self.rfile.readline()
                    return data
                data += self.rfile.read(size)
                self.rfile.readline()
        n = int(self.headers.get('Content-Length') or 0)
        return self.rfile.read(n) if n else b''

    def _proxy(self):
        if self.path == '/__proxy_health':
            body = b'ok'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        started = time.time()
        raw = self._read_body()
        model, moved = None, 0
        if self.command == 'POST' and urllib.parse.urlsplit(self.path).path.rstrip('/') in ('/v1/messages', '/v1/messages/count_tokens'):
            raw, model, moved = rewrite_body(raw)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        headers['Host'] = self.upstream.netloc
        if raw or self.command in ('POST', 'PUT', 'PATCH'):
            headers['Content-Length'] = str(len(raw))
        conn_cls = http.client.HTTPSConnection if self.upstream.scheme == 'https' else http.client.HTTPConnection
        conn = conn_cls(self.upstream.netloc, timeout=900)
        try:
            conn.request(self.command, self.path, body=raw or None, headers=headers)
            resp = conn.getresponse()
            self.send_response(resp.status, resp.reason)
            length = resp.getheader('Content-Length')
            for k, v in resp.getheaders():
                if k.lower() not in HOP_BY_HOP or k.lower() == 'content-length':
                    self.send_header(k, v)
            if length is None:
                self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            while True:
                chunk = resp.read1(65536) if hasattr(resp, 'read1') else resp.read(65536)
                if not chunk:
                    break
                if length is None:
                    self.wfile.write(b'%x\r\n%s\r\n' % (len(chunk), chunk))
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()
            if length is None:
                self.wfile.write(b'0\r\n\r\n')
            status = resp.status
        except Exception as e:  # upstream unreachable etc.
            status = 502
            msg = json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': f'local proxy: {e}'}}).encode()
            try:
                self.send_response(502)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
            except Exception:
                pass
        finally:
            conn.close()
        note = f' moved={moved}' if moved else ''
        log.info(f'{self.command} {self.path} model={model} status={status}{note} {time.time() - started:.1f}s')

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _proxy


def setup_logging(log_file=None):
    """Log to stdout, or to a rotating file (service mode)."""
    if log_file:
        handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding='utf-8')
    else:
        handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s', '%Y-%m-%d %H:%M:%S'))
    log.handlers[:] = [handler]
    log.setLevel(logging.INFO)


def make_server(port=DEFAULT_PORT, upstream=DEFAULT_UPSTREAM):
    Handler.upstream = urllib.parse.urlsplit(upstream)
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    log.info(f'Portkey image proxy on http://127.0.0.1:{port} -> {upstream}')
    return server


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=DEFAULT_PORT)
    ap.add_argument('--upstream', default=DEFAULT_UPSTREAM)
    ap.add_argument('--log-file')
    args = ap.parse_args()
    setup_logging(args.log_file)
    make_server(args.port, args.upstream).serve_forever()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    main()
