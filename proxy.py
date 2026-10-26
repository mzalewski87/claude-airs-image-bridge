"""Local proxy between Claude Desktop (3P gateway mode) and Portkey.

Portkey's Anthropic -> Gemini adapter serialises images inside tool_result blocks
as base64 text (~1.47 chars/token, so one screenshot costs 100k+ tokens) and drops
images placed next to a tool_result. Images in a separate user turn work fine.

For every /v1/messages request whose model does not start with "anthropic.", this
proxy:
1. Prunes historical images/documents beyond the most recent MAX_RECENT_MEDIA (default 6)
   to prevent payload size explosion (Vertex AI 20MB limit) and gateway timeouts (>36s).
2. Moves recent image/document blocks out of tool_result messages into a synthetic
   follow-up user turn.
3. Ensures strict role alternation (user/assistant) and non-empty content blocks.
4. Locally intercepts /v1/messages/count_tokens, which Vertex AI on Portkey does not support,
   returning immediate 200 OK with approximate token count.
5. Logs upstream errors with full response text and protects against streaming disconnect bugs.

Usage: python -I proxy.py [--port 8787] [--upstream https://aigw.portkey.ai]
Then set inferenceGatewayBaseUrl to http://127.0.0.1:8787 in the Claude-3p config.
"""
import argparse, copy, http.client, json, logging, logging.handlers, os, sys, threading, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger('portkey_image_proxy')
DEFAULT_PORT = 8787
DEFAULT_UPSTREAM = 'https://aigw.portkey.ai'
DEFAULT_TIMEOUT = int(os.environ.get('PORTKEY_PROXY_TIMEOUT', '1200'))
PING_EVENT = b'event: ping\ndata: {"type": "ping"}\n\n'

MEDIA = ('image', 'document')
MAX_RECENT_MEDIA = 6
HOP_BY_HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te',
              'trailers', 'transfer-encoding', 'upgrade', 'host', 'content-length', 'accept-encoding'}
ASSISTANT_BRIDGE = 'Please share the images returned by the tool call above.'
USER_INTRO = 'Images returned by the tool call(s) above, in order:'


def needs_rewrite(model):
    return isinstance(model, str) and not model.startswith('anthropic.')


def merge_consecutive_roles(messages):
    """Merge adjacent messages with the same role and filter empty text blocks."""
    out = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        c = msg.get('content', [])
        if isinstance(c, list):
            c = [b for b in c if not (isinstance(b, dict) and b.get('type') == 'text' and not b.get('text'))]
        if not c:
            continue
        msg = dict(msg, content=c)

        if not out:
            out.append(copy.deepcopy(msg))
            continue
        prev = out[-1]
        if prev.get('role') == msg.get('role'):
            prev_c = prev.get('content', [])
            msg_c = msg.get('content', [])
            if isinstance(prev_c, str): prev_c = [{'type': 'text', 'text': prev_c}]
            if isinstance(msg_c, str): msg_c = [{'type': 'text', 'text': msg_c}]
            if isinstance(prev_c, list) and isinstance(msg_c, list):
                out[-1] = dict(prev, content=prev_c + msg_c)
            else:
                out.append(copy.deepcopy(msg))
        else:
            out.append(copy.deepcopy(msg))
    return out


