"""State-machine tests for the host-approved late-join queue.

`backend/src/socket_handlers/party.py` registers its handlers against a
Socket.IO server, but the queue logic itself only needs `emit`,
`enter_room`, `leave_room`, and `get_environ`. These tests drive the
registered handlers with a fake server, so they cover the queue without
a live socket connection, Emby, or the optional `socketio` package.
"""

import asyncio
import unittest
from datetime import datetime

from backend.src.socket_handlers import party as party_module


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

    async def enter_room(self, sid, room):
        pass

    async def leave_room(self, sid, room):
        pass

    def get_environ(self, sid):
        return {}

    def events(self, name):
        return [e for e in self.emitted if e["event"] == name]


class FakePartyManager:
    def __init__(self, party):
        self.party = party

    def get(self, party_id):
        return self.party if party_id == self.party["id"] else None

    def exists(self, party_id):
        return party_id == self.party["id"]

    def members_list(self, party_id):
        return [
            {"username": name, "avatar_uuid": None}
            for name in self.party["users"].values()
        ]


class FakeConfig:
    MAX_USERS_PER_PARTY = 0
    MAX_PENDING_JOIN_REQUESTS = 10
    LATE_JOIN_APPROVAL_ENABLED = True
    LATE_JOIN_REQUEST_TIMEOUT_SECONDS = 300
    LATE_JOIN_DENY_COOLDOWN_SECONDS = 0
    BINGE_WATCH_ENABLED = False


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
    now = datetime.now().isoformat()
    return {
        "id": "ABC12",
        "users": {"host-sid": "Host"},
        "participants": {"host-client": {"username": "Host", "sid": "host-sid"}},
        "sid_client_ids": {"host-sid": "host-client"},
        "current_video": {
            "item_id": "item-1", "title": "Movie", "overview": "",
            "run_time_seconds": 7200, "media_source_id": "ms-1",
            "selected_by": "host-client", "item_type": "Movie",
        },
        "playback_state": {"playing": True, "time": 100, "last_update": now},
        "user_streams": {},
        "ready_check": None,
        "join_requests": {},
        "join_denied_until": {},
        "client_codecs": {},
        "binge_watch_active": False,
        "pending_auto_advance": None,
        "episode_list": None,
        "host_client_id": "host-client",
        "host_username": "Host",
        "host_left_at": None,
        "host_access_token": "token",
        "host_user_id": "user",
    }


class JoinRequestQueueTests(unittest.IsolatedAsyncioTestCase):
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
            # None keeps admission on the metadata-only path; the tests
            # are about queue state, not Emby stream construction.
            "create_user_stream": None,
            "session_secret": None,
        }
        party_module.register(self.ctx)
        self.join = self.sio.handlers["join_party"]
        self.decide = self.sio.handlers["join_request_decision"]
        self.gone = self.ctx["handle_join_request_sid_gone"]

    async def asyncTearDown(self):
        # Pending timeout watchdogs would otherwise be destroyed with the
        # test loop and emit noisy warnings.
        for request in list(self.party.get("join_requests", {}).values()):
            task = request.get("timeout_task")
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def async_join(self, sid, username, client_id):
        await self.join(sid, {
            "party_id": "ABC12",
            "username": username,
            "client_id": client_id,
            "avatar_uuid": None,
        })

    async def test_new_mid_playback_joiner_is_queued_not_admitted(self):
        await self.async_join("bob-sid", "Bob", "bob-client")

        self.assertNotIn("Bob", self.party["users"].values())
        self.assertEqual(len(self.party["join_requests"]), 1)
        self.assertEqual(
            self.sio.events("join_request_pending")[-1]["to"], "bob-sid"
        )
        self.assertEqual(
            self.sio.events("join_requests_update")[-1]["room"], "ABC12"
        )

    async def test_reload_refreshes_the_same_request(self):
        await self.async_join("bob-sid", "Bob", "bob-client")
        request_id = next(iter(self.party["join_requests"]))

        await self.async_join("bob-sid-2", "Bob", "bob-client")

        self.assertEqual(len(self.party["join_requests"]), 1)
        self.assertEqual(
            self.party["join_requests"][request_id]["sid"], "bob-sid-2"
        )
        self.assertNotIn("Bob", self.party["users"].values())

    async def test_host_approve_admits_at_current_position_without_restart(self):
        await self.async_join("bob-sid", "Bob", "bob-client")
        request_id = next(iter(self.party["join_requests"]))

        await self.decide("host-sid", {
            "party_id": "ABC12",
            "request_id": request_id,
            "action": "approve",
        })

        self.assertIn("Bob", self.party["users"].values())
        self.assertEqual(self.party["join_requests"], {})
        self.assertEqual(
            self.sio.events("join_request_resolved")[-1]["data"]["action"],
            "approved",
        )
        self.assertTrue(self.sio.events("user_joined"))
        sync = self.sio.events("sync_state")[-1]
        self.assertEqual(sync["to"], "bob-sid")
        self.assertEqual(sync["data"]["current_video"]["item_id"], "item-1")
        # The room clock is untouched: approval must not restart the movie.
        self.assertEqual(self.party["playback_state"]["time"], 100)

    async def test_host_deny_rejects_without_adding_the_user(self):
        await self.async_join("carol-sid", "Carol", "carol-client")
        request_id = next(iter(self.party["join_requests"]))

        await self.decide("host-sid", {
            "party_id": "ABC12",
            "request_id": request_id,
            "action": "deny",
        })

        self.assertNotIn("Carol", self.party["users"].values())
        self.assertEqual(self.party["join_requests"], {})
        self.assertEqual(
            self.sio.events("join_rejected")[-1]["to"], "carol-sid"
        )

    async def test_disconnect_cancels_only_that_request_and_sets_no_cooldown(self):
        await self.async_join("dave-sid", "Dave", "dave-client")

        await self.gone(self.party, "ABC12", "dave-sid")

        self.assertEqual(self.party["join_requests"], {})
        self.assertEqual(self.party["join_denied_until"], {})
        self.assertEqual(
            self.sio.events("join_request_resolved")[-1]["data"]["action"],
            "cancelled",
        )

    async def test_a_non_host_cannot_decide(self):
        await self.async_join("erin-sid", "Erin", "erin-client")
        request_id = next(iter(self.party["join_requests"]))
        before = len(self.sio.emitted)

        await self.decide("bob-sid", {
            "party_id": "ABC12",
            "request_id": request_id,
            "action": "approve",
        })

        self.assertIn(request_id, self.party["join_requests"])
        self.assertTrue(any(
            e["event"] == "error" for e in self.sio.emitted[before:]
        ))

    async def test_empty_queue_cap_rejects_additional_requesters(self):
        self.ctx["config"].MAX_PENDING_JOIN_REQUESTS = 1

        await self.async_join("one-sid", "One", "one-client")
        await self.async_join("two-sid", "Two", "two-client")

        self.assertEqual(len(self.party["join_requests"]), 1)
        self.assertEqual(
            self.sio.events("join_rejected")[-1]["to"], "two-sid"
        )

    async def test_host_absent_falls_back_to_admitting_the_queue(self):
        self.party["host_left_at"] = datetime.now().isoformat()

        await self.async_join("frank-sid", "Frank", "frank-client")

        self.assertIn("Frank", self.party["users"].values())
        self.assertEqual(self.party["join_requests"], {})


if __name__ == "__main__":
    unittest.main()
