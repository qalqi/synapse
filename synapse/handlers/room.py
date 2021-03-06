# -*- coding: utf-8 -*-
# Copyright 2014 - 2016 OpenMarket Ltd
# Copyright 2018-2019 New Vector Ltd
# Copyright 2019 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Contains functions for performing events on rooms."""

import itertools
import logging
import math
import string
from collections import OrderedDict

from six import iteritems, string_types

from twisted.internet import defer

from synapse.api.constants import EventTypes, JoinRules, RoomCreationPreset
from synapse.api.errors import AuthError, Codes, NotFoundError, StoreError, SynapseError
from synapse.api.room_versions import KNOWN_ROOM_VERSIONS, RoomVersion
from synapse.events.utils import copy_power_levels_contents
from synapse.http.endpoint import parse_and_validate_server_name
from synapse.storage.state import StateFilter
from synapse.types import (
    Requester,
    RoomAlias,
    RoomID,
    RoomStreamToken,
    StateMap,
    StreamToken,
    UserID,
)
from synapse.util import stringutils
from synapse.util.async_helpers import Linearizer
from synapse.util.caches.response_cache import ResponseCache
from synapse.visibility import filter_events_for_client

from ._base import BaseHandler

logger = logging.getLogger(__name__)

id_server_scheme = "https://"

FIVE_MINUTES_IN_MS = 5 * 60 * 1000


