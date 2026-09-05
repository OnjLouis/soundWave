#include <windows.h>
#include <wchar.h>

typedef int (__cdecl *PyMainFunction)(int, wchar_t **);

int wmain(int argc, wchar_t **argv) {
    wchar_t pythonDll[MAX_PATH];
    wchar_t pythonPattern[MAX_PATH];
    wchar_t **pythonArgv;
    WIN32_FIND_DATAW findData;
    HANDLE search;
    HMODULE python;
    PyMainFunction pyMain;
    int result;
    int index;

    if (argc < 4) {
        return 2;
    }
    if (swprintf(pythonPattern, MAX_PATH, L"%ls\\python3??.dll", argv[1]) < 0) {
        return 3;
    }
    search = FindFirstFileW(pythonPattern, &findData);
    if (search == INVALID_HANDLE_VALUE) {
        return 4;
    }
    FindClose(search);
    if (swprintf(pythonDll, MAX_PATH, L"%ls\\%ls", argv[1], findData.cFileName) < 0) {
        return 3;
    }
    SetDllDirectoryW(argv[1]);
    python = LoadLibraryW(pythonDll);
    if (python == NULL) {
        return 5;
    }
    pyMain = (PyMainFunction)GetProcAddress(python, "Py_Main");
    if (pyMain == NULL) {
        FreeLibrary(python);
        return 6;
    }

    pythonArgv = (wchar_t **)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(wchar_t *) * (argc - 1));
    if (pythonArgv == NULL) {
        FreeLibrary(python);
        return 7;
    }
    pythonArgv[0] = argv[0];
    for (index = 2; index < argc; ++index) {
        pythonArgv[index - 1] = argv[index];
    }
    result = pyMain(argc - 1, pythonArgv);
    HeapFree(GetProcessHeap(), 0, pythonArgv);
    FreeLibrary(python);
    return result;
}
