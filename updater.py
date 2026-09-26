"""Optional self-update. Friends only see a new version if latest.json is hosted."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urlparse

APP_VERSION = '3.11.5'
USER_AGENT = f'AIUsageWidget/{APP_VERSION}'
LAUNCHER_EXE = 'AI Usage.exe'
JSON_LIMIT = 256 * 1024
ZIP_LIMIT = 30 * 1024 * 1024
UNPACKED_LIMIT = 100 * 1024 * 1024
CHECK_EVERY = 30 * 60
# Written into every release zip by publish_update.ps1: the files that version
# ships. The next update removes what the old list had and the new one lacks.
MANIFEST = 'package_files.json'
STAGE_PREFIX = 'AIUsageWidget-update-'
BACKUP_PREFIX = 'AIUsageWidget-backup-'
APPLY_SCRIPT = 'AIUsageWidget-apply.py'
WAIT_FOR_EXIT = 20.0
APPLY_ATTEMPTS = 3
LOG_LIMIT = 256 * 1024


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


def same_release_origin(url, feed):
    """The archive must come from where the feed does: same host, and on
    GitHub the same owner/repository, so a feed cannot send us elsewhere."""
    a, b = urlparse(str(url or '')), urlparse(str(feed or ''))
    if a.scheme != 'https' or b.scheme != 'https' or a.netloc.lower() != b.netloc.lower():
        return False
    if a.netloc.lower() == 'github.com':
        repo = [part.lower() for part in a.path.split('/') if part][:2]
        return len(repo) == 2 and repo == [part.lower() for part in b.path.split('/') if part][:2]
    return True


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
    if feed and not same_release_origin(zip_url, feed):
        return None
    digest = str(payload.get('sha256') or '').strip().lower()
    if digest and not re.fullmatch(r'[0-9a-f]{64}', digest):
        return None
    notes = str(payload.get('notes') or '').strip()
    return {'version': version, 'zip': zip_url, 'notes': notes, 'sha256': digest}


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


def _relative_name(name):
    """A zip or manifest entry as a safe relative path, or None."""
    name = str(name or '').replace('\\', '/')
    if not name or name.endswith('/') or name.startswith('/'):
        return None
    parts = Path(name).parts
    if '..' in parts or Path(name).is_absolute() or ':' in parts[0]:
        return None
    return name


def safe_extract(archive, dest, limit=UNPACKED_LIMIT):
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    total = 0
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            name = _relative_name(info.filename)
            if name is None or info.is_dir():
                continue
            target = (dest / name).resolve()
            if dest != target and dest not in target.parents:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, 'wb') as out:
                while True:
                    chunk = src.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise RuntimeError('업데이트 파일이 너무 큽니다.')
                    out.write(chunk)


def download_and_stage(zip_url, timeout=60, sha256=''):
    if not https_url(zip_url):
        raise RuntimeError('업데이트 주소가 올바르지 않습니다.')
    tmp = Path(tempfile.mkdtemp(prefix=STAGE_PREFIX))
    try:
        archive = tmp / 'AIUsageWidget.zip'
        extracted = tmp / 'files'
        digest = hashlib.sha256()
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
                    digest.update(chunk)
                    out.write(chunk)
        if sha256 and digest.hexdigest() != sha256.lower():
            raise RuntimeError('받은 업데이트 파일이 배포된 파일과 다릅니다. 잠시 뒤 다시 시도하세요.')
        try:
            safe_extract(archive, extracted)
        except zipfile.BadZipFile:
            raise RuntimeError('업데이트 파일이 손상되었습니다. 잠시 뒤 다시 시도하세요.') from None
        if not (extracted / 'usage_widget.py').is_file():
            raise RuntimeError('업데이트 파일에 위젯이 없습니다.')
        return extracted
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def default_log_path():
    return Path(os.environ.get('APPDATA') or Path.home()) / 'AiUsageWidget' / 'update.log'


def start_apply(source, target=None, log=None):
    """Hand the staged files to a separate process that applies them once we exit.

    The widget's own fonts stay locked while it runs, so the copy has to wait
    for this process to end. The helper is this module, copied out of the
    install folder it is about to overwrite.
    """
    target = Path(target or Path(__file__).resolve().parent)
    script = Path(tempfile.gettempdir()) / APPLY_SCRIPT
    script.write_bytes(Path(__file__).read_bytes())
    subprocess.Popen(
        [
            sys.executable, '-B', str(script), 'apply',
            '--target', str(target),
            '--source', str(source),
            '--wait-pid', str(os.getpid()),
            '--log', str(log or default_log_path()),
        ],
        cwd=tempfile.gettempdir(),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    return True


def launch_after_update(target):
    target = Path(target)
    exe = target / LAUNCHER_EXE
    if exe.is_file():
        return exe
    return target / 'start_usage_widget.vbs'


def package_files(root):
    """Every file under `root` as a sorted list of relative '/' paths."""
    root = Path(root)
    return sorted(path.relative_to(root).as_posix() for path in root.rglob('*') if path.is_file())


def read_manifest(root):
    try:
        data = json.loads((Path(root) / MANIFEST).read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        return []
    files = data.get('files') if isinstance(data, dict) else None
    if not isinstance(files, list):
        return []
    return [name for name in (_relative_name(item) for item in files if isinstance(item, str)) if name]


def apply_update(source, target, backup):
    """Copy `source` over `target` and remove files the old package no longer has.

    All or nothing: every file that is overwritten or removed is saved to
    `backup` first, and any failure puts them back and deletes the files this
    attempt added, then raises. Returns the number of obsolete files removed.
    """
    source, target, backup = Path(source), Path(target), Path(backup)
    old = set(read_manifest(target))
    new_files = package_files(source)
    # A file the new package carries is never obsolete, listed or not.
    new = set(read_manifest(source)) | set(new_files)
    new.add(MANIFEST)
    saved, created = [], []
    try:
        for name in new_files:
            dest = target / name
            if dest.exists():
                keep = backup / name
                keep.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, keep)
                saved.append(name)
            else:
                created.append(name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            partial = dest.with_name(dest.name + '.updating')
            shutil.copy2(source / name, partial)
            os.replace(partial, dest)
        removed = 0
        root = target.resolve()
        for name in sorted(old - new):
            dest = target / name
            if not dest.is_file() or root not in dest.resolve().parents:
                continue
            keep = backup / name
            keep.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, keep)
            saved.append(name)
            dest.unlink()
            removed += 1
        if MANIFEST not in new_files:
            (target / MANIFEST).write_text(json.dumps({'files': sorted(new)}, indent=1), encoding='utf-8')
        return removed
    except BaseException as exc:
        lost = _roll_back(target, backup, saved, created)
        if lost:
            raise RollbackIncomplete(lost) from exc
        raise


class RollbackIncomplete(RuntimeError):
    """Some saved files could not be put back; their copies stay in the backup."""

    def __init__(self, names):
        super().__init__(f'could not restore {len(names)} file(s): {", ".join(names[:5])}')
        self.names = names


def _roll_back(target, backup, saved, created):
    """Undo one attempt. Returns the saved files that could not be restored."""
    for name in created:
        for path in (target / name, (target / name).with_name(Path(name).name + '.updating')):
            try:
                path.unlink()
            except OSError:
                pass
    lost = []
    for name in saved:
        try:
            shutil.copy2(backup / name, target / name)
        except OSError:
            lost.append(name)
        try:
            (target / name).with_name(Path(name).name + '.updating').unlink()
        except OSError:
            pass
    return lost


def wait_for_exit(pid, timeout=WAIT_FOR_EXIT):
    """True once `pid` has ended, False if it is still running after `timeout`."""
    if sys.platform == 'win32':
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
        if not handle:
            return True
        try:
            return kernel.WaitForSingleObject(ctypes.c_void_p(handle), int(timeout * 1000)) == 0
        finally:
            kernel.CloseHandle(ctypes.c_void_p(handle))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(int(pid), 0)
        except OSError:
            return True
        time.sleep(0.2)
    return False


def _log(path, message):
    if not path:
        return
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > LOG_LIMIT:
            os.replace(path, path.with_name(path.name + '.1'))
        with path.open('a', encoding='utf-8') as out:
            out.write(time.strftime('%Y-%m-%d %H:%M:%S') + ' ' + message + '\n')
    except OSError:
        pass


def _clean_temp(keep=()):
    """Remove every staged download and backup, this run's and older ones."""
    keep = {Path(path) for path in keep}
    for pattern in (STAGE_PREFIX + '*', BACKUP_PREFIX + '*'):
        for path in Path(tempfile.gettempdir()).glob(pattern):
            if path not in keep:
                shutil.rmtree(path, ignore_errors=True)


