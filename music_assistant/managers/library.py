"""LibraryManager: Orchestrates synchronisation of music providers into the library."""
import asyncio
import functools
import logging
import time
from typing import Any, List

from music_assistant.constants import EVENT_MUSIC_SYNC_STATUS, EVENT_PROVIDER_REGISTERED
from music_assistant.helpers.util import callback, run_periodic
from music_assistant.models.media_types import (
    Album,
    Artist,
    MediaItem,
    MediaType,
    Playlist,
    Radio,
    Track,
)
from music_assistant.models.provider import ProviderType

LOGGER = logging.getLogger("music_manager")


def sync_task(desc):
    """Return decorator to report a sync task."""

    def wrapper(func):
        @functools.wraps(func)
        async def async_wrapped(*args):
            method_class = args[0]
            prov_id = args[1]
            # check if this sync task is not already running
            for sync_prov_id, sync_desc in method_class.running_sync_jobs:
                if sync_prov_id == prov_id and sync_desc == desc:
                    LOGGER.debug(
                        "Syncjob %s for provider %s is already running!", desc, prov_id
                    )
                    return
            LOGGER.debug("Start syncjob %s for provider %s.", desc, prov_id)
            sync_job = (prov_id, desc)
            method_class.running_sync_jobs.append(sync_job)
            method_class.mass.signal_event(
                EVENT_MUSIC_SYNC_STATUS, method_class.running_sync_jobs
            )
            await func(*args)
            LOGGER.debug("Finished syncing %s for provider %s", desc, prov_id)
            method_class.running_sync_jobs.remove(sync_job)
            method_class.mass.signal_event(
                EVENT_MUSIC_SYNC_STATUS, method_class.running_sync_jobs
            )

        return async_wrapped

    return wrapper


