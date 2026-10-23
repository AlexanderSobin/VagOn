"""VAG 'Security Access' login codes on UDS modules.

The workshop login (the 5-digit code a technician types in ODIS/VCDS, e.g.
20103, 31347, 28183) is applied to UDS SecurityAccess level 3: the module
returns a 4-byte seed, the key is (seed + login) mod 2^32 sent at level 4.
This is the documented workshop procedure (see openpilot's vw_mqb_config.py
and clusterleg); it is NOT a bypass of SFD/component protection, which VagOn
does not implement.

Wrong keys count against the module's attempt counter: after a few failures it
answers 0x36/0x37 and blocks logins for ~10 minutes. VagOn therefore tries a
login at most once per module and code per connection unless forced.
"""

from __future__ import annotations

from ..errors import NegativeResponse, SafetyError
from ..uds import UdsClient

LOGIN_LEVEL = 0x03

# Codes commonly cited for MQB/MQB-Evo modules (adaptation unless noted).
COMMON_LOGINS: dict[int, list[int]] = {
    0x03: [40168, 28183],
    0x09: [31347, 20103],
    0x13: [20103, 14117],
    0x17: [20103, 25327],
    0x19: [20103, 31347],
    0x3C: [20103],
    0x44: [19249, 28183, 40168],
    0x5F: [20103],
    0xA5: [20103],
}


def login_key(seed: bytes, login: int) -> bytes:
    if len(seed) != 4:
        raise SafetyError(f"неожиданная длина seed ({len(seed)} байт) — логин по коду не применим")
    return ((int.from_bytes(seed, "big") + login) & 0xFFFFFFFF).to_bytes(4, "big")


class LoginGuard:
    """Remembers failed logins so VagOn never burns the module's attempt counter."""

    def __init__(self) -> None:
        self.failed: set[tuple[int, int]] = set()
        self.locked_modules: set[int] = set()

    def check(self, module_addr: int, login: int, force: bool = False) -> None:
        if force:
            return
        if module_addr in self.locked_modules:
            raise SafetyError("блок заблокировал логин после неудачных попыток. Оставьте зажигание "
                              "включённым ~10 минут и переподключитесь.")
        if (module_addr, login) in self.failed:
            raise SafetyError(f"код {login} уже был отвергнут этим блоком — повтор не делаю "
                              "(чтобы не заблокировать блок). Проверьте код.")


def perform_login(uds: UdsClient, module_addr: int, login: int, guard: LoginGuard,
                  force: bool = False) -> bool:
    """Run seed/key with a login code. Returns True if a key was sent (False if already open)."""
    if not (0 <= login <= 99999):
        raise SafetyError("логин-код должен быть числом 0..99999")
    guard.check(module_addr, login, force)
    seed = uds.request_seed(LOGIN_LEVEL)
    if seed and not any(seed):
        return False  # already unlocked
    try:
        uds.send_key(LOGIN_LEVEL, login_key(seed, login))
    except NegativeResponse as e:
        if e.nrc == 0x35:
            guard.failed.add((module_addr, login))
        elif e.nrc in (0x36, 0x37):
            guard.locked_modules.add(module_addr)
        raise
    return True
