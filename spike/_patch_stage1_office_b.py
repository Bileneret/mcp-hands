"""Патч: определение PID Word без опоры на необязательное свойство Hwnd."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def patch(relative: str, replacements: list[tuple[str, str]], note: str) -> None:
    path = ROOT / relative
    text = path.read_text(encoding="utf-8")
    for old, new in replacements:
        if old not in text:
            raise SystemExit(f"не найден фрагмент в {relative}: {old[:70]!r}")
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")
    print(f"{relative}: {note}")


patch("mcp_hands/probe_worker.py", [
    ('''    def op_word_start(self, visible: bool = False) -> dict:
        """Поднимает Word: без предупреждений, невидимым, и сообщает свой pid."""
        import win32com.client

        self._prepare_com()
        if self._word is not None:
            return {"pid": self._word_pid(), "reused": True}
        application = win32com.client.DispatchEx("Word.Application")''',
     '''    @staticmethod
    def _winword_pids() -> set:
        """Список процессов WINWORD.EXE.

        `Application.Hwnd` при позднем связывании может быть недоступно, поэтому pid
        определяется сравнением списка процессов до и после запуска: `DispatchEx`
        создаёт НОВЫЙ экземпляр Word, и разница — это он.
        """
        import ctypes
        import ctypes.wintypes as wintypes

        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi.EnumProcesses.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
                                        ctypes.POINTER(wintypes.DWORD)]
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                        wintypes.LPWSTR,
                                                        ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        capacity = 2048
        buffer = (wintypes.DWORD * capacity)()
        needed = wintypes.DWORD(0)
        if not psapi.EnumProcesses(buffer, ctypes.sizeof(buffer), ctypes.byref(needed)):
            return set()
        found = set()
        for index in range(needed.value // ctypes.sizeof(wintypes.DWORD)):
            pid = int(buffer[index])
            if not pid:
                continue
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                continue
            try:
                name = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(len(name))
                if kernel32.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
                    if name.value.rsplit("\\\\", 1)[-1].upper() == "WINWORD.EXE":
                        found.add(pid)
            finally:
                kernel32.CloseHandle(handle)
        return found

    def op_word_start(self, visible: bool = False) -> dict:
        """Поднимает Word: без предупреждений, невидимым, и сообщает свой pid."""
        import win32com.client

        self._prepare_com()
        if self._word is not None:
            return {"pid": self._word_pid(), "reused": True}
        before = self._winword_pids()
        application = win32com.client.DispatchEx("Word.Application")'''),
    ('''        self._word = application
        self._word_version = str(application.Version)
        pid = self._word_pid()
        registered = self.op_register_process(pid=pid, image="WINWORD.EXE",
                                              note=f"Word {self._word_version} через COM")
        return {"pid": pid, "version": self._word_version, "registry": registered}''',
     '''        self._word = application
        self._word_version = str(application.Version)
        new_pids = sorted(self._winword_pids() - before)
        self._word_process_id = new_pids[0] if len(new_pids) == 1 else self._word_pid()
        registered = self.op_register_process(pid=self._word_pid(), image="WINWORD.EXE",
                                              note=f"Word {self._word_version} через COM")
        return {"pid": self._word_pid(), "version": self._word_version,
                "registry": registered, "new_processes": new_pids}'''),
    ('''    def _word_pid(self) -> int:
        """PID процесса Word: получаем из его же окна — так его видит и супервизор."""
        import ctypes
        import ctypes.wintypes as wintypes

        hwnd = int(self._word.Hwnd or 0)
        if not hwnd:
            return 0
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        return int(pid.value)''',
     '''    def _word_pid(self) -> int:
        """PID нашего экземпляра Word, определённый при запуске."""
        return int(getattr(self, "_word_process_id", 0) or 0)'''),
    ('''        self._word = None
        self._word_version = ""''',
     '''        self._word = None
        self._word_version = ""
        self._word_process_id = 0'''),
], "PID Word через разницу списка процессов")
