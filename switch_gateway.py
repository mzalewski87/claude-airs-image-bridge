"""Point Claude Desktop (3P) at the local proxy or back at Portkey directly (Windows & macOS).

Usage: python switch_gateway.py proxy|direct|status [--port 8787]
Keeps a one-time backup next to the config (*.json.bak). Restart Claude Desktop afterwards.
"""
import json, os, shutil, sys

DIRECT = 'https://aigw.portkey.ai'


def _candidate_libs():
    override = os.environ.get('CLAUDE_3P_CONFIG_LIB')
    if override:
        return [os.path.expanduser(os.path.expandvars(override))]
    if sys.platform == 'darwin':
        base = os.path.expanduser('~/Library/Application Support')
        return [
            os.path.join(base, 'Claude-3p', 'configLibrary'),
            os.path.join(base, 'Claude Nest-3p', 'configLibrary'),
            os.path.join(base, 'Claude', 'configLibrary'),
        ]
    if sys.platform == 'win32':
        local = os.path.expandvars(r'%LOCALAPPDATA%')
        roaming = os.path.expandvars(r'%APPDATA%')
        return [
            os.path.join(local, 'Claude-3p', 'configLibrary'),
            os.path.join(roaming, 'Claude-3p', 'configLibrary'),
        ]
    base = os.path.expanduser('~/.config')
    return [
        os.path.join(base, 'Claude-3p', 'configLibrary'),
        os.path.join(base, 'Claude', 'configLibrary'),
    ]


def find_lib():
    candidates = _candidate_libs()
    for path in candidates:
        if os.path.exists(os.path.join(path, '_meta.json')):
            return path
    for path in candidates:
        if os.path.isdir(path):
            return path
    return candidates[0]


def config_path():
    # _meta.json names the config Desktop currently applies.
    lib = find_lib()
    with open(os.path.join(lib, '_meta.json'), encoding='utf-8') as f:
        return os.path.join(lib, json.load(f)['appliedId'] + '.json')


def current_gateway():
    with open(config_path(), encoding='utf-8') as f:
        return json.load(f).get('inferenceGatewayBaseUrl')


def set_gateway(mode, port='8787'):
    """Return (path, old_url, new_url)."""
    path = config_path()
    if not os.path.exists(path + '.bak'):
        shutil.copy2(path, path + '.bak')
    with open(path, encoding='utf-8') as f:
        cfg = json.load(f)
    old = cfg.get('inferenceGatewayBaseUrl')
    cfg['inferenceGatewayBaseUrl'] = f'http://127.0.0.1:{port}' if mode == 'proxy' else DIRECT
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    return path, old, cfg['inferenceGatewayBaseUrl']


if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else ''
    if mode == 'status':
        try:
            print(f'{config_path()} -> {current_gateway()}')
        except Exception as e:
            sys.exit(f'status error: {e}')
        sys.exit(0)
    if mode not in ('proxy', 'direct'):
        sys.exit(__doc__)
    port = sys.argv[sys.argv.index('--port') + 1] if '--port' in sys.argv else '8787'
    path, old, new = set_gateway(mode, port)
    print(f'inferenceGatewayBaseUrl: {old} -> {new}  ({path})')
    print('Restart Claude Desktop to apply.')
