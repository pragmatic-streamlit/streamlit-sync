from datetime import datetime
from itertools import chain
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Set

import streamlit as st
from diskcache import Cache, Index

from . import st_hack
from .exceptions import StreamlitSyncException
from .utils import LAST_SYNCED_KEY, is_synced


@st.cache_resource
def get_existing_room_names() -> Set[str]:
    """Singleton containing all existing room names."""
    return set()


@st.cache_resource
def get_synced_state(room_name: str) -> "_SyncedState":
    """Return the room synced state.

    Each room is a singleton, synced by all connected sessions."""
    return _SyncedState(room_name=room_name)


class _SyncedState:
    def __init__(self, room_name: str) -> None:
        self.room_name: str = room_name
        self._lock: Lock = Lock()
        self.use_cache = False

        with self._lock:
            self.last_updated: datetime = datetime.fromtimestamp(0)
            self._registered_sessions: Set[str] = set()
            self.state: Dict[str, Any] = {}

    def __repr__(self) -> str:
        rep = (
            "<SyncedState "
            f"room={self.room_name} "
            f"active_users={self.nb_active_sessions}"
        )
        if self.use_cache:
            rep += f" (cached to {self.room_cache_dir})"
        rep += ">"
        return rep

    @property
    def nb_active_sessions(self) -> int:
        """Return estimated number of action sessions.

        Might not be exact if a session has disconnected since last rerun.
        """
        return len(self._registered_sessions)

    def attach_to_disk(self, cache_dir: Path) -> None:
        """Attach a room to disk for caching."""
        room_cache_dir = cache_dir / self.room_name
        if self.use_cache:
            assert self.room_cache_dir is not None
            if self.room_cache_dir != room_cache_dir:
                raise StreamlitSyncException(
                    f"Cannot attach room {self.room_name}"
                    f" to cache dir {room_cache_dir}:"
                    f" already attached to {self.room_cache_dir}"
                )
        else:
            self.use_cache = True
            self.room_cache_dir: Path = room_cache_dir
            self._cache = Cache(self.room_cache_dir)
            self.state = Index.fromcache(self._cache)

    def delete(self) -> None:
        """Reset the room values and discard it from existing room.

        TODO: remove room from existing room names
        TODO: remove SyncedState from streamlit singleton
        TODO: implement delete when cache is used.
        """
        raise NotImplementedError()

    def register_session(self) -> None:
        """Register a new session to the room."""
        with self._lock:
            self._registered_sessions.add(st_hack.get_session_id())
            get_existing_room_names().add(self.room_name)

    def unregister_session(self) -> None:
        """Unregister a session from the room."""
        with self._lock:
            self._registered_sessions.discard(st_hack.get_session_id())

    def sync(self) -> None:
        """Synchronize all session state values and widget with other sessions.

        Logic:
        1.   If the synced state has been updated since last time, update current
             session values and rerun the session.

        2.   Else, check for all values from streamlit (both widgets and session state)
              a. If at least 1 value has been updated, update the synced state and rerun
                 all sessions.
              b. Else, do nothing.
        """
        with self._lock:
            internal_session_state = st_hack.get_session_state()._state

            if st.session_state.get(LAST_SYNCED_KEY) != self.last_updated:
                # Means current SessionState is not synced with SyncedState
                # -> update streamlit internal state and reload
                st_hack.set_internal_values(self.state)
                st.session_state[LAST_SYNCED_KEY] = self.last_updated
                st.experimental_rerun()
                st.stop()
            else:
                internal_session_state._new_widget_state.widget_metadata

                # Check if new data from streamlit frontend
                updated_values = {}
                for key, value in chain(
                    internal_session_state._new_session_state.items(),
                    internal_session_state._new_widget_state.items(),
                    st.session_state.items(),
                ):
                    if st_hack.is_form_submitter_value(key):
                        # Form widgets must not be synced
                        continue

                    if st_hack.is_trigger_value(key, internal_session_state):
                        # Trigger values correspond to buttons
                        # -> we don't want to propagate the effect of the button
                        #    to avoid performing twice the action
                        continue

                    if not is_synced(key):
                        # Some keys are not synced
                        continue

                    key = st_hack.widget_id_to_user_key(key)

                    if value != self.state.get(key):
                        updated_values[key] = value

                # Current SessionState has newer values than _SyncedState
                # -> update _SyncedState values
                # -> trigger rerun for all connected sessions
                if len(updated_values) > 0:
                    self.state.update(updated_values)
                    self.last_updated = datetime.now()
                    self._trigger_sessions()
                    st.session_state[LAST_SYNCED_KEY] = self.last_updated

    def _trigger_sessions(self) -> None:
        """Trigger rerun on all active sessions except the session that triggered it.

        If a session is not active anymore, it is removed from the room. Most probably
        the user closed the tab.
        """
        current_session_id = st_hack.get_session_id()
        inactive_sessions = set()
        for session_id in self._registered_sessions:
            if session_id != current_session_id:
                # We need to trigger rerun in other sessions.
                # => We can't use st.experimental_rerun()
                runtime = st_hack.get_runtime_instance()
                session = runtime._session_mgr.get_session_info(session_id)
                if session is None:
                    # It is most likely that this session stopped
                    inactive_sessions.add(session_id)
                    continue
                else:
                    session.session.request_rerun(None)

        for session_id in inactive_sessions:
            self._registered_sessions.discard(session_id)
