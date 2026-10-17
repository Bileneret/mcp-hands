"""Патч №4: устойчивость тестов к среде и требования к COM-исполнителю в плане."""

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


# 1) conftest: пропуск тестов, которым нужен захват экрана
patch("tests/conftest.py", [
    ('''@pytest.fixture
def target():''',
     '''@pytest.fixture(scope="session")
def capture_available() -> bool:
    """Доступен ли захват экрана в этой сессии.

    В ограниченных сессиях (служба, заблокированный рабочий стол) отказывают и DXGI,
    и BitBlt — тесты, которым нужен кадр, должны пропускаться с внятной причиной,
    а не падать, создавая ложную картину поломки кода.
    """
    from mcp_hands.screen import ScreenCaptureError, ScreenGrabber

    try:
        ScreenGrabber().grab(timeout_s=0.2)
        return True
    except ScreenCaptureError:
        return False


@pytest.fixture
def require_capture(capture_available: bool) -> None:
    if not capture_available:
        pytest.skip("захват экрана недоступен в этой сессии: DXGI и BitBlt отказали")


@pytest.fixture
def target():'''),
], "пропуск тестов при недоступном захвате")

# 2) живые тесты захвата и OCR требуют кадра
patch("tests/test_screen.py", [
    ('''@pytest.fixture(scope="module")
def grabber():
    return ScreenGrabber()''',
     '''@pytest.fixture(scope="module")
def grabber():
    return ScreenGrabber()


@pytest.fixture(autouse=True)
def _needs_capture(require_capture):
    """Без рабочего захвата эти проверки бессмысленны — пропускаем с причиной."""'''),
], "пропуск при недоступном захвате")

patch("tests/test_ocr.py", [
    ('''pytestmark = pytest.mark.skipif(not available(), reason="в системе нет движка Windows OCR")''',
     '''pytestmark = pytest.mark.skipif(not available(), reason="в системе нет движка Windows OCR")


@pytest.fixture(autouse=True)
def _needs_capture(require_capture):
    """OCR проверяется на живом кропе окна, значит нужен рабочий захват."""'''),
], "пропуск при недоступном захвате")

# 3) ROADMAP: требования к COM-исполнителю из раздела 2 отчёта аудита
patch("ROADMAP.md", [
    ('''- [ ] Офисный COM-исполнитель: рабочий процесс + таймаут + `kill`, детект модальных
      диалогов через `SetWinEventHook`; обработка `Protected View`,
      `RPC_E_SERVERCALL_RETRYLATER`, реализация `IMessageFilter`''',
     '''- [ ] Офисный COM-исполнитель: рабочий процесс + таймаут + `kill`, детект модальных
      диалогов через `SetWinEventHook`; обработка `Protected View`,
      `RPC_E_SERVERCALL_RETRYLATER`, реализация `IMessageFilter`.
      Требования из аудита (`reports/09_STAGE1_AUDIT.md`, раздел 2), обязательные к реализации:
      `word.DisplayAlerts = 0` сразу после запуска; `SetErrorMode(SEM_FAILCRITICALERRORS |
      SEM_NOGPFAULTERRORBOX)` перед открытием файлов; подписка `EventPump` на
      `EVENT_OBJECT_SHOW` с фильтром по pid Word (модалка приходит за 50–150 мс — ждать
      весь таймаут шага нельзя); retry-цикл (3 попытки, 100–300 мс) на `0x8001010A`
      и `RPC_E_CALL_REJECTED`; регистрация pid Word в `ProbeProcess.track_process`
      (механизм уже реализован и покрыт тестом)'''),
], "требования к COM-исполнителю")