def rewrite_messages(messages, max_recent=MAX_RECENT_MEDIA):
    """Return (new_messages, moved_count). Pure function; input is not modified."""
    # Pre-merge consecutive identical roles so multi-tool calls and split user responses
    # remain grouped before media extraction. This guarantees that all tool_results matching
    # an assistant's tool_use calls remain grouped in the immediate follow-up user turn.
    messages = merge_consecutive_roles(messages)

    all_media = []
    for m_idx, msg in enumerate(messages):
        content = msg.get('content') if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for b_idx, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            if block.get('type') in MEDIA:
                all_media.append((m_idx, b_idx, None))
            elif block.get('type') == 'tool_result' and isinstance(block.get('content'), list):
                for s_idx, sub in enumerate(block['content']):
                    if isinstance(sub, dict) and sub.get('type') in MEDIA:
                        all_media.append((m_idx, b_idx, s_idx))

    pruned_set = set(all_media[:-max_recent]) if (max_recent and len(all_media) > max_recent) else set()
    moved_total = 0

    # Step 1: Prune historical media to save context window and avoid gateway timeouts
    step1 = []
    for m_idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            step1.append(msg)
            continue
        content = msg.get('content')
        if not isinstance(content, list):
            step1.append(msg)
            continue

        new_content = []
        for b_idx, block in enumerate(content):
            if not isinstance(block, dict):
                new_content.append(block)
                continue
            if (m_idx, b_idx, None) in pruned_set:
                new_content.append({'type': 'text', 'text': '[Historical media omitted from prompt context to prevent gateway timeout]'})
            elif block.get('type') == 'tool_result' and isinstance(block.get('content'), list):
                new_subs = []
                for s_idx, sub in enumerate(block['content']):
                    if (m_idx, b_idx, s_idx) in pruned_set:
                        t_id = block.get('tool_use_id', '')
                        new_subs.append({'type': 'text', 'text': f'[Historical media from tool {t_id} omitted from prompt context to prevent gateway timeout]'})
                    else:
                        new_subs.append(sub)
                new_content.append(dict(block, content=new_subs))
            else:
                new_content.append(block)
        step1.append(dict(msg, content=new_content))

    # Step 2: Move recent media out of tool_result into synthetic turns
    step2 = []
    for msg in step1:
        content = msg.get('content') if isinstance(msg, dict) else None
        if msg.get('role') != 'user' or not isinstance(content, list) or \
                not any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in content):
            step2.append(msg)
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
                t_id = block.get('tool_use_id', '')
                follow.append({'type': 'text', 'text': f'Tool result {t_id}:'})
                follow.extend(copy.deepcopy(media))
            elif block.get('type') in MEDIA:
                follow.append(copy.deepcopy(block))
            else:
                kept.append(block)
        moved = sum(1 for b in follow if b.get('type') in MEDIA)
        if not moved:
            step2.append(msg)
            continue
        moved_total += moved
        texts = [b for b in kept if isinstance(b, dict) and b.get('type') == 'text']
        kept = [b for b in kept if not (isinstance(b, dict) and b.get('type') == 'text')]
        if not kept:
            kept = [{'type': 'text', 'text': '[Tool result omitted]'}]
        step2.append(dict(msg, content=kept))
        step2.append({'role': 'assistant', 'content': [{'type': 'text', 'text': ASSISTANT_BRIDGE}]})
        user_c = [{'type': 'text', 'text': USER_INTRO}] + follow + texts
        step2.append({'role': 'user', 'content': user_c})

    # Step 3: Merge consecutive identical roles to guarantee strict role alternation
    out = merge_consecutive_roles(step2)
    return out, moved_total


def rewrite_body(raw):
    """Return (bytes, model, moved). Leaves the body untouched unless something moved or pruned."""
    try:
        body = json.loads(raw)
    except ValueError:
        return raw, None, 0
    model = body.get('model') if isinstance(body, dict) else None
    if not needs_rewrite(model) or not isinstance(body.get('messages'), list):
        return raw, model, 0
    messages, moved = rewrite_messages(body['messages'])
    if not moved and messages == body['messages']:
        return raw, model, 0
    body['messages'] = messages
    return json.dumps(body, ensure_ascii=False).encode('utf-8'), model, moved


