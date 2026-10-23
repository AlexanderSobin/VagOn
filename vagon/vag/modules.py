"""VAG control modules: VCDS/ODIS address → UDS CAN IDs.

CAN IDs come from ODIS logical-link data (github.com/ConnorHowell/vag-uds-ids);
for VAG modules the response ID is request + 0x6A, OBD-II style modules
(engine/gearbox) use 0x7E0.. → +8. The address ↔ link mapping is the usual
VCDS numbering on MQB/MQB-Evo. `protected` modules are read-only in VagOn.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..hexutil import parse_int


@dataclass(frozen=True)
class Module:
    address: int
    name_ru: str
    name_en: str
    tx_id: int
    rx_id: int
    odis_link: str = ""
    protected: bool = False   # never written by VagOn (immobilizer, airbag...)

    @property
    def addr_hex(self) -> str:
        return f"{self.address:02X}"

    def label(self, lang: str = "ru") -> str:
        return f"{self.addr_hex} {self.name_ru if lang == 'ru' else self.name_en}"


def _m(addr, ru, en, tx, link="", protected=False, rx=None):
    return Module(addr, ru, en, tx, rx if rx is not None else tx + 0x6A, link, protected)


MODULES: list[Module] = [
    _m(0x01, "Двигатель", "Engine", 0x7E0, "LL_EnginContrModul1UDS", rx=0x7E8),
    _m(0x02, "АКПП / DSG", "Transmission", 0x7E1, "LL_TransContrModulUDS", rx=0x7E9),
    _m(0x03, "ABS / ESP", "Brakes 1", 0x713, "LL_Brake1UDS"),
    _m(0x05, "Доступ и запуск (Kessy)", "Access/Start Authorization", 0x732, "LL_AccesStartInterUDS"),
    _m(0x08, "Климат", "Auto HVAC", 0x746, "LL_AirCondiUDS"),
    _m(0x09, "Центральная электроника (BCM)", "Central Electrics", 0x70E, "LL_CentrElectUDS"),
    _m(0x10, "Парковка (Park Assist)", "Park/Steer Assist", 0x70A, "LL_ParkiAssis2UDS"),
    _m(0x13, "Адаптивный круиз (ACC)", "Distance Regulation", 0x757, "LL_AdaptCruisContrUDS"),
    _m(0x15, "Подушки безопасности", "Airbags", 0x715, "LL_AirbaUDS", protected=True),
    _m(0x16, "Рулевая колонка (подрулевые)", "Steering Column Electronics", 0x70C, "LL_SteerColumElectUDS"),
    _m(0x17, "Приборная панель (Virtual Cockpit)", "Instruments", 0x714, "LL_DashBoardUDS"),
    _m(0x19, "Шлюз (Gateway)", "CAN Gateway", 0x710, "LL_GatewUDS"),
    _m(0x22, "Полный привод", "AWD", 0x70F, "LL_AllWheelContrUDS"),
    _m(0x25, "Иммобилайзер", "Immobilizer", 0x711, "LL_ImmobUDS", protected=True),
    _m(0x2B, "Блокировка рулевой колонки", "Steering Column Lock", 0x731, "LL_SteerColumLockiUDS",
       protected=True),
    _m(0x36, "Сиденье водителя", "Seat Adjustment Driver", 0x74C, "LL_SeatAdjusDriveSideUDS"),
    _m(0x06, "Сиденье пассажира", "Seat Adjustment Passenger", 0x74D, "LL_SeatAdjusPasseSideUDS"),
    _m(0x3C, "Контроль слепых зон (Side Assist)", "Lane Change Assist", 0x74E, "LL_LaneChangAssisUDS"),
    _m(0x42, "Дверь водителя", "Door Electronics Driver", 0x74A, "LL_DoorElectDriveSideUDS"),
    _m(0x52, "Дверь пассажира", "Door Electronics Passenger", 0x74B, "LL_DoorElectPasseSideUDS"),
    _m(0xBB, "Дверь задняя левая", "Door Rear Driver", 0x73E, "LL_DoorElectRearDriveSideUDS"),
    _m(0xBC, "Дверь задняя правая", "Door Rear Passenger", 0x73F, "LL_DoorElectRearPasseSideUDS"),
    _m(0x44, "Усилитель руля (EPS)", "Steering Assist", 0x712, "LL_SteerAssisUDS"),
    _m(0x47, "Аудиосистема", "Sound System", 0x76F, "LL_SoundSysteUDS"),
    _m(0x53, "Стояночный тормоз (EPB)", "Parking Brake", 0x752, "LL_ParkiBrakeUDS"),
    _m(0x55, "Корректор фар", "Headlight Range", 0x754, "LL_HeadlRegulUDS"),
    _m(0x5F, "Мультимедиа MIB3 (MMI)", "Information Electronics", 0x773, "LL_InforContrUnit1UDS"),
    _m(0x61, "Регулирование АКБ", "Battery Regulation", 0x728, "LL_BatteRegulUDS"),
    _m(0x65, "Контроль давления в шинах", "Tire Pressure", 0x70B, "LL_TirePressMonit1UDS"),
    _m(0x6C, "Камера заднего вида", "Rear View Camera", 0x769, "LL_CamerSysteRearViewUDS"),
    _m(0x6D, "Крышка багажника", "Deck Lid", 0x723, "LL_DeckLidContrUnitUDS"),
    _m(0x75, "Телематика (Audi connect / eCall)", "Telematics", 0x767, "LL_TelemUDS"),
    _m(0x76, "Парктроник", "Park Assist", 0x70A, "LL_ParkiAssisUDS"),
    _m(0x77, "Телефон / OCU", "Telephone", 0x76B, "LL_TelepUDS"),
    _m(0x82, "Проекционный дисплей (HUD)", "Head Up Display", 0x71B, "LL_HeadUpDisplUDS"),
    _m(0x8B, "Адаптивный круиз 2", "Adaptive Cruise Control 2", 0x756, "LL_AdaptCruisContr2UDS"),
    _m(0xA5, "Фронтальная камера (ассистенты)", "Front Sensors Driver Assistance", 0x74F,
       "LL_FrontSensoDriveAssisSysteUDS"),
    _m(0xA9, "Генератор звука (Sound Actuator)", "Structure Borne Sound", 0x71C,
       "LL_ActuaForStrucBorneSoundUDS"),
    _m(0xCA, "Люк / панорамная крыша", "Sunroof", 0x72D, "LL_ElectRoofContrUDS"),
    _m(0x20, "Ассистент дальнего света", "High Beam Assist", 0x730, "LL_HighBeamAssisUDS"),
]

_BY_ADDR = {}
for _mod in MODULES:
    _BY_ADDR.setdefault(_mod.address, _mod)


def get_module(addr) -> Module:
    """Accept 9, '09', '0x09', '5F', 'A5' (VCDS addresses are hex)."""
    if isinstance(addr, Module):
        return addr
    if isinstance(addr, int):
        value = addr
    else:
        s = str(addr).strip()
        try:
            value = parse_int(s) if s.lower().startswith("0x") else parse_int("0x" + s)
        except ValueError:
            raise KeyError(f"неверный адрес блока {addr!r} (нужно 2 hex-цифры, например 09 или 5F)") from None
    try:
        return _BY_ADDR[value]
    except KeyError:
        raise KeyError(f"неизвестный блок {addr!r}; известные: "
                       + ", ".join(m.addr_hex for m in MODULES)) from None


def custom_module(address: int, tx_id: int, rx_id: int | None = None, name: str = "") -> Module:
    return Module(address, name or f"Блок {address:02X}", name or f"Module {address:02X}",
                  tx_id, rx_id if rx_id is not None else tx_id + 0x6A)
