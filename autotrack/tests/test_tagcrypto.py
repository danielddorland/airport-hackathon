"""
Tests for the tag crypto (and the no-hardware parts of real_ble.py).

Run either way, from the autotrack folder:
    .venv\\Scripts\\python -m pytest tests/test_tagcrypto.py
    .venv\\Scripts\\python tests/test_tagcrypto.py
No Bluetooth or ESP32 needed.
"""

import asyncio
import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from autotrack import tagcrypto as tc  # noqa: E402

KEY = bytes.fromhex("807caa672f37e3f1106012fc70a608d8")   # TAG-0001
OTHER_KEYS = {
    "TAG-0002": bytes(range(16)),
    "TAG-0003": bytes(range(16, 32)),
    "TAG-0004": b"\xff" * 16,
}


# --------------------------------------------------------------- tagcrypto

def test_known_answer_vectors_match_sketch():
    # These hex strings are ALSO hard-coded in autotrack_tag.ino, which prints
    # them at boot. If this test passes and the tag prints PASS, both agree.
    assert KEY.hex() == tc.KAT_KEY_HEX
    assert tc.make_adv_payload(KEY, 0).hex() == "0100000000060d2c50a73d7a20"
    assert tc.make_adv_payload(KEY, 0).hex() == tc.KAT_ADV_PAYLOAD_HEX
    assert tc.make_command(KEY, 1, 100000).hex() == "01000186a03c1c96a94134cf97"
    assert tc.make_command(KEY, 1, 100000).hex() == tc.KAT_CMD_HEX


def test_key_file_matches_sketch_key():
    keys = tc.load_keys(os.path.join(ROOT, "data", "tag_keys.json"))
    assert keys["TAG-0001"] == KEY


def test_round_trip():
    for counter in (0, 1, 100000, 123456789, 0xFFFFFFFF):
        payload = tc.make_adv_payload(KEY, counter)
        assert len(payload) == 13 and payload[0] == 0x01
        parsed = tc.parse_adv_payload(payload)
        assert parsed is not None
        got_counter, mac = parsed
        assert got_counter == counter
        assert tc.verify(KEY, got_counter, mac)


def test_wrong_key_rejected():
    payload = tc.make_adv_payload(KEY, 100042)
    counter, mac = tc.parse_adv_payload(payload)
    assert not tc.verify(OTHER_KEYS["TAG-0002"], counter, mac)
    assert tc.resolve(payload, OTHER_KEYS) is None


def test_tampered_byte_rejected():
    payload = tc.make_adv_payload(KEY, 100042)
    for i in range(1, len(payload)):          # every counter and mac byte
        bad = bytearray(payload)
        bad[i] ^= 0x01
        assert tc.resolve(bytes(bad), {"TAG-0001": KEY}) is None, i
    bad = bytearray(payload)
    bad[0] = 0x02                              # unknown version
    assert tc.parse_adv_payload(bytes(bad)) is None


def test_bad_shapes_rejected():
    assert tc.parse_adv_payload(None) is None
    assert tc.parse_adv_payload(b"") is None
    assert tc.parse_adv_payload(b"\x01" * 12) is None
    assert tc.parse_adv_payload(b"\x01" * 14) is None


def test_resolve_picks_right_serial():
    keys = dict(OTHER_KEYS)
    keys["TAG-0001"] = KEY
    assert tc.resolve(tc.make_adv_payload(KEY, 200001), keys) == "TAG-0001"
    assert tc.resolve(tc.make_adv_payload(OTHER_KEYS["TAG-0003"], 5), keys) == "TAG-0003"


def test_command_hmac():
    cmd = tc.make_command(KEY, tc.MODE_WRONG_AIRCRAFT, 100500)
    assert len(cmd) == 13 and cmd[0] == 1
    assert int.from_bytes(cmd[1:5], "big") == 100500
    # accepted inside the 60 s window, both sides
    assert tc.check_command(KEY, cmd, 100500) == tc.RESULT_OK
    assert tc.check_command(KEY, cmd, 100560) == tc.RESULT_OK
    assert tc.check_command(KEY, cmd, 100440) == tc.RESULT_OK
    # too old / too new
    assert tc.check_command(KEY, cmd, 100561) == tc.RESULT_STALE_COUNTER
    assert tc.check_command(KEY, cmd, 100439) == tc.RESULT_STALE_COUNTER
    # wrong key, tampered mode, tampered counter, wrong length
    assert tc.check_command(OTHER_KEYS["TAG-0002"], cmd, 100500) == tc.RESULT_BAD_MAC
    bad = bytearray(cmd); bad[0] = 0
    assert tc.check_command(KEY, bytes(bad), 100500) == tc.RESULT_BAD_MAC
    bad = bytearray(cmd); bad[4] ^= 1
    assert tc.check_command(KEY, bytes(bad), 100500) == tc.RESULT_BAD_MAC
    assert tc.check_command(KEY, cmd[:12], 100500) == tc.RESULT_BAD_LENGTH
    # an advertisement mac is never a valid command mac (different label)
    assert tc.command_mac(KEY, 1, 7) != tc.adv_mac(KEY, 7)
    try:
        tc.make_command(KEY, 9, 1)
        raise AssertionError("mode 9 should be refused")
    except ValueError:
        pass


