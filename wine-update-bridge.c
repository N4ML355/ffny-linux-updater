/* Local Wine adapter: keep the Windows launcher's progress/exit-code contract
 * while the Linux host builds the Windows game. No game code is included.
 *
 * Setup.sh compiles this file with FFNY_TOOLS_DIR and FFNY_PREFIX pointing to
 * this machine's Unix paths; FFNY_TOOLS_DIR / FFNY_PREFIX / WINEPREFIX
 * environment variables override them at runtime. The game launches this bridge as
 * <install>\..\..\ffny-tools\wine-update-bridge.exe (installed by setup.sh).
 */
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <windows.h>
#include <shellapi.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>

#ifndef FFNY_TOOLS_DIR
#define FFNY_TOOLS_DIR "/usr/local/share/ffny-tools"
#endif
#ifndef FFNY_PREFIX
#define FFNY_PREFIX ""
#endif

static int append(wchar_t *out, size_t cap, const wchar_t *arg) {
    size_t used = wcslen(out), slashes = 0;
    if (used + 4 >= cap) return 0;
    if (used) out[used++] = L' ';
    out[used++] = L'"';
    for (; *arg; ++arg) {
        if (*arg == L'\\') { ++slashes; continue; }
        size_t n = slashes * (*arg == L'"' ? 2 : 1) + (*arg == L'"');
        if (used + n + 3 >= cap) return 0;
        while (n--) out[used++] = L'\\';
        slashes = 0;
        out[used++] = *arg;
    }
    if (used + 2 * slashes + 2 >= cap) return 0;
    while (slashes--) { out[used++] = L'\\'; out[used++] = L'\\'; }
    out[used++] = L'"'; out[used] = 0;
    return 1;
}

static int utf8_to_wide(const char *text, wchar_t *out, int count) {
    out[0] = 0;
    if (!text || !*text) return 0;
    if (!MultiByteToWideChar(CP_UTF8, 0, text, -1, out, count)) { out[0] = 0; return 0; }
    return 1;
}

static const char *env_or(const char *name, const char *fallback) {
    static char value[4096];
    DWORD size = GetEnvironmentVariableA(name, value, sizeof(value));
    return (size && size < sizeof(value)) ? value : fallback;
}

/* Testing aid: with FFNY_BRIDGE_ECHO=<file>, write the spawn command line and
 * succeed without running the adapter. */
static void echo_command(const char *file, const wchar_t *command) {
    HANDLE handle = CreateFileA(file, FILE_APPEND_DATA, FILE_SHARE_READ, NULL,
                                OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (handle == INVALID_HANDLE_VALUE) return;
    int size = WideCharToMultiByte(CP_UTF8, 0, command, -1, NULL, 0, NULL, NULL);
    char *text = size > 0 ? malloc((size_t)size) : NULL;
    if (text) {
        DWORD written;
        WideCharToMultiByte(CP_UTF8, 0, command, -1, text, size, NULL, NULL);
        SetFilePointer(handle, 0, NULL, FILE_END);
        WriteFile(handle, text, (DWORD)strlen(text), &written, NULL);
        WriteFile(handle, "\n", 1, &written, NULL);
        free(text);
    }
    CloseHandle(handle);
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR ignored, int show) {
    (void)instance; (void)previous; (void)ignored; (void)show;
    int argc;
    wchar_t **argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (!argv) return 4;
    if (argc == 4 && !wcscmp(argv[1], L"--restart")) {
        HANDLE old = OpenProcess(SYNCHRONIZE, FALSE, wcstoul(argv[2], NULL, 10));
        if (old) { WaitForSingleObject(old, INFINITE); CloseHandle(old); }
        Sleep(2000);
        wchar_t exe[2048], cmd[4096] = L"";
        swprintf(exe, 2048, L"%ls\\DefJamLauncher.exe", argv[3]);
        if (!append(cmd, 4096, exe)) return 4;
        STARTUPINFOW si = {sizeof(si)}; PROCESS_INFORMATION pi;
        if (!CreateProcessW(exe, cmd, NULL, NULL, FALSE, CREATE_NO_WINDOW,
                            NULL, argv[3], &si, &pi)) return 4;
        CloseHandle(pi.hThread); CloseHandle(pi.hProcess);
        LocalFree(argv); return 0;
    }
    const wchar_t *log = NULL;
    for (int i = 1; i + 1 < argc; ++i)
        if (!wcscmp(argv[i], L"--log")) log = argv[i + 1];
    if (!log) { LocalFree(argv); return 2; }
    wchar_t tools[4096], prefix[4096], python[8192], script[8192];
    wchar_t done[2048], cmd[16384] = L"";
    utf8_to_wide(env_or("FFNY_TOOLS_DIR", FFNY_TOOLS_DIR), tools, 4096);
    /* The prefix the game actually runs in wins over the compile-time default:
     * a fresh install can use any Wine prefix the player starts the setup in. */
    const char *wineprefix = env_or("WINEPREFIX", "");
    const char *configured = env_or("FFNY_PREFIX", FFNY_PREFIX);
    utf8_to_wide(*wineprefix == '/' ? wineprefix : configured, prefix, 4096);
    if (!tools[0]) { LocalFree(argv); return 4; }
    swprintf(python, 8192, L"Z:%ls\\venv\\bin\\python", tools);
    for (wchar_t *p = python; *p; ++p) if (*p == L'/') *p = L'\\';
    if (!append(cmd, 16384, python)) { LocalFree(argv); return 4; }
    swprintf(script, 8192, L"%ls/wine-update.py", tools);
    if (!append(cmd, 16384, script)) { LocalFree(argv); return 4; }
    for (int i = 1; i < argc; ++i) if (!append(cmd, 16384, argv[i])) { LocalFree(argv); return 4; }
    if (prefix[0] && (!append(cmd, 16384, L"--prefix") || !append(cmd, 16384, prefix))) {
        LocalFree(argv); return 4;
    }
    swprintf(done, 2048, L"%ls.bridge-%lu-%llu.done", log, GetCurrentProcessId(), GetTickCount64());
    if (!append(cmd, 16384, L"--done-file") || !append(cmd, 16384, done)) { LocalFree(argv); return 4; }
    const char *echo = env_or("FFNY_BRIDGE_ECHO", "");
    if (*echo) {
        FILE *file = _wfopen(done, L"w");
        if (file) { fprintf(file, "0\n"); fclose(file); }
        echo_command(echo, cmd);
        LocalFree(argv);
        return 0;
    }
    STARTUPINFOW si = {sizeof(si)}; PROCESS_INFORMATION pi = {0};
    if (!CreateProcessW(NULL, cmd, NULL, NULL, FALSE, CREATE_NO_WINDOW, NULL, NULL, &si, &pi)) {
        FILE *f = _wfopen(log, L"a");
        if (f) { fprintf(f, "Setup stopped: Could not start the Linux update adapter (error %lu)\n", GetLastError()); fclose(f); }
        LocalFree(argv); return 4;
    }
    /* Wine starts ELF processes without Windows process handles. The host
     * publishes a per-invocation completion file; never treat spawn as success. */
    if (pi.hThread) CloseHandle(pi.hThread);
    if (pi.hProcess) CloseHandle(pi.hProcess);
    ULONGLONG start = GetTickCount64();
    int result = 4;
    while (GetTickCount64() - start < 3ull * 60 * 60 * 1000) {
        FILE *f = _wfopen(done, L"r");
        if (f) { if (fscanf(f, "%d", &result) != 1) result = 4; fclose(f); DeleteFileW(done); break; }
        Sleep(500);
    }
    LocalFree(argv);
    return result;
}
