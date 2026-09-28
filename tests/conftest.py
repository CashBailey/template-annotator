"""Shared pytest plumbing for the areaDef test suite.

Import order matters: QT_QPA_PLATFORM is set before anything can pull in Qt.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

# Make `helpers` importable as a plain package from anywhere pytest is invoked.
_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from helpers.cli import CliRunner  # noqa: E402


def pytest_addoption(parser):
    parser.addoption(
        "--update-goldens",
        action="store_true",
        default=False,
        help="rewrite the committed golden images instead of comparing against them",
    )


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def repo_root() -> Path:
    return _TESTS_DIR.parent


@pytest.fixture(scope="session")
def areadef_script(repo_root: Path) -> Path:
    script = repo_root / "areaDef.py"
    assert script.is_file(), f"areaDef.py not found at {script}"
    return script


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return _TESTS_DIR / "fixtures"


@pytest.fixture(scope="session")
def goldens_dir() -> Path:
    """Platform-scoped golden images (rendering differs across Qt/OS builds)."""
    return _TESTS_DIR / "goldens" / "linux-py"


@pytest.fixture(scope="session")
def update_goldens(request) -> bool:
    return bool(request.config.getoption("--update-goldens")
                or os.environ.get("UPDATE_GOLDENS") == "1")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@pytest.fixture
def cli(areadef_script: Path, tmp_path: Path) -> CliRunner:
    """Run the CLI with tmp_path as cwd, so relative outputs land there."""
    return CliRunner(areadef_script, cwd=tmp_path)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
# pytest-qt's `qapp` fixture reuses QApplication.instance(); never build a second
# QApplication here.

@pytest.fixture
def annotator_window(qapp, qtbot, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    from areaDef import AnnotatorWindow

    def discard_before_close(win):
        # closeEvent() pops a MODAL "unsaved changes" prompt (areaDef.py:4604)
        # that would hang teardown forever under the offscreen platform. Answer
        # it with No (= discard) and clear the flag so the prompt never opens.
        # This must live in before_close_func: pytest-qt closes registered
        # widgets in pytest_runtest_teardown, i.e. BEFORE fixture finalizers.
        monkeypatch.setattr(
            QMessageBox, "question",
            staticmethod(lambda *a, **k: QMessageBox.StandardButton.No),
        )
        win._has_unsaved_changes = False

    win = AnnotatorWindow(theme="light")
    qtbot.addWidget(win, before_close_func=discard_before_close)
    return win


# ---------------------------------------------------------------------------
# Dialog stubs
# ---------------------------------------------------------------------------

def _caption(args, kwargs) -> str:
    """Qt dialog statics take (parent, caption, ...); caption may be a kwarg."""
    if "caption" in kwargs:
        return str(kwargs["caption"])
    return str(args[1]) if len(args) > 1 else ""


class FileDialogStub:
    """Scripted replacement for the QFileDialog save/open pickers.

    Covers both routes the app uses: the ``getOpenFileName`` static, and the
    constructed ``QFileDialog`` the save paths need (only an instance can call
    ``setDefaultSuffix``, which is what makes Qt run its overwrite check against
    the real, suffixed filename). ``getSaveFileName`` is stubbed too so a stray
    static call cannot open a real modal dialog.

    Fill ``open_paths`` / ``save_paths`` with the paths to hand back, in order;
    an exhausted queue returns "" (the user-cancelled result). Every call is
    recorded in ``calls`` as ``(kind, caption)``, and each save dialog's
    ``defaultSuffix()`` in ``save_suffixes``.

    Do not request this fixture in a test that needs a real dialog:
    ``QFileDialog.exec`` and ``selectedFiles`` are patched class-wide.
    """

    def __init__(self) -> None:
        self.open_paths: list[str] = []
        self.save_paths: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self.save_suffixes: list[str] = []

    def _pop(self, queue: list[str]) -> str:
        return str(queue.pop(0)) if queue else ""

    @property
    def open_calls(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if c[0] == "open"]

    @property
    def save_calls(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if c[0] == "save"]


@pytest.fixture
def no_file_dialogs(monkeypatch) -> FileDialogStub:
    from PyQt6.QtWidgets import QFileDialog

    stub = FileDialogStub()

    def get_open(*args, **kwargs):
        stub.calls.append(("open", _caption(args, kwargs)))
        return (stub._pop(stub.open_paths), "")

    def get_save(*args, **kwargs):
        stub.calls.append(("save", _caption(args, kwargs)))
        return (stub._pop(stub.save_paths), "")

    def dialog_exec(self, *args, **kwargs):
        """QFileDialog.exec() for the constructed save dialog."""
        stub.calls.append(("save", self.windowTitle()))
        stub.save_suffixes.append(self.defaultSuffix())
        path = stub._pop(stub.save_paths)
        self._stub_selection = [path] if path else []
        return 1 if path else 0      # QDialog.Accepted / Rejected

    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(get_open))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(get_save))
    monkeypatch.setattr(QFileDialog, "exec", dialog_exec)
    monkeypatch.setattr(QFileDialog, "selectedFiles",
                        lambda self: list(getattr(self, "_stub_selection", [])))
    return stub


class MessageBoxStub:
    """Scripted replacement for the QMessageBox statics.

    ``question`` returns ``answers.pop(0)`` if that queue is non-empty, else the
    ``answer`` attribute. warning/information/critical are silenced and recorded
    in ``messages`` as ``(level, title, text)``.
    """

    def __init__(self, default_answer) -> None:
        self.answer = default_answer
        self.answers: list = []
        self.questions: list[tuple[str, str]] = []
        self.messages: list[tuple[str, str, str]] = []

    def _next_answer(self):
        return self.answers.pop(0) if self.answers else self.answer


@pytest.fixture
def auto_answer_messagebox(monkeypatch) -> MessageBoxStub:
    from PyQt6.QtWidgets import QMessageBox

    stub = MessageBoxStub(QMessageBox.StandardButton.Yes)

    def question(*args, **kwargs):
        stub.questions.append((_caption(args, kwargs),
                               str(args[2]) if len(args) > 2 else ""))
        return stub._next_answer()

    def record(level):
        def _f(*args, **kwargs):
            stub.messages.append((level, _caption(args, kwargs),
                                  str(args[2]) if len(args) > 2 else ""))
            return QMessageBox.StandardButton.Ok
        return _f

    monkeypatch.setattr(QMessageBox, "question", staticmethod(question))
    for level in ("warning", "information", "critical"):
        monkeypatch.setattr(QMessageBox, level, staticmethod(record(level)))
    return stub


class MenuStub:
    """Scripted replacement for ``QMenu.exec`` (the popup menus).

    Set ``choice`` to the text of the entry to pick; ``None`` (the default)
    dismisses the menu the way pressing Escape does. Every popup appends its
    entry texts to ``menus``, so a test can assert what was offered as well as
    what picking one did.
    """

    def __init__(self) -> None:
        self.choice: str | None = None
        self.menus: list[list[str]] = []

    @property
    def last(self) -> list[str]:
        assert self.menus, "no menu was popped up"
        return self.menus[-1]


@pytest.fixture
def scripted_menu(monkeypatch) -> MenuStub:
    from PyQt6.QtWidgets import QMenu

    stub = MenuStub()

    def menu_exec(self, *args, **kwargs):
        actions = self.actions()
        texts = [a.text() for a in actions]
        stub.menus.append(texts)
        if stub.choice is None:
            return None
        for action in actions:
            if action.text() == stub.choice:
                return action
        raise AssertionError(f"no {stub.choice!r} entry in {texts}")

    monkeypatch.setattr(QMenu, "exec", menu_exec)
    return stub


class InputDialogStub:
    """Scripted replacement for QInputDialog.getText.

    Returns ``(texts.pop(0), accept)``; an exhausted queue returns ``("", False)``
    (the user-cancelled result). Calls land in ``calls`` as ``(title, label)``.
    """

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.accept = True
        self.calls: list[tuple[str, str]] = []


@pytest.fixture
def scripted_input_dialog(monkeypatch) -> InputDialogStub:
    from PyQt6.QtWidgets import QInputDialog

    stub = InputDialogStub()

    def get_text(*args, **kwargs):
        stub.calls.append((_caption(args, kwargs),
                           str(args[2]) if len(args) > 2 else ""))
        if not stub.texts:
            return ("", False)
        return (str(stub.texts.pop(0)), stub.accept)

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(get_text))
    return stub
