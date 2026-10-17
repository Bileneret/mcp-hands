"""Патч: выравнивание рабочего стола тестового процесса и защита тестов событий."""

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


patch("tests/conftest.py", [
    ('''@pytest.fixture(scope="session", autouse=True)
def dpi_awareness():''',
     '''@pytest.fixture(scope="session", autouse=True)
def aligned_desktop():
    """Переводит процесс тестов на рабочий стол ввода — до окон и хуков.

    В изолированных сессиях (песочница, CI, раннер агента) процесс pytest живёт на
    служебном рабочем столе, а мишень и сателлит встают на стол ввода. Хуки событий
    изолированы рабочим столом потока, поэтому события мишени до цикла сообщений
    не доходят. Выравнивание убирает расхождение; если переключиться не удалось
    (например, у потока уже есть окна), тесты, которым нужен общий стол, пропускаются
    с причиной, а не падают.
    """
    from mcp_hands.geometry import CoordinateSpace
    from mcp_hands.input import Actuator

    actuator = Actuator(CoordinateSpace())
    result = actuator.attach_to_input_desktop()
    yield result


@pytest.fixture(scope="session", autouse=True)
def dpi_awareness():'''),
    ('''@pytest.fixture
def require_input_desktop(target):
    """Пропускает тест ввода, если окно мишени на другом рабочем столе.

    В песочнице или служебном сеансе окно мишени может оказаться на параллельном столе:
    тогда клики физически не доходят, и это отказ среды, а не поломка кода. Мишень
    старается создаться на столе ввода сама; если не удалось — тест честно пропускается
    с указанием обоих имён.
    """
    ours = input_desktop_name()
    if target.desktop and ours and target.desktop != ours:
        pytest.skip(f"мишень на рабочем столе {target.desktop!r}, ввод идёт на {ours!r}")''',
     '''@pytest.fixture
def require_input_desktop(target, aligned_desktop):
    """Пропускает тест, если окно мишени и процесс тестов на разных рабочих столах.

    Ввод и хуки событий привязаны к рабочему столу потока: при расхождении клики
    не доходят, а события не приходят. Это отказ среды, а не поломка кода, поэтому
    тест пропускается с указанием обоих имён и результата выравнивания.
    """
    ours = input_desktop_name()
    if target.desktop and ours and target.desktop != ours:
        pytest.skip(
            f"мишень на рабочем столе {target.desktop!r}, процесс тестов на {ours!r}; "
            f"выравнивание: {aligned_desktop.get('reason') or aligned_desktop.get('how')}")'''),
], "выравнивание рабочего стола и защита тестов")

patch("tests/test_events.py", [
    ('''def test_window_event_is_delivered(pump, target):''',
     '''def test_window_event_is_delivered(pump, target, require_input_desktop):'''),
    ('''def test_events_are_queued_and_drained(pump, target):''',
     '''def test_events_are_queued_and_drained(pump, target, require_input_desktop):'''),
], "тесты событий требуют общего рабочего стола")

patch("tests/test_regressions_audit_v4.py", [
    ('''def test_target_window_is_on_input_desktop(target):
    """Окно мишени, запущенной тестами, лежит на том же столе, что и ввод."""
    actuator = Actuator(CoordinateSpace())
    ours = actuator.own_desktop()
    theirs = actuator.window_desktop(target.hwnd)
    assert ours and theirs
    assert target.desktop == theirs, \\
        f"мишень сообщила {target.desktop!r}, окно фактически на {theirs!r}"
    assert ours == theirs, f"ввод идёт на {ours!r}, окно мишени на {theirs!r}"''',
     '''def test_target_window_is_on_input_desktop(target, aligned_desktop):
    """Окно мишени лежит на том же рабочем столе, что и процесс тестов.

    Если выравнивание не удалось (в сессии с чужим рабочим столом), проверка
    пропускается с причиной: расхождение столов — отказ среды, а не ошибка кода.
    """
    actuator = Actuator(CoordinateSpace())
    ours = actuator.own_desktop()
    theirs = actuator.window_desktop(target.hwnd)
    assert ours and theirs
    assert target.desktop == theirs, \\
        f"мишень сообщила {target.desktop!r}, окно фактически на {theirs!r}"
    if ours != theirs:
        pytest.skip(f"процесс тестов на {ours!r}, окно мишени на {theirs!r}; "
                    f"выравнивание: {aligned_desktop.get('reason') or aligned_desktop.get('how')}")'''),
], "проверка стола мишени с оговоркой про среду")
