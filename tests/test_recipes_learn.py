import json
import os

import pytest

from vagon.app import SIM_LABELS
from vagon.errors import SafetyError
from vagon.recipes import RecipeBook, apply_plan, build_learned_recipe, plan_recipe, recipe_from_json, save_user_recipe
from vagon.vag.labels import LabelDB
from vagon.vag.learn import LearnSession, infer_channel, sweep

from .conftest import ecu


@pytest.fixture
def labels():
    return LabelDB.load_default(extra_files=[SIM_LABELS])


def test_bundled_recipes_valid():
    book = RecipeBook.load_default(include_user=False)
    ids = [r.id for r in book.list()]
    assert "mmi.wireless_carplay" in ids and "lights.rs3_taillight_animation" in ids
    assert len(ids) == len(set(ids))
    for r in book.list():
        assert r.sources, f"{r.id} has no sources"
        assert r.title.get("ru") and r.title.get("en")


def test_bundled_labels_never_ship_real_car_guesses():
    """Real-car channel→DID data must come from the user's car (learn/ODX), not from us."""
    from vagon.paths import package_data
    folder = package_data("labels")
    assert not os.path.isdir(folder) or not [f for f in os.listdir(folder) if f.endswith(".json")]
    data = json.load(open(SIM_LABELS, encoding="utf-8"))
    assert all("SIM" in k for k in data["datasets"])


def test_recipe_validation():
    with pytest.raises(ValueError):
        recipe_from_json({"id": "x", "module": "09", "changes": [{"kind": "nope", "on": 1}]})
    with pytest.raises(ValueError):
        recipe_from_json({"id": "x", "module": "09", "changes": []})
    with pytest.raises(ValueError):
        recipe_from_json({"id": "x", "module": "09", "changes": [{"kind": "coding_bit", "bit": 1, "on": 1}]})
    with pytest.raises(KeyError):
        recipe_from_json({"id": "x", "module": "ZZ", "changes": [{"kind": "channel", "channel": "a", "on": 1}]})


def test_coding_bit_recipe_on_off(sim, vehicle, labels):
    r = RecipeBook.load_default().get("cockpit.needle_sweep")
    e = ecu(sim, 0x17)
    e.dids[0x0600] = bytes([0x00]) * 20
    p = plan_recipe(vehicle, r, "on", labels)
    assert p.state == "off" and not p.blockers
    apply_plan(vehicle, p)
    assert e.dids[0x0600][1] & 0x01 == 1
    p2 = plan_recipe(vehicle, r, "on", labels)
    assert p2.state == "on" and p2.nothing_to_do
    apply_plan(vehicle, plan_recipe(vehicle, r, "off", labels))
    assert e.dids[0x0600][1] & 0x01 == 0


def test_channel_recipe_with_sim_labels(sim, vehicle, labels):
    r = RecipeBook.load_default().get("mmi.wireless_carplay")
    p = plan_recipe(vehicle, r, "on", labels)
    assert not p.blockers, p.blockers
    apply_plan(vehicle, p)
    e = ecu(sim, 0x5F)
    assert e.dids[0x0960] == b"\x03" and e.dids[0x0961] == b"\x01" and e.dids[0x0963] == b"\x07"
    assert plan_recipe(vehicle, r, "on", labels).state == "on"


def test_channel_recipe_blocked_without_labels(vehicle):
    r = RecipeBook.load_default().get("mmi.wireless_carplay")
    p = plan_recipe(vehicle, r, "on", LabelDB.load_default())
    assert p.blockers and any("выучите" in b for b in p.blockers)
    with pytest.raises(SafetyError):
        apply_plan(vehicle, p)


def test_optional_channels_only_warn(vehicle, labels):
    r = recipe_from_json({"id": "t", "module": "5F", "sources": ["x"], "changes": [
        {"kind": "channel", "channel": "5_ghz_wlan", "on": "on"},
        {"kind": "channel", "channel": "does_not_exist", "on": "on", "optional": True},
        {"kind": "channel", "channel": "also_missing", "alt": ["Apple_DIO"], "on": "activated"}]})
    p = plan_recipe(vehicle, r, "on", labels)
    assert not p.blockers and any("необязательный" in w for w in p.warnings)
    assert len(p.modules[0].lines) == 2


