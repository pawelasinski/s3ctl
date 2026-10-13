"""Rich output and console/file logging for the CLI."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from rich.console import Console
from rich.json import JSON
from rich.logging import RichHandler
from rich.table import Table

console = Console(markup=False)
logger = logging.getLogger("s3ctl")


def setup_logging(log_dir: Path, verbose: bool = False) -> Path:
    """Configure Rich console logging and a rotating UTF-8 file log.

    Parameters
    ----------
    log_dir : pathlib.Path
        Directory containing the application log.
    verbose : bool, optional
        Include application debug messages.

    Returns
    -------
    pathlib.Path
        Log file path.
    
    Todo:
    -----
    * Make the log filename, maxBytes, and backupCount configurable.

    """
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "s3ctl.log"
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    terminal = RichHandler(console=console, markup=False, show_path=False)
    file = RotatingFileHandler(
        path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(terminal)
    logger.addHandler(file)
    logger.debug("Logging initialized.")
    return path


def log_output(text: str) -> None:
    """Save displayed data to file handlers without repeating it on screen."""
    record = logger.makeRecord("s3ctl", logging.INFO, __file__, 0, text, (), None)
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler):
            handler.handle(record)


def print_table(
    headers: list[str], rows: Iterable[Iterable[Any]], pager: bool = False
) -> None:
    """Render a Rich table and log its complete text.

    Parameters
    ----------
    headers : list of str
        Column labels.
    rows : iterable of iterable
        Table cell values.
    pager : bool, optional
        Open an interactive pager on a terminal; print normally otherwise.
    
    """
    table = Table(show_lines=False)
    for header in headers:
        table.add_column(header, overflow="fold")
    for row in rows:
        table.add_row(*(str(cell) for cell in row))
    with console.capture() as capture:
        console.print(table)
    log_output(capture.get())
    if pager and console.is_terminal:
        with console.pager(styles=True):
            console.print(table)
    else:
        console.print(table)


def show_json(title: str, document: Any) -> None:
    """Display a policy document with Rich and store it in the file log."""
    console.print(title, style="bold")
    if isinstance(document, str):
        try:
            document = json.loads(document)
        except json.JSONDecodeError:
            document = json.loads(unquote(document))
    text = json.dumps(document, indent=2, ensure_ascii=False, default=str)
    console.print(JSON(text))
    log_output(f"{title}\n{text}")
