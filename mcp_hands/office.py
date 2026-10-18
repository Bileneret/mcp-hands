"""Офисный COM-исполнитель: Word только через процесс-сателлит.

Почему так, а не «просто вызвать COM»:

  * вызовы COM к Office **синхронные**. Модальный диалог («файл заблокирован»,
    «восстановить документ?», активация лицензии) блокирует вызвавший поток намертво,
    и снять блокировку изнутри нельзя. Единственный способ — выполнять их в отдельном
    процессе и убивать процесс по бюджету;
  * Word поднимает служба DCOM, то есть он **не ребёнок** воркера: после убийства воркера
    он остаётся висеть и держать файл. Поэтому pid регистрируется в файле-реестре сателлита
    сразу после запуска — иначе, зависнув на блокирующем вызове, воркер не успеет сообщить
    pid родителю;
  * Word регулярно занят фоновой перерисовкой и проверкой правописания: вызов в такой
    момент возвращает `RPC_E_SERVERCALL_RETRYLATER` (0x8001010A) или `RPC_E_CALL_REJECTED`
    (0x80010001). Это не ошибка, а приглашение повторить.

Требования, зафиксированные аудитами, собраны здесь в одном месте: `DisplayAlerts = 0`,
`SetErrorMode`, детект модальных окон по pid, повтор на занятость сервера, регистрация
и снятие pid, отдельный бюджет для COM-операций.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from pathlib import Path

from .probe import (DOMAIN_OPERATION_TIMEOUT_S, ProbeAborted, ProbeError, ProbeProcess,
                    ProbeTimeout)


def _absolute(path: str) -> str:
    """Приводит путь к абсолютному: относительный Word понимает по-своему.

    Word сохраняет и ищет файлы относительно своей «папки документов», а не рабочего
    каталога вызывающего процесса: без приведения пути файл окажется не там, где ждут,
    и проверка постусловия не найдёт результат.
    """
    return str(Path(path).expanduser().resolve())

WORD_IMAGE = "WINWORD.EXE"
WD_FORMAT_PDF = 17

#: Бюджеты COM-операций подобраны по замерам на стенде и различаются по цене:
#: спросить состояние — доли секунды, поднять или закрыть Word — секунды,
#: открыть документ и напечатать PDF — десятки секунд. Единый бюджет здесь вреден:
#: слишком большой держит зависший вызов, слишком малый срывает долгую печать.
OFFICE_TIMEOUTS = {
    "word_start": 30.0,
    "word_quit": 30.0,
    "word_status": 5.0,
    "word_new_document": 30.0,
    "word_open": 45.0,
    "word_replace": 45.0,
    "word_save_as": 60.0,
    "word_format": 60.0,
}
DEFAULT_OFFICE_TIMEOUT_S = 45.0


def timeout_for(op: str) -> float:
    return OFFICE_TIMEOUTS.get(op, DEFAULT_OFFICE_TIMEOUT_S)

#: Коды «сервер занят»: Word сам просит повторить вызов.
RPC_E_CALL_REJECTED = 0x80010001
RPC_E_SERVERCALL_RETRYLATER = 0x8001010A
BUSY_CODES = (RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER)

#: Классы окон, которыми Office показывает модальные диалоги и защищённый просмотр.
MODAL_DIALOG_CLASSES = ("#32770", "bosa_sdm_msword", "NUIDialog", "MsoCommandBarPopup")


@dataclass
class OfficeOutcome:
    """Результат COM-операции вместе с признаками, которые важны сценарию."""

    ok: bool
    value: object = None
    reason: str = ""
    attempts: int = 1
    ms: float = 0.0
    modal_dialog: str = ""
    details: dict = field(default_factory=dict)

    def describe(self) -> dict:
        return {"ok": self.ok, "reason": self.reason, "attempts": self.attempts,
                "ms": round(self.ms, 1), "modal_dialog": self.modal_dialog, **self.details}


class WordSession:
    """Клиентская сторона Word: операции идут в сателлите, состояние — там же."""

    def __init__(self, probe: ProbeProcess, modal_watcher=None) -> None:
        self.probe = probe
        self.pid = 0
        self.modal_watcher = modal_watcher
        self.history: list[dict] = []

    # --- запуск и завершение ---------------------------------------------

    def start(self, visible: bool = False) -> OfficeOutcome:
        """Поднимает Word и берёт его под опеку супервизора ДО первой блокирующей операции."""
        outcome = self._call("word_start", visible=visible)
        if outcome.ok and isinstance(outcome.value, dict):
            self.pid = int(outcome.value.get("pid") or 0)
            if self.pid:
                self.probe.track_process(self.pid, note="Word через COM", image=WORD_IMAGE)
                # Регистрация в файле-реестре: если воркер зависнет внутри COM,
                # родитель узнает pid из файла, а не из ответа.
                with_errors = self.probe.call("register_process", pid=self.pid,
                                              image=WORD_IMAGE, note="Word через COM",
                                              timeout_s=DOMAIN_OPERATION_TIMEOUT_S)
                outcome.details["registry"] = with_errors
        return outcome

    def quit(self, save: bool = False) -> OfficeOutcome:
        """Закрывает Word и снимает его с учёта.

        Если штатное закрытие не удалось (Word занят, отвечает ошибкой), процесс
        добивается принудительно: он уже под опекой, и «не закрылся» не должно означать
        «остался висеть». Результат проверяется фактом — исчез ли процесс.
        """
        outcome = self._call("word_quit", save=save)
        pid = self.pid
        if pid:
            # `Quit` возвращает управление раньше, чем процесс действительно выходит:
            # сразу после вызова он ещё жив. Поэтому здесь ожидание, а не разовая проверка.
            gone = self._wait_process_gone(pid, timeout_s=15.0)
            if not gone:
                killed = self.probe.kill_tracked()
                outcome.details["forced_kill"] = killed
                outcome.reason = (outcome.reason or "закрытие не подтвердилось") + \
                    f"; Word снят принудительно ({killed})"
                gone = self._wait_process_gone(pid, timeout_s=5.0)
            outcome.details["process_gone"] = gone
            if not gone:
                outcome.ok = False
                outcome.reason = f"Word с pid {pid} продолжает работать после закрытия"
            self.probe.untrack_process(pid)
            try:
                self.probe.call("unregister_process", pid=pid,
                                timeout_s=DOMAIN_OPERATION_TIMEOUT_S)
            except (ProbeError, ProbeTimeout):
                pass  # сателлит мог быть снят; запись всё равно уберётся при его перезапуске
            self.pid = 0
        return outcome

    # --- работа с документами --------------------------------------------

    def new_document(self, text: str = "") -> OfficeOutcome:
        return self._call("word_new_document", text=text)

    def open(self, path: str) -> OfficeOutcome:
        """Открывает документ. Самый опасный вызов: здесь чаще всего всплывают модалки."""
        return self._call("word_open", path=_absolute(path))

    def replace(self, mapping: dict, match_case: bool = False) -> OfficeOutcome:
        """Заменяет плейсхолдеры во всём документе."""
        return self._call("word_replace", mapping=dict(mapping), match_case=match_case)

    def format_document(self, actions: list) -> OfficeOutcome:
        """Форматирование: стили/шрифты/интервалы/оглавление; каждое подтверждается
        перечитыванием свойства в сателлите."""
        return self._call("word_format", actions=list(actions))

    def save_as(self, path: str, as_pdf: bool = False) -> OfficeOutcome:
        return self._call("word_save_as", path=_absolute(path), as_pdf=as_pdf)

    def export_pdf(self, path: str) -> OfficeOutcome:
        """Экспорт в PDF. Word на этой операции часто отвечает «сервер занят» — повтор встроен."""
        return self._call("word_save_as", path=_absolute(path), as_pdf=True)

    def status(self) -> OfficeOutcome:
        return self._call("word_status")

    # --- механика вызова --------------------------------------------------

    def _call(self, op: str, timeout_s: float | None = None, **kwargs) -> OfficeOutcome:
        """Вызов COM-операции с повторами на занятость сервера и ЖИВЫМ сторожем модалок.

        Сторож опрашивается, пока операция выполняется: `ProbeProcess.call` умеет звать
        наблюдателя между короткими ожиданиями. Иначе модальное окно Word обнаружилось бы
        только по истечении бюджета, да ещё и без имени диалога.
        """
        budget = timeout_s if timeout_s is not None else timeout_for(op)
        started = time.perf_counter()
        attempts = 0
        last_reason = ""
        while attempts < 3:
            attempts += 1
            guard = None
            if self.modal_watcher is not None:
                guard = lambda: self._modal_reason()  # noqa: E731 — короткая обёртка для колбэка
            try:
                value = self.probe.call(op, timeout_s=budget, on_tick=guard, **kwargs)
                outcome = OfficeOutcome(True, value=value, attempts=attempts,
                                        ms=(time.perf_counter() - started) * 1000.0)
                self._remember(op, outcome)
                return outcome
            except ProbeAborted as exc:
                outcome = OfficeOutcome(False, reason=str(exc), attempts=attempts,
                                        modal_dialog=self._modal_reason(),
                                        ms=(time.perf_counter() - started) * 1000.0)
                outcome.details["budget_s"] = budget
                self._remember(op, outcome)
                return outcome
            except ProbeTimeout as exc:
                outcome = OfficeOutcome(False, reason=self._timeout_reason(exc), attempts=attempts,
                                        ms=(time.perf_counter() - started) * 1000.0)
                # Модальное окно могло появиться в последний момент: опрашиваем сторожа
                # и в этой ветке, иначе причина отказа останется необъяснённой.
                outcome.modal_dialog = self._modal_reason()
                if outcome.modal_dialog:
                    outcome.reason = (f"модальный диалог Word ({outcome.modal_dialog}): "
                                      f"нужен человек")
                outcome.details["budget_s"] = budget
                self._remember(op, outcome)
                return outcome
            except ProbeError as exc:
                last_reason = str(exc)
                if self._is_busy(last_reason) and attempts < 3:
                    time.sleep(0.1 * attempts)  # Word сам просит повторить
                    continue
                outcome = OfficeOutcome(False, reason=last_reason, attempts=attempts,
                                        ms=(time.perf_counter() - started) * 1000.0)
                outcome.modal_dialog = self._modal_reason()
                if outcome.modal_dialog:
                    outcome.reason = (f"модальный диалог Word ({outcome.modal_dialog}): "
                                      f"нужен человек")
                self._remember(op, outcome)
                return outcome
        return OfficeOutcome(False, reason=last_reason or "операция не удалась", attempts=attempts)

    def _wait_process_gone(self, pid: int, timeout_s: float) -> bool:
        """Ждёт фактического завершения процесса: закрытие COM опережает выход Word."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if not self.probe.process_identity(pid)["image"]:
                return True
            time.sleep(0.25)
        return not self.probe.process_identity(pid)["image"]

    def _modal_reason(self) -> str:
        """Причина остановки, если Word показывает модальное окно, иначе пусто."""
        if self.modal_watcher is None:
            return ""
        modal = self.modal_watcher.recent(word_pid=self.pid)
        return f"модальный диалог Word ({modal}): нужен человек" if modal else ""

    @staticmethod
    def _is_busy(reason: str) -> bool:
        lowered = reason.lower()
        return any(hex(code).lower() in lowered or str(code) in reason for code in BUSY_CODES) \
            or "retrylater" in lowered or "call_rejected" in lowered

    @staticmethod
    def _timeout_reason(exc: Exception) -> str:
        return (f"COM-операция не ответила за отведённое время: сателлит снят "
                f"({exc}). Вероятная причина — модальный диалог Word; нужен человек")

    def _remember(self, op: str, outcome: OfficeOutcome) -> None:
        self.history.append({"op": op, **outcome.describe()})
        del self.history[:-20]


class ModalWatcher:
    """Следит за появлением модальных окон процесса Word.

    Модальное окно Office появляется за 50–150 мс. Ждать полный бюджет шага (десятки
    секунд) нельзя: человек должен узнать о препятствии сразу, а сценарий — уйти
    в режим «нужен человек», а не «повисло».
    """

    def __init__(self, pump) -> None:
        self.pump = pump
        self.seen: list[dict] = []

    def _filter(self, event, word_pid: int) -> bool:
        if word_pid and event.process_id != word_pid:
            return False
        return event.class_name in MODAL_DIALOG_CLASSES or event.title.strip().endswith("?")

    def check(self, word_pid: int = 0) -> dict | None:
        for event in self.pump.drain():
            if self._filter(event, word_pid):
                self.seen.append(event.describe())
        return self.seen[-1] if self.seen else None

    def recent(self, word_pid: int = 0) -> str:
        self.check(word_pid)
        if not self.seen:
            return ""
        last = self.seen[-1]
        return f"{last['class_name']}: {last['title']}"