class RoomCreationHandler(BaseHandler):

    PRESETS_DICT = {
        RoomCreationPreset.PRIVATE_CHAT: {
            "join_rules": JoinRules.INVITE,
            "history_visibility": "shared",
            "original_invitees_have_ops": False,
            "guest_can_join": True,
            "power_level_content_override": {"invite": 0},
        },
        RoomCreationPreset.TRUSTED_PRIVATE_CHAT: {
            "join_rules": JoinRules.INVITE,
            "history_visibility": "shared",
            "original_invitees_have_ops": True,
            "guest_can_join": True,
            "power_level_content_override": {"invite": 0},
        },
        RoomCreationPreset.PUBLIC_CHAT: {
            "join_rules": JoinRules.PUBLIC,
            "history_visibility": "shared",
            "original_invitees_have_ops": False,
            "guest_can_join": False,
            "power_level_content_override": {},
        },
    }

    def __init__(self, hs):
        super(RoomCreationHandler, self).__init__(hs)

        self.spam_checker = hs.get_spam_checker()
        self.event_creation_handler = hs.get_event_creation_handler()
        self.room_member_handler = hs.get_room_member_handler()
        self.config = hs.config

        # linearizer to stop two upgrades happening at once
        self._upgrade_linearizer = Linearizer("room_upgrade_linearizer")

        # If a user tries to update the same room multiple times in quick
        # succession, only process the first attempt and return its result to
        # subsequent requests
        self._upgrade_response_cache = ResponseCache(
            hs, "room_upgrade", timeout_ms=FIVE_MINUTES_IN_MS
        )
        self._server_notices_mxid = hs.config.server_notices_mxid

        self.third_party_event_rules = hs.get_third_party_event_rules()

    @defer.inlineCallbacks
    def upgrade_room(
        self, requester: Requester, old_room_id: str, new_version: RoomVersion
    ):
        """Replace a room with a new room with a different version

        Args:
            requester: the user requesting the upgrade
            old_room_id: the id of the room to be replaced
            new_version: the new room version to use

        Returns:
            Deferred[unicode]: the new room id
        """
        yield self.ratelimit(requester)

        user_id = requester.user.to_string()

        # Check if this room is already being upgraded by another person
        for key in self._upgrade_response_cache.pending_result_cache:
            if key[0] == old_room_id and key[1] != user_id:
                # Two different people are trying to upgrade the same room.
                # Send the second an error.
                #
                # Note that this of course only gets caught if both users are
                # on the same homeserver.
                raise SynapseError(
                    400, "An upgrade for this room is currently in progress"
                )

        # Upgrade the room
        #
        # If this user has sent multiple upgrade requests for the same room
        # and one of them is not complete yet, cache the response and
        # return it to all subsequent requests
        ret = yield self._upgrade_response_cache.wrap(
            (old_room_id, user_id),
            self._upgrade_room,
            requester,
            old_room_id,
            new_version,  # args for _upgrade_room
        )

        return ret

    @defer.inlineCallbacks
    def _upgrade_room(
        self, requester: Requester, old_room_id: str, new_version: RoomVersion
    ):
        user_id = requester.user.to_string()

        # start by allocating a new room id
        r = yield self.store.get_room(old_room_id)
        if r is None:
            raise NotFoundError("Unknown room id %s" % (old_room_id,))
        new_room_id = yield self._generate_room_id(
            creator_id=user_id, is_public=r["is_public"], room_version=new_version,
        )

        logger.info("Creating new room %s to replace %s", new_room_id, old_room_id)

        # we create and auth the tombstone event before properly creating the new
        # room, to check our user has perms in the old room.
        (
            tombstone_event,
            tombstone_context,
        ) = yield self.event_creation_handler.create_event(
            requester,
            {
                "type": EventTypes.Tombstone,
                "state_key": "",
                "room_id": old_room_id,
                "sender": user_id,
                "content": {
                    "body": "This room has been replaced",
                    "replacement_room": new_room_id,
                },
            },
            token_id=requester.access_token_id,
        )
        old_room_version = yield self.store.get_room_version_id(old_room_id)
        yield self.auth.check_from_context(
            old_room_version, tombstone_event, tombstone_context
        )

        yield self.clone_existing_room(
            requester,
            old_room_id=old_room_id,
            new_room_id=new_room_id,
            new_room_version=new_version,
            tombstone_event_id=tombstone_event.event_id,
        )

        # now send the tombstone
        yield self.event_creation_handler.send_nonmember_event(
            requester, tombstone_event, tombstone_context
        )

        old_room_state = yield tombstone_context.get_current_state_ids()

        # update any aliases
        yield self._move_aliases_to_new_room(
            requester, old_room_id, new_room_id, old_room_state
        )

        # Copy over user push rules, tags and migrate room directory state
        yield self.room_member_handler.transfer_room_state_on_room_upgrade(
            old_room_id, new_room_id
        )

        # finally, shut down the PLs in the old room, and update them in the new
        # room.
        yield self._update_upgraded_room_pls(
            requester, old_room_id, new_room_id, old_room_state,
        )

        return new_room_id

    @defer.inlineCallbacks
    def _update_upgraded_room_pls(
        self,
        requester: Requester,
        old_room_id: str,
        new_room_id: str,
        old_room_state: StateMap[str],
    ):
        """Send updated power levels in both rooms after an upgrade

        Args:
            requester: the user requesting the upgrade
            old_room_id: the id of the room to be replaced
            new_room_id: the id of the replacement room
            old_room_state: the state map for the old room

        Returns:
            Deferred
        """
        old_room_pl_event_id = old_room_state.get((EventTypes.PowerLevels, ""))

        if old_room_pl_event_id is None:
            logger.warning(
                "Not supported: upgrading a room with no PL event. Not setting PLs "
                "in old room."
            )
            return

        old_room_pl_state = yield self.store.get_event(old_room_pl_event_id)

        # we try to stop regular users from speaking by setting the PL required
        # to send regular events and invites to 'Moderator' level. That's normally
        # 50, but if the default PL in a room is 50 or more, then we set the
        # required PL above that.

        pl_content = dict(old_room_pl_state.content)
        users_default = int(pl_content.get("users_default", 0))
        restricted_level = max(users_default + 1, 50)

        updated = False
        for v in ("invite", "events_default"):
            current = int(pl_content.get(v, 0))
            if current < restricted_level:
                logger.debug(
                    "Setting level for %s in %s to %i (was %i)",
                    v,
                    old_room_id,
                    restricted_level,
                    current,
                )
                pl_content[v] = restricted_level
                updated = True
            else:
                logger.debug("Not setting level for %s (already %i)", v, current)

        if updated:
            try:
                yield self.event_creation_handler.create_and_send_nonmember_event(
                    requester,
                    {
                        "type": EventTypes.PowerLevels,
                        "state_key": "",
                        "room_id": old_room_id,
                        "sender": requester.user.to_string(),
                        "content": pl_content,
                    },
                    ratelimit=False,
                )
            except AuthError as e:
                logger.warning("Unable to update PLs in old room: %s", e)

        yield self.event_creation_handler.create_and_send_nonmember_event(
            requester,
            {
                "type": EventTypes.PowerLevels,
                "state_key": "",
                "room_id": new_room_id,
                "sender": requester.user.to_string(),
                "content": old_room_pl_state.content,
            },
            ratelimit=False,
        )

    @defer.inlineCallbacks
    def clone_existing_room(
        self,
        requester: Requester,
        old_room_id: str,
        new_room_id: str,
        new_room_version: RoomVersion,
        tombstone_event_id: str,
    ):
        """Populate a new room based on an old room

        Args:
            requester: the user requesting the upgrade
            old_room_id : the id of the room to be replaced
            new_room_id: the id to give the new room (should already have been
                created with _gemerate_room_id())
            new_room_version: the new room version to use
            tombstone_event_id: the ID of the tombstone event in the old room.
        Returns:
            Deferred
        """
        user_id = requester.user.to_string()

        if not self.spam_checker.user_may_create_room(user_id):
            raise SynapseError(403, "You are not permitted to create rooms")

        creation_content = {
            "room_version": new_room_version.identifier,
            "predecessor": {"room_id": old_room_id, "event_id": tombstone_event_id},
        }

        # Check if old room was non-federatable

        # Get old room's create event
        old_room_create_event = yield self.store.get_create_event_for_room(old_room_id)

        # Check if the create event specified a non-federatable room
        if not old_room_create_event.content.get("m.federate", True):
            # If so, mark the new room as non-federatable as well
            creation_content["m.federate"] = False

        initial_state = {}

        # Replicate relevant room events
        types_to_copy = (
            (EventTypes.JoinRules, ""),
            (EventTypes.Name, ""),
            (EventTypes.Topic, ""),
            (EventTypes.RoomHistoryVisibility, ""),
            (EventTypes.GuestAccess, ""),
            (EventTypes.RoomAvatar, ""),
            (EventTypes.RoomEncryption, ""),
            (EventTypes.ServerACL, ""),
            (EventTypes.RelatedGroups, ""),
            (EventTypes.PowerLevels, ""),
        )

        old_room_state_ids = yield self.store.get_filtered_current_state_ids(
            old_room_id, StateFilter.from_types(types_to_copy)
        )
        # map from event_id to BaseEvent
        old_room_state_events = yield self.store.get_events(old_room_state_ids.values())

        for k, old_event_id in iteritems(old_room_state_ids):
            old_event = old_room_state_events.get(old_event_id)
            if old_event:
                initial_state[k] = old_event.content

        # deep-copy the power-levels event before we start modifying it
        # note that if frozen_dicts are enabled, `power_levels` will be a frozen
        # dict so we can't just copy.deepcopy it.
        initial_state[
            (EventTypes.PowerLevels, "")
        ] = power_levels = copy_power_levels_contents(
            initial_state[(EventTypes.PowerLevels, "")]
        )

        # Resolve the minimum power level required to send any state event
        # We will give the upgrading user this power level temporarily (if necessary) such that
        # they are able to copy all of the state events over, then revert them back to their
        # original power level afterwards in _update_upgraded_room_pls

        # Copy over user power levels now as this will not be possible with >100PL users once
        # the room has been created

        # Calculate the minimum power level needed to clone the room
        event_power_levels = power_levels.get("events", {})
        state_default = power_levels.get("state_default", 0)
        ban = power_levels.get("ban")
        needed_power_level = max(state_default, ban, max(event_power_levels.values()))

        # Raise the requester's power level in the new room if necessary
        current_power_level = power_levels["users"][user_id]
        if current_power_level < needed_power_level:
            power_levels["users"][user_id] = needed_power_level

        yield self._send_events_for_new_room(
            requester,
            new_room_id,
            # we expect to override all the presets with initial_state, so this is
            # somewhat arbitrary.
            preset_config=RoomCreationPreset.PRIVATE_CHAT,
            invite_list=[],
            initial_state=initial_state,
            creation_content=creation_content,
        )

        # Transfer membership events
        old_room_member_state_ids = yield self.store.get_filtered_current_state_ids(
            old_room_id, StateFilter.from_types([(EventTypes.Member, None)])
        )

        # map from event_id to BaseEvent
        old_room_member_state_events = yield self.store.get_events(
            old_room_member_state_ids.values()
        )
        for k, old_event in iteritems(old_room_member_state_events):
            # Only transfer ban events
            if (
                "membership" in old_event.content
                and old_event.content["membership"] == "ban"
            ):
                yield self.room_member_handler.update_membership(
                    requester,
                    UserID.from_string(old_event["state_key"]),
                    new_room_id,
                    "ban",
                    ratelimit=False,
                    content=old_event.content,
                )

        # XXX invites/joins
        # XXX 3pid invites

    @defer.inlineCallbacks
    def _move_aliases_to_new_room(
        self,
        requester: Requester,
        old_room_id: str,
        new_room_id: str,
        old_room_state: StateMap[str],
    ):
        directory_handler = self.hs.get_handlers().directory_handler

        aliases = yield self.store.get_aliases_for_room(old_room_id)

        # check to see if we have a canonical alias.
        canonical_alias_event = None
        canonical_alias_event_id = old_room_state.get((EventTypes.CanonicalAlias, ""))
        if canonical_alias_event_id:
            canonical_alias_event = yield self.store.get_event(canonical_alias_event_id)

        # first we try to remove the aliases from the old room (we suppress sending
        # the room_aliases event until the end).
        #
        # Note that we'll only be able to remove aliases that (a) aren't owned by an AS,
        # and (b) unless the user is a server admin, which the user created.
        #
        # This is probably correct - given we don't allow such aliases to be deleted
        # normally, it would be odd to allow it in the case of doing a room upgrade -
        # but it makes the upgrade less effective, and you have to wonder why a room
        # admin can't remove aliases that point to that room anyway.
        # (cf https://github.com/matrix-org/synapse/issues/2360)
        #
        removed_aliases = []
        for alias_str in aliases:
            alias = RoomAlias.from_string(alias_str)
            try:
                yield directory_handler.delete_association(requester, alias)
                removed_aliases.append(alias_str)
            except SynapseError as e:
                logger.warning("Unable to remove alias %s from old room: %s", alias, e)

        # if we didn't find any aliases, or couldn't remove anyway, we can skip the rest
        # of this.
        if not removed_aliases:
            return

        # we can now add any aliases we successfully removed to the new room.
        for alias in removed_aliases:
            try:
                yield directory_handler.create_association(
                    requester,
                    RoomAlias.from_string(alias),
                    new_room_id,
                    servers=(self.hs.hostname,),
                    check_membership=False,
                )
                logger.info("Moved alias %s to new room", alias)
            except SynapseError as e:
                # I'm not really expecting this to happen, but it could if the spam
                # checking module decides it shouldn't, or similar.
                logger.error("Error adding alias %s to new room: %s", alias, e)

        # If a canonical alias event existed for the old room, fire a canonical
        # alias event for the new room with a copy of the information.
        try:
            if canonical_alias_event:
                yield self.event_creation_handler.create_and_send_nonmember_event(
                    requester,
                    {
                        "type": EventTypes.CanonicalAlias,
                        "state_key": "",
                        "room_id": new_room_id,
                        "sender": requester.user.to_string(),
                        "content": canonical_alias_event.content,
                    },
                    ratelimit=False,
                )
        except SynapseError as e:
            # again I'm not really expecting this to fail, but if it does, I'd rather
            # we returned the new room to the client at this point.
            logger.error("Unable to send updated alias events in new room: %s", e)

    @defer.inlineCallbacks
    def create_room(self, requester, config, ratelimit=True, creator_join_profile=None):
        """ Creates a new room.

        Args:
            requester (synapse.types.Requester):
                The user who requested the room creation.
            config (dict) : A dict of configuration options.
            ratelimit (bool): set to False to disable the rate limiter

            creator_join_profile (dict|None):
                Set to override the displayname and avatar for the creating
                user in this room. If unset, displayname and avatar will be
                derived from the user's profile. If set, should contain the
                values to go in the body of the 'join' event (typically
                `avatar_url` and/or `displayname`.

        Returns:
            Deferred[dict]:
                a dict containing the keys `room_id` and, if an alias was
                requested, `room_alias`.
        Raises:
            SynapseError if the room ID couldn't be stored, or something went
            horribly wrong.
            ResourceLimitError if server is blocked to some resource being
            exceeded
        """
        user_id = requester.user.to_string()

        yield self.auth.check_auth_blocking(user_id)

        if (
            self._server_notices_mxid is not None
            and requester.user.to_string() == self._server_notices_mxid
        ):
            # allow the server notices mxid to create rooms
            is_requester_admin = True
        else:
            is_requester_admin = yield self.auth.is_server_admin(requester.user)

        # Check whether the third party rules allows/changes the room create
        # request.
        event_allowed = yield self.third_party_event_rules.on_create_room(
            requester, config, is_requester_admin=is_requester_admin
        )
        if not event_allowed:
            raise SynapseError(
                403, "You are not permitted to create rooms", Codes.FORBIDDEN
            )

        if not is_requester_admin and not self.spam_checker.user_may_create_room(
            user_id
        ):
            raise SynapseError(403, "You are not permitted to create rooms")

        if ratelimit:
            yield self.ratelimit(requester)

        room_version_id = config.get(
            "room_version", self.config.default_room_version.identifier
        )

        if not isinstance(room_version_id, string_types):
            raise SynapseError(400, "room_version must be a string", Codes.BAD_JSON)

        room_version = KNOWN_ROOM_VERSIONS.get(room_version_id)
        if room_version is None:
            raise SynapseError(
                400,
                "Your homeserver does not support this room version",
                Codes.UNSUPPORTED_ROOM_VERSION,
            )

        if "room_alias_name" in config:
            for wchar in string.whitespace:
                if wchar in config["room_alias_name"]:
                    raise SynapseError(400, "Invalid characters in room alias")

            room_alias = RoomAlias(config["room_alias_name"], self.hs.hostname)
            mapping = yield self.store.get_association_from_room_alias(room_alias)

            if mapping:
                raise SynapseError(400, "Room alias already taken", Codes.ROOM_IN_USE)
        else:
            room_alias = None

        invite_list = config.get("invite", [])
        for i in invite_list:
            try:
                uid = UserID.from_string(i)
                parse_and_validate_server_name(uid.domain)
            except Exception:
                raise SynapseError(400, "Invalid user_id: %s" % (i,))

        yield self.event_creation_handler.assert_accepted_privacy_policy(requester)

        power_level_content_override = config.get("power_level_content_override")
        if (
            power_level_content_override
            and "users" in power_level_content_override
            and user_id not in power_level_content_override["users"]
        ):
            raise SynapseError(
                400,
                "Not a valid power_level_content_override: 'users' did not contain %s"
                % (user_id,),
            )

        invite_3pid_list = config.get("invite_3pid", [])

        visibility = config.get("visibility", None)
        is_public = visibility == "public"

        room_id = yield self._generate_room_id(
            creator_id=user_id, is_public=is_public, room_version=room_version,
        )

        directory_handler = self.hs.get_handlers().directory_handler
        if room_alias:
            yield directory_handler.create_association(
                requester=requester,
                room_id=room_id,
                room_alias=room_alias,
                servers=[self.hs.hostname],
                check_membership=False,
            )

        preset_config = config.get(
            "preset",
            RoomCreationPreset.PRIVATE_CHAT
            if visibility == "private"
            else RoomCreationPreset.PUBLIC_CHAT,
        )

        raw_initial_state = config.get("initial_state", [])

        initial_state = OrderedDict()
        for val in raw_initial_state:
            initial_state[(val["type"], val.get("state_key", ""))] = val["content"]

        creation_content = config.get("creation_content", {})

        # override any attempt to set room versions via the creation_content
        creation_content["room_version"] = room_version.identifier

        yield self._send_events_for_new_room(
            requester,
            room_id,
            preset_config=preset_config,
            invite_list=invite_list,
            initial_state=initial_state,
            creation_content=creation_content,
            room_alias=room_alias,
            power_level_content_override=power_level_content_override,
            creator_join_profile=creator_join_profile,
        )

        if "name" in config:
            name = config["name"]
            yield self.event_creation_handler.create_and_send_nonmember_event(
                requester,
                {
                    "type": EventTypes.Name,
                    "room_id": room_id,
                    "sender": user_id,
                    "state_key": "",
                    "content": {"name": name},
                },
                ratelimit=False,
            )

        if "topic" in config:
            topic = config["topic"]
            yield self.event_creation_handler.create_and_send_nonmember_event(
                requester,
                {
                    "type": EventTypes.Topic,
                    "room_id": room_id,
                    "sender": user_id,
                    "state_key": "",
                    "content": {"topic": topic},
                },
                ratelimit=False,
            )

        for invitee in invite_list:
            content = {}
            is_direct = config.get("is_direct", None)
            if is_direct:
                content["is_direct"] = is_direct

            yield self.room_member_handler.update_membership(
                requester,
                UserID.from_string(invitee),
                room_id,
                "invite",
                ratelimit=False,
                content=content,
            )

        for invite_3pid in invite_3pid_list:
            id_server = invite_3pid["id_server"]
            id_access_token = invite_3pid.get("id_access_token")  # optional
            address = invite_3pid["address"]
            medium = invite_3pid["medium"]
            yield self.hs.get_room_member_handler().do_3pid_invite(
                room_id,
                requester.user,
                medium,
                address,
                id_server,
                requester,
                txn_id=None,
                id_access_token=id_access_token,
            )

        result = {"room_id": room_id}

        if room_alias:
            result["room_alias"] = room_alias.to_string()

        return result

    @defer.inlineCallbacks
    def _send_events_for_new_room(
        self,
        creator,  # A Requester object.
        room_id,
        preset_config,
        invite_list,
        initial_state,
        creation_content,
        room_alias=None,
        power_level_content_override=None,  # Doesn't apply when initial state has power level state event content
        creator_join_profile=None,
    ):
        def create(etype, content, **kwargs):
            e = {"type": etype, "content": content}

            e.update(event_keys)
            e.update(kwargs)

            return e

        @defer.inlineCallbacks
        def send(etype, content, **kwargs):
            event = create(etype, content, **kwargs)
            logger.debug("Sending %s in new room", etype)
            yield self.event_creation_handler.create_and_send_nonmember_event(
                creator, event, ratelimit=False
            )

        config = RoomCreationHandler.PRESETS_DICT[preset_config]

        creator_id = creator.user.to_string()

        event_keys = {"room_id": room_id, "sender": creator_id, "state_key": ""}

        creation_content.update({"creator": creator_id})
        yield send(etype=EventTypes.Create, content=creation_content)

        logger.debug("Sending %s in new room", EventTypes.Member)
        yield self.room_member_handler.update_membership(
            creator,
            creator.user,
            room_id,
            "join",
            ratelimit=False,
            content=creator_join_profile,
        )

        # We treat the power levels override specially as this needs to be one
        # of the first events that get sent into a room.
        pl_content = initial_state.pop((EventTypes.PowerLevels, ""), None)
        if pl_content is not None:
            yield send(etype=EventTypes.PowerLevels, content=pl_content)
        else:
            power_level_content = {
                "users": {creator_id: 100},
                "users_default": 0,
                "events": {
                    EventTypes.Name: 50,
                    EventTypes.PowerLevels: 100,
                    EventTypes.RoomHistoryVisibility: 100,
                    EventTypes.CanonicalAlias: 50,
                    EventTypes.RoomAvatar: 50,
                    EventTypes.Tombstone: 100,
                    EventTypes.ServerACL: 100,
                },
                "events_default": 0,
                "state_default": 50,
                "ban": 50,
                "kick": 50,
                "redact": 50,
                "invite": 50,
            }

            if config["original_invitees_have_ops"]:
                for invitee in invite_list:
                    power_level_content["users"][invitee] = 100

            # Power levels overrides are defined per chat preset
            power_level_content.update(config["power_level_content_override"])

            if power_level_content_override:
                power_level_content.update(power_level_content_override)

            yield send(etype=EventTypes.PowerLevels, content=power_level_content)

        if room_alias and (EventTypes.CanonicalAlias, "") not in initial_state:
            yield send(
                etype=EventTypes.CanonicalAlias,
                content={"alias": room_alias.to_string()},
            )

        if (EventTypes.JoinRules, "") not in initial_state:
            yield send(
                etype=EventTypes.JoinRules, content={"join_rule": config["join_rules"]}
            )

        if (EventTypes.RoomHistoryVisibility, "") not in initial_state:
            yield send(
                etype=EventTypes.RoomHistoryVisibility,
                content={"history_visibility": config["history_visibility"]},
            )

        if config["guest_can_join"]:
            if (EventTypes.GuestAccess, "") not in initial_state:
                yield send(
                    etype=EventTypes.GuestAccess, content={"guest_access": "can_join"}
                )

        for (etype, state_key), content in initial_state.items():
            yield send(etype=etype, state_key=state_key, content=content)

    @defer.inlineCallbacks
    def _generate_room_id(
        self, creator_id: str, is_public: str, room_version: RoomVersion,
    ):
        # autogen room IDs and try to create it. We may clash, so just
        # try a few times till one goes through, giving up eventually.
        attempts = 0
        while attempts < 5:
            try:
                random_string = stringutils.random_string(18)
                gen_room_id = RoomID(random_string, self.hs.hostname).to_string()
                if isinstance(gen_room_id, bytes):
                    gen_room_id = gen_room_id.decode("utf-8")
                yield self.store.store_room(
                    room_id=gen_room_id,
                    room_creator_user_id=creator_id,
                    is_public=is_public,
                    room_version=room_version,
                )
                return gen_room_id
            except StoreError:
                attempts += 1
        raise StoreError(500, "Couldn't generate a room ID.")


