from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol


@dataclass(slots=True)
class PhoneActivation:
    activation_id: str
    phone_number: str
    country_phone_code: int | None = None

    @property
    def e164_number(self) -> str:
        digits = re.sub(r"\D", "", self.phone_number)
        return f"+{digits}" if digits else self.phone_number

    @property
    def local_number(self) -> str:
        digits = re.sub(r"\D", "", self.phone_number)
        prefix = str(self.country_phone_code or "").strip()
        if prefix and digits.startswith(prefix):
            local = digits[len(prefix) :]
            if local:
                return local
        return digits

    @property
    def country_code_value(self) -> str | None:
        if self.country_phone_code is None:
            return None
        return f"+{self.country_phone_code}"


class PhoneOtpProvider(Protocol):
    @property
    def is_configured(self) -> bool: ...

    def close(self) -> None: ...

    def request_number(self, *, operator: str | None = None) -> PhoneActivation: ...

    def wait_for_code(
        self,
        activation_id: str,
        *,
        poll_interval_seconds: float,
        timeout_seconds: float,
    ) -> str: ...

    def finish_activation(self, activation_id: str) -> None: ...

    def cancel_activation(self, activation_id: str) -> None: ...
