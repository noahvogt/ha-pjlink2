"""PJLink2 media_player platform."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
import logging
from typing import Any

from aiopjlink import (
    PJLink,
    PJLinkException,
    PJLinkProjectorError,
    Power,
    Sources,
    Lamp,
    Information,
)

from homeassistant import config_entries, core
from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    PLATFORM_SCHEMA,
)
from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_TIMEOUT,
)
from homeassistant.core import HomeAssistant as HomeAssistantType

import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

import voluptuous as vol

from .const import (
    DOMAIN,
    CONF_SOURCES,
    CONF_ENCODING,
    DEFAULT_ENCODING,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    ATTR_PRODUCT_NAME,
    ATTR_MANUFACTURER_NAME,
    ATTR_PROJECTOR_NAME,
    ATTR_RESOLUTION_X,
    ATTR_RESOLUTION_Y,
    ATTR_LAMP_HOURS,
    ProjectorState,
)

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=3)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
        vol.Optional(CONF_NAME): cv.string,
        vol.Optional(CONF_SOURCES): vol.Schema({cv.string: cv.string}),
        vol.Optional(CONF_ENCODING, default=DEFAULT_ENCODING): cv.string,
        vol.Optional(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): cv.positive_float,
    }
)


async def async_setup_platform(
    hass: HomeAssistantType,
    config: ConfigType,
    async_add_entities: Callable,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    host = config.get(CONF_HOST)
    port = config.get(CONF_PORT)
    password = config.get(CONF_PASSWORD)
    timeout = config.get(CONF_TIMEOUT)
    name = config.get(CONF_NAME)
    sources = config.get(CONF_SOURCES)
    pjl = PJLink(host, port, password, timeout)
    devices = [PJLink2MediaPlayer(pjl, name, sources)]
    async_add_entities(devices, update_before_add=False)


class PJLink2MediaPlayer(MediaPlayerEntity):

    _attr_supported_features = (
        MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.SELECT_SOURCE
    )

    def __init__(self, pjl, name, sources):
        super().__init__()
        self._projector = pjl
        self.attrs: dict[str, Any] = {}
        self._name = name
        self._state = MediaPlayerState.OFF

        # The Fix: Decoupling TCP socket state from HA's availability state
        self._socket_open = False
        self._is_available = False

        self._connectionErrorLogged = False
        self._current_source = None

        if sources:
            self._source_mapping = sources
            self._source_list = list(self._source_mapping.values())
            self._reverse_mapping = {
                v: k for k, v in self._source_mapping.items()
            }
            self._dynamic_sources = False
        else:
            self._source_mapping = {}
            self._source_list = []
            self._reverse_mapping = {}
            self._dynamic_sources = True

    async def async_will_remove_from_hass(self) -> None:
        await super().async_will_remove_from_hass()
        if self._socket_open:
            try:
                await self._projector.__aexit__(0, 0, 0)
            except Exception:
                pass

    @property
    def name(self) -> str:
        return self._name

    @property
    def unique_id(self) -> str:
        return self._projector._address

    @property
    def available(self) -> bool:
        return self._is_available

    @property
    def state(self) -> MediaPlayerState:
        return self._state

    @property
    def source(self) -> str | None:
        return self._current_source

    @property
    def source_list(self) -> list[str]:
        return self._source_list

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.attrs

    async def async_turn_on(self) -> None:
        await Power(self._projector).set(Power.ON)
        self._state = MediaPlayerState.ON

    async def async_turn_off(self) -> None:
        await Power(self._projector).set(Power.OFF)
        self._state = MediaPlayerState.OFF

    async def async_select_source(self, source: str) -> None:
        raw_source = self._reverse_mapping.get(source, source)
        source_type = raw_source[0]
        source_index = raw_source[1]

        await Sources(self._projector).set(source_type, source_index)
        self._current_source = source

    async def async_update(self) -> None:
        try:
            if not self._socket_open:
                await self._projector.__aenter__()
                self._socket_open = True
                self._is_available = True
                info = await Information(self._projector).table()
                self.attrs[ATTR_PRODUCT_NAME] = info.get("product_name")
                self.attrs[ATTR_MANUFACTURER_NAME] = info.get(
                    "manufacturer_name"
                )
                self.attrs[ATTR_PROJECTOR_NAME] = info.get("projector_name")
                if self._name is None:
                    self._name = info.get("projector_name")

            pwr = await Power(self._projector).get()
            if pwr == Power.State.OFF:
                self._state = MediaPlayerState.OFF
            elif pwr == Power.State.ON:
                self._state = MediaPlayerState.ON
            elif pwr in (Power.State.COOLING, Power.State.WARMING):
                self._state = MediaPlayerState.ON

            if pwr == Power.ON:
                try:
                    current = await Sources(self._projector).get()
                    if isinstance(current, (tuple, list)):
                        src_type = (
                            current[0].value
                            if hasattr(current[0], "value")
                            else current[0]
                        )
                        src_index = current[1]
                        raw_source = f"{src_type}{src_index}"
                    else:
                        raw_source = str(current)

                    if (
                        self._dynamic_sources
                        and raw_source not in self._source_list
                    ):
                        self._source_list.append(raw_source)

                    self._current_source = self._source_mapping.get(
                        raw_source, raw_source
                    )
                except Exception as e:
                    if "ERR3" in repr(e) or "unavailable" in repr(e):
                        raise e
                    _LOGGER.debug("Ignored error getting source: %s", repr(e))

                try:
                    self.attrs[ATTR_LAMP_HOURS] = await Lamp(
                        self._projector
                    ).hours()
                except Exception:
                    pass

                try:
                    res = await Sources(self._projector).resolution()
                    self.attrs[ATTR_RESOLUTION_X] = res[0]
                    self.attrs[ATTR_RESOLUTION_Y] = res[1]
                except Exception:
                    self.attrs.pop(ATTR_RESOLUTION_X, None)
                    self.attrs.pop(ATTR_RESOLUTION_Y, None)

            elif pwr == Power.State.OFF:
                self.attrs.pop(ATTR_RESOLUTION_X, None)
                self.attrs.pop(ATTR_RESOLUTION_Y, None)
                self._current_source = None

            self._connectionErrorLogged = False

        except Exception as err:
            err_str = repr(err)

            if self._socket_open:
                self._socket_open = False
                try:
                    await self._projector.__aexit__(0, 0, 0)
                except Exception:
                    pass

            if "ERR3" in err_str or "unavailable" in err_str:
                _LOGGER.debug(
                    "Projector is busy switching inputs. Reconnecting next poll."
                )
                # Notice we do NOT set self._is_available = False here!
                # This keeps the attributes stable in Home Assistant.
            else:
                if not self._connectionErrorLogged:
                    _LOGGER.error(
                        "PJLink2 ERROR for %s: %s", self._name, err_str
                    )
                    self._connectionErrorLogged = True
                self._is_available = False
                self._state = MediaPlayerState.OFF
