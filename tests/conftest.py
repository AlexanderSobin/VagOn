import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vagon.sim.car import make_sim_elm  # noqa: E402
from vagon.sim.link import SimLink  # noqa: E402
from vagon.transport.elm327 import Elm327  # noqa: E402
from vagon.vag.store import Store  # noqa: E402
from vagon.vag.vehicle import Vehicle  # noqa: E402


@pytest.fixture(autouse=True)
def vagon_home(tmp_path, monkeypatch):
    home = tmp_path / "vagon_home"
    monkeypatch.setenv("VAGON_HOME", str(home))
    monkeypatch.delenv("VAGON_ADAPTER", raising=False)
    return home


@pytest.fixture
def sim():
    return make_sim_elm()


@pytest.fixture
def elm(sim):
    return Elm327(SimLink(sim)).connect()


@pytest.fixture
def vehicle(elm):
    return Vehicle(elm, Store())


def ecu(sim, addr: int):
    return next(e for e in sim.ecus if e.address == addr)
