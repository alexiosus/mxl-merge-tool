/* Launcher for the MXL merge tool.
 *
 * Finds the runtime next to itself and runs the Python entry point. Nothing is
 * packed or unpacked: the file is a plain native executable, so antivirus
 * heuristics have nothing to react to.
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <wchar.h>

#define COMMAND_MAX 32768

static int strip_file_name(wchar_t *path)
{
    wchar_t *slash = wcsrchr(path, L'\\');
    if (slash == NULL) {
        return 0;
    }
    *slash = L'\0';
    return 1;
}

static int report_path_too_long(void)
{
    MessageBoxW(NULL,
                L"Путь к программе слишком длинный.\n"
                L"Установите программу ближе к корню диска.",
                L"MXL merge tool", MB_ICONERROR | MB_OK);
    return 1;
}

static int report_runtime_missing(void)
{
    MessageBoxW(NULL,
                L"Не удалось запустить среду выполнения.\n"
                L"Проверьте, что рядом с программой есть папка runtime.",
                L"MXL merge tool", MB_ICONERROR | MB_OK);
    return 1;
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR arguments, int show)
{
    wchar_t root[MAX_PATH];
    wchar_t python[MAX_PATH];
    wchar_t script[MAX_PATH];
    wchar_t command[COMMAND_MAX];
    STARTUPINFOW startup;
    PROCESS_INFORMATION process;
    DWORD code = 0;
    DWORD root_len;

    (void)instance;
    (void)previous;
    (void)show;

    /* GetModuleFileNameW returns 0 on outright failure, and returns exactly
     * the buffer size (with the result silently truncated, not
     * NUL-terminated) when the path did not fit. Both are the same failure
     * mode for us: we cannot trust root. */
    root_len = GetModuleFileNameW(NULL, root, MAX_PATH);
    if (root_len == 0 || root_len >= MAX_PATH) {
        return report_path_too_long();
    }

    if (!strip_file_name(root)) {
        /* GetModuleFileNameW always returns an absolute path, so this
         * should be unreachable; treat it as the same failure rather than
         * silently continuing with a bogus root. */
        return report_path_too_long();
    }

    /* _snwprintf, unlike C99 snprintf, does NOT NUL-terminate the buffer on
     * truncation -- it returns a negative value and leaves the array full.
     * Check every call and force-terminate every buffer afterwards so a
     * truncated result can never be read past its end, either here or by a
     * later change. */
    if (_snwprintf(python, MAX_PATH, L"%s\\runtime\\pythonw.exe", root) < 0) {
        python[MAX_PATH - 1] = L'\0';
        return report_path_too_long();
    }
    python[MAX_PATH - 1] = L'\0';

    if (_snwprintf(script, MAX_PATH, L"%s\\app\\mxl_tool.py", root) < 0) {
        script[MAX_PATH - 1] = L'\0';
        return report_path_too_long();
    }
    script[MAX_PATH - 1] = L'\0';

    if (arguments == NULL || arguments[0] == L'\0') {
        if (_snwprintf(command, COMMAND_MAX, L"\"%s\" \"%s\" setup-gui", python, script) < 0) {
            command[COMMAND_MAX - 1] = L'\0';
            return report_path_too_long();
        }
    } else {
        if (_snwprintf(command, COMMAND_MAX, L"\"%s\" \"%s\" %s", python, script, arguments) < 0) {
            command[COMMAND_MAX - 1] = L'\0';
            return report_path_too_long();
        }
    }
    command[COMMAND_MAX - 1] = L'\0';

    ZeroMemory(&startup, sizeof(startup));
    startup.cb = sizeof(startup);
    ZeroMemory(&process, sizeof(process));

    if (!CreateProcessW(python, command, NULL, NULL, FALSE, 0, NULL, root,
                        &startup, &process)) {
        return report_runtime_missing();
    }

    WaitForSingleObject(process.hProcess, INFINITE);
    GetExitCodeProcess(process.hProcess, &code);
    CloseHandle(process.hProcess);
    CloseHandle(process.hThread);
    return (int)code;
}
