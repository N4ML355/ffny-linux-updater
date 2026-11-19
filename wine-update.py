#!/usr/bin/env python3
"""Build signed FFNY Online Windows updates on Linux, for wine-update-bridge.exe.

The official launcher still downloads and verifies updates. This adapter verifies
the signed manifest again, extracts the setup's ZIP without running its Windows
installer, and builds a separate candidate. Receipts change only after success.

Machine-specific paths come from, in order: command line, ./config.json (written
by setup.sh), environment variables (FFNY_INSTALL, FFNY_DATA, FFNY_PREFIX,
FFNY_LOG, FFNY_TOOLCHAIN).
"""
import argparse
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
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

    def installation(self):
        if not self.app or not (self.app / 'installed.json').is_file():
            raise ValueError('No FFNY Online installation found at the configured path '
                             '(installed.json is missing); run setup.sh first')
        if not self.data or not self.data.is_dir():
            raise ValueError('The FFNY Online data directory is missing; run setup.sh first')
        return self.app, self.data

    def event(self, text, step=0):
        print(text, flush=True)
        with self.log.open('a') as f:
            f.write(text + '\n')
        if self.status:
            atomic(self.status, text + '\n')
            if step:
                atomic(str(self.status) + '.stage', f'{step} 5 linux {text}\n')
                atomic(str(self.status) + '.progress', f'{(step - 1) * 200}\n')

    def run(self, command, cwd):
        with self.log.open('a') as log:
            log.write('Running: ' + repr(list(map(str, command))) + '\n')
            log.flush()
            subprocess.run(list(map(str, command)), cwd=cwd, env=self.env,
                           stdout=log, stderr=subprocess.STDOUT, check=True)

    def execute(self):
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
        atomic(candidate / 'signed-release.json', raw)
        self.event('Extracting the verified Windows sources', 2)
        manifest = extract_setup(setup, candidate)
        source = candidate / 'source'
        build_number = int((source / 'config/remaster-version.txt').read_text().strip())
        if build_number != release['build']:
            raise ValueError('Payload build does not match the signed manifest')
        self.event('Preparing Wine launcher integration', 2)
        patch_sources(source)
        (source / 'game').symlink_to(data / 'extracted', target_is_directory=True)
        (source / 'logs').mkdir(exist_ok=True)
        atomic(candidate / 'wine-adapter.json', json.dumps({
            'adapter': 1, 'build': build_number, 'source_commit': manifest['source_commit'],
            'toolkit_commit': manifest['toolkit_commit'], 'setup_sha256': release['sha256'],
        }, indent=2))
        if self.args.prepare_only:
            self.event(f'Prepared candidate: {candidate}')
            return
        self.build(candidate)
        if not self.args.build_only:
            self.activate(candidate)

    def build(self, candidate):
        if not self.toolchain:
            raise ValueError('No mingw toolchain found: run setup.sh or pass --toolchain')
        _, data = self.installation()
        source = candidate / 'source'
        self.event('Analysing the game dump', 3)
        self.run([sys.executable, '-B', 'scripts/pipeline.py', 'analyze', '--data', data, '--no-archive'], source)
        self.event('Translating the game code', 3)
        self.run([sys.executable, '-B', 'scripts/pipeline.py', 'recomp'], source)
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

    def activate(self, candidate):
        app, data = self.installation()
        source = candidate / 'source'
        complete = json.loads((candidate / 'build-complete.json').read_text())
        if sha(source / 'bin/defjam_recomp.exe') != complete['exe_sha256']:
            raise ValueError('Candidate executable checksum mismatch')
        manifest = json.loads((candidate / 'manifest.json').read_text())
        old = json.loads((app / 'installed.json').read_text())
        new = dict(old, source_commit=manifest['source_commit'], toolkit_commit=manifest['toolkit_commit'],
                   source=self.windows_path(source), data=self.windows_path(data),
                   executable=self.windows_path(source / 'bin/defjam_recomp.exe'))
        backup = candidate / 'previous-install'
        backup.mkdir(exist_ok=True)
        for name in ('installed.ini', 'installed.json'):
            if (backup / name).exists():
                raise ValueError('This candidate was already activated; keeping its rollback receipts')
            shutil.copyfile(app / name, backup / name)
        # installed.ini is the launcher's actual switch; publish it last.
        content = '[install]\r\n' + ''.join(f'{k}={new[k]}\r\n' for k in ('source', 'data', 'executable'))
        try:
            atomic(app / 'installed.json', json.dumps(new, indent=2))
            atomic(app / 'installed.ini', b'\xff\xfe' + content.encode('utf-16-le'))
        except BaseException:
            for name in ('installed.json', 'installed.ini'):
                atomic(app / name, (backup / name).read_bytes())
            raise
        self.event('Update installed. Restart from the launcher to play.', 5)
        if self.status:
            atomic(str(self.status) + '.progress', '1000\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--setup', help='already downloaded DefJamSetup-*.exe')
    parser.add_argument('--install-dir', help='FFNY Online "app" directory (contains installed.json)')
    parser.add_argument('--data-dir', help='FFNY Online data directory')
    parser.add_argument('--prefix', help='Wine prefix used by the game')
    parser.add_argument('--toolchain', help='llvm-mingw root (bin/x86_64-w64-mingw32-gcc)')
    parser.add_argument('--log')
    parser.add_argument('--status-file')
    parser.add_argument('--done-file')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--build-only', action='store_true')
    parser.add_argument('--resume-build', type=Path)
    parser.add_argument('--activate', type=Path)
    args = parser.parse_args()
    update = Update(args)
    code = 4
    try:
        with (TOOLS / 'wine-update.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('Another update is already running') from None
            if args.resume_build:
                update.build(args.resume_build.resolve())
            elif args.activate:
                update.activate(args.activate.resolve())
            else:
                update.execute()
        code = 0
    except Exception as error:
        update.event('Setup stopped: ' + str(error))
    finally:
        if args.done_file:
            atomic(update.local_path(args.done_file), str(code) + '\n')
    return code


if __name__ == '__main__':
    sys.exit(main())