class LibraryManager:
    """Manage sync of musicproviders to library."""

    def __init__(self, mass):
        """Initialize class."""
        self.running_sync_jobs = []
        self.mass = mass
        self.cache = mass.cache
        self.mass.add_event_listener(self.mass_event, [EVENT_PROVIDER_REGISTERED])

    async def async_setup(self):
        """Async initialize of module."""
        # schedule sync task
        self.mass.add_job(self.__async_music_providers_sync())

    @callback
    def mass_event(self, msg: str, msg_details: Any):
        """Handle message on eventbus."""
        if msg == EVENT_PROVIDER_REGISTERED:
            # schedule a sync task when a new provider registers
            provider = self.mass.get_provider(msg_details)
            if provider.type == ProviderType.MUSIC_PROVIDER:
                self.mass.add_job(self.async_music_provider_sync(msg_details))

    ################ GET MediaItems that are added in the library ################

    async def async_get_library_artists(self, orderby: str = "name") -> List[Artist]:
        """Return all library artists, optionally filtered by provider."""
        return await self.mass.database.async_get_library_artists(orderby=orderby)

    async def async_get_library_albums(self, orderby: str = "name") -> List[Album]:
        """Return all library albums, optionally filtered by provider."""
        return await self.mass.database.async_get_library_albums(orderby=orderby)

    async def async_get_library_tracks(self, orderby: str = "name") -> List[Track]:
        """Return all library tracks, optionally filtered by provider."""
        return await self.mass.database.async_get_library_tracks(orderby=orderby)

    async def async_get_library_playlists(
        self, orderby: str = "name"
    ) -> List[Playlist]:
        """Return all library playlists, optionally filtered by provider."""
        return await self.mass.database.async_get_library_playlists(orderby=orderby)

    async def async_get_library_radios(self, orderby: str = "name") -> List[Playlist]:
        """Return all library radios, optionally filtered by provider."""
        return await self.mass.database.async_get_library_radios(orderby=orderby)

    async def async_get_library_playlist_by_name(self, name: str) -> Playlist:
        """Get in-library playlist by name."""
        for playlist in await self.mass.music.async_get_library_playlists():
            if playlist.name == name:
                return playlist
        return None

    async def async_get_radio_by_name(self, name: str) -> Radio:
        """Get in-library radio by name."""
        for radio in await self.mass.music.async_get_library_radios():
            if radio.name == name:
                return radio
        return None

    async def async_library_add(self, media_items: List[MediaItem]):
        """Add media item(s) to the library."""
        result = False
        for media_item in media_items:
            # add to provider's libraries
            for prov in media_item.provider_ids:
                provider = self.mass.get_provider(prov.provider)
                if provider:
                    result = await provider.async_library_add(
                        prov.item_id, media_item.media_type
                    )
            # mark as library item in internal db
            if media_item.provider == "database":
                await self.mass.database.async_add_to_library(
                    media_item.item_id, media_item.media_type, media_item.provider
                )
        return result

    async def async_library_remove(self, media_items: List[MediaItem]):
        """Remove media item(s) from the library."""
        result = False
        for media_item in media_items:
            # remove from provider's libraries
            for prov in media_item.provider_ids:
                provider = self.mass.get_provider(prov.provider)
                if provider:
                    result = await provider.async_library_remove(
                        prov.item_id, media_item.media_type
                    )
            # mark as library item in internal db
            if media_item.provider == "database":
                await self.mass.database.async_remove_from_library(
                    media_item.item_id, media_item.media_type, media_item.provider
                )
        return result

    async def async_add_playlist_tracks(self, db_playlist_id: int, tracks: List[Track]):
        """Add tracks to playlist - make sure we dont add duplicates."""
        # we can only edit playlists that are in the database (marked as editable)
        playlist = await self.mass.music.async_get_playlist(db_playlist_id, "database")
        if not playlist or not playlist.is_editable:
            return False
        # playlist can only have one provider (for now)
        playlist_prov = playlist.provider_ids[0]
        # grab all existing track ids in the playlist so we can check for duplicates
        cur_playlist_track_ids = []
        for item in await self.mass.music.async_get_playlist_tracks(
            playlist_prov.item_id, playlist_prov.provider
        ):
            cur_playlist_track_ids.append(item.item_id)
            cur_playlist_track_ids += [i.item_id for i in item.provider_ids]
        track_ids_to_add = []
        for track in tracks:
            # check for duplicates
            already_exists = track.item_id in cur_playlist_track_ids
            for track_prov in track.provider_ids:
                if track_prov.item_id in cur_playlist_track_ids:
                    already_exists = True
            if already_exists:
                continue
            # we can only add a track to a provider playlist if track is available on that provider
            # this should all be handled in the frontend but these checks are here just to be safe
            # a track can contain multiple versions on the same provider
            # simply sort by quality and just add the first one (assuming track is still available)
            for track_version in sorted(
                track.provider_ids, key=lambda x: x.quality, reverse=True
            ):
                if track_version.provider == playlist_prov.provider:
                    track_ids_to_add.append(track_version.item_id)
                    break
                if playlist_prov.provider == "file":
                    # the file provider can handle uri's from all providers so simply add the uri
                    uri = f"{track_version.provider}://{track_version.item_id}"
                    track_ids_to_add.append(uri)
                    break
        # actually add the tracks to the playlist on the provider
        if track_ids_to_add:
            # invalidate cache
            await self.mass.database.async_update_playlist(
                playlist.item_id, "checksum", str(time.time())
            )
            # return result of the action on the provider
            provider = self.mass.get_provider(playlist_prov.provider)
            return await provider.async_add_playlist_tracks(
                playlist_prov.item_id, track_ids_to_add
            )
        return False

    async def async_remove_playlist_tracks(self, db_playlist_id, tracks: List[Track]):
        """Remove tracks from playlist."""
        # we can only edit playlists that are in the database (marked as editable)
        playlist = await self.mass.music.async_get_playlist(db_playlist_id, "database")
        if not playlist or not playlist.is_editable:
            return False
        # playlist can only have one provider (for now)
        prov_playlist = playlist.provider_ids[0]
        track_ids_to_remove = []
        for track in tracks:
            # a track can contain multiple versions on the same provider, remove all
            for track_provider in track.provider_ids:
                if track_provider.provider == prov_playlist.provider:
                    track_ids_to_remove.append(track_provider.item_id)
        # actually remove the tracks from the playlist on the provider
        if track_ids_to_remove:
            # invalidate cache
            await self.mass.database.async_update_playlist(
                playlist.item_id, "checksum", str(time.time())
            )
            provider = self.mass.get_provider(prov_playlist.provider)
            return await provider.async_remove_playlist_tracks(
                prov_playlist.item_id, track_ids_to_remove
            )

    @run_periodic(3600 * 3)
    async def __async_music_providers_sync(self):
        """Periodic sync of all music providers."""
        await asyncio.sleep(10)
        for prov in self.mass.get_providers(ProviderType.MUSIC_PROVIDER):
            await self.async_music_provider_sync(prov.id)

    async def async_music_provider_sync(self, prov_id: str):
        """
        Sync a music provider.

        param prov_id: {string} -- provider id to sync
        """
        provider = self.mass.get_provider(prov_id)
        if not provider:
            return
        if MediaType.Album in provider.supported_mediatypes:
            await self.async_library_albums_sync(prov_id)
        if MediaType.Track in provider.supported_mediatypes:
            await self.async_library_tracks_sync(prov_id)
        if MediaType.Artist in provider.supported_mediatypes:
            await self.async_library_artists_sync(prov_id)
        if MediaType.Playlist in provider.supported_mediatypes:
            await self.async_library_playlists_sync(prov_id)
        if MediaType.Radio in provider.supported_mediatypes:
            await self.async_library_radios_sync(prov_id)

    @sync_task("artists")
    async def async_library_artists_sync(self, provider_id: str):
        """Sync library artists for given provider."""
        music_provider = self.mass.get_provider(provider_id)
        cache_key = f"library_artists_{provider_id}"
        prev_db_ids = await self.mass.cache.async_get(cache_key, default=[])
        cur_db_ids = []
        for item in await music_provider.async_get_library_artists():
            db_item = await self.mass.music.async_get_artist(item.item_id, provider_id)
            cur_db_ids.append(db_item.item_id)
            await self.mass.database.async_add_to_library(
                db_item.item_id, MediaType.Artist, provider_id
            )
        # process deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.async_remove_from_library(
                    db_id, MediaType.Artist, provider_id
                )
        # store ids in cache for next sync
        await self.mass.cache.async_set(cache_key, cur_db_ids)

    @sync_task("albums")
    async def async_library_albums_sync(self, provider_id: str):
        """Sync library albums for given provider."""
        music_provider = self.mass.get_provider(provider_id)
        cache_key = f"library_albums_{provider_id}"
        prev_db_ids = await self.mass.cache.async_get(cache_key, default=[])
        cur_db_ids = []
        for item in await music_provider.async_get_library_albums():
            db_album = await self.mass.music.async_get_album(item.item_id, provider_id)
            if db_album.available != item.available:
                # album availability changed, sort this out with auto matching magic
                db_album = await self.mass.music.async_match_album(db_album)
            cur_db_ids.append(db_album.item_id)
            await self.mass.database.async_add_to_library(
                db_album.item_id, MediaType.Album, provider_id
            )
            # precache album tracks
            for album_track in await self.mass.music.async_get_album_tracks(
                item.item_id, provider_id
            ):
                # try to find substitutes for unavailable tracks with matching technique
                if not album_track.available:
                    if album_track.provider == "database":
                        await self.mass.music.async_match_track(album_track)
                    else:
                        await self.mass.music.async_add_track(album_track)
        # process album deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.async_remove_from_library(
                    db_id, MediaType.Album, provider_id
                )
        # store ids in cache for next sync
        await self.mass.cache.async_set(cache_key, cur_db_ids)

    @sync_task("tracks")
    async def async_library_tracks_sync(self, provider_id: str):
        """Sync library tracks for given provider."""
        music_provider = self.mass.get_provider(provider_id)
        cache_key = f"library_tracks_{provider_id}"
        prev_db_ids = await self.mass.cache.async_get(cache_key, default=[])
        cur_db_ids = []
        for item in await music_provider.async_get_library_tracks():
            db_item = await self.mass.music.async_get_track(item.item_id, provider_id)
            if db_item.available != item.available:
                # track availability changed, sort this out with auto matching magic
                db_item = await self.mass.music.async_add_track(item)
            cur_db_ids.append(db_item.item_id)
            if db_item.item_id not in prev_db_ids:
                await self.mass.database.async_add_to_library(
                    db_item.item_id, MediaType.Track, provider_id
                )
        # process deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.async_remove_from_library(
                    db_id, MediaType.Track, provider_id
                )
        # store ids in cache for next sync
        await self.mass.cache.async_set(cache_key, cur_db_ids)

    @sync_task("playlists")
    async def async_library_playlists_sync(self, provider_id: str):
        """Sync library playlists for given provider."""
        music_provider = self.mass.get_provider(provider_id)
        cache_key = f"library_playlists_{provider_id}"
        prev_db_ids = await self.mass.cache.async_get(cache_key, default=[])
        cur_db_ids = []
        for playlist in await music_provider.async_get_library_playlists():
            db_item = await self.mass.music.async_get_playlist(
                playlist.item_id, provider_id
            )
            if db_item.checksum != playlist.checksum:
                db_item = await self.mass.database.async_add_playlist(playlist)
            cur_db_ids.append(db_item.item_id)
            await self.mass.database.async_add_to_library(
                db_item.item_id, MediaType.Playlist, playlist.provider
            )
            # precache playlist tracks
            for playlist_track in await self.mass.music.async_get_playlist_tracks(
                playlist.item_id, provider_id
            ):
                # try to find substitutes for unavailable tracks with matching technique
                if not playlist_track.available:
                    if playlist_track.provider == "database":
                        await self.mass.music.async_match_track(playlist_track)
                    else:
                        await self.mass.music.async_add_track(playlist_track)
        # process playlist deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.async_remove_from_library(
                    db_id, MediaType.Playlist, provider_id
                )
        # store ids in cache for next sync
        await self.mass.cache.async_set(cache_key, cur_db_ids)

    @sync_task("radios")
    async def async_library_radios_sync(self, provider_id: str):
        """Sync library radios for given provider."""
        music_provider = self.mass.get_provider(provider_id)
        cache_key = f"library_radios_{provider_id}"
        prev_db_ids = await self.mass.cache.async_get(cache_key, default=[])
        cur_db_ids = []
        for item in await music_provider.async_get_library_radios():
            db_radio = await self.mass.music.async_get_radio(item.item_id, provider_id)
            cur_db_ids.append(db_radio.item_id)
            await self.mass.database.async_add_to_library(
                db_radio.item_id, MediaType.Radio, provider_id
            )
        # process deletions
        for db_id in prev_db_ids:
            if db_id not in cur_db_ids:
                await self.mass.database.async_remove_from_library(
                    db_id, MediaType.Radio, provider_id
                )
        # store ids in cache for next sync
        await self.mass.cache.async_set(cache_key, cur_db_ids)