class RoomContextHandler(object):
    def __init__(self, hs):
        self.hs = hs
        self.store = hs.get_datastore()
        self.storage = hs.get_storage()
        self.state_store = self.storage.state

    @defer.inlineCallbacks
    def get_event_context(self, user, room_id, event_id, limit, event_filter):
        """Retrieves events, pagination tokens and state around a given event
        in a room.

        Args:
            user (UserID)
            room_id (str)
            event_id (str)
            limit (int): The maximum number of events to return in total
                (excluding state).
            event_filter (Filter|None): the filter to apply to the events returned
                (excluding the target event_id)

        Returns:
            dict, or None if the event isn't found
        """
        before_limit = math.floor(limit / 2.0)
        after_limit = limit - before_limit

        users = yield self.store.get_users_in_room(room_id)
        is_peeking = user.to_string() not in users

        def filter_evts(events):
            return filter_events_for_client(
                self.storage, user.to_string(), events, is_peeking=is_peeking
            )

        event = yield self.store.get_event(
            event_id, get_prev_content=True, allow_none=True
        )
        if not event:
            return None

        filtered = yield (filter_evts([event]))
        if not filtered:
            raise AuthError(403, "You don't have permission to access that event.")

        results = yield self.store.get_events_around(
            room_id, event_id, before_limit, after_limit, event_filter
        )

        if event_filter:
            results["events_before"] = event_filter.filter(results["events_before"])
            results["events_after"] = event_filter.filter(results["events_after"])

        results["events_before"] = yield filter_evts(results["events_before"])
        results["events_after"] = yield filter_evts(results["events_after"])
        # filter_evts can return a pruned event in case the user is allowed to see that
        # there's something there but not see the content, so use the event that's in
        # `filtered` rather than the event we retrieved from the datastore.
        results["event"] = filtered[0]

        if results["events_after"]:
            last_event_id = results["events_after"][-1].event_id
        else:
            last_event_id = event_id

        if event_filter and event_filter.lazy_load_members():
            state_filter = StateFilter.from_lazy_load_member_list(
                ev.sender
                for ev in itertools.chain(
                    results["events_before"],
                    (results["event"],),
                    results["events_after"],
                )
            )
        else:
            state_filter = StateFilter.all()

        # XXX: why do we return the state as of the last event rather than the
        # first? Shouldn't we be consistent with /sync?
        # https://github.com/matrix-org/matrix-doc/issues/687

        state = yield self.state_store.get_state_for_events(
            [last_event_id], state_filter=state_filter
        )

        state_events = list(state[last_event_id].values())
        if event_filter:
            state_events = event_filter.filter(state_events)

        results["state"] = yield filter_evts(state_events)

        # We use a dummy token here as we only care about the room portion of
        # the token, which we replace.
        token = StreamToken.START

        results["start"] = token.copy_and_replace(
            "room_key", results["start"]
        ).to_string()

        results["end"] = token.copy_and_replace("room_key", results["end"]).to_string()

        return results


