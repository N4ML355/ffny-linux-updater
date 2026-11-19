#!/usr/bin/env python3
"""Build signed FFNY Online Windows releases on Linux, for wine-update-bridge.exe.

Two entry points, chosen by the launcher or by this repository's setup:

* the official launcher downloads and verifies an update, then hands it here
  (--setup): the signature and the setup hash are verified again, the payload is
  extracted without running the Windows installer, and a new candidate is built.
* the fork's Windows setup (github.com/N4ML355/ffny-online, wine-latest) runs under
  Wine and hands its already unpacked payload here (--payload): the payload
  manifest is verified instead, and a fresh installation can be made from the
  player's own game dump (--dump).

Either way the game is cross-compiled with llvm-mingw and receipts change only
after a successful build. Machine-specific paths come from, in order: command
line, ./config.json (written by setup.sh), environment variables (FFNY_INSTALL,
FFNY_DATA, FFNY_PREFIX, FFNY_LOG, FFNY_TOOLCHAIN).
"""
import argparse
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

import pefile
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

TOOLS = Path(__file__).resolve().parent
CONFIG = TOOLS / 'config.json'
BASE_URL = 'https://play.defjam.online/updates/'
PUBLIC_KEY = bytes.fromhex(
    '046f8ce805a386c294c1a24de83d07ce6a0c296ebf836d00434e9a5f0256c6dd7a'
    '6d60d26ef38562ee1e29b99dddca155d89f2a6a28a32c4559c78ea00d28e5c05')
# Number of steps the setup's own progress files use (no HD textures).
STEPS = 5


class CancelError(Exception):
    pass


def load_config():
    try:
        return json.loads(CONFIG.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def resolve(value, key, env, fallback=None):
    if value:
        return value
    config = load_config()
    if config.get(key):
        return config[key]
    return os.environ.get(env) or fallback


def find_toolchain(explicit):
    if explicit:
        return Path(explicit).expanduser().resolve()
    configured = load_config().get('toolchain')
    if configured:
        return Path(configured).expanduser().resolve()
    bundled = sorted((TOOLS / 'toolchain').glob('llvm-mingw-*/bin/x86_64-w64-mingw32-gcc'))
    if bundled:
        return bundled[0].parent.parent
    system = shutil.which('x86_64-w64-mingw32-gcc')
    if system:
        return Path(system).resolve().parent.parent
    raise ValueError('No mingw toolchain found: run setup.sh or pass --toolchain')


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.pending')
    tmp.write_bytes(data if isinstance(data, bytes) else data.encode('utf-8'))
    tmp.replace(path)


def sha(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def fetch(name):
    with urllib.request.urlopen(BASE_URL + name, timeout=60) as response:
        return response.read()


def signed_release():
    raw = fetch('latest.json')
    signature = bytes.fromhex(fetch('latest.json.sig').decode().strip())
    if len(signature) != 64:
        raise ValueError('Invalid update signature length')
    key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), PUBLIC_KEY)
    key.verify(utils.encode_dss_signature(int.from_bytes(signature[:32], 'big'),
                                         int.from_bytes(signature[32:], 'big')),
               raw, ec.ECDSA(hashes.SHA256()))
    release = json.loads(raw)
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+\.exe', release['setup']) or
            not re.fullmatch(r'[0-9a-f]{64}', release['sha256']) or
            not 0 < release['size'] <= 512 * 1024 * 1024):
        raise ValueError('Invalid signed update manifest')
    return release, raw


def safe_relative(name):
    path = PurePosixPath(name)
    if (not name or '\\' in name or ':' in name or path.is_absolute()
            or any(p in {'', '.', '..'} or p.rstrip(' .') != p for p in name.split('/'))):
        raise ValueError('Unsafe payload or disc path: ' + name)
    if any(re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?', p, re.I)
           for p in path.parts):
        raise ValueError('Reserved Windows filename: ' + name)
    return Path(*path.parts)


