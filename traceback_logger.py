"""
Traceback Logger Module - Handles error logging and traceback management.
"""

import sys
import inspect
import traceback
from enum import Enum
from typing import Optional

class Status(Enum):
    """Error types used by the scraper's error reporting system."""
    NOTFOUND = "not_found"
    ERROR    = "error"
    TIMEOUT  = "timeout"
    FETCH    = "fetch_error"
    PARSE    = "parse_error"
    WARNING  = "warning"

class TracebackLogger:
    """
    Handles error logging with traceback, storing the last error.

    This class provides centralized error logging with automatic caller detection,
    traceback formatting, and error storage for later inspection.

    Example:
        logger = TracebackLogger()
        try:
            risky_operation()
        except Exception as e:
            logger.log(Status.ERROR, exc=e, message="Operation failed")
            print(logger.get_last_error())
    """

    def __init__(self):
        """Initialize logger with empty error storage."""
        self._last_error: Optional[tuple] = None

    def log(self, error_type: Status, exc: Optional[Exception] = None, message: str = "") -> None:
        """
        Print traceback and error details to stderr; store last error.

        Args:
            error_type: Status enum member (e.g., Status.NOTFOUND, Status.ERROR)
            exc: Optional exception instance to include in traceback
            message: Optional custom message to provide additional context

        Example:
            logger.log(Status.TIMEOUT, exc=timeout_exc, message="SEC API timeout")
        """
        # Get caller's method name (skip this method and go up one more frame)
        frame = inspect.currentframe()
        caller_frame = frame.f_back.f_back if frame and frame.f_back else None
        method_name = caller_frame.f_code.co_name if caller_frame else "<unknown>"

        error_name = error_type.name

        # Build traceback string
        tb_str = ""
        if exc is not None:
            tb_str = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))

        output_lines = []
        if tb_str:
            output_lines.append(tb_str.rstrip('\n'))
        output_lines.append(f"[{error_name}] in {method_name}()")
        if message:
            output_lines.append(f"Message: {message}")

        sys.stderr.write("\n".join(output_lines) + "\n")
        self._last_error = (error_name, method_name, message, tb_str)

    def get_last_error(self) -> Optional[tuple]:
        """
        Return the last logged error as a tuple.

        Returns:
            Tuple of (error_name, method_name, message, traceback_string) or None if no error logged.

        Example:
            last = logger.get_last_error()
            if last:
                error_name, method, msg, tb = last
                print(f"Last error: {error_name} in {method}")
        """
        return self._last_error