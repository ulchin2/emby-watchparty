"""Party handlers: join_party, leave_party, host-approved join requests."""

import asyncio
import secrets
import time
from datetime import datetime
from backend.src.quality import DEFAULT_QUALITY_ID
from backend.src.utils import generate_random_username

# Video codecs a client may claim to decode. Allowlisted rather than
# trusted, because the value reaches an Emby stream URL: an unconstrained
# string would let a client put arbitrary text into VideoCodec=. h264 is
# always implied, so a client that claims nothing still gets a stream.
_ALLOWED_CLIENT_CODECS = frozenset({"h264", "hevc", "av1", "vp9"})


def _parse_client_codecs(raw) -> set:
    """Normalise a client's claimed codec list into a trusted set."""
    if not isinstance(raw, list):
        return {"h264"}
    claimed = {
        item.strip().lower()
        for item in raw
        if isinstance(item, str) and item.strip()
    }
    return (claimed & _ALLOWED_CLIENT_CODECS) | {"h264"}


def register(ctx):
    sio = ctx['sio']
    emby_client = ctx['emby_client']
    config = ctx['config']
    logger = ctx['logger']
    party_manager = ctx['party_manager']
    token_manager = ctx['token_manager']
    create_user_stream = ctx.get('create_user_stream')
    session_secret = ctx.get('session_secret')

    def _cookie_session(environ):
        """Read the party-bound session cookie off the raw ASGI environ.

        Same decoder as connection.py:_decode_session; broken out here
        so join_party can gate the eviction path on cookie proof.
        Returns the decoded session dict or None. Session cookie name +
        max-age are kept in sync with SessionMiddleware config in
        backend/app.py.
        """
        if not session_secret:
            return None
        raw = environ.get("HTTP_COOKIE", "")
        if not raw:
            return None
        # Reuse connection.py helpers rather than duplicating parse
        # logic; a stray drift between the two parsers would silently
        # break auth on one side.
        from backend.src.socket_handlers.connection import _decode_session
        return _decode_session(environ, session_secret)

    # -------------------------------------------------------------------------
    # Shared party helpers
    # -------------------------------------------------------------------------

    def _current_party_time(party):
        """Return the server's current best estimate of playback position."""
        playback_state = party.get("playback_state", {})
        current_time = playback_state.get("time", 0)
        if playback_state.get("playing") and playback_state.get("last_update"):
            try:
                last_update = datetime.fromisoformat(playback_state["last_update"])
                elapsed = (datetime.now() - last_update).total_seconds()
                if 0 < elapsed < 30:
                    current_time += elapsed
            except Exception:
                pass
        return current_time

    def _replace_sid(party, old_sid, new_sid, username, client_id=None,
                     avatar_uuid=None):
        """Move all sid-keyed party state from an old socket to a new one."""
        if old_sid and old_sid != new_sid:
            party["users"].pop(old_sid, None)
            party.setdefault("join_times", {}).pop(old_sid, None)
            party.setdefault("sid_client_ids", {}).pop(old_sid, None)

            # selected_by is keyed on client_id (stable across reloads),
            # so no remapping is needed when sid changes.

            old_stream = party.get("user_streams", {}).pop(old_sid, None)
            if old_stream and old_stream.get("play_session_id") and party.get("current_video"):
                current_time = party["playback_state"].get("time", 0)
                host_token = party.get("host_access_token")
                host_user = party.get("host_user_id")
                emby_client.report_playback_stopped(
                    item_id=party["current_video"]["item_id"],
                    media_source_id=old_stream["media_source_id"],
                    play_session_id=old_stream["play_session_id"],
                    position_seconds=current_time,
                    run_time_seconds=party["current_video"].get("run_time_seconds"),
                    access_token=host_token,
                    user_id=host_user,
                )
                emby_client.stop_active_encodings(
                    play_session_id=old_stream["play_session_id"],
                    access_token=host_token,
                )

            drift_strikes = party.get("drift_strikes")
            if drift_strikes and old_sid in drift_strikes:
                drift_strikes[new_sid] = drift_strikes.pop(old_sid)

            rc = party.get("ready_check")
            if rc and rc.get("active"):
                if old_sid in rc["expected_sids"]:
                    rc["expected_sids"].discard(old_sid)
                    rc["expected_sids"].add(new_sid)
                if old_sid in rc["ready_sids"]:
                    rc["ready_sids"].discard(old_sid)
                    rc["ready_sids"].add(new_sid)

        party["users"][new_sid] = username
        party.setdefault("join_times", {})[new_sid] = datetime.now().isoformat()
        if client_id:
            participants = party.setdefault("participants", {})
            # Preserve any previously-recorded avatar_uuid if the new
            # call did not supply one, so a reconnect without
            # avatar_uuid in the payload does not erase it.
            existing = participants.get(client_id, {})
            participants[client_id] = {
                "username": username,
                "sid": new_sid,
                "last_seen": datetime.now().isoformat(),
                "avatar_uuid": avatar_uuid or existing.get("avatar_uuid"),
            }
            party.setdefault("sid_client_ids", {})[new_sid] = client_id

    async def _build_rejoin_video(party, party_id, sid):
        """Create a fresh per-user stream for a known participant rejoining."""
        current_video = party.get("current_video")
        if not current_video or not create_user_stream:
            return None

        current_time = _current_party_time(party)
        stream = create_user_stream(
            party, party_id, sid, current_video["item_id"], None,
            audio_index=None, subtitle_index=None,
            quality=current_video.get("quality", DEFAULT_QUALITY_ID),
            start_seconds=current_time,
            # Lock the late-joiner to the same Emby version the rest of
            # the party is watching. Without this they'd silently get
            # Emby's default source (issue #43).
            media_source_id=current_video.get("media_source_id"),
        )
        if not stream:
            return None

        stream_url = stream["stream_url_base"]
        if config.ENABLE_HLS_TOKEN_VALIDATION:
            user_token = token_manager.get_or_create(party_id, sid)
            if user_token:
                stream_url += f"&token={user_token}"

        return {
            "item_id": current_video.get("item_id"),
            "title": current_video.get("title"),
            "overview": current_video.get("overview"),
            "stream_url": stream_url,
            "audio_index": stream.get("audio_index"),
            "subtitle_index": stream.get("subtitle_index"),
            "media_source_id": stream.get("media_source_id"),
            "selected_by": current_video.get("selected_by"),
            "quality": stream.get("quality", current_video.get("quality", DEFAULT_QUALITY_ID)),
            # The frontend clamps Jump/Seek against this. Omitting it made
            # every client-side clamp a no-op, so any member could seek
            # the whole room past the end of the file.
            "run_time_seconds": current_video.get("run_time_seconds"),
            # Carry the binge-watching metadata so a late joiner sees
            # the same control-strip button visibility and library
            # NEXT badge as the rest of the room.
            "item_type": current_video.get("item_type"),
            "series_id": current_video.get("series_id"),
            "season_id": current_video.get("season_id"),
            "episode_index": current_video.get("episode_index"),
            "next_item_id": current_video.get("next_item_id"),
            "next_item_title": current_video.get("next_item_title"),
        }, current_time

    # -------------------------------------------------------------------------
    # Join request queue
    #
    # Mid-playback joins are no longer decided by a vote. A new viewer is
    # held as a request in party["join_requests"] until the host admits or
    # declines them. The queue is party state (not a transient event), so
    # the host can dismiss the notification -- or reload the page -- and
    # still find the request in the Join requests list. No cooldown is
    # applied unless the host explicitly declines.
    # -------------------------------------------------------------------------

    def _join_request_timeout_seconds():
        return max(0, int(getattr(config, "LATE_JOIN_REQUEST_TIMEOUT_SECONDS", 300) or 0))

    def _new_join_request_id(party):
        while True:
            request_id = secrets.token_urlsafe(6)
            if request_id not in party.setdefault("join_requests", {}):
                return request_id

    def _request_payload(request):
        return {
            "id": request["id"],
            "username": request["username"],
            "avatar_uuid": request.get("avatar_uuid"),
            # Epoch milliseconds so the browser can compute "waiting for
            # 0:42" without trusting its clock against a server ISO
            # timestamp.
            "requested_at": int(request["requested_at"] * 1000),
            "waiting_seconds": max(0, int(time.time() - request["requested_at"])),
            "timeout_seconds": _join_request_timeout_seconds(),
        }

    def _join_requests_payload(party):
        return {
            "requests": [
                _request_payload(request)
                for request in (party.get("join_requests") or {}).values()
            ],
            "timeout_seconds": _join_request_timeout_seconds(),
        }

    async def _broadcast_join_requests(party, party_id):
        await sio.emit("join_requests_update", _join_requests_payload(party), room=party_id)

    def _drop_join_request(party, request_id, cancel_task=True):
        request = (party.get("join_requests") or {}).pop(request_id, None)
        if not request:
            return None
        task = request.get("timeout_task")
        # Never cancel ourselves: _expire_join_request calls this with
        # cancel_task=False precisely to avoid raising CancelledError at
        # its next await (same failure mode the old vote watchdog had).
        if (
            cancel_task
            and task
            and not task.done()
            and task is not asyncio.current_task()
        ):
            task.cancel()
        request["timeout_task"] = None
        return request

    def _schedule_join_request_timeout(party, party_id, request):
        previous = request.get("timeout_task")
        if previous and not previous.done():
            previous.cancel()
        timeout_seconds = _join_request_timeout_seconds()
        if timeout_seconds > 0:
            request["timeout_task"] = asyncio.create_task(
                _expire_join_request(party_id, request["id"], timeout_seconds)
            )
        else:
            request["timeout_task"] = None

    async def _expire_join_request(party_id, request_id, timeout_seconds):
        """Drop a request nobody acted on and let the joiner retry.

        An expiry is NOT a decline: no cooldown is recorded, so the same
        person can request again immediately if the host is still there.
        """
        try:
            await asyncio.sleep(timeout_seconds)
        except asyncio.CancelledError:
            return

        party = party_manager.get(party_id)
        if not party:
            return
        request = (party.get("join_requests") or {}).get(request_id)
        if not request:
            return

        sid = request["sid"]
        username = request["username"]
        client_id = request.get("client_id")
        _drop_join_request(party, request_id, cancel_task=False)
        if client_id:
            party.setdefault("client_codecs", {}).pop(client_id, None)
        logger.info(
            f"Join request expired in party {party_id} for {username} "
            f"after {timeout_seconds}s"
        )
        await sio.emit("join_request_resolved", {
            "request_id": request_id,
            "username": username,
            "action": "expired",
        }, room=party_id)
        await _broadcast_join_requests(party, party_id)
        await sio.emit("join_rejected", {
            "message": "The host did not respond to your request in time. You can try again."
        }, to=sid)
        try:
            await sio.leave_room(sid, party_id)
        except Exception:
            pass

    async def _enqueue_join_request(party, party_id, sid, username, client_id, avatar_uuid):
        """Hold a new joiner in the queue.

        Re-requesting from the same browser (reload / retry) updates the
        existing entry in place instead of stacking duplicates. Returns
        the request dict, or None when the queue was full (the caller has
        already been told why).
        """
        requests = party.setdefault("join_requests", {})
        timeout_seconds = _join_request_timeout_seconds()

        existing = next(
            (r for r in requests.values() if r.get("client_id") == client_id),
            None,
        )
        if existing:
            old_sid = existing.get("sid")
            if old_sid and old_sid != sid:
                try:
                    await sio.leave_room(old_sid, party_id)
                except Exception:
                    pass
            existing.update({
                "sid": sid,
                "username": username,
                "avatar_uuid": avatar_uuid,
            })
            existing["requested_at"] = time.time()
            _schedule_join_request_timeout(party, party_id, existing)
            await sio.enter_room(sid, party_id)
            await sio.emit("join_request_pending", _request_payload(existing), to=sid)
            await _broadcast_join_requests(party, party_id)
            logger.info(
                f"Join request refreshed in party {party_id} for {username} "
                f"(id={existing['id']})"
            )
            return existing

        max_pending = max(0, int(getattr(config, "MAX_PENDING_JOIN_REQUESTS", 10) or 0))
        if max_pending and len(requests) >= max_pending:
            logger.info(
                f"Join rejected in party {party_id}: {len(requests)} requests "
                f"already pending (user: {username})"
            )
            await sio.emit("join_rejected", {
                "message": (
                    "Too many people are waiting to join right now. "
                    "Please try again in a moment."
                )
            }, to=sid)
            return None

        request = {
            "id": _new_join_request_id(party),
            "sid": sid,
            "username": username,
            "client_id": client_id,
            "avatar_uuid": avatar_uuid,
            "requested_at": time.time(),
            "timeout_task": None,
        }
        requests[request["id"]] = request

        # Put the joiner in the Socket.IO room so they receive queue
        # updates, but deliberately keep them OUT of party["users"] until
        # the host approves. That keeps them invisible to playback,
        # ready-check and participant-count logic while waiting.
        await sio.enter_room(sid, party_id)
        _schedule_join_request_timeout(party, party_id, request)
        await sio.emit("join_request_pending", _request_payload(request), to=sid)
        await _broadcast_join_requests(party, party_id)
        logger.info(
            f"Join request queued in party {party_id} for {username} "
            f"(id={request['id']}, timeout={timeout_seconds}s)"
        )
        return request

    async def _finalize_member_join(party, party_id, sid, username, client_id,
                                    avatar_uuid, rejoin=False, old_sid=None,
                                    reclaim_host=False):
        """Add a socket to party["users"] and hand it a sync_state."""
        await sio.enter_room(sid, party_id)
        _replace_sid(party, old_sid, sid, username, client_id, avatar_uuid)

        if reclaim_host:
            try_host_reclaim = ctx.get("try_host_reclaim")
            if try_host_reclaim:
                await try_host_reclaim(party_id, client_id, sid)

        await sio.emit("user_joined", {
            "username": username,
            "users": list(party["users"].values()),
            "members": party_manager.members_list(party_id),
            "rejoin": rejoin,
        }, room=party_id)

        await _send_sync_state(party, party_id, sid)

    def _pending_auto_advance_payload(party):
        pending = party.get("pending_auto_advance")
        if not pending:
            return None
        deadline = pending.get("deadline")
        countdown_seconds = None
        if deadline:
            try:
                remaining = (
                    datetime.fromisoformat(deadline) - datetime.now()
                ).total_seconds()
                countdown_seconds = max(0, int(remaining))
            except Exception:
                countdown_seconds = None
        episode_list = party.get("episode_list") or []
        return {
            "next_item_id": pending.get("next_item_id"),
            "next_title": pending.get("next_title"),
            "next_index_number": pending.get("next_index_number"),
            "total_episodes": (
                max((ep.get("IndexNumber") or 0) for ep in episode_list)
                if episode_list else 0
            ),
            "deadline": deadline,
            "countdown_seconds": countdown_seconds,
        }

    async def _send_sync_state(party, party_id, sid):
        """Send the authoritative room state to one socket.

        Also carries the current join-request queue so a host who reloads
        the page sees pending requests immediately, not only when the
        next request arrives.
        """
        current_video = None
        playback_state = party["playback_state"].copy()
        if party.get("current_video"):
            # If video is active, create a fresh per-user stream at the
            # current party position so reload/rejoin can resume without
            # triggering an approval request or restarting everyone.
            rejoin_video = await _build_rejoin_video(party, party_id, sid)
            if rejoin_video:
                current_video, current_time = rejoin_video
                playback_state["time"] = current_time
            else:
                cv = party["current_video"]
                current_video = {
                    "item_id": cv.get("item_id"),
                    "title": cv.get("title"),
                    "overview": cv.get("overview"),
                    "selected_by": cv.get("selected_by"),
                    "run_time_seconds": cv.get("run_time_seconds"),
                    "item_type": cv.get("item_type"),
                    "series_id": cv.get("series_id"),
                    "season_id": cv.get("season_id"),
                    "episode_index": cv.get("episode_index"),
                    "next_item_id": cv.get("next_item_id"),
                    "next_item_title": cv.get("next_item_title"),
                }

        await sio.emit("sync_state", {
            "current_video": current_video,
            "playback_state": playback_state,
            "binge_watch": {
                "available": bool(config.BINGE_WATCH_ENABLED),
                "active": bool(party.get("binge_watch_active")),
            },
            "pending_auto_advance": _pending_auto_advance_payload(party),
            "join_requests": _join_requests_payload(party),
        }, to=sid)

    def _caller_is_host(party, sid):
        """True when this socket belongs to the party's current host.

        Mirrors the other host-only socket handlers (binge toggles,
        library broadcast), which key on the persistent client_id rather
        than the transient sid. The HTTP layer still owns the stronger
        host_session_grant proof for admin routes.
        """
        client_id = party.get("sid_client_ids", {}).get(sid)
        return bool(client_id) and client_id == party.get("host_client_id")

    def _denied_retry_after(party, client_id):
        denied = party.setdefault("join_denied_until", {})
        now = time.time()
        # Prune expired entries while we are here so the dict cannot grow
        # without bound over a long-lived party.
        for cid, until in list(denied.items()):
            if until <= now:
                denied.pop(cid, None)
        until = denied.get(client_id, 0)
        if until > now:
            return int(until - now) + 1
        return 0

    async def _admit_join_request(party, party_id, request, decided_by=None):
        request_id = request["id"]
        username = request["username"]
        sid = request["sid"]
        client_id = request.get("client_id") or f"sid:{sid}"

        _drop_join_request(party, request_id)
        logger.info(
            f"Join request approved in party {party_id} for {username} "
            f"(id={request_id}, by {decided_by or 'system'})"
        )
        await sio.emit("join_request_resolved", {
            "request_id": request_id,
            "username": username,
            "action": "approved",
        }, room=party_id)
        await _broadcast_join_requests(party, party_id)

        # Admission is the normal join path: the viewer is added to the
        # room and receives sync_state. If a video is active they get a
        # per-user stream starting at the current party position, with
        # no room-wide restart.
        await _finalize_member_join(
            party, party_id, sid, username, client_id,
            request.get("avatar_uuid"), rejoin=False,
        )

    async def _deny_join_request(party, party_id, request, decided_by=None):
        request_id = request["id"]
        username = request["username"]
        sid = request["sid"]
        client_id = request.get("client_id")

        _drop_join_request(party, request_id)

        cooldown = max(0, int(getattr(config, "LATE_JOIN_DENY_COOLDOWN_SECONDS", 0) or 0))
        if cooldown > 0 and client_id:
            party.setdefault("join_denied_until", {})[client_id] = time.time() + cooldown

        logger.info(
            f"Join request declined in party {party_id} for {username} "
            f"(id={request_id}, by {decided_by or 'system'})"
        )
        await sio.emit("join_request_resolved", {
            "request_id": request_id,
            "username": username,
            "action": "denied",
        }, room=party_id)
        await _broadcast_join_requests(party, party_id)
        await sio.emit("join_rejected", {
            "message": "The host declined your request to join."
        }, to=sid)
        try:
            await sio.leave_room(sid, party_id)
        except Exception:
            pass
        # The rejected client never became a participant, but join_party
        # records codec support before the approval gate. Drop it so a
        # stream of rejected attempts cannot grow the map.
        if client_id:
            party.setdefault("client_codecs", {}).pop(client_id, None)

    async def _handle_join_request_sid_gone(party, party_id, sid):
        """Remove a request whose socket left (reload, tab close, leave)."""
        for request in list((party.get("join_requests") or {}).values()):
            if request.get("sid") != sid:
                continue
            request_id = request["id"]
            username = request["username"]
            client_id = request.get("client_id")
            _drop_join_request(party, request_id)
            if client_id:
                party.setdefault("client_codecs", {}).pop(client_id, None)
            logger.info(
                f"Join request cancelled in party {party_id} for {username} "
                f"(id={request_id}, socket left)"
            )
            await sio.emit("join_request_resolved", {
                "request_id": request_id,
                "username": username,
                "action": "cancelled",
            }, room=party_id)
            await _broadcast_join_requests(party, party_id)
            try:
                await sio.leave_room(sid, party_id)
            except Exception:
                pass
            return request
        return None

    async def _flush_pending_join_requests(party, party_id):
        """Admit queued viewers when the party has no active host.

        Nobody is left to approve them, and holding the queue hostage to
        a departed host would strand the joiners until the timeout. The
        same fallback covers host step-down and the host grace expiry.
        """
        if party.get("host_client_id") and party.get("host_left_at") is None:
            return
        for request in list((party.get("join_requests") or {}).values()):
            if party.get("host_client_id") and party.get("host_left_at") is None:
                break
            await _admit_join_request(party, party_id, request, decided_by="host_left")

    # Expose helpers in ctx so connection.py can keep the queue in sync
    # when a socket drops or the host grace window expires.
    ctx['handle_join_request_sid_gone'] = _handle_join_request_sid_gone
    ctx['flush_pending_join_requests'] = _flush_pending_join_requests

    # -------------------------------------------------------------------------
    # Event handlers
    # -------------------------------------------------------------------------

    @sio.on("join_party")
    async def handle_join_party(sid, data):
        party_id = data.get("party_id", "").strip().upper()
        username = data.get("username", "").strip()
        client_id = str(data.get("client_id", "")).strip()
        avatar_uuid = data.get("avatar_uuid")
        if isinstance(avatar_uuid, str):
            avatar_uuid = avatar_uuid.strip() or None
        else:
            avatar_uuid = None
        client_codecs = _parse_client_codecs(data.get("video_codecs"))

        if not username:
            username = generate_random_username()
            logger.info(f"Generated random username: {username}")
        if not client_id:
            client_id = f"sid:{sid}"

        if not party_manager.exists(party_id):
            logger.warning(f"Join failed: party {party_id} not found (user: {username})")
            await sio.emit("error", {"message": "Watch party not found"}, to=sid)
            return

        party = party_manager.get(party_id)
        participants = party.setdefault("participants", {})
        sid_client_ids = party.setdefault("sid_client_ids", {})
        # Keyed on client_id rather than sid, for the same reason
        # selected_by is: it stays valid across a reload or a reconnect,
        # so a returning viewer keeps the codecs they reported and the
        # eviction and rejoin paths below need no special handling.
        party.setdefault("client_codecs", {})[client_id] = client_codecs
        known_participant = client_id in participants
        rejoin = known_participant
        old_sid = participants.get(client_id, {}).get("sid")

        # A pending request from this same browser (page reload, retry,
        # second tab) is reused rather than duplicated.
        pending_request = next(
            (
                request
                for request in (party.get("join_requests") or {}).values()
                if request.get("client_id") == client_id
            ),
            None,
        )

        if known_participant:
            # Reattach: move this client's party state to the new socket.
            # No approval request fires for someone who is already a
            # member, which is what makes a refresh mid-movie safe.
            if old_sid and old_sid != sid:
                await sio.leave_room(old_sid, party_id)
                logger.info(f"Reattached participant {username} in {party_id}: {old_sid} -> {sid}")
            if pending_request:
                # Stale request left over from before this client became
                # a member; drop it so it cannot resurface later.
                _drop_join_request(party, pending_request["id"])
                await _broadcast_join_requests(party, party_id)
            await _finalize_member_join(
                party, party_id, sid, username, client_id, avatar_uuid,
                rejoin=True, old_sid=old_sid, reclaim_host=True,
            )
            return

        # Historically this evicted any existing member with a matching
        # display name (with a different client_id) so duplicate-tab /
        # lost-client_id refreshes would land on their seat. But username
        # is a public string and there is no party password, so anyone
        # knowing (party code, member name) could kick the real member
        # off their seat, inherit their ready_check membership, and DoS
        # their Emby transcode via _replace_sid's teardown.
        #
        # Gate the eviction on a valid session cookie carrying the SAME
        # client_id as the joiner. HTTP /api/party/<id>/join is the only
        # route that mints those cookies, and it stores the
        # caller-supplied client_id verbatim -- so a legitimate
        # duplicate-tab / same-browser user matches, but a random
        # attacker with just the party code + username does not.
        evicted_sid = None
        environ = sio.get_environ(sid) or {}
        cookie_session = _cookie_session(environ)
        cookie_client_id = cookie_session.get("client_id") if cookie_session else None
        cookie_party_id = (cookie_session.get("party_id") or "").upper() if cookie_session else ""
        same_browser = (
            cookie_client_id == client_id and cookie_party_id == party_id
        )
        for stale_sid, existing_name in list(party["users"].items()):
            if existing_name == username and stale_sid != sid:
                if not same_browser:
                    logger.warning(
                        f"Join eviction REJECTED in {party_id}: "
                        f"caller sid={sid} lacks session-cookie proof "
                        f"for username '{username}'; refusing to evict "
                        f"stale_sid={stale_sid}"
                    )
                    await sio.emit("error", {
                        "message": (
                            "That username is already in use. Pick "
                            "another display name."
                        )
                    }, to=sid)
                    return
                stale_client_id = sid_client_ids.get(stale_sid)
                if stale_client_id:
                    participants.pop(stale_client_id, None)
                await sio.leave_room(stale_sid, party_id)
                evicted_sid = stale_sid
                rejoin = True
                logger.info(f"Evicted stale session {stale_sid} for {username}")
                break

        # -----------------------------------------------------------------
        # Join approval gate
        #
        # Approval applies to every new person entering a party that
        # already has members -- including the lobby, before any video is
        # selected. It is not tied to playback state.
        # -----------------------------------------------------------------
        approval_enabled = bool(getattr(config, "LATE_JOIN_APPROVAL_ENABLED", True))
        has_existing_users = len(party["users"]) > 0
        # An absent host (grace window running, or stepped down) must not
        # strand a joiner in a queue nobody can clear. In that state the
        # party falls back to the normal join path.
        has_active_host = (
            bool(party.get("host_client_id"))
            and party.get("host_left_at") is None
        )
        needs_approval = (
            not rejoin
            and approval_enabled
            and has_existing_users
            and has_active_host
        )

        if pending_request and not needs_approval:
            # The reason for holding them disappeared (host left, or the
            # room emptied). Admit instead of making them wait for a
            # decision that no longer has anything to protect.
            if pending_request.get("sid") and pending_request["sid"] != sid:
                try:
                    await sio.leave_room(pending_request["sid"], party_id)
                except Exception:
                    pass
            _drop_join_request(party, pending_request["id"])
            await sio.emit("join_request_resolved", {
                "request_id": pending_request["id"],
                "username": pending_request["username"],
                "action": "approved",
            }, room=party_id)
            await _broadcast_join_requests(party, party_id)
            pending_request = None

        if pending_request and needs_approval:
            # Same browser asking again while still queued: refresh the
            # existing entry (new sid, same id) and keep the host's list
            # stable instead of stacking duplicates.
            await _enqueue_join_request(
                party, party_id, sid, username, client_id, avatar_uuid,
            )
            return

        # Check max users. Pending requests already occupy a slot so a
        # queue cannot grow past the party cap while the host decides.
        current_count = len(party["users"]) + len(party.get("join_requests") or {})
        if pending_request:
            current_count -= 1
        if (
            not rejoin
            and config.MAX_USERS_PER_PARTY > 0
            and current_count >= config.MAX_USERS_PER_PARTY
        ):
            logger.warning(f"Party {party_id} is full")
            # join_rejected is the event PartyView turns into a visible
            # alert + redirect. Plain `error` only reaches chat, which is
            # hidden behind the join screen for someone who never joined.
            await sio.emit("join_rejected", {
                "message": f"Party is full (max {config.MAX_USERS_PER_PARTY} users)"
            }, to=sid)
            return

        if needs_approval:
            retry_after = _denied_retry_after(party, client_id)
            if retry_after:
                logger.info(
                    f"Join rejected: decline cooldown active in party {party_id} "
                    f"({retry_after}s remaining, user: {username})"
                )
                await sio.emit("join_rejected", {
                    "message": (
                        f"The host declined a recent request. "
                        f"Try again in {retry_after} seconds."
                    ),
                    "retry_after": retry_after,
                }, to=sid)
                return

            # Hold the joiner in the request queue. The host keeps a
            # persistent list; dismissing the notification does not
            # reject anybody and does not start a cooldown.
            await _enqueue_join_request(
                party, party_id, sid, username, client_id, avatar_uuid,
            )
            return

        # Normal join path: no active video, approval disabled, empty
        # party, no active host, or a same-browser reattach.
        await _finalize_member_join(
            party, party_id, sid, username, client_id, avatar_uuid,
            rejoin=rejoin, old_sid=evicted_sid, reclaim_host=True,
        )

    @sio.on("join_request_decision")
    async def handle_join_request_decision(sid, data):
        """Host admits or declines one queued join request.

        The queue itself lives on the party dict, so a decision is valid
        even after the host reloaded the page or dismissed the toast.
        """
        party_id = str(data.get("party_id", "")).strip().upper()
        request_id = str(data.get("request_id", "")).strip()
        action = str(data.get("action", "")).strip().lower()

        if action not in ("approve", "deny"):
            return

        party = party_manager.get(party_id)
        if not party:
            return

        if not _caller_is_host(party, sid):
            logger.warning(
                f"Join request decision REJECTED in {party_id}: sid={sid} "
                f"is not the host (action={action}, request={request_id})"
            )
            await sio.emit("error", {
                "message": "Only the host can approve or decline join requests."
            }, to=sid)
            return

        request = (party.get("join_requests") or {}).get(request_id)
        if not request:
            # Double click / another tab already resolved it. Re-send the
            # current queue so every client converges.
            await _broadcast_join_requests(party, party_id)
            return

        decided_by = party["users"].get(sid) or party.get("host_username") or "host"

        if action == "approve":
            if (
                config.MAX_USERS_PER_PARTY > 0
                and len(party["users"]) >= config.MAX_USERS_PER_PARTY
            ):
                logger.info(
                    f"Join approval blocked in {party_id}: party is full "
                    f"(max {config.MAX_USERS_PER_PARTY})"
                )
                await sio.emit("error", {
                    "message": (
                        f"Party is full (max {config.MAX_USERS_PER_PARTY} "
                        f"users), so this request cannot be admitted."
                    )
                }, to=sid)
                return
            await _admit_join_request(party, party_id, request, decided_by=decided_by)
        else:
            await _deny_join_request(party, party_id, request, decided_by=decided_by)

    @sio.on("leave_party")
    async def handle_leave_party(sid, data):
        party_id = data.get("party_id", "").strip().upper()
        party = party_manager.get(party_id)
        if not party:
            return

        # If the leaving socket was waiting in the join-request queue,
        # drop that request first so the host's list does not show a
        # person who has already navigated away.
        await _handle_join_request_sid_gone(party, party_id, sid)

        host_departed = False

        # Host leaving explicitly (clicked Leave). A disconnect gets a 5s grace
        # window for refresh-as-rejoin, but an explicit leave is intentional, so
        # transition the lock state now. This MUST run before the
        # sid_client_ids pop below, which erases the mapping used to identify
        # the host (and is why the disconnect handler alone could not catch an
        # explicit leave).
        departing_client_id = party.get("sid_client_ids", {}).get(sid)
        if (
            departing_client_id
            and departing_client_id == party.get("host_client_id")
            and party.get("host_left_at") is None
        ):
            host_username = party.get("host_username") or "?"
            host_departed = True
            pending = ctx.get("pending_host_clear", {})
            grace_task = pending.pop(party_id, None)
            if grace_task and not grace_task.done():
                grace_task.cancel()
            if party.get("current_video"):
                # PLAYING-ONLY: keep the token so the in-flight stream finishes
                # for everyone else; a later video_ended/stop_video clears it.
                party_manager.mark_host_left(party_id)
                logger.info(
                    f"Host '{host_username}' left party {party_id} during "
                    f"playback -> PLAYING-ONLY"
                )
                await sio.emit("host_left", {
                    "previous_host": host_username,
                    "reason": "leave",
                    "playing_only": True,
                }, room=party_id, skip_sid=sid)
            else:
                party_manager.clear_host(party_id)
                logger.info(
                    f"Host '{host_username}' left party {party_id} (no playback) "
                    f"-> LOCKED"
                )
                await sio.emit("host_left", {
                    "previous_host": host_username,
                    "reason": "leave",
                }, room=party_id, skip_sid=sid)

        if sid in party["users"]:
            username = party["users"][sid]

            # Stop this user's individual transcode
            user_stream = party.get("user_streams", {}).get(sid)
            if user_stream and user_stream.get("play_session_id") and party.get("current_video"):
                current_time = party["playback_state"].get("time", 0)
                host_token = party.get("host_access_token")
                host_user = party.get("host_user_id")
                emby_client.report_playback_stopped(
                    item_id=party["current_video"]["item_id"],
                    media_source_id=user_stream["media_source_id"],
                    play_session_id=user_stream["play_session_id"],
                    position_seconds=current_time,
                    run_time_seconds=party["current_video"].get("run_time_seconds"),
                    access_token=host_token,
                    user_id=host_user,
                )
                emby_client.stop_active_encodings(
                    play_session_id=user_stream["play_session_id"],
                    access_token=host_token,
                )
            party.get("user_streams", {}).pop(sid, None)

            await sio.leave_room(sid, party_id)
            client_id = party.setdefault("sid_client_ids", {}).pop(sid, None)
            if client_id:
                party.setdefault("participants", {}).pop(client_id, None)
            del party["users"][sid]

            # Remove from any active ready check so we don't wait forever
            rc = party.get("ready_check")
            if rc and rc.get("active"):
                rc["expected_sids"].discard(sid)
                rc["ready_sids"].discard(sid)
                if rc["ready_sids"] >= rc["expected_sids"] and rc["expected_sids"]:
                    party["ready_check"] = None
                    playback_state = party.get("playback_state", {})
                    if playback_state.get("playing"):
                        playback_state["last_update"] = datetime.now().isoformat()
                    logger.info(f"All users ready in party {party_id} (after leave)")
                    await sio.emit("all_ready", {
                        "time": playback_state.get("time", 0),
                        "playing": playback_state.get("playing", False),
                    }, room=party_id)
                elif not rc["expected_sids"]:
                    party["ready_check"] = None
                else:
                    ready_names = [party["users"].get(s, "?") for s in rc["ready_sids"]]
                    waiting_names = [party["users"].get(s, "?") for s in rc["expected_sids"] - rc["ready_sids"]]
                    await sio.emit("ready_check_update", {
                        "ready": ready_names, "waiting": waiting_names,
                    }, room=party_id)

            await sio.emit("user_left", {
                "username": username,
                "users": list(party["users"].values()),
                "members": party_manager.members_list(party_id),
            }, room=party_id)

        if host_departed:
            # Nobody is left to approve the queue. Admitting the pending
            # joiners is better than leaving them stuck until the request
            # timeout, and the party is now playing-only/locked anyway.
            await _flush_pending_join_requests(party, party_id)

    @sio.on("update_avatar")
    async def handle_update_avatar(sid, data):
        """Re-bind the caller's avatar_uuid and broadcast the new
        member roster so every connected client re-renders.

        Used after the avatar setup modal saves an upload, links a
        Gravatar, or recovers via a code. Without this, only a page
        refresh would pick up the new avatar.
        """
        party_id = data.get("party_id", "").strip().upper()
        new_uuid = data.get("avatar_uuid")
        if isinstance(new_uuid, str):
            new_uuid = new_uuid.strip() or None
        elif new_uuid is not None:
            return

        party = party_manager.get(party_id)
        if not party:
            return
        client_id = party.get("sid_client_ids", {}).get(sid)
        if not client_id:
            return
        participant = party.get("participants", {}).get(client_id)
        if not participant:
            return
        participant["avatar_uuid"] = new_uuid

        await sio.emit("members_update", {
            "members": party_manager.members_list(party_id),
        }, room=party_id)
