from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path
from typing import Optional


_LOG_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_DATEFMT = "%H:%M:%S"
_FILE_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_FILE_DATEFMT = "%Y-%m-%d %H:%M:%S"

_CONFIGURED_ROOT: set[str] = set()
_PLATFORM_HANDLERS: dict[str, logging.Handler] = {}


def _level_from_name(name: str, *, verbose: bool = False) -> int:
    key = (name or "").strip().upper()
    if key in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        return getattr(logging, key)
    return logging.DEBUG if verbose else logging.INFO


def _ensure_utf8_stdout() -> None:
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass


def setup_scrape_logging(
    *,
    verbose: bool = False,
    platform: str = "",
    log_level: str = "",
) -> logging.Logger:
    """Logger console (et optionnellement fichier plateforme) pour bench scrape."""
    _ensure_utf8_stdout()
    level = _level_from_name(log_level, verbose=verbose)
    name = f"vf_scrape.{platform}" if platform else "vf_scrape"
    log = logging.getLogger(name)
    log.setLevel(level)
    if name not in _CONFIGURED_ROOT:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter(_LOG_FMT, datefmt=_LOG_DATEFMT))
        h.setLevel(level)
        log.addHandler(h)
        log.propagate = False
        _CONFIGURED_ROOT.add(name)
    else:
        log.setLevel(level)
        for h in log.handlers:
            if isinstance(h, logging.StreamHandler) and h.stream is sys.stdout:
                h.setLevel(level)
    # Propagate DEBUG to vf.* loggers when verbose
    if level <= logging.DEBUG:
        for pkg in ("vf", "vf_scrape", "vf.vinted_scrape"):
            pkg_log = logging.getLogger(pkg)
            if not pkg_log.handlers:
                pkg_log.setLevel(logging.DEBUG)
                pkg_log.propagate = True
            else:
                pkg_log.setLevel(min(pkg_log.level, logging.DEBUG))
    return log


def attach_platform_file_logger(
    platform: str,
    log_dir: str | Path,
    *,
    log_level: str = "DEBUG",
    day: Optional[str] = None,
) -> Path:
    """Ajoute un FileHandler par plateforme — bench_out/logs/vinted_2026-06-12.log."""
    plat = (platform or "unknown").lower()
    root = Path(log_dir)
    root.mkdir(parents=True, exist_ok=True)
    day_str = day or date.today().isoformat()
    path = root / f"{plat}_{day_str}.log"
    level = _level_from_name(log_level, verbose=True)

    for logger_name in (f"vf_scrape.{plat}", "vf_scrape", f"vf.{plat}", "vf.vinted_scrape", "vf.market"):
        log = logging.getLogger(logger_name)
        log.setLevel(min(log.level, level) if log.level else level)

    key = f"{plat}:{path}"
    if key in _PLATFORM_HANDLERS:
        return path

    fh = logging.FileHandler(path, encoding="utf-8", mode="a")
    fh.setFormatter(logging.Formatter(_FILE_FMT, datefmt=_FILE_DATEFMT))
    fh.setLevel(level)

    for logger_name in ("vf_scrape", f"vf_scrape.{plat}", "vf", "vf.vinted_scrape", "vf.market", "vf.browser"):
        log = logging.getLogger(logger_name)
        log.addHandler(fh)
        log.setLevel(level)
        log.propagate = logger_name not in ("vf_scrape", f"vf_scrape.{plat}")

    _PLATFORM_HANDLERS[key] = fh
    return path


def detach_platform_file_loggers() -> None:
    """Retire les FileHandlers de test (évite doublons entre runs)."""
    for key, fh in list(_PLATFORM_HANDLERS.items()):
        for log in logging.Logger.manager.loggerDict.values():
            if isinstance(log, logging.Logger) and fh in log.handlers:
                log.removeHandler(fh)
        try:
            fh.close()
        except Exception:
            pass
        _PLATFORM_HANDLERS.pop(key, None)
