"""Exception hierarchy and UDS negative response codes."""

from __future__ import annotations


class VagOnError(Exception):
    """Base class for all VagOn errors."""


class TransportError(VagOnError):
    """The link to the adapter failed or the adapter misbehaved."""


class AdapterError(TransportError):
    """The adapter reported an error or answered something unexpected."""


class TimeoutError_(TransportError):
    """No answer from the module in time."""


class IsoTpError(VagOnError):
    """Malformed ISO-TP frame sequence."""


class SafetyError(VagOnError):
    """An operation was refused by a safety check (nothing was written)."""


class VerifyError(VagOnError):
    """A write was acknowledged but reading back shows a different value."""


# ISO 14229-1 negative response codes, with explanations in Russian
# (the primary UI language) and English.
NRC = {
    0x10: ("generalReject", "общий отказ", "general reject"),
    0x11: ("serviceNotSupported", "сервис не поддерживается блоком", "service not supported"),
    0x12: ("subFunctionNotSupported", "подфункция не поддерживается", "sub-function not supported"),
    0x13: ("incorrectMessageLengthOrInvalidFormat",
           "неверная длина/формат запроса (например, неверная длина кодировки)",
           "incorrect message length or invalid format"),
    0x14: ("responseTooLong", "ответ слишком длинный", "response too long"),
    0x21: ("busyRepeatRequest", "блок занят, повторите", "busy, repeat request"),
    0x22: ("conditionsNotCorrect",
           "условия не выполнены (зажигание, скорость, двигатель, капот, SFD...)",
           "conditions not correct (ignition, speed, engine running, SFD...)"),
    0x24: ("requestSequenceError",
           "нарушена последовательность (нужно сначала сессию/логин/дату программирования)",
           "request sequence error"),
    0x25: ("noResponseFromSubnetComponent", "нет ответа от подчинённого компонента",
           "no response from subnet component"),
    0x26: ("failurePreventsExecutionOfRequestedAction", "неисправность мешает выполнению",
           "failure prevents execution"),
    0x31: ("requestOutOfRange",
           "запрос вне диапазона (такого идентификатора/значения нет в этом блоке)",
           "request out of range (identifier/value not supported)"),
    0x33: ("securityAccessDenied",
           "доступ запрещён (нужен логин; на 8Y часто означает защиту SFD)",
           "security access denied (login needed; on MQB-Evo often SFD protection)"),
    0x34: ("authenticationRequired", "требуется аутентификация (SFD/0x29)",
           "authentication required"),
    0x35: ("invalidKey", "неверный ключ/логин-код", "invalid key / login code"),
    0x36: ("exceededNumberOfAttempts",
           "превышено число попыток логина — подождите ~10 мин с включённым зажиганием",
           "exceeded number of attempts — wait ~10 min with ignition on"),
    0x37: ("requiredTimeDelayNotExpired",
           "ещё не прошла задержка после неудачных попыток — подождите",
           "required time delay not expired"),
    0x70: ("uploadDownloadNotAccepted", "загрузка не принята", "upload/download not accepted"),
    0x71: ("transferDataSuspended", "передача приостановлена", "transfer data suspended"),
    0x72: ("generalProgrammingFailure", "ошибка записи в память блока",
           "general programming failure"),
    0x73: ("wrongBlockSequenceCounter", "неверный счётчик блоков", "wrong block sequence counter"),
    0x78: ("requestCorrectlyReceivedResponsePending", "запрос принят, ответ позже",
           "response pending"),
    0x7E: ("subFunctionNotSupportedInActiveSession",
           "подфункция недоступна в текущей сессии", "sub-function not supported in active session"),
    0x7F: ("serviceNotSupportedInActiveSession",
           "сервис недоступен в текущей сессии (нужна расширенная сессия)",
           "service not supported in active session"),
    0x81: ("rpmTooHigh", "обороты слишком высокие", "rpm too high"),
    0x82: ("rpmTooLow", "обороты слишком низкие", "rpm too low"),
    0x83: ("engineIsRunning", "двигатель работает — заглушите", "engine is running"),
    0x84: ("engineIsNotRunning", "двигатель не работает", "engine is not running"),
    0x85: ("engineRunTimeTooLow", "двигатель работает слишком мало", "engine run time too low"),
    0x86: ("temperatureTooHigh", "температура слишком высокая", "temperature too high"),
    0x87: ("temperatureTooLow", "температура слишком низкая", "temperature too low"),
    0x88: ("vehicleSpeedTooHigh", "скорость слишком высокая", "vehicle speed too high"),
    0x89: ("vehicleSpeedTooLow", "скорость слишком низкая", "vehicle speed too low"),
    0x8A: ("throttlePedalTooHigh", "педаль газа нажата", "throttle/pedal too high"),
    0x8B: ("throttlePedalTooLow", "педаль газа слишком низко", "throttle/pedal too low"),
    0x8C: ("transmissionRangeNotInNeutral", "КПП не в нейтрали", "transmission not in neutral"),
    0x8D: ("transmissionRangeNotInGear", "КПП не в передаче", "transmission not in gear"),
    0x8F: ("brakeSwitchNotClosed", "нажмите тормоз", "brake switch not closed"),
    0x90: ("shifterLeverNotInPark", "селектор не в P", "shifter lever not in park"),
    0x91: ("torqueConverterClutchLocked", "гидротрансформатор заблокирован",
           "torque converter clutch locked"),
    0x92: ("voltageTooHigh", "напряжение слишком высокое", "voltage too high"),
    0x93: ("voltageTooLow", "напряжение слишком низкое — подключите зарядное",
           "voltage too low"),
}


def nrc_name(code: int) -> str:
    entry = NRC.get(code)
    return entry[0] if entry else f"NRC_0x{code:02X}"


def nrc_text(code: int, lang: str = "ru") -> str:
    entry = NRC.get(code)
    if not entry:
        return f"неизвестный код 0x{code:02X}" if lang == "ru" else f"unknown code 0x{code:02X}"
    return entry[1] if lang == "ru" else entry[2]


class NegativeResponse(VagOnError):
    """The module answered with 0x7F <sid> <nrc>."""

    def __init__(self, sid: int, nrc: int, request: bytes = b""):
        self.sid = sid
        self.nrc = nrc
        self.request = request
        super().__init__(
            f"блок отказал: сервис 0x{sid:02X}, код 0x{nrc:02X} {nrc_name(nrc)} — {nrc_text(nrc)}"
        )


class ResponsePendingTimeout(TimeoutError_):
    """The module said 'response pending' (0x78) but the final answer never arrived."""
