"""PJLink2 media_player platform."""
from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
import logging
from typing import Any

from aiopjlink import PJLink, PJLinkException, PJLinkProjectorError, Power, Sources, Lamp, Information 

from homeassistant import config_entries, core
from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    PLATFORM_SCHEMA
)
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_NAME, CONF_PASSWORD, CONF_TIMEOUT
from homeassistant.core import HomeAssistant as HomeAssistantType

import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

import voluptuous as vol

from .const import DOMAIN, CONF_ENCODING, DEFAULT_ENCODING, DEFAULT_PORT, DEFAULT_TIMEOUT, ATTR_PRODUCT_NAME, ATTR_MANUFACTURER_NAME, ATTR_PROJECTOR_NAME, ATTR_RESOLUTION_X, ATTR_RESOLUTION_Y, ATTR_LAMP_HOURS, ProjectorState

_LOGGER = logging.getLogger(__name__)
# Time between updating data from projector
SCAN_INTERVAL = timedelta(seconds=3)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
        vol.Optional(CONF_NAME): cv.string,
        vol.Optional(CONF_ENCODING, default=DEFAULT_ENCODING): cv.string,
        vol.Optional(CONF_PASSWORD) : cv.string,
        vol.Optional(CONF_TIMEOUT, default=DEFAULT_TIMEOUT) : cv.positive_float
    }
)

async def async_setup_platform(
    hass: HomeAssistantType,
    config: ConfigType,
    async_add_entities: Callable,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the media_player platform."""
    host = config.get(CONF_HOST)
    port =  config.get(CONF_PORT)
    password = config.get(CONF_PASSWORD)
    timeout = config.get(CONF_TIMEOUT)
    name = config.get(CONF_NAME)
    pjl = PJLink(host, port, password, timeout)
    devices = [PJLink2MediaPlayer(pjl, name)]
    async_add_entities(devices, update_before_add=False)


class PJLink2MediaPlayer(MediaPlayerEntity):
    """Representation of a PJLink2 media player."""

    _attr_supported_features = (
        MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.SELECT_SOURCE
    )

    def __init__(self, pjl, name):
        super().__init__()
        self._projector = pjl
        self.attrs: dict[str, Any] = {}
        self._name = name
        self._state = MediaPlayerState.OFF
        self._available = False
        self._connectionErrorLogged = False
        self._current_source = None
        
        # PJLink standard inputs. You can modify these to friendly names later.
        # Format is typically "31" for HDMI1, "32" for HDMI2, etc.
        self._source_list = ["11", "12", "21", "22", "31", "32", "33"] 

    async def async_will_remove_from_hass(self) -> None:
        """Close connection."""
        await super().async_will_remove_from_hass()
        if self._available:
            try:
                await self._projector.__aexit__(0,0,0)
            except (PJLinkException, OSError) as err:
                _LOGGER.error("PJLink2 ERROR when closing connection: %s", repr(err))

    @property
    def name(self) -> str:
        return self._name

    @property
    def unique_id(self) -> str:
        return self._projector._address

    @property
    def available(self) -> bool:
        return self._available

    @property
    def state(self) -> MediaPlayerState:
        return self._state

    @property
    def source(self) -> str | None:
        """Name of the current input source."""
        return self._current_source

    @property
    def source_list(self) -> list[str]:
        """List of available input sources."""
        return self._source_list

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the custom PJLink2 attributes."""
        return self.attrs

    async def async_turn_on(self) -> None:
        """Turn the projector on."""
        await Power(self._projector).set(Power.ON)
        self._state = MediaPlayerState.ON

    async def async_turn_off(self) -> None:
        """Turn the projector off."""
        await Power(self._projector).set(Power.OFF)
        self._state = MediaPlayerState.OFF

    async def async_select_source(self, source: str) -> None:
        """Select input source."""
        await Sources(self._projector).set(source)
        self._current_source = source

    async def async_update(self) -> None:
        """Update data from projector."""
        try:
            if not self._available:
                await self._projector.__aenter__()
                self._available = True
                info = await Information(self._projector).table()
                self.attrs[ATTR_PRODUCT_NAME] = info["product_name"]
                self.attrs[ATTR_MANUFACTURER_NAME] = info["manufacturer_name"]
                self.attrs[ATTR_PROJECTOR_NAME] = info["projector_name"]
                if self._name == None: self._name = info["projector_name"]
                
            pwr = await Power(self._projector).get()
            if pwr == Power.State.OFF: self._state = MediaPlayerState.OFF
            elif pwr == Power.State.ON: self._state = MediaPlayerState.ON
            elif pwr in (Power.State.COOLING, Power.State.WARMING): self._state = MediaPlayerState.ON # Keeps UI active during transition
            
            if pwr == Power.ON:
                res = await Sources(self._projector).resolution()
                self.attrs[ATTR_RESOLUTION_X] = res[0]
                self.attrs[ATTR_RESOLUTION_Y] = res[1]
                self.attrs[ATTR_LAMP_HOURS] = await Lamp(self._projector).hours()
                
                # Fetch current source
                self._current_source = await Sources(self._projector).get()
            else:
                self.attrs.pop(ATTR_RESOLUTION_X, None)
                self.attrs.pop(ATTR_RESOLUTION_Y, None)
                self._current_source = None
                
            self._connectionErrorLogged = False 
        
        except PJLinkProjectorError:
            self.attrs.pop(ATTR_RESOLUTION_X, None)
            self.attrs.pop(ATTR_RESOLUTION_Y, None)
        except (PJLinkException, OSError) as err:
            if not self._connectionErrorLogged: 
                _LOGGER.error("PJLink2 ERROR for %s: %s", self._name, repr(err))
                self._connectionErrorLogged = True
            self._state = MediaPlayerState.OFF
            if self._available:
                self._available = False
                try:
                    await self._projector.__aexit__(0,0,0)
                except Exception:
                    pass
