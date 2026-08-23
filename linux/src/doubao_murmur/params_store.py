"""Persist ASR params (cookies, device_id, web_id) to a JSON file.

Mirrors ASRParamsStore.swift.
Location: $XDG_CONFIG_HOME/doubao-murmur/asr_params.json
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

from doubao_murmur.config import (
    _atomic_write_json,
    _harden_private_json_path,
    get_params_path,
)

logger = logging.getLogger(__name__)


@dataclass
class ASRParams:
    """Parameters needed to establish a WSS ASR connection."""

    cookies: dict[str, str]
    device_id: str
    web_id: str

    @property
    def cookie_header(self) -> str:
        """Build the Cookie header string for HTTP/WSS requests."""
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())


class ParamsStore:
    """Persist ASR params to JSON file."""

    @staticmethod
    def save(params: ASRParams) -> None:
        try:
            _atomic_write_json(get_params_path(), asdict(params), mode=0o600)
            logger.info("Saved private ASR params")
        except Exception as error:
            logger.error(
                "Failed to save ASR params (%s)", error.__class__.__name__
            )

    @staticmethod
    def load() -> ASRParams | None:
        path = get_params_path()
        if not path.exists():
            return None
        if not _harden_private_json_path(path):
            logger.error("Refusing unsafe ASR params path")
            return None
        try:
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or set(data) != {
                "cookies",
                "device_id",
                "web_id",
            }:
                raise ValueError("invalid ASR params shape")
            cookies = data["cookies"]
            device_id = data["device_id"]
            web_id = data["web_id"]
            if (
                not isinstance(cookies, dict)
                or not isinstance(device_id, str)
                or not isinstance(web_id, str)
                or any(
                    not isinstance(key, str)
                    or not isinstance(value, str)
                    or any(character in key or character in value for character in "\r\n")
                    for key, value in cookies.items()
                )
            ):
                raise ValueError("invalid ASR params values")
            return ASRParams(
                cookies=cookies,
                device_id=device_id,
                web_id=web_id,
            )
        except Exception as error:
            logger.error(
                "Failed to load ASR params (%s)", error.__class__.__name__
            )
            return None

    @staticmethod
    def clear() -> None:
        try:
            get_params_path().unlink(missing_ok=True)
            logger.info("Cleared saved params")
        except Exception as error:
            logger.error(
                "Failed to clear ASR params (%s)", error.__class__.__name__
            )

    @staticmethod
    def has_saved() -> bool:
        path = get_params_path()
        return path.exists() and _harden_private_json_path(path)