def calculate_approx_tokens(raw):
    """Approximate token count for /v1/messages/count_tokens."""
    try:
        body = json.loads(raw) if raw else {}
    except Exception:
        body = {}
    text_chars = 0
    image_count = 0
    messages = body.get('messages', [])
    if isinstance(messages, list):
        for m in messages:
            if not isinstance(m, dict):
                continue
            c = m.get('content')
            if isinstance(c, str):
                text_chars += len(c)
            elif isinstance(c, list):
                for b in c:
                    if not isinstance(b, dict):
                        continue
                    btype = b.get('type')
                    if btype == 'text':
                        text_chars += len(b.get('text', ''))
                    elif btype in MEDIA:
                        image_count += 1
                    elif btype == 'tool_result' and isinstance(b.get('content'), list):
                        for sub in b['content']:
                            if isinstance(sub, dict):
                                if sub.get('type') in MEDIA:
                                    image_count += 1
                                elif sub.get('type') == 'text':
                                    text_chars += len(sub.get('text', ''))
                    elif btype == 'tool_use':
                        text_chars += len(json.dumps(b.get('input', {})))
    system = body.get('system')
    if isinstance(system, str):
        text_chars += len(system)
    elif isinstance(system, list):
        for b in system:
            if isinstance(b, dict) and b.get('type') == 'text':
                text_chars += len(b.get('text', ''))
    tools = body.get('tools')
    if isinstance(tools, list):
        text_chars += len(json.dumps(tools))
    approx_tokens = max(1, (text_chars // 4) + (image_count * 1600))
    return approx_tokens, body.get('model')


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

    def _handle_count_tokens(self, raw):
        tokens, model = calculate_approx_tokens(raw)
        resp_data = json.dumps({'input_tokens': tokens}).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(resp_data)))
        self.end_headers()
        self.wfile.write(resp_data)
        log.info(f'POST {self.path} model={model} status=200 (intercepted count_tokens: {tokens} tokens)')

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
        parsed_path = urllib.parse.urlsplit(self.path).path.rstrip('/')

        # Intercept count_tokens locally to avoid upstream 500 unsupported errors
        if self.command == 'POST' and parsed_path == '/v1/messages/count_tokens':
            self._handle_count_tokens(raw)
            return

        model, moved = None, 0
        if self.command == 'POST' and parsed_path == '/v1/messages':
            raw, model, moved = rewrite_body(raw)

        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        headers['Host'] = self.upstream.netloc
        if 'user-agent' not in (k.lower() for k in headers) or 'python-urllib' in headers.get('User-Agent', '').lower():
            headers['User-Agent'] = 'Anthropic/Claude'
        if raw or self.command in ('POST', 'PUT', 'PATCH'):
            headers['Content-Length'] = str(len(raw))

        conn_cls = http.client.HTTPSConnection if self.upstream.scheme == 'https' else http.client.HTTPConnection
        timeout = getattr(self, 'timeout', getattr(self.server, 'timeout', DEFAULT_TIMEOUT))
        conn = conn_cls(self.upstream.netloc, timeout=timeout)
        status = 502
        headers_sent = False

        is_stream = bool(raw and (b'"stream": true' in raw or b'"stream":true' in raw))

        if is_stream:
            write_lock = threading.Lock()
            stop_ping = threading.Event()
            last_active = [time.time()]

            # Send 200 text/event-stream headers immediately to client (TTFB < 5ms).
            # This completely eliminates client connection timeouts and resets the watchdog.
            self.send_response(200, 'OK')
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'keep-alive')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            headers_sent = True

            def ping_worker():
                ping_chunk = b'%x\r\n%s\r\n' % (len(PING_EVENT), PING_EVENT)
                while not stop_ping.wait(15.0):
                    if time.time() - last_active[0] >= 14.0:
                        try:
                            with write_lock:
                                self.wfile.write(ping_chunk)
                                self.wfile.flush()
                            last_active[0] = time.time()
                        except Exception:
                            break

            ping_thread = threading.Thread(target=ping_worker, daemon=True)
            ping_thread.start()

            try:
                conn.request(self.command, self.path, body=raw or None, headers=headers)
                resp = conn.getresponse()
                status = resp.status

                if status >= 400:
                    stop_ping.set()
                    err_body = resp.read()
                    err_text = err_body[:500].decode('utf-8', 'replace')
                    hdrs = dict(resp.getheaders())
                    log.error(f'Upstream HTTP {status} for {self.command} {self.path} model={model}: {err_text} (headers: {hdrs})')
                    err_msg = err_text if err_body else (hdrs.get('x-portkey-error') or f'Upstream HTTP {status} error from {self.upstream.netloc}')
                    err_payload = b'event: error\ndata: ' + json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': err_msg}}).encode('utf-8') + b'\n\n'
                    with write_lock:
                        self.wfile.write(b'%x\r\n%s\r\n0\r\n\r\n' % (len(err_payload), err_payload))
                        self.wfile.flush()
                    return

                while True:
                    chunk = resp.read1(65536) if hasattr(resp, 'read1') else resp.read(65536)
                    if not chunk:
                        break
                    with write_lock:
                        self.wfile.write(b'%x\r\n%s\r\n' % (len(chunk), chunk))
                        self.wfile.flush()
                    last_active[0] = time.time()

                stop_ping.set()
                with write_lock:
                    self.wfile.write(b'0\r\n\r\n')
                    self.wfile.flush()
            except Exception as e:
                stop_ping.set()
                log.warning(f'Proxy streaming notice for {self.path}: {type(e).__name__}: {e}')
            finally:
                stop_ping.set()
                conn.close()
                note = f' moved={moved}' if moved else ''
                log.info(f'{self.command} {self.path} model={model} status={status}{note} {time.time() - started:.1f}s')
            return

        # Synchronous handling for non-streaming requests
        try:
            conn.request(self.command, self.path, body=raw or None, headers=headers)
            resp = conn.getresponse()
            status = resp.status

            if status >= 400:
                err_body = resp.read()
                err_text = err_body[:500].decode('utf-8', 'replace')
                hdrs = dict(resp.getheaders())
                log.error(f'Upstream HTTP {status} for {self.command} {self.path} model={model}: {err_text} (headers: {hdrs})')
                if not err_body:
                    err_msg = hdrs.get('x-portkey-error') or f'Upstream HTTP {status} error from {self.upstream.netloc}'
                    err_body = json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': err_msg}}).encode('utf-8')
                self.send_response(status, resp.reason)
                for k, v in resp.getheaders():
                    if k.lower() not in HOP_BY_HOP and k.lower() not in ('content-length', 'content-type'):
                        self.send_header(k, v)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(err_body)))
                self.end_headers()
                headers_sent = True
                self.wfile.write(err_body)
                return

            self.send_response(resp.status, resp.reason)
            length = resp.getheader('Content-Length')
            for k, v in resp.getheaders():
                if k.lower() not in HOP_BY_HOP or k.lower() == 'content-length':
                    self.send_header(k, v)
            if length is None:
                self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            headers_sent = True

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
        except Exception as e:
            if not headers_sent:
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
            log.warning(f'Proxy streaming notice for {self.path}: {type(e).__name__}: {e}')
        finally:
            conn.close()
            note = f' moved={moved}' if moved else ''
            log.info(f'{self.command} {self.path} model={model} status={status}{note} {time.time() - started:.1f}s')

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _proxy


def setup_logging(log_file=None):
    """Log to stdout, or to a rotating file (service mode)."""
    if log_file:
        log_dir = os.path.dirname(os.path.abspath(log_file))
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding='utf-8')
    else:
        handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s', '%Y-%m-%d %H:%M:%S'))
    log.handlers[:] = [handler]
    log.setLevel(logging.INFO)


def make_server(port=DEFAULT_PORT, upstream=DEFAULT_UPSTREAM, timeout=DEFAULT_TIMEOUT):
    Handler.upstream = urllib.parse.urlsplit(upstream)
    Handler.timeout = timeout
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.timeout = timeout
    log.info(f'Portkey image proxy on http://127.0.0.1:{port} -> {upstream} (timeout: {timeout}s)')
    return server


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=DEFAULT_PORT)
    ap.add_argument('--upstream', default=DEFAULT_UPSTREAM)
    ap.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT, help='Upstream connection and read timeout in seconds')
    ap.add_argument('--log-file')
    args = ap.parse_args()
    setup_logging(args.log_file)
    make_server(args.port, args.upstream, timeout=args.timeout).serve_forever()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    main()