def relaunch(target):
    launch = launch_after_update(target)
    if not launch.is_file():
        return None
    if launch.suffix.lower() == '.exe':
        command = [str(launch)]
    else:
        command = ['wscript.exe', str(launch)]
    subprocess.Popen(command, cwd=str(target), close_fds=True)
    return launch


def run_apply(target, source, wait_pid, log=None, *, attempts=APPLY_ATTEMPTS, pause=1.0,
              wait=wait_for_exit, launch=relaunch):
    """The helper process: wait for the widget, apply, and always start it again."""
    target, source = Path(target), Path(source)
    if not wait(wait_pid):
        _log(log, f'widget {wait_pid} still running; applying anyway')
    applied = False
    kept = set()
    for attempt in range(1, attempts + 1):
        backup = Path(tempfile.mkdtemp(prefix=BACKUP_PREFIX))
        try:
            removed = apply_update(source, target, backup)
            applied = True
            _log(log, f'applied {source} to {target}; removed {removed} obsolete file(s)')
            break
        except RollbackIncomplete as exc:
            # The only good copy of these files is in the backup: keep it.
            kept.add(backup)
            _log(log, f'attempt {attempt} failed; {exc}; originals kept in {backup}')
        except Exception as exc:
            # Antivirus scans and slow exits hold files briefly; try again.
            _log(log, f'attempt {attempt} failed and was rolled back: {exc!r}')
        finally:
            if backup not in kept:
                shutil.rmtree(backup, ignore_errors=True)
        if attempt < attempts:
            time.sleep(pause)
    if applied:
        # A later attempt that succeeded made the install whole again.
        for backup in kept:
            shutil.rmtree(backup, ignore_errors=True)
        kept.clear()
    try:
        launch(target)
    except OSError as exc:
        _log(log, f'restart failed: {exc!r}')
    _clean_temp(keep=kept)
    return applied


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog='updater')
    parser.add_argument('command', choices=['apply'])
    parser.add_argument('--target', required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--wait-pid', type=int, required=True)
    parser.add_argument('--log', default='')
    args = parser.parse_args(argv)
    return 0 if run_apply(args.target, args.source, args.wait_pid, args.log or None) else 1


if __name__ == '__main__':
    raise SystemExit(main())