def test_revert_from_backup_when_no_off(sim, vehicle, labels):
    r = RecipeBook.load_default().get("lights.rs3_taillight_animation")
    e = ecu(sim, 0x09)
    assert e.dids[0x0902] == b"\x01"
    apply_plan(vehicle, plan_recipe(vehicle, r, "on", labels))
    assert e.dids[0x0902] == b"\x05"
    apply_plan(vehicle, plan_recipe(vehicle, r, "off", labels))
    assert e.dids[0x0902] == b"\x01"


def test_requires_blocks_mismatch(vehicle, labels):
    r = recipe_from_json({"id": "t", "module": "17", "sources": ["x"],
                          "requires": {"dataset_prefix": ["EV_SOMETHING_ELSE"]},
                          "changes": [{"kind": "coding_bit", "byte": 0, "bit": 0, "on": 1, "off": 0}]})
    p = plan_recipe(vehicle, r, "on", labels)
    assert p.blockers and "датасет" in p.blockers[0]


def test_byte_out_of_range_blocks(vehicle, labels):
    r = recipe_from_json({"id": "t", "module": "44", "sources": ["x"],
                          "changes": [{"kind": "coding_bit", "byte": 10, "bit": 0, "on": 1, "off": 0}]})
    assert plan_recipe(vehicle, r, "on", labels).blockers


def test_session_from_label(sim, vehicle, labels):
    r = RecipeBook.load_default().get("mmi.developer_mode")
    p = plan_recipe(vehicle, r, "on", labels)
    assert p.modules[0].session == 0x4F and p.modules[0].login == 20103
    apply_plan(vehicle, p)
    assert ecu(sim, 0x5F).dids[0x0970] == b"\x01"


def test_sweep_and_learn_cycle(sim, vehicle):
    s = vehicle.module("09")
    data = sweep(s, [(0x0900, 0x090F), (0x0600, 0x0600)], vin="VIN")
    assert {"0x0901", "0x0902", "0x0903", "0x0904", "0x0600"} <= set(data["dids"])
    ls = LearnSession.start(s, [int(k, 16) for k in data["dids"]], "VIN")
    ecu(sim, 0x09).dids[0x0904] = b"\x0B"          # 'another tool' clears bit 2
    changes = LearnSession.load("09").finish(s)
    assert [(c.did, c.old, c.new) for c in changes] == [(0x0904, b"\x0F", b"\x0B")]
    ch = infer_channel(changes[0], "LTM_Global_OFF_Wegabhängig", "active", "not active")
    assert (ch.did, ch.bit, ch.bits) == (0x0904, 2, 1)
    rec = build_learned_recipe("user.t", "T", "09", changes, s.ident())
    path = save_user_recipe(rec)
    book = RecipeBook.load_default()
    r = book.get("user.t")
    assert r.status == "user" and r.origin == path
    assert ls.data["module"] == "09"


def test_learned_recipe_replay(sim, vehicle, labels):
    s = vehicle.module("09")
    e = ecu(sim, 0x09)
    LearnSession.start(s, [0x0902, 0x0904], "VIN")
    e.dids[0x0902] = b"\x05"
    e.dids[0x0904] = b"\x0A"
    changes = LearnSession.load("09").finish(s)
    rec = build_learned_recipe("user.combo", "Combo", "09", changes, s.ident())
    apply_plan(vehicle, plan_recipe(vehicle, rec, "off", labels))
    assert e.dids[0x0902] == b"\x01" and e.dids[0x0904] == b"\x0F"
    apply_plan(vehicle, plan_recipe(vehicle, rec, "on", labels))
    assert e.dids[0x0902] == b"\x05" and e.dids[0x0904] == b"\x0A"


def test_learn_ignores_workshop_dids(sim, vehicle):
    s = vehicle.module("09")
    LearnSession.start(s, [0x0902, 0xF198, 0xF199], "VIN")
    e = ecu(sim, 0x09)
    e.dids[0xF199] = b"\x26\x10\x24"     # another tester wrote its date/code
    e.dids[0xF198] = b"\x01\x02\x03\x04\x05\x06"
    e.dids[0x0902] = b"\x05"
    changes = LearnSession.load("09").finish(s)
    assert [c.did for c in changes] == [0x0902]


