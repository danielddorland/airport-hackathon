"""
tagcrypto.py - the "secret handshake" between the server and the AutoTrack tag
==============================================================================

Pure Python standard library (hmac + hashlib). No Bluetooth code in here, so it
can be imported and tested on any computer.

THE IDEA IN PLAIN ENGLISH
-------------------------
Every tag has a secret 16-byte KEY. Only the tag and the server know it
(the server keeps them in data/tag_keys.json, the tag has it in its sketch).

1) What the tag broadcasts (about once per second it changes):

     manufacturer data, company ID 0xFFFF ("test / unassigned")
     payload (13 bytes) = version  (1 byte, always 0x01)
                        + counter  (4 bytes, big-endian, goes up by 1 every second)
                        + mac      (8 bytes) = first 8 bytes of
                                   HMAC-SHA256(key, b"AT1" + counter as 4 bytes)

   The tag's serial number (e.g. "TAG-0001") is NEVER broadcast. Somebody
   listening only sees a number that goes up and 8 random-looking bytes.
   The server finds out WHICH tag it is by trying every key it knows
   (see resolve()). Without the key you cannot fake a valid broadcast.

2) What the server writes to the tag to make it flash / beep (a "command"):

     command (13 bytes) = mode    (1 byte: 0 off, 1 WRONG AIRCRAFT, 2 OK, 3 locate)
                        + counter (4 bytes, big-endian, the latest counter the
                                   server saw the tag broadcast)
                        + mac     (8 bytes) = first 8 bytes of
                                   HMAC-SHA256(key, b"CMD" + mode + counter)

   The tag only obeys if the mac is right AND the counter is within 60 of the
   tag's own current counter (so an old recorded command cannot be replayed
   much later).

3) After a write, the server can READ the same characteristic to get a
   6-byte status from the tag: mode, result code, current counter.
   See parse_status().

The tag-side code (firmware/autotrack_tag/autotrack_tag.ino) does exactly the
same maths with mbedtls. At boot it prints the "known answer" test vectors
below so you can check both sides agree.
"""

import hashlib
import hmac
import json
from typing import Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants shared with the sketch. If you change one, change it there too!
# ---------------------------------------------------------------------------

COMPANY_ID = 0xFFFF          # Bluetooth SIG "test / not assigned" company ID
VERSION = 0x01               # first byte of the advertising payload
ADV_PAYLOAD_LEN = 13         # 1 version + 4 counter + 8 mac
COMMAND_LEN = 13             # 1 mode + 4 counter + 8 mac
MAC_LEN = 8                  # we keep the first 8 bytes of the 32-byte HMAC
COUNTER_WINDOW = 60          # tag accepts commands whose counter is within 60 s

ADV_LABEL = b"AT1"           # "domain separation" labels: an advertisement mac
CMD_LABEL = b"CMD"           # can never be re-used as a command mac

# GATT UUIDs of the tag (used by real_ble.py to connect and write commands).
SERVICE_UUID = "6b570001-7d42-4147-a56d-8061e50f3042"
COMMAND_CHAR_UUID = "6b570002-7d42-4147-a56d-8061e50f3042"

# Alert modes
MODE_OFF = 0                 # normal: faint green heartbeat every 3 s
MODE_WRONG_AIRCRAFT = 1      # fast red flash + beeping until MODE_OFF
MODE_OK = 2                  # two green blinks ("loading confirmed")
MODE_LOCATE = 3              # blue blinks + chirps for about 5 s
MODE_NAMES = {0: "off", 1: "WRONG AIRCRAFT", 2: "OK", 3: "locate"}

# Result codes the tag reports in its status (byte 1)
RESULT_OK = 0
RESULT_BAD_LENGTH = 1
RESULT_BAD_MAC = 2
RESULT_STALE_COUNTER = 3     # counter more than 60 away from the tag's counter
RESULT_BAD_MODE = 4
RESULT_OLDER_THAN_LAST = 5   # counter lower than the last command the tag accepted
RESULT_NONE_YET = 0xFF       # no command received since boot
RESULT_NAMES = {
    0: "accepted",
    1: "bad length",
    2: "bad mac (wrong key?)",
    3: "counter too old/new",
    4: "unknown mode",
    5: "older than last accepted command",
    0xFF: "no command yet",
}

# ---------------------------------------------------------------------------
# Known-answer test vectors for TAG-0001's key. These EXACT values are also
# hard-coded in the sketch, which prints PASS/FAIL for them at boot.
# ---------------------------------------------------------------------------
KAT_KEY_HEX = "807caa672f37e3f1106012fc70a608d8"
KAT_ADV_COUNTER = 0
KAT_ADV_PAYLOAD_HEX = "0100000000060d2c50a73d7a20"       # counter 0
KAT_CMD_MODE = 1
KAT_CMD_COUNTER = 100000
KAT_CMD_HEX = "01000186a03c1c96a94134cf97"               # mode 1, counter 100000


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _check_counter(counter: int) -> None:
    if not isinstance(counter, int) or not 0 <= counter <= 0xFFFFFFFF:
        raise ValueError("counter must be an int between 0 and 4294967295")


def _check_key(key: bytes) -> None:
    if not isinstance(key, (bytes, bytearray)) or len(key) == 0:
        raise ValueError("key must be non-empty bytes (16 bytes expected)")


