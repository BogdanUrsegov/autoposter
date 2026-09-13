"""Инструменты разработчика: архив проекта, обновление кода, рестарт, логи.

Работает и на Windows (локальная разработка), и на Linux/systemd (VPS).
Секреты и окружение (.env, venv, логи, бэкапы, БД) из архивов исключаются
и никогда не перезаписываются при обновлении.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BACKUP_DIR = PROJECT_ROOT / "backups"
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "bot.log"
TMP_DIR = PROJECT_ROOT / "tmp_deploy"

MAX_BACKUPS = 7

EXCLUDE_DIRS = {
    "__pycache__",
    ".git",
    ".github",
    ".idea",
    ".vscode",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".claude",
    "venv",
    ".venv",
    "env",
    "node_modules",
    "backups",
    "logs",
    "data",
    "tmp_deploy",
    "site-packages",
}

EXCLUDE_NAMES = {
    ".env",
    ".env.local",
    ".env.prod",
    ".env.production",
    ".DS_Store",
    "Thumbs.db",
}

EXCLUDE_GLOBS = (
    "*.pyc",
    "*.pyo",
    "*.pyd",
    "*.log",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "*.zip",
    "*.tar",
    "*.tar.gz",
    "*.session",
    "*.session-journal",
    "*.swp",
    "*.bak",
)

PROTECTED_PATHS = {".env", ".env.local", ".env.prod", ".env.production"}

ERROR_PATTERN = re.compile(
    r"(ERROR|CRITICAL|Traceback|Exception|Error:|error:|FAILED|failed)", re.IGNORECASE
)


# --------------------------------------------------------------------------- #
# Отбор файлов проекта                                                         #
# --------------------------------------------------------------------------- #
def _is_excluded_name(name: str) -> bool:
    if name in EXCLUDE_NAMES:
        return True
    return any(fnmatch.fnmatch(name, pattern) for pattern in EXCLUDE_GLOBS)


def iter_project_files(root: Path | None = None):
    base = root or PROJECT_ROOT
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
        current = Path(dirpath)
        for filename in sorted(filenames):
            if _is_excluded_name(filename):
                continue
            full = current / filename
            try:
                if full.stat().st_size > 25 * 1024 * 1024:
                    continue
            except OSError:
                continue
            yield full, full.relative_to(base).as_posix()


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Архив проекта                                                                #
# --------------------------------------------------------------------------- #
def build_project_archive(dest_dir: Path | None = None) -> tuple[Path, int]:
    target_dir = dest_dir or TMP_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = target_dir / f"autoposter_source_{stamp}.zip"

    count = 0
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for full, rel in iter_project_files():
            zf.write(full, rel)
            count += 1
    return archive, count


# --------------------------------------------------------------------------- #
# Бэкапы                                                                       #
# --------------------------------------------------------------------------- #
def create_code_backup(tag: str = "pre_update") -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = BACKUP_DIR / f"{tag}_{stamp}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for full, rel in iter_project_files():
            zf.write(full, rel)
    _prune_backups()
    return archive


def _prune_backups() -> None:
    if not BACKUP_DIR.exists():
        return
    backups = sorted(BACKUP_DIR.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[MAX_BACKUPS:]:
        try:
            old.unlink()
        except OSError:
            pass


def latest_backup() -> Path | None:
    if not BACKUP_DIR.exists():
        return None
    backups = sorted(BACKUP_DIR.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    return backups[0] if backups else None


# --------------------------------------------------------------------------- #
# Применение обновления                                                        #
# --------------------------------------------------------------------------- #
@dataclass
class UpdateResult:
    ok: bool = False
    error: str = ""
    backup: Path | None = None
    updated: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    unchanged: int = 0
    requirements_changed: bool = False

    @property
    def total_changed(self) -> int:
        return len(self.updated) + len(self.added)


def _safe_relpath(name: str, strip_prefix: str) -> str | None:
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or ":" in normalized.split("/")[0]:
        return None
    if strip_prefix and normalized.startswith(strip_prefix):
        normalized = normalized[len(strip_prefix):]
    if not normalized or normalized.endswith("/"):
        return None
    parts = normalized.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    if any(p in EXCLUDE_DIRS for p in parts[:-1]):
        return None
    if normalized in PROTECTED_PATHS or _is_excluded_name(parts[-1]):
        return None
    return normalized


ANCHOR_DIRS = ("bot/", "admin/", "database/")


def _detect_prefix(names: list[str]) -> str:
    """Архив может содержать общую папку сверху — находим и отбрасываем её."""
    normalized = [n.replace("\\", "/") for n in names if not n.endswith("/")]
    if any(n.startswith(a) for a in ANCHOR_DIRS for n in normalized):
        return ""
    tops = {n.split("/")[0] for n in normalized if "/" in n}
    if len(tops) == 1:
        top = tops.pop()
        if any(n.startswith(f"{top}/{a}") for a in ANCHOR_DIRS for n in normalized):
            return f"{top}/"
    return ""


def apply_update_archive(zip_path: Path) -> UpdateResult:
    """Распаковывает архив поверх проекта, сохранив .env, venv, логи, БД и бэкапы."""
    result = UpdateResult()

    if not zipfile.is_zipfile(zip_path):
        result.error = "Файл не является zip-архивом"
        return result

    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad:
            result.error = f"Архив повреждён: {bad}"
            return result

        names = zf.namelist()
        prefix = _detect_prefix(names)

        planned: list[tuple[str, str]] = []
        for name in names:
            info = zf.getinfo(name)
            if info.is_dir():
                continue
            rel = _safe_relpath(name, prefix)
            if rel is None:
                result.skipped.append(name)
                continue
            planned.append((name, rel))

        if not planned:
            result.error = "В архиве нет подходящих файлов (нужна папка bot/ внутри)"
            return result

        if not any(rel.startswith("bot/") for _, rel in planned):
            result.error = "В архиве не найдена папка bot/ — обновление отменено"
            return result

        result.backup = create_code_backup("pre_update")

        for name, rel in planned:
            target = PROJECT_ROOT / rel
            data = zf.read(name)
            existed = target.exists()
            if existed:
                try:
                    if hashlib.sha256(data).hexdigest() == _file_hash(target):
                        result.unchanged += 1
                        continue
                except OSError:
                    pass
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as fh:
                fh.write(data)
            if existed:
                result.updated.append(rel)
            else:
                result.added.append(rel)
            if rel == "requirements.txt":
                result.requirements_changed = True

    _purge_pycache()
    result.ok = True
    return result


def restore_backup(backup: Path) -> UpdateResult:
    return apply_update_archive(backup)


def _purge_pycache() -> None:
    for dirpath, dirnames, _ in os.walk(PROJECT_ROOT):
        if "__pycache__" in dirnames:
            shutil.rmtree(Path(dirpath) / "__pycache__", ignore_errors=True)
            dirnames.remove("__pycache__")
        dirnames[:] = [d for d in dirnames if d not in {"venv", ".venv", ".git"}]


# --------------------------------------------------------------------------- #
# Зависимости                                                                  #
# --------------------------------------------------------------------------- #
def install_requirements(timeout: int = 900) -> tuple[bool, str]:
    req = PROJECT_ROOT / "requirements.txt"
    if not req.exists():
        return False, "requirements.txt не найден"
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(req), "--upgrade"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "pip install превысил таймаут"
    except OSError as e:
        return False, f"Не удалось запустить pip: {e}"
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, output.strip()[-3000:] or "(пустой вывод)"


# --------------------------------------------------------------------------- #
# Рестарт                                                                      #
# --------------------------------------------------------------------------- #
def systemd_available() -> bool:
    return (
        sys.platform.startswith("linux")
        and shutil.which("systemctl") is not None
        and Path("/run/systemd/system").exists()
    )


def restart_service(delay: float = 2.0) -> tuple[bool, str]:
    service = settings.service_name
    if systemd_available():
        try:
            subprocess.Popen(
                ["/bin/sh", "-c", f"sleep {delay}; systemctl restart {service}"],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True, f"systemctl restart {service}"
        except OSError as e:
            return False, f"Ошибка systemctl: {e}"

    def _exec_restart() -> None:
        time.sleep(delay)
        try:
            os.chdir(PROJECT_ROOT)
            os.execv(sys.executable, [sys.executable, "run.py"])
        except OSError as e:  # pragma: no cover
            logger.error("Не удалось перезапустить процесс: %s", e)

    threading.Thread(target=_exec_restart, daemon=True).start()
    return True, "перезапуск процесса (systemd не найден)"


# --------------------------------------------------------------------------- #
# Логи и статус                                                                #
# --------------------------------------------------------------------------- #
def _journal_logs(lines: int) -> str | None:
    if not systemd_available():
        return None
    try:
        proc = subprocess.run(
            [
                "journalctl",
                "-u",
                settings.service_name,
                "-n",
                str(lines),
                "--no-pager",
                "-o",
                "short-iso",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    text = (proc.stdout or "").strip()
    return text or None


def _file_logs(lines: int) -> str | None:
    if not LOG_FILE.exists():
        return None
    try:
        with LOG_FILE.open("r", encoding="utf-8", errors="replace") as fh:
            tail = fh.readlines()[-lines:]
    except OSError:
        return None
    return "".join(tail).strip() or None


def read_logs(lines: int = 200, errors_only: bool = False, source: str = "auto") -> tuple[str, str]:
    fetch = lines * 6 if errors_only else lines
    fetch = min(fetch, 20000)

    text: str | None = None
    used = ""
    if source in ("auto", "journal"):
        text = _journal_logs(fetch)
        used = "journalctl" if text else ""
    if text is None and source in ("auto", "file"):
        text = _file_logs(fetch)
        used = "logs/bot.log" if text else used

    if not text:
        return "", used or "нет данных"

    if errors_only:
        matched = [ln for ln in text.splitlines() if ERROR_PATTERN.search(ln)]
        text = "\n".join(matched[-lines:])
        used = f"{used} (только ошибки)"
        if not text:
            return "", used
    return text, used


def clear_log_file() -> tuple[bool, str]:
    if not LOG_FILE.exists():
        return False, "Файл логов не найден"
    try:
        size = LOG_FILE.stat().st_size
        with LOG_FILE.open("w", encoding="utf-8"):
            pass
        return True, f"Очищено {size // 1024} КБ"
    except OSError as e:
        return False, str(e)


def _run(cmd: list[str], timeout: int = 30) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (proc.stdout or proc.stderr or "").strip()


def service_status() -> str:
    parts: list[str] = []
    parts.append(f"🖥 Платформа: {sys.platform}")
    parts.append(f"🐍 Python: {sys.version.split()[0]}")
    parts.append(f"📁 Каталог: {PROJECT_ROOT}")
    parts.append(f"🔧 PID: {os.getpid()}")

    if systemd_available():
        active = _run(["systemctl", "is-active", settings.service_name]) or "unknown"
        since = _run(
            [
                "systemctl",
                "show",
                settings.service_name,
                "--property=ActiveEnterTimestamp",
                "--value",
            ]
        )
        parts.append(f"⚙️ Сервис: {settings.service_name} — {active}")
        if since:
            parts.append(f"🕐 Запущен: {since}")
        mem = _run(["free", "-m"]).splitlines()
        if len(mem) > 1:
            parts.append(f"💾 RAM: {' '.join(mem[1].split()[1:4])} МБ (всего/занято/свободно)")
        disk = _run(["df", "-h", str(PROJECT_ROOT)]).splitlines()
        if len(disk) > 1:
            cols = disk[-1].split()
            if len(cols) >= 5:
                parts.append(f"💿 Диск: {cols[2]} занято из {cols[1]} ({cols[4]})")
        upt = _run(["uptime", "-p"])
        if upt:
            parts.append(f"⏱ Аптайм сервера: {upt}")
    else:
        parts.append("⚙️ systemd не обнаружен (локальный запуск)")

    files = sum(1 for _ in iter_project_files())
    parts.append(f"📦 Файлов в проекте: {files}")

    backup = latest_backup()
    if backup:
        stamp = datetime.fromtimestamp(backup.stat().st_mtime).strftime("%d.%m.%Y %H:%M")
        parts.append(f"↩️ Последний бэкап кода: {backup.name} ({stamp})")
    else:
        parts.append("↩️ Бэкапов кода пока нет")

    return "\n".join(parts)


def cleanup_tmp() -> None:
    shutil.rmtree(TMP_DIR, ignore_errors=True)