class RoomEventSource(object):
    def __init__(self, hs):
        self.store = hs.get_datastore()

    @defer.inlineCallbacks
    def get_new_events(
        self, user, from_key, limit, room_ids, is_guest, explicit_room_id=None
    ):
        # We just ignore the key for now.

        to_key = yield self.get_current_key()

        from_token = RoomStreamToken.parse(from_key)
        if from_token.topological:
            logger.warning("Stream has topological part!!!! %r", from_key)
            from_key = "s%s" % (from_token.stream,)

        app_service = self.store.get_app_service_by_user_id(user.to_string())
        if app_service:
            # We no longer support AS users using /sync directly.
            # See https://github.com/matrix-org/matrix-doc/issues/1144
            raise NotImplementedError()
        else:
            room_events = yield self.store.get_membership_changes_for_user(
                user.to_string(), from_key, to_key
            )

            room_to_events = yield self.store.get_room_events_stream_for_rooms(
                room_ids=room_ids,
                from_key=from_key,
                to_key=to_key,
                limit=limit or 10,
                order="ASC",
            )

            events = list(room_events)
            events.extend(e for evs, _ in room_to_events.values() for e in evs)

            events.sort(key=lambda e: e.internal_metadata.order)

            if limit:
                events[:] = events[:limit]

            if events:
                end_key = events[-1].internal_metadata.after
            else:
                end_key = to_key

        return (events, end_key)

    def get_current_key(self):
        return self.store.get_room_events_max_id()

    def get_current_key_for_room(self, room_id):
        return self.store.get_room_events_max_id(room_id)
