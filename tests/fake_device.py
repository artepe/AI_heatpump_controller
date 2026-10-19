"""A transport simulator. All state is synthetic, all routes are real.

Copied unchanged from the Mango Tuya Local test suite
(artepe/chinaheatpump-tuya-local-home-assistant, MIT).
"""

import base64
import struct


class Hardware:
    def __init__(self, profile):
        self.profile = profile
        self.values = {p.key: min(p.upper, max(p.lower, 0)) for p in profile.parameters.values()}
        self.values.update({"0:20": 2, "1:10": 1, "14:16": 30})
        self.original = dict(self.values)
        self.sent = []
        self.reads = self.writes = self.closes = 0
        self.plans = {}
        self.ignore_target = self.ignore_restore = self.fail_send = False
        self.abort_after_send = None
        self.status_error = False
        self.status_responses = []
        self.status_followups = []
        self.status_calls = 0
        self.factories = 0
        self.socket_timeouts = []
        self.private_status = False
        self.hold_read = None
        self.hold_only_after_write = False

    def factory(self, *args, **kwargs):
        assert kwargs["version"] == 3.5 and kwargs["persist"] is True
        self.factories += 1
        return Device(self)


class Device:
    def __init__(self, hw):
        self.hw = hw
        self.queue = []

    def set_socketTimeout(self, timeout):
        assert timeout in (1, 3)
        self.hw.socket_timeouts.append(timeout)

    def close(self):
        self.hw.closes += 1

    def status(self):
        self.hw.status_calls += 1
        if self.hw.status_followups:
            self.queue.extend(self.hw.status_followups)
            self.hw.status_followups = []
        if self.hw.status_responses:
            return self.hw.status_responses.pop(0)
        if self.hw.status_error:
            return {"Error": "PRIVATE_STATUS_PAYLOAD", "Err": "901"}
        return {"dps": {"102": "PRIVATE_VALUE"}} if self.hw.private_status else {"dps": {}}

    def set_value(self, dp, value, nowait=False):
        assert dp == 117 and nowait is True
        raw = base64.b64decode(value, validate=True)
        assert len(raw) == 244 and raw[0] == 1
        self.hw.sent.append(raw)
        if raw[4] == 4:
            assert raw[4:7] == b"\x04\x00\x00" and not any(raw[11:])
            self.hw.writes += 1
            key = f"{raw[7]}:{raw[8]}"
            value = struct.unpack_from(">h", raw, 9)[0]
            if not (
                (self.hw.writes == 1 and self.hw.ignore_target)
                or self.hw.writes > 1
                and self.hw.ignore_restore
            ):
                self.hw.values[key] = value
            if self.hw.writes == 1 and self.hw.abort_after_send is not None:
                self.hw.abort_after_send.set()
            if self.hw.writes == 1 and self.hw.fail_send:
                raise OSError("PRIVATE_TRANSPORT_PAYLOAD")
            return None
        assert raw[4:7] == b"\x03\x04\x00" and not any(raw[8:])
        self.hw.reads += 1
        group = raw[7]
        section = self.hw.profile.sections[group]
        action = self.hw.plans.get(self.hw.reads)
        self.queue = [{"dps": {"102": "PRIVATE_VALUE"}}]
        if action == "missing":
            return None
        for page in range(section["page_count"]):
            frame = bytearray(244)
            header = b"old" if action == "wrong_header" else raw[1:4]
            frame[:9] = (
                b"\x01" + header + bytes((section["page_count"], page, 5, group, section["declared_count"]))
            )
            for index, ident in enumerate(section["record_ids_in_order"][page * 23 : page * 23 + 23]):
                slot = page * 23 + index
                key = f"{group}:{slot}"
                p = self.hw.profile.parameters.get(key)
                flags, tag, lower, upper, current = (
                    (0, 0, 0, 0, 0) if p is None else (p.flags, p.tag, p.lower, p.upper, self.hw.values[key])
                )
                if action == "stale":
                    current = self.hw.original.get(key, 0)
                if action == "narrow_bounds" and key == "1:10":
                    lower = 0
                if action == "changed_metadata" and key in ("1:10", "14:16"):
                    flags ^= 1
                struct.pack_into(">HBBhhh", frame, 9 + index * 10, ident, flags, tag, current, upper, lower)
            if page == 0 and action == "wrong_id":
                frame[9] ^= 1
            if action == "wrong_padding":
                frame[-1] = 1
            self.queue.append({"dps": {str(108 + page): base64.b64encode(frame).decode()}})
        self.queue.reverse()
        return None

    def receive(self):
        if self.hw.hold_read is not None and (not self.hw.hold_only_after_write or self.hw.writes == 1):
            started, release = self.hw.hold_read
            started.set()
            release.wait(5)
            self.hw.hold_read = None
        return self.queue.pop(0) if self.queue else None
