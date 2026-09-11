"""Optional self-update. Friends only see a new version if latest.json is hosted."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlparse

APP_VERSION = '3.2.14'
USER_AGENT = f'AIUsageWidget/{APP_VERSION}'
JSON_LIMIT = 256 * 1024
ZIP_LIMIT = 30 * 1024 * 1024
CHECK_EVERY = 30 * 60

APPLY_PS1 = r'''param([string]$Target,[string]$Source,[int]$WaitPid,[string]$Launch)
$ErrorActionPreference = 'Stop'
for ($i = 0; $i -lt 80; $i++) {
    if (-not (Get-Process -Id $WaitPid -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 200
}
Start-Sleep -Milliseconds 400
Copy-Item -Path (Join-Path $Source '*') -Destination $Target -Recurse -Force
if (Test-Path -LiteralPath $Launch) {
    Start-Process -FilePath 'wscript.exe' -ArgumentList ('"' + $Launch + '"')
}
'''


def parse_version(text):
    parts = []
    for piece in str(text or '').strip().split('.'):
        digits = ''.join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits or 0))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def is_newer(remote, local=APP_VERSION):
    return parse_version(remote) > parse_version(local)


def load_feed_url(root=None):
    path = Path(root or Path(__file__).resolve().parent) / 'feed_url.txt'
    if not path.is_file():
        return ''
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            return line
    return ''


def https_url(url):
    parsed = urlparse(str(url or ''))
    return parsed.scheme == 'https' and bool(parsed.netloc)


def sibling_zip_url(feed):
    feed = str(feed or '')
    if feed.endswith('latest.json'):
        return feed[:-len('latest.json')] + 'AIUsageWidget.zip'
    return ''


def pending_update(payload, local=APP_VERSION, feed=''):
    if not isinstance(payload, dict):
        return None
    version = str(payload.get('version') or '').strip()
    zip_url = str(payload.get('zip') or payload.get('url') or '').strip() or sibling_zip_url(feed)
    if not version or not is_newer(version, local) or not https_url(zip_url):
        return None
    notes = str(payload.get('notes') or '').strip()
    return {'version': version, 'zip': zip_url, 'notes': notes}


def note_lines(notes, limit=3):
    parts = []
    text = str(notes or '').replace('\r\n', '\n').replace('\r', '\n')
    for chunk in text.split('\n'):
        for bit in chunk.split(' / '):
            bit = bit.replace('`', '').strip()
            if not bit:
                continue
            parts.append(bit)
            if len(parts) >= limit:
                return parts
    return parts


def update_confirm_text(info):
    info = info or {}
    version = str(info.get('version') or '').strip()
    lines = [f'새 버전 {version}' if version else '새 버전']
    for note in note_lines(info.get('notes')):
        lines.append(f'· {note}')
    lines.append('')
    lines.append('이 파일을 받고 위젯을 다시 시작할까요?')
    return '\n'.join(lines)


def _open(url, timeout):
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_latest(url, timeout=8):
    if not https_url(url):
        return None
    try:
        with _open(url, timeout) as resp:
            raw = resp.read(JSON_LIMIT + 1)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None
    if not raw or len(raw) > JSON_LIMIT:
        return None
    try:
        payload = json.loads(raw.decode('utf-8-sig'))
    except (UnicodeError, json.JSONDecodeError):
        return None
    return pending_update(payload, feed=url)


def safe_extract(archive, dest):
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            name = info.filename.replace('\\', '/')
            if not name or name.endswith('/'):
                continue
            parts = Path(name).parts
            if info.is_dir() or name.startswith('/') or '..' in parts:
                continue
            target = (dest / name).resolve()
            if dest != target and dest not in target.parents:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, 'wb') as out:
                out.write(src.read())


def download_and_stage(zip_url, timeout=60):
    if not https_url(zip_url):
        raise RuntimeError('업데이트 주소가 올바르지 않습니다.')
    tmp = Path(tempfile.mkdtemp(prefix='AIUsageWidget-update-'))
    archive = tmp / 'AIUsageWidget.zip'
    extracted = tmp / 'files'
    with _open(zip_url, timeout) as resp:
        size = 0
        with open(archive, 'wb') as out:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > ZIP_LIMIT:
                    raise RuntimeError('업데이트 파일이 너무 큽니다.')
                out.write(chunk)
    safe_extract(archive, extracted)
    if not (extracted / 'usage_widget.py').is_file():
        raise RuntimeError('업데이트 파일에 위젯이 없습니다.')
    return extracted


def start_apply(source, target=None):
    target = Path(target or Path(__file__).resolve().parent)
    launch = target / 'start_usage_widget.vbs'
    script = Path(tempfile.gettempdir()) / 'AIUsageWidget-apply.ps1'
    script.write_text(APPLY_PS1, encoding='utf-8')
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    subprocess.Popen(
        [
            'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', str(script),
            '-Target', str(target),
            '-Source', str(source),
            '-WaitPid', str(os.getpid()),
            '-Launch', str(launch),
        ],
        creationflags=flags,
    )
    return True