def _mac(key: bytes, message: bytes) -> bytes:
    """First 8 bytes of HMAC-SHA256(key, message)."""
    return hmac.new(bytes(key), message, hashlib.sha256).digest()[:MAC_LEN]


def adv_mac(key: bytes, counter: int) -> bytes:
    """The 8-byte mac a tag with this key broadcasts for this counter."""
    _check_key(key)
    _check_counter(counter)
    return _mac(key, ADV_LABEL + counter.to_bytes(4, "big"))


# ---------------------------------------------------------------------------
# Advertisement (tag -> server)
# ---------------------------------------------------------------------------

def make_adv_payload(key: bytes, counter: int) -> bytes:
    """Build the 13-byte manufacturer payload the tag broadcasts.

    This is the part AFTER the 2-byte company ID (bleak gives you exactly this
    part in AdvertisementData.manufacturer_data[0xFFFF]).
    """
    return bytes([VERSION]) + counter.to_bytes(4, "big") + adv_mac(key, counter)


def parse_adv_payload(data) -> Optional[Tuple[int, bytes]]:
    """Split a manufacturer payload into (counter, mac).

    Returns None if it is not an AutoTrack v1 payload (wrong length or version).
    Does NOT check the mac - use verify() or resolve() for that.
    """
    if data is None:
        return None
    try:
        data = bytes(data)
    except (TypeError, ValueError):
        return None
    if len(data) != ADV_PAYLOAD_LEN or data[0] != VERSION:
        return None
    counter = int.from_bytes(data[1:5], "big")
    mac = data[5:13]
    return counter, mac


def verify(key: bytes, counter: int, mac: bytes) -> bool:
    """True if `mac` is the correct advertisement mac for this key + counter."""
    try:
        expected = adv_mac(key, counter)
    except ValueError:
        return False
    # compare_digest takes the same time whether the first byte or the last
    # byte is wrong, so an attacker cannot guess the mac byte by byte.
    return hmac.compare_digest(expected, bytes(mac))


def resolve(payload, keys: Dict[str, bytes]) -> Optional[str]:
    """Find which tag sent this payload.

    payload : the 13-byte manufacturer payload (after the company ID)
    keys    : {"TAG-0001": b"<16 bytes>", ...}
    returns : the serial whose key makes the mac valid, or None.
    """
    parsed = parse_adv_payload(payload)
    if parsed is None:
        return None
    counter, mac = parsed
    for serial, key in keys.items():
        if verify(key, counter, mac):
            return serial
    return None


# ---------------------------------------------------------------------------
# Commands (server -> tag)
# ---------------------------------------------------------------------------

def command_mac(key: bytes, mode: int, counter: int) -> bytes:
    _check_key(key)
    _check_counter(counter)
    return _mac(key, CMD_LABEL + bytes([mode]) + counter.to_bytes(4, "big"))


def make_command(key: bytes, mode: int, counter: int) -> bytes:
    """Build the 13-byte command to write to the tag's command characteristic.

    mode    : 0 off, 1 WRONG AIRCRAFT, 2 OK, 3 locate
    counter : the latest counter the server saw this tag broadcast
    """
    if mode not in MODE_NAMES:
        raise ValueError("mode must be 0, 1, 2 or 3")
    return bytes([mode]) + counter.to_bytes(4, "big") + command_mac(key, mode, counter)


def check_command(key: bytes, command, tag_counter: int) -> int:
    """Python copy of the check the TAG does (used by tests only).

    Returns one of the RESULT_* codes. (The real tag additionally rejects
    commands older than the last one it accepted.)
    """
    command = bytes(command)
    if len(command) != COMMAND_LEN:
        return RESULT_BAD_LENGTH
    mode = command[0]
    counter = int.from_bytes(command[1:5], "big")
    if not hmac.compare_digest(command_mac(key, mode, counter), command[5:13]):
        return RESULT_BAD_MAC
    if mode not in MODE_NAMES:
        return RESULT_BAD_MODE
    if abs(tag_counter - counter) > COUNTER_WINDOW:
        return RESULT_STALE_COUNTER
    return RESULT_OK


def parse_status(data) -> Optional[Dict[str, int]]:
    """Decode the 6-byte status the tag returns when you READ the characteristic.

    byte 0   : the tag's mode (0-3)
    byte 1   : result of the last command (RESULT_* code)
    byte 2-5 : the tag's current counter (big-endian)
    """
    if data is None:
        return None
    data = bytes(data)
    if len(data) < 6:
        return None
    return {
        "mode": data[0],
        "result": data[1],
        "counter": int.from_bytes(data[2:6], "big"),
    }


# ---------------------------------------------------------------------------
# Key file
# ---------------------------------------------------------------------------

def load_keys(path) -> Dict[str, bytes]:
    """Read data/tag_keys.json ({"TAG-0001": "<32 hex chars>"}) into
    {"TAG-0001": b"<16 bytes>"}."""
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    keys = {}
    for serial, hex_key in raw.items():
        key = bytes.fromhex(hex_key)
        if len(key) != 16:
            raise ValueError("key for {} must be 32 hex characters".format(serial))
        keys[serial] = key
    return keys
