"""Channel discovery, property queries, and logical name resolution."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .adapters.oxygen import ChannelInfo, OxygenAdapter
from .config import ChannelMapping, EngineConfig

logger = logging.getLogger(__name__)


class ChannelError(Exception):
    """Channel resolution or discovery failure."""


@dataclass
class ResolvedChannel:
    logical_name: str
    oxygen_name: str
    channel_id: str
    role: str
    channel_type: str = "unknown"
    properties: list[str] = field(default_factory=list)


class ChannelManager:
    """Discover and resolve OXYGEN channels; never guess unknown property names."""

    def __init__(self, adapter: OxygenAdapter, config: EngineConfig) -> None:
        self.adapter = adapter
        self.config = config
        self._by_logical: dict[str, ResolvedChannel] = {}
        self._by_oxygen_name: dict[str, ResolvedChannel] = {}
        self._by_id: dict[str, ResolvedChannel] = {}
        self._discovered: list[ChannelInfo] = []
        self._setup_fingerprint: str | None = None

    @property
    def discovered(self) -> list[ChannelInfo]:
        return list(self._discovered)

    def discover(self) -> list[ChannelInfo]:
        channels = self.adapter.list_channels()
        self._discovered = channels
        fingerprint = "|".join(sorted(f"{c.channel_id}:{c.name}" for c in channels))
        if self._setup_fingerprint is not None and fingerprint != self._setup_fingerprint:
            logger.warning("OXYGEN setup change detected (channels renamed/removed/added)")
        self._setup_fingerprint = fingerprint
        self._rebuild_mappings(channels)
        return channels

    def _rebuild_mappings(self, channels: list[ChannelInfo]) -> None:
        by_name = {c.name: c for c in channels}
        by_id = {c.channel_id: c for c in channels}
        self._by_logical.clear()
        self._by_oxygen_name.clear()
        self._by_id.clear()

        mappings: list[ChannelMapping] = list(self.config.channels)
        # Also include DO and AOUT configured names as discoverable.
        for do in self.config.do_commands:
            mappings.append(
                ChannelMapping(
                    logical_name=do.logical_name,
                    oxygen_name=do.oxygen_name,
                    channel_id=do.channel_id,
                    role="digital_output",
                )
            )
        for aout in self.config.aout_roles:
            mappings.append(
                ChannelMapping(
                    logical_name=aout.logical_name,
                    oxygen_name=aout.oxygen_name,
                    channel_id=aout.channel_id,
                    role=aout.role if aout.role != "none" else "aout",
                )
            )

        seen_logical: set[str] = set()
        for mapping in mappings:
            if mapping.logical_name in seen_logical:
                continue
            seen_logical.add(mapping.logical_name)
            info: ChannelInfo | None = None
            if mapping.channel_id and mapping.channel_id in by_id:
                info = by_id[mapping.channel_id]
                if info.name != mapping.oxygen_name:
                    logger.warning(
                        "Channel ID %s name changed: expected %r, found %r",
                        mapping.channel_id,
                        mapping.oxygen_name,
                        info.name,
                    )
            elif mapping.oxygen_name in by_name:
                info = by_name[mapping.oxygen_name]
            else:
                resolved = self.adapter.resolve_channel_id(mapping.oxygen_name)
                if resolved and resolved in by_id:
                    info = by_id[resolved]

            if info is None:
                logger.warning(
                    "Channel not found: logical=%s oxygen_name=%s",
                    mapping.logical_name,
                    mapping.oxygen_name,
                )
                continue

            props: list[str] = []
            try:
                props = self.adapter.list_properties(info.channel_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not list properties for %s: %s", info.name, exc)

            resolved_ch = ResolvedChannel(
                logical_name=mapping.logical_name,
                oxygen_name=info.name,
                channel_id=info.channel_id,
                role=mapping.role,
                channel_type=info.channel_type,
                properties=props,
            )
            self._by_logical[mapping.logical_name] = resolved_ch
            self._by_oxygen_name[info.name] = resolved_ch
            self._by_id[info.channel_id] = resolved_ch

    def get(self, logical_name: str) -> ResolvedChannel:
        if logical_name not in self._by_logical:
            raise ChannelError(f"Unresolved logical channel: {logical_name}")
        return self._by_logical[logical_name]

    def get_by_oxygen_name(self, oxygen_name: str) -> ResolvedChannel:
        if oxygen_name not in self._by_oxygen_name:
            raise ChannelError(f"Unresolved OXYGEN channel name: {oxygen_name}")
        return self._by_oxygen_name[oxygen_name]

    def resolve_id(self, oxygen_name: str) -> str:
        return self.get_by_oxygen_name(oxygen_name).channel_id

    def get_property(self, logical_or_oxygen: str, prop: str) -> str:
        """Read a property; prop must be explicitly provided (never guessed)."""
        ch = self._lookup(logical_or_oxygen)
        if ch.properties and prop not in ch.properties:
            # Refresh properties once — setup may have changed.
            ch.properties = self.adapter.list_properties(ch.channel_id)
            if prop not in ch.properties:
                raise ChannelError(
                    f"Property {prop!r} not available on channel {ch.oxygen_name}. "
                    f"Available: {ch.properties}"
                )
        return self.adapter.get_property(ch.channel_id, prop)

    def _lookup(self, name: str) -> ResolvedChannel:
        if name in self._by_logical:
            return self._by_logical[name]
        if name in self._by_oxygen_name:
            return self._by_oxygen_name[name]
        raise ChannelError(f"Unknown channel: {name}")