def verify_payload(payload):
    """The setup payload's own inventory: a moving branch or a partial unpack is refused."""
    payload = Path(payload)
    manifest = json.loads((payload / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('schema') != 1 or manifest.get('platform') != 'windows-x64':
        raise ValueError('Unsupported setup payload')
    if not re.fullmatch(r'\d+\.\d+\.\d+', manifest.get('version', '')):
        raise ValueError('Invalid release version')
    for key in ('source_commit', 'toolkit_commit'):
        if not re.fullmatch('[0-9a-f]{40}', manifest.get(key, '')):
            raise ValueError('Invalid release identity: ' + key)
    expected = manifest['files']
    for name, digest in expected.items():
        path = payload / safe_relative(name)
        if path.is_symlink() or not path.is_file() or sha(path) != digest:
            raise ValueError('Payload verification failed: ' + name)
    actual = {p.relative_to(payload).as_posix() for p in payload.rglob('*') if p.is_file()}
    if actual != set(expected) | {'manifest.json'}:
        raise ValueError('Unexpected files in setup payload')
    return manifest


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_dump(folder, source):
    module = load_module('setup_verify_dump', source / 'scripts/verify-dump.py')
    manifest = json.loads((source / 'config/dump-manifest.json').read_text(encoding='utf-8'))
    problems = module.verify(str(folder), manifest)
    if problems:
        raise ValueError('Dump verification failed: ' + '; '.join(problems))


def locate_dump(dump):
    """A chosen folder without default.xbe at its top is often the folder around the dump."""
    if not dump.is_dir() or (dump / 'default.xbe').is_file():
        return dump
    try:
        images = [p for p in dump.iterdir() if p.is_file() and p.suffix.lower() in ('.iso', '.xiso')]
        folders = [p for p in dump.iterdir() if p.is_dir() and (p / 'default.xbe').is_file()]
        folders += [q for p in dump.iterdir() if p.is_dir() for q in p.iterdir()
                    if q.is_dir() and (q / 'default.xbe').is_file()]
    except OSError:
        return dump
    found = images + folders
    if len(found) == 1:
        return found[0].resolve()
    raise ValueError('The chosen dump folder has no default.xbe' + (
        ' and holds more than one possible dump' if found else '') +
        '. Choose the Xbox disc image itself, or the folder that contains default.xbe.')


def extract_setup(setup, destination):
    pe = pefile.PE(str(setup))
    payload = None
    for kind in pe.DIRECTORY_ENTRY_RESOURCE.entries:
        if kind.id != 10:  # RT_RCDATA
            continue
        for item in kind.directory.entries:
            if item.id == 101:
                entry = item.directory.entries[0].data.struct
                payload = pe.get_data(entry.OffsetToData, entry.Size)
    pe.close()
    if payload is None:
        raise ValueError('Setup ZIP resource 101 is missing')
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for member in archive.infolist():
            name = PurePosixPath(member.filename.replace('\\', '/'))
            if name.is_absolute() or '..' in name.parts or ':' in str(name):
                raise ValueError('Invalid payload path')
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Unexpected symbolic link in setup payload')
        archive.extractall(destination)
    return json.loads((destination / 'manifest.json').read_text())


def replace_once(path, old, new):
    text = path.read_text()
    if text.count(old) != 1:
        raise ValueError(f'Unsupported source layout in {path.name}; keeping the installed version')
    path.write_text(text.replace(old, new, 1))


def patch_sources(source):
    """Only compiler compatibility and the Wine update/restart handoff."""
    path = source / 'src/hooks/updater.c'
    replace_once(path, '#include "updater_guard.h"', '''#include "updater_guard.h"

/* Local Wine build adapter; signature/download checks below stay unchanged. */
static int wine_update_bridge(wchar_t *path, size_t count, const char *install)
{
    HMODULE ntdll = GetModuleHandleW(L"ntdll.dll");
    wchar_t root[MAX_PATH * 2];
    if (!ntdll || !GetProcAddress(ntdll, "wine_get_version")) return 0;
    if (!MultiByteToWideChar(CP_UTF8, 0, install, -1, root, MAX_PATH * 2)) return 0;
    _snwprintf(path, count, L"%ls\\\\..\\\\..\\\\ffny-tools\\\\wine-update-bridge.exe", root);
    path[count - 1] = 0;
    return GetFileAttributesW(path) != INVALID_FILE_ATTRIBUTES;
}
''')
    replace_once(path, 'wpath, winstall, wdata, wstatus, wlog);', '''wpath, winstall, wdata, wstatus, wlog);
        {
            wchar_t bridge[MAX_PATH * 2];
            if (wine_update_bridge(bridge, MAX_PATH * 2, install))
                _snwprintf(cmd, sizeof(cmd) / sizeof(cmd[0]),
                    L"\\"%ls\\" --setup \\"%ls\\" --install-dir \\"%ls\\" --data-dir \\"%ls\\""
                    L" --status-file \\"%ls\\" --log \\"%ls\\"",
                    bridge, wpath, winstall, wdata, wstatus, wlog);
        }''')
    marker = '    snprintf(cmd, sizeof(cmd),\n             "powershell.exe'
    replacement = '''    {
        wchar_t bridge[MAX_PATH * 2], root[MAX_PATH * 2], command[MAX_PATH * 6];
        if (wine_update_bridge(bridge, MAX_PATH * 2, install)) {
            STARTUPINFOW startup = {sizeof(startup)};
            PROCESS_INFORMATION process;
            MultiByteToWideChar(CP_UTF8, 0, install, -1, root, MAX_PATH * 2);
            _snwprintf(command, MAX_PATH * 6, L"\\"%ls\\" --restart %lu \\"%ls\\"",
                       bridge, (unsigned long)GetCurrentProcessId(), root);
            if (CreateProcessW(NULL, command, NULL, NULL, FALSE, CREATE_NO_WINDOW,
                               NULL, NULL, &startup, &process)) {
                CloseHandle(process.hThread); CloseHandle(process.hProcess);
            }
            return;
        }
    }
    snprintf(cmd, sizeof(cmd),
             "powershell.exe'''
    replace_once(path, marker, replacement)
    path = source / 'src/hooks/d3d11_translator.c'
    text = path.read_text()
    path.write_text(text.replace('LoadCursorW(NULL, IDC_ARROW)',
                                 'LoadCursorW(NULL, MAKEINTRESOURCEW(32512))'))
    path = source / 'src/hooks/rollback.c'
    text = path.read_text()
    if 'InterlockedExchange8(' in text:
        replace_once(path, 'static void first_write(uint32_t p, int game)',
                     '#if defined(__MINGW32__)\n'
                     'static __inline__ char InterlockedExchange8(volatile char *t, char v) '
                     '{ return (char)__atomic_exchange_n(t, v, __ATOMIC_SEQ_CST); }\n'
                     '#endif\nstatic void first_write(uint32_t p, int game)')
    text = path.read_text()
    path.write_text(text.replace('static int arena_free_now(uint32_t a)\n{',
                                 'int arena_free_now(uint32_t a)\n{', 1))
    path = source / 'CMakeLists.txt'
    replace_once(path, 'd3d11 dxgi dxguid xinput winmm dbghelp shell32 comdlg32)',
                 'd3d11 dxgi dxguid xinput winmm dbghelp shell32 comdlg32 crypt32 ws2_32)')


class Update:
    def __init__(self, args):
        self.args = args
        self.prefix = Path(resolve(args.prefix, 'prefix', 'FFNY_PREFIX',
                                   os.environ.get('WINEPREFIX') or Path.home() / '.wine')).expanduser()
        install = resolve(args.install_dir, 'install_dir', 'FFNY_INSTALL')
        data = resolve(args.data_dir, 'data_dir', 'FFNY_DATA')
        self.app = self.local_path(install) if install else None
        self.data = self.local_path(data) if data else (self.app.parent / 'data' if self.app else None)
        self.dump = self.local_path(args.dump) if args.dump else None
        self.cancel = self.local_path(args.cancel_file) if args.cancel_file else None
        try:
            self.toolchain = find_toolchain(resolve(args.toolchain, 'toolchain', 'FFNY_TOOLCHAIN'))
        except ValueError:
            self.toolchain = None
        self.log = self.local_path(resolve(args.log, 'log', 'FFNY_LOG', TOOLS / 'wine-update.log'))
        self.status = self.local_path(args.status_file) if args.status_file else None
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.env = os.environ.copy()
        if self.toolchain:
            self.env['PATH'] = f"{self.toolchain / 'bin'}:{self.env.get('PATH', '/usr/local/bin:/usr/bin:/bin')}"
        if self.data:
            self.env['DEFJAM_DATA'] = str(self.data)
        for key in list(self.env):
            if key.startswith(('RECOMP_', 'LD_', 'DYLD_')):
                self.env.pop(key)

    def local_path(self, value):
        value = str(value)
        if re.match(r'^[A-Za-z]:[\\/]', value):
            return (self.prefix / 'dosdevices' / (value[0].lower() + ':') /
                    value[3:].replace('\\', '/')).resolve()
        return Path(value).expanduser().resolve()

    def windows_path(self, path):
        path = Path(path).resolve()
        for link in sorted((self.prefix / 'dosdevices').glob('[A-Za-z]:')):
            try:
                relative = path.relative_to(Path(os.path.realpath(link)))
            except ValueError:
                continue
            return f'{link.name[0].upper()}:\\' + str(relative).replace('/', '\\')
        return 'Z:' + str(path).replace('/', '\\')

    def paths(self):
        if not self.app:
            raise ValueError('No FFNY Online install folder configured: run setup.sh or pass --install-dir')
        if not self.data:
            raise ValueError('No FFNY Online data folder configured: pass --data-dir')
        return self.app, self.data

    def installation(self):
        app, data = self.paths()
        if not (app / 'installed.json').is_file():
            raise ValueError('No FFNY Online installation found at the configured path '
                             '(installed.json is missing); run setup.sh first')
        if not data.is_dir():
            raise ValueError('The FFNY Online data directory is missing; run setup.sh first')
        return app, data

    def cancelled(self):
        if self.cancel and self.cancel.exists():
            raise CancelError()

    def event(self, text, step=0):
        print(text, flush=True)
        with self.log.open('a') as f:
            f.write(text + '\n')
        if self.status:
            atomic(self.status, text + '\n')
            if step:
                atomic(str(self.status) + '.stage', f'{step} {STEPS} linux {text}\n')
                atomic(str(self.status) + '.progress', f'{(step - 1) * 1000 // STEPS}\n')

    def run(self, command, cwd):
        with self.log.open('a') as log:
            log.write('Running: ' + repr(list(map(str, command))) + '\n')
            log.flush()
            subprocess.run(list(map(str, command)), cwd=cwd, env=self.env,
                           stdout=log, stderr=subprocess.STDOUT, check=True)

    def prepare_candidate(self, candidate, data, build):
        source = candidate / 'source'
        build_number = int((source / 'config/remaster-version.txt').read_text().strip())
        if build_number != build:
            raise ValueError('Payload build does not match the release manifest')
        self.event('Preparing Wine launcher integration', 2)
        patch_sources(source)
        game = source / 'game'
        target = (data / 'extracted').resolve()
        if game.is_symlink() and game.resolve() == target:
            pass
        elif game.exists() or game.is_symlink():
            raise ValueError('Existing game link points to different data')
        else:
            game.symlink_to(target, target_is_directory=True)
        (source / 'logs').mkdir(exist_ok=True)
        manifest = json.loads((candidate / 'manifest.json').read_text())
        atomic(candidate / 'wine-adapter.json', json.dumps({
            'adapter': 1, 'build': build_number, 'source_commit': manifest['source_commit'],
            'toolkit_commit': manifest['toolkit_commit'],
        }, indent=2))
        return build_number

    def prepare_data(self, candidate):
        """Fresh extraction from --dump, or the game files this installation already has."""
        app, data = self.paths()
        source = candidate / 'source'
        extracted = data / 'extracted'
        data.mkdir(parents=True, exist_ok=True)
        if self.dump is None:
            if not extracted.is_dir():
                raise ValueError('This installation has no game files to reuse; run setup with your dump')
            return False
        self.event('Verifying and preparing your dump', 2)
        return self.extract_dump(source)

    def extract_dump(self, source):
        _, data = self.paths()
        dump = locate_dump(self.dump)
        extracted = data / 'extracted'
        state = data / 'extraction-state.json'
        if not dump.exists():
            raise ValueError('Dump does not exist')
        if dump.is_dir():
            validate_dump(dump, source)
            files = sorted(p for p in dump.rglob('*') if p.is_file())
            if any(p.is_symlink() for p in dump.rglob('*')):
                raise ValueError('Dump folder must not contain symlinks')
            fingerprint = {p.relative_to(dump).as_posix(): sha(p) for p in files}
        else:
            fingerprint = {'image': sha(dump)}
        if state.is_file() and extracted.is_dir():
            recorded = json.loads(state.read_text(encoding='utf-8'))
            current = {p.relative_to(extracted).as_posix(): sha(p)
                       for p in extracted.rglob('*') if p.is_file()}
            if recorded.get('input') == fingerprint and recorded.get('outputs') == current:
                validate_dump(extracted, source)
                self.event('Verified extraction reused')
                return False
        if extracted.exists():
            raise ValueError('Existing data/extracted differs from this dump; choose a new data folder')
        staging = Path(tempfile.mkdtemp(prefix='extract-pending-', dir=data))
        try:
            if dump.is_dir():
                for p in files:
                    self.cancelled()
                    destination = staging / safe_relative(p.relative_to(dump).as_posix())
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(p, destination)
            else:
                sys.path.insert(0, str(source / 'tools/xboxrecomp'))
                try:
                    from tools.xiso.xdvdfs import Xiso, XisoError, SECTOR
                    with Xiso(str(dump)) as iso:
                        seen_dirs, seen_files = set(), set()
                        pending = [('', iso.root_sector, iso.root_size)]
                        total_size = dump.stat().st_size
                        while pending:
                            directory, sector, size = pending.pop()
                            if (sector, size) in seen_dirs or size > 16 * 1024 * 1024:
                                raise ValueError('Malformed disc directory')
                            seen_dirs.add((sector, size))
                            if iso.base + sector * SECTOR + size > total_size:
                                raise ValueError('Truncated disc directory')
                            for entry in iso._read_dir(sector, size):
                                self.cancelled()
                                name = (directory + '/' + entry.name).lstrip('/')
                                destination = staging / safe_relative(name)
                                folded = name.casefold()
                                if folded in seen_files or len(seen_files) >= 100000:
                                    raise ValueError('Duplicate or excessive disc entries')
                                seen_files.add(folded)
                                if iso.base + entry.sector * SECTOR + entry.size > total_size:
                                    raise ValueError('Truncated disc entry')
                                if entry.is_dir:
                                    pending.append((name, entry.sector, entry.size))
                                else:
                                    iso.extract(entry, str(destination))
                except XisoError as ex:
                    raise ValueError('Invalid Xbox disc image: ' + str(ex)) from ex
                finally:
                    sys.path.pop(0)
            validate_dump(staging, source)
            outputs = {p.relative_to(staging).as_posix(): sha(p)
                       for p in staging.rglob('*') if p.is_file()}
            atomic(state, json.dumps({'input': fingerprint, 'outputs': outputs}) + '\n')
            staging.rename(extracted)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return True

    def execute(self):
        """Signed update from the official launcher (--setup) or --check."""
        app, data = self.installation()
        self.event('Verifying the signed official update', 1)
        release, raw = signed_release()
        if self.args.check:
            self.event(f'Verified latest release: build {release["build"]}, {release["version"]}')
            return
        downloads = TOOLS / 'downloads'
        downloads.mkdir(exist_ok=True)
        if self.args.setup:
            setup = self.local_path(self.args.setup)
        else:
            setup = downloads / release['setup']
            if not setup.is_file() or sha(setup) != release['sha256']:
                self.event('Downloading the signed Windows setup', 1)
                atomic(setup, fetch(release['setup']))
        if setup.stat().st_size != release['size'] or sha(setup) != release['sha256']:
            raise ValueError('Setup does not match the current signed release; retry from the launcher')
        candidate = app / 'versions' / f'linux-wine-{release["build"]}-{int(time.time())}'
        if candidate.exists():
            raise ValueError(f'Candidate already exists: {candidate}; preserved for inspection')
        candidate.mkdir()
        self.event('Extracting the verified Windows sources', 2)
        extract_setup(setup, candidate)
        verify_payload(candidate)
        atomic(candidate / 'signed-release.json', raw)
        self.prepare_candidate(candidate, data, release['build'])
        fresh = self.prepare_data(candidate)
        if self.args.prepare_only:
            self.event(f'Prepared candidate: {candidate}')
            return
        self.build(candidate)
        self.prepare_online(candidate)
        if not self.args.build_only:
            self.activate(candidate)

    def check_destination(self, app, data):
        """Same ownership rules as the official setup, so a Windows install can take over later."""
        marker = app / 'setup-owner.json'
        identity = {'schema': 1, 'product': 'DefJamRecompiled'}
        if marker.is_file():
            if json.loads(marker.read_text(encoding='utf-8')) != identity:
                raise ValueError('Destination has a different setup ownership marker')
        receipt = app / 'installed.json'
        if receipt.is_file():
            previous = json.loads(receipt.read_text(encoding='utf-8'))
            if previous.get('schema') != 1:
                raise ValueError('Destination has an invalid installation receipt')
            if self.local_path(previous['data']) != data.resolve():
                raise ValueError('Updates must use the installed data folder: ' + previous['data'])
        elif app.is_dir() and any(p.name not in {'logs', 'setup.lock', 'play.lock', '.DS_Store'}
                                  for p in app.iterdir()):
            raise ValueError('Choose an empty install folder or a recognized Def Jam installation')

    def execute_local(self):
        """The fork's setup under Wine: --payload (already unpacked and verified by it)."""
        app, data = self.paths()
        self.check_destination(app, data)
        payload = self.local_path(self.args.payload)
        if not (payload / 'manifest.json').is_file():
            raise ValueError('The setup payload folder is incomplete: ' + str(payload))
        self.event('Verifying the setup payload', 1)
        verify_payload(payload)
        build = int((payload / 'source/config/remaster-version.txt').read_text().strip())
        candidate = app / 'versions' / f'linux-wine-{build}-{int(time.time())}'
        if candidate.exists():
            raise ValueError(f'Candidate already exists: {candidate}; preserved for inspection')
        app.mkdir(parents=True, exist_ok=True)
        candidate.mkdir(parents=True)
        self.event('Copying the verified Windows sources', 2)
        shutil.copytree(payload, candidate, dirs_exist_ok=True)
        self.prepare_candidate(candidate, data, build)
        fresh = self.prepare_data(candidate)
        if self.args.prepare_only:
            self.event(f'Prepared candidate: {candidate}')
            return
        self.build(candidate)
        self.prepare_online(candidate)
        if not self.args.build_only:
            self.activate(candidate)

    def build(self, candidate):
        if not self.toolchain:
            raise ValueError('No mingw toolchain found: run setup.sh or pass --toolchain')
        _, data = self.paths()
        source = candidate / 'source'
        self.cancelled()
        self.event('Analysing the game dump', 3)
        self.run([sys.executable, '-B', 'scripts/pipeline.py', 'analyze', '--data', data, '--no-archive'], source)
        self.cancelled()
        self.event('Translating the game code', 3)
        self.run([sys.executable, '-B', 'scripts/pipeline.py', 'recomp'], source)
        self.cancelled()
        self.event('Compiling the Windows game on Linux', 4)
        common = '-DNTDDI_VERSION=0x0A00000B -D_WIN32_WINNT=0x0A00 -DWINVER=0x0A00 '
        # Source-dir overrides from the payload's own dependencies.json, like the official
        # setup's engine.py imgui_override(): the source sits one level below setup-deps/<name>.
        overrides = []
        for item in json.loads((candidate / 'dependencies.json').read_text()):
            sd = item.get('source_dir', '').replace('\\', '/')
            if '/sdl3/' in sd + '/':
                overrides.append(f'-DFETCHCONTENT_SOURCE_DIR_SDL3={source / PurePosixPath(sd)}')
            elif '/imgui/' in sd + '/':
                overrides.append(f'-DFETCHCONTENT_SOURCE_DIR_IMGUI={source / PurePosixPath(sd)}')
        if len(overrides) != 2:
            raise ValueError('The payload does not inventory the SDL3 and Dear ImGui sources')
        build = source / 'build/win-x64-release'
        self.run(['cmake', '-S', source, '-B', build, '-G', 'Ninja',
                  '-DCMAKE_BUILD_TYPE=RelWithDebInfo',
                  f'-DCMAKE_TOOLCHAIN_FILE={TOOLS / "ffny-toolchain.cmake"}',
                  '-DCMAKE_C_FLAGS=' + common + '-Wno-int-conversion -Wno-incompatible-pointer-types -Wno-implicit-function-declaration',
                  '-DCMAKE_CXX_FLAGS=' + common + '-fms-extensions -Wno-int-conversion -Wno-incompatible-pointer-types',
                  '-DCMAKE_EXE_LINKER_FLAGS=-static', *overrides], source)
        self.run(['cmake', '--build', build, '--target', 'defjam_recomp', '--parallel', '2'], source)
        exe = build / 'defjam_recomp.exe'
        if not exe.is_file() or exe.stat().st_size < 1024 * 1024:
            raise ValueError('No complete Windows game executable was built')
        (source / 'bin').mkdir(exist_ok=True)
        shutil.copyfile(exe, source / 'bin/defjam_recomp.exe')
        atomic(candidate / 'build-complete.json', json.dumps({'exe_sha256': sha(exe)}))
        self.event(f'Windows candidate built: {candidate}', 5)

    def prepare_online(self, candidate):
        """The ONLINE main-menu entry and the online screens, made from the player's files.
        The setup reports a dump it does not recognise but keeps going."""
        source = candidate / 'source'
        for script, what in (('apt-patch.py', 'the ONLINE entry in the main menu'),
                             ('online-art.py', "the online screens' art")):
            try:
                self.run([sys.executable, '-B', str(source / 'scripts' / script), '--data', self.data], source)
            except subprocess.CalledProcessError as error:
                self.event(f'Could not prepare {what} ({error}); online play stays in the launcher')

    def activate(self, candidate):
        app, data = self.paths()
        source = candidate / 'source'
        complete = json.loads((candidate / 'build-complete.json').read_text())
        if sha(source / 'bin/defjam_recomp.exe') != complete['exe_sha256']:
            raise ValueError('Candidate executable checksum mismatch')
        manifest = json.loads((candidate / 'manifest.json').read_text())
        receipt_path = app / 'installed.json'
        previous = json.loads(receipt_path.read_text()) if receipt_path.is_file() else None
        receipt = dict(previous) if previous else {}
        receipt.update({key: manifest[key] for key in ('version', 'source_commit', 'toolkit_commit')})
        receipt.setdefault('hd_packs', [])
        receipt.update(schema=1, source=self.windows_path(source), data=self.windows_path(data),
                       executable=self.windows_path(source / 'bin/defjam_recomp.exe'))
        launcher = candidate / 'DefJamLauncher.exe'
        if launcher.is_file():
            atomic(app / 'DefJamLauncher.exe', launcher.read_bytes())
        if previous is None:
            atomic(app / 'setup-owner.json',
                   json.dumps({'schema': 1, 'product': 'DefJamRecompiled'}, indent=2) + '\n')
        else:
            backup = candidate / 'previous-install'
            backup.mkdir(exist_ok=True)
            for name in ('installed.ini', 'installed.json'):
                if (backup / name).exists():
                    raise ValueError('This candidate was already activated; keeping its rollback receipts')
                shutil.copyfile(app / name, backup / name)
        # installed.ini is the launcher's actual switch; publish it last.
        content = '[install]\r\n' + ''.join(f'{key}={receipt[key]}\r\n'
                                            for key in ('source', 'data', 'executable'))
        try:
            atomic(receipt_path, json.dumps(receipt, indent=2))
            atomic(app / 'installed.ini', b'\xff\xfe' + content.encode('utf-16-le'))
        except BaseException:
            if previous is None:
                for name in ('installed.json', 'installed.ini'):
                    (app / name).unlink(missing_ok=True)
            else:
                for name in ('installed.json', 'installed.ini'):
                    atomic(app / name, (backup / name).read_bytes())
            raise
        self.event('Setup complete. Start the game from DefJamLauncher.exe.', 5)
        if self.status:
            atomic(str(self.status) + '.progress', '1000\n')

    def save_config(self):
        config = load_config()
        config.update(install_dir=str(self.app), data_dir=str(self.data), prefix=str(self.prefix))
        if self.toolchain:
            config['toolchain'] = str(self.toolchain)
        atomic(CONFIG, json.dumps(config, indent=2) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--setup', help='downloaded DefJamSetup-*.exe (signed update)')
    parser.add_argument('--payload', help='already unpacked setup payload (fork setup under Wine)')
    parser.add_argument('--dump', help='Xbox disc image or extracted game folder (fresh install)')
    parser.add_argument('--install-dir', help='FFNY Online folder (contains installed.json once installed)')
    parser.add_argument('--data-dir', help='FFNY Online data folder')
    parser.add_argument('--prefix', help='Wine prefix used by the game')
    parser.add_argument('--toolchain', help='llvm-mingw root (bin/x86_64-w64-mingw32-gcc)')
    parser.add_argument('--log')
    parser.add_argument('--status-file')
    parser.add_argument('--cancel-file')
    parser.add_argument('--done-file')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--build-only', action='store_true')
    parser.add_argument('--resume-build', type=Path)
    parser.add_argument('--activate', type=Path)
    # Flags the fork's Windows setup/wizard passes; handled or accepted here.
    parser.add_argument('--hd-textures', action='store_true')
    parser.add_argument('--install-prerequisites', action='store_true')
    parser.add_argument('--reuse-extracted', action='store_true')
    parser.add_argument('--allow-running', action='store_true')
    parser.add_argument('--silent', action='store_true')
    parser.add_argument('--no-shortcuts', action='store_true')
    parser.add_argument('--no-desktop-shortcut', action='store_true')
    parser.add_argument('--steam-shortcut', action='store_true')
    args = parser.parse_args()
    update = Update(args)
    code = 4
    try:
        with (TOOLS / 'wine-update.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('Another update is already running') from None
            if args.hd_textures:
                raise ValueError('HD textures are not supported by the Linux adapter yet; '
                                 'install without them')
            if args.resume_build:
                update.build(args.resume_build.resolve())
            elif args.activate:
                update.activate(args.activate.resolve())
            elif args.payload:
                update.execute_local()
            else:
                update.execute()
        code = 0
        if not args.check and not args.prepare_only and not args.resume_build:
            update.save_config()
    except CancelError:
        update.event('Setup cancelled; run again to resume completed work')
        code = 6
    except Exception as error:
        update.event('Setup stopped: ' + str(error))
    finally:
        if args.done_file:
            atomic(update.local_path(args.done_file), str(code) + '\n')
    return code


if __name__ == '__main__':
    sys.exit(main())
