"""Only the host may start a party-wide video selection.

`select_video` is a host action: it spends the host's Emby token and
replaces every viewer's stream. The UI hides the library from spectators,
but the socket event still has to reject them.
"""

import unittest

from backend.src.socket_handlers import playback as playback_module


class FakeSio:
    def __init__(self):
        self.handlers = {}
        self.emitted = []

    def on(self, event):
        def register(fn):
            self.handlers[event] = fn
            return fn
        return register

    async def emit(self, event, data=None, to=None, room=None, **kwargs):
        self.emitted.append({
            "event": event, "data": data, "to": to, "room": room,
        })

    def events(self, name):
        return [e for e in self.emitted if e["event"] == name]


class FakePartyManager:
    def __init__(self, party):
        self.party = party

    def get(self, party_id):
        return self.party if party_id == self.party["id"] else None

    def exists(self, party_id):
        return party_id == self.party["id"]

    def is_unlocked(self, party_id):
        return True


class FakeConfig:
    ENABLE_HLS_TOKEN_VALIDATION = False
    BINGE_WATCH_ENABLED = False
    BINGE_WATCH_COUNTDOWN_SECONDS = 4


class FakeLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass


def make_party():
    return {
        "id": "ABC12",
        "users": {"host-sid": "Host", "member-sid": "Member"},
        "sid_client_ids": {"host-sid": "host-client", "member-sid": "member-client"},
        "host_client_id": "host-client",
        "host_access_token": "token",
        "host_user_id": "user",
        "current_video": None,
        "playback_state": {"playing": False, "time": 0},
        "pending_auto_advance": None,
        "episode_list": None,
        "user_streams": {},
        "join_requests": {},
        "ready_check": None,
    }


class SelectVideoAuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.party = make_party()
        self.sio = FakeSio()
        self.ctx = {
            "sio": self.sio,
            "emby_client": object(),
            "config": FakeConfig(),
            "logger": FakeLogger(),
            "party_manager": FakePartyManager(self.party),
            "token_manager": object(),
            "session_secret": None,
        }
        playback_module.register(self.ctx)
        self.select_video = self.sio.handlers["select_video"]

    async def test_a_spectator_cannot_select_a_video(self):
        await self.select_video("member-sid", {
            "party_id": "ABC12",
            "item_id": "item-1",
            "item_name": "Movie",
        })

        errors = self.sio.events("error")
        self.assertTrue(errors)
        self.assertEqual(errors[-1]["to"], "member-sid")
        self.assertIn("Only the host", errors[-1]["data"]["message"])
        self.assertIsNone(self.party["current_video"])


if __name__ == "__main__":
    unittest.main()