def test_parse_status():
    st = tc.parse_status(bytes([1, 0]) + (100123).to_bytes(4, "big"))
    assert st == {"mode": 1, "result": 0, "counter": 100123}
    assert tc.parse_status(b"\x00") is None


# --------------------------------------------------- real_ble (no bleak needed)

def _fake_adv(payload, rssi=-60, address="AA:BB:CC:DD:EE:FF"):
    device = SimpleNamespace(address=address, name=None)
    adv = SimpleNamespace(manufacturer_data={0xFFFF: payload}, rssi=rssi,
                          local_name=None, service_uuids=[])
    return device, adv


def test_real_ble_verifies_and_rejects_replays():
    from autotrack.real_ble import RealBLE
    seen = []
    keys = dict(OTHER_KEYS)
    keys["TAG-0001"] = KEY
    ble = RealBLE("belt-1", keys, on_sighting=seen.append, log=lambda m: None)

    ble._on_advertisement(*_fake_adv(tc.make_adv_payload(KEY, 100010), rssi=-55))
    ble._on_advertisement(*_fake_adv(tc.make_adv_payload(KEY, 100010), rssi=-57))  # same second: OK
    ble._on_advertisement(*_fake_adv(tc.make_adv_payload(KEY, 100011), rssi=-58))
    ble._on_advertisement(*_fake_adv(tc.make_adv_payload(KEY, 100005)))            # replay: drop
    ble._on_advertisement(*_fake_adv(tc.make_adv_payload(b"x" * 16, 100012)))       # unknown key
    forged = bytearray(tc.make_adv_payload(KEY, 100013)); forged[-1] ^= 1
    ble._on_advertisement(*_fake_adv(bytes(forged)))                                # forged
    # an event with only a name / no manufacturer data (Windows scan response)
    ble._on_advertisement(SimpleNamespace(address="AA:BB:CC:DD:EE:FF", name="AT"),
                          SimpleNamespace(manufacturer_data={}, rssi=-50, local_name="AT"))

    assert [s["counter"] for s in seen] == [100010, 100010, 100011]
    assert [s["rssi"] for s in seen] == [-55, -57, -58]
    assert all(s["tag_serial"] == "TAG-0001" and s["receiver"] == "belt-1" for s in seen)
    assert set(seen[0]) == {"receiver", "tag_serial", "rssi", "t", "counter"}
    assert ble.status["tags"]["TAG-0001"]["counter"] == 100011


def test_real_ble_newest_mode_wins():
    """One write in flight per tag; requests made meanwhile collapse to the newest."""
    from autotrack.real_ble import RealBLE

    async def scenario():
        ble = RealBLE("belt-1", {"TAG-0001": KEY}, on_sighting=lambda s: None,
                      log=lambda m: None)
        ble._on_advertisement(*_fake_adv(tc.make_adv_payload(KEY, 100010)))
        written = []

        async def fake_connect_write(serial, device, mode):  # stands in for Bluetooth
            await asyncio.sleep(0.05)
            written.append(mode)
            return True, "accepted by tag"

        ble._connect_write = fake_connect_write
        t1 = asyncio.ensure_future(ble.set_alert("TAG-0001", 1))
        await asyncio.sleep(0.01)            # mode 1 is now in flight
        t2 = asyncio.ensure_future(ble.set_alert("TAG-0001", 0))
        await asyncio.sleep(0)
        t3 = asyncio.ensure_future(ble.set_alert("TAG-0001", 3))
        results = await asyncio.gather(t1, t2, t3)
        assert written == [1, 3], written
        assert results == [True, False, True], results
        assert ble.status["last_write"]["mode"] == 3
        assert await ble.set_alert("TAG-9999", 1) is False      # unknown tag
        await ble.stop()

    asyncio.run(scenario())


def test_real_ble_imports_without_bleak_and_start_never_raises():
    """start() must not raise even when bleak is missing or Bluetooth is off."""
    import builtins
    from autotrack.real_ble import RealBLE
    real_import = builtins.__import__

    def no_bleak(name, *args, **kwargs):
        if name == "bleak" or name.startswith("bleak."):
            raise ImportError("pretend bleak is missing")
        return real_import(name, *args, **kwargs)

    async def scenario():
        ble = RealBLE("belt-1", {"TAG-0001": KEY}, on_sighting=lambda s: None,
                      log=lambda m: None)
        builtins.__import__ = no_bleak
        try:
            await ble.start()
        finally:
            builtins.__import__ = real_import
        assert ble.status["scanning"] is False
        assert "bleak" in ble.status["error"]
        await ble.stop()

    asyncio.run(scenario())


if __name__ == "__main__":
    failed = 0
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        try:
            fn()
            print("PASS", name)
        except Exception as e:  # show every failure, not just the first
            failed += 1
            print("FAIL", name, "->", type(e).__name__, e)
    print("\n{} passed, {} failed".format(len(tests) - failed, failed))
    print("Tag key self-test vector (counter 0):", tc.KAT_ADV_PAYLOAD_HEX)
    print("Command vector (mode 1, counter 100000):", tc.KAT_CMD_HEX)
    sys.exit(1 if failed else 0)
