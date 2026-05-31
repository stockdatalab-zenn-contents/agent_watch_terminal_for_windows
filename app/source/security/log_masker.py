"""Log masking module for filtering sensitive data from log messages.

Replaces API keys, tokens, passwords, and other secrets with ***REDACTED***
before they are recorded to log files.
"""

import logging
import re

REDACTED = "***REDACTED***"

# ---------------------------------------------------------------------------
# Compiled patterns (order matters -- more specific patterns first)
# ---------------------------------------------------------------------------

_PATTERNS: list[re.Pattern[str]] = [
    # PowerShell env var assignment: $env:TOKEN="value"
    re.compile(
        r'(\$env:(?:API_?KEY|SECRET|PASSWORD|PASSWD|TOKEN|CREDENTIAL))\s*=\s*"[^"]*"',
        re.IGNORECASE,
    ),
    re.compile(
        r"(\$env:(?:API_?KEY|SECRET|PASSWORD|PASSWD|TOKEN|CREDENTIAL))\s*=\s*'[^']*'",
        re.IGNORECASE,
    ),
    # Shell / config env var assignment: API_KEY=value (unquoted, single-quoted, double-quoted)
    re.compile(
        r'((?:^|(?<=\s))[\w]*(?:API_?KEY|SECRET|PASSWORD|PASSWD|TOKEN|CREDENTIAL)[\w]*)\s*=\s*"[^"]*"',
        re.IGNORECASE,
    ),
    re.compile(
        r"((?:^|(?<=\s))[\w]*(?:API_?KEY|SECRET|PASSWORD|PASSWD|TOKEN|CREDENTIAL)[\w]*)\s*=\s*'[^']*'",
        re.IGNORECASE,
    ),
    re.compile(
        r"((?:^|(?<=\s))[\w]*(?:API_?KEY|SECRET|PASSWORD|PASSWD|TOKEN|CREDENTIAL)[\w]*)\s*=\s*(\S+)",
        re.IGNORECASE,
    ),
    # Anthropic API key: sk-ant-api03-...
    re.compile(r"sk-ant-[\w\-]{20,}"),
    # OpenAI API key: sk-...
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    # Bearer token in authorization headers
    re.compile(r"(Bearer\s+)\S{8,}", re.IGNORECASE),
    # Long random strings that look like keys/tokens (48+ alphanumeric + common special chars)
    re.compile(r"(?<![/\\])(?:[A-Za-z0-9+/=_\-]){48,}"),
]

# Replacement functions per pattern index.
# If a pattern uses capture groups for the key name, we keep the key visible
# and only redact the value.

def _replace_env_quoted(m: re.Match[str]) -> str:
    """Replace value in env var assignments that use quotes."""
    return f"{m.group(1)}={REDACTED}"


def _replace_env_unquoted(m: re.Match[str]) -> str:
    """Replace value in unquoted env var assignments."""
    return f"{m.group(1)}={REDACTED}"


def _replace_bearer(m: re.Match[str]) -> str:
    """Replace the token after 'Bearer '."""
    return f"{m.group(1)}{REDACTED}"


def _replace_full(m: re.Match[str]) -> str:
    """Replace the entire match."""
    return REDACTED


# Map each pattern index to its replacement callable.
_REPLACERS: list[object] = [
    _replace_env_quoted,    # $env:TOKEN="..."
    _replace_env_quoted,    # $env:TOKEN='...'
    _replace_env_quoted,    # KEY="..."
    _replace_env_quoted,    # KEY='...'
    _replace_env_unquoted,  # KEY=value
    _replace_full,          # sk-ant-...
    _replace_full,          # sk-...
    _replace_bearer,        # Bearer xxx
    _replace_full,          # long random string
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def mask_secrets(text: str) -> str:
    """Replace sensitive patterns in *text* with ***REDACTED***.

    Parameters
    ----------
    text : str
        Raw log message or arbitrary string.

    Returns
    -------
    str
        Sanitized string with secrets masked.
    """
    for pattern, replacer in zip(_PATTERNS, _REPLACERS):
        text = pattern.sub(replacer, text)  # type: ignore[arg-type]
    return text


class SecretMaskingFilter(logging.Filter):
    """Logging filter that masks secrets in log records.

    Attach to any ``logging.Handler`` or ``logging.Logger``::

        handler.addFilter(SecretMaskingFilter())
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Mask secrets in the log record message and args.

        Always returns ``True`` so the record is still emitted.
        """
        if isinstance(record.msg, str):
            record.msg = mask_secrets(record.msg)

        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: mask_secrets(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    mask_secrets(a) if isinstance(a, str) else a
                    for a in record.args
                )
        return True