def test_learned_recipe_keeps_dataset_guard(sim, vehicle, labels):
    s = vehicle.module("09")
    LearnSession.start(s, [0x0902], "VIN")
    ecu(sim, 0x09).dids[0x0902] = b"\x05"
    changes = LearnSession.load("09").finish(s)
    save_user_recipe(build_learned_recipe("user.guard", "G", "09", changes, s.ident()))
    r = RecipeBook.load_default().get("user.guard")
    assert r.requires == {"09": {"dataset_prefix": ["EV_BCMSIM_AU38X"]}}
    ecu(sim, 0x09).dids[0xF19E] = b"EV_OTHER\x00"
    vehicle.module("09")._ident = None
    assert any("датасет" in b for b in plan_recipe(vehicle, r, "on", labels).blockers)


def test_revert_uses_only_this_cars_backup(sim, vehicle, labels):
    from vagon.sim.link import SimLink
    from vagon.transport.elm327 import Elm327
    from vagon.vag.store import Store
    from vagon.vag.vehicle import Vehicle
    r = RecipeBook.load_default().get("lights.rs3_taillight_animation")
    e = ecu(sim, 0x09)
    e.dids[0x0902] = b"\x03"
    apply_plan(vehicle, plan_recipe(vehicle, r, "on", labels))
    vehicle.store.save_backup(vin="WAUOTHERCAR000001", module="09", ident={}, coding=None,
                              dids={"0x0902": "01"}, reason="recipe lights.rs3_taillight_animation on")
    v2 = Vehicle(Elm327(SimLink(sim)).connect(), Store())       # like a fresh CLI run (VIN unknown)
    apply_plan(v2, plan_recipe(v2, r, "off", labels))
    assert e.dids[0x0902] == b"\x03"


def test_recipe_validation_strict():
    base = {"id": "x", "module": "09", "sources": ["s"]}
    bad_changes = [
        {"kind": "coding_bit", "byte": -1, "bit": 0, "on": 1},
        {"kind": "coding_bit", "byte": 0, "bit": 8, "on": 1},
        {"kind": "did_bits", "did": "0x0901", "byte": 0, "mask": "0x01", "on": "0x02"},
        {"kind": "did_bytes", "did": "0x0901", "offset": -2, "on": "00"},
        {"kind": "did_bytes", "did": "0x0901", "on": "0000", "off": "00"},
    ]
    for c in bad_changes:
        with pytest.raises(ValueError):
            recipe_from_json({**base, "changes": [c]})
    ok = {"kind": "coding_bit", "byte": 0, "bit": 0, "on": 1, "off": 0}
    with pytest.raises(ValueError):
        recipe_from_json({**base, "requires": {"dataset_prefix": "EV_WRONG"}, "changes": [ok]})
    with pytest.raises(ValueError):
        recipe_from_json({**base, "requires": {"datset_prefix": ["EV"]}, "changes": [ok]})
    r = recipe_from_json({**base, "requires": {"0x17": {"dataset_prefix": ["EV_K"]}}, "changes": [ok]})
    assert r.requires == {"17": {"dataset_prefix": ["EV_K"]}}


def test_labels_need_dataset_and_bad_user_files_tolerated(vagon_home):
    from vagon.errors import VagOnError
    from vagon.vag.labels import Channel, LabelDB, save_user_channel
    with pytest.raises(VagOnError, match="датасет"):
        save_user_channel("5F", "", Channel("Developer mode", 0x0970, bits=1, values={"a": 0, "b": 1}))
    save_user_channel("5F", "EV_X", Channel("Developer mode", 0x0970, bits=1, values={"a": 0, "b": 1}))
    with pytest.raises(VagOnError, match="уже привязан"):
        save_user_channel("09", "EV_X", Channel("Other", 0x0901, bits=1, values={"a": 0, "b": 1}))
    bad = vagon_home / "labels" / "broken.json"
    bad.write_text('{"vagon_labels": 1, "datasets": {"EV": {"channels": [{"name": "x", "did": "0x1", "bit": 9}]}}}')
    db = LabelDB.load_default()
    assert db.load_errors and db.find("5F", "EV_X", "developer mode") is not None
    (vagon_home / "recipes").mkdir(exist_ok=True)
    (vagon_home / "recipes" / "broken.json").write_text("{not json")
    book = RecipeBook.load_default()
    assert book.load_errors and "mmi.wireless_carplay" in book.recipes
