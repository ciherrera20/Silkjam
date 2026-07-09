from dataclasses import dataclass
from pathlib import Path
from typing import Self

import logging

from pydantic import BaseModel, Field, PrivateAttr

logger = logging.getLogger(__name__)


@dataclass
class ServerPropertiesLine:
    key: str | None
    value: str


def parse_properties(path: Path) -> tuple[dict[str, str], list[ServerPropertiesLine]]:
    """Parse a Minecraft server.properties file."""

    properties: dict[str, str] = {}
    layout: list[ServerPropertiesLine] = []

    with path.open(encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")

            stripped = line.strip()

            # Preserve blank lines.
            if not stripped:
                layout.append(ServerPropertiesLine(None, ""))
                continue

            # Preserve comments.
            if stripped.startswith("#"):
                layout.append(ServerPropertiesLine(None, line))
                continue

            key, sep, value = line.partition("=")
            if not sep:
                logger.warning("Incomplete property line: %s", line)
                layout.append(ServerPropertiesLine(None, line))
                continue

            key = key.strip()
            value = value.strip()

            if key in properties:
                logger.warning("Duplicate key %s, last value will be kept", key)

            properties[key] = value
            layout.append(ServerPropertiesLine(key, value))

    return properties, layout


class ServerProperties(BaseModel):
    motd: str
    max_players: int = Field(alias="max-players")
    server_port: int | None = Field(alias="server-port")
    rcon_port: int | None = Field(alias="rcon.port")
    rcon_password: str | None = Field(alias="rcon.password")
    enable_rcon: bool = Field(alias="enable-rcon")

    _layout: list[ServerPropertiesLine] = PrivateAttr(default_factory=list)

    @classmethod
    def default(cls) -> Self:
        return cls.model_construct(
            _fields_set=None,
            **{
                "motd": "A Minecraft Server",
                "max-players": 20,
                "server-port": None,
                "rcon.port": None,
                "rcon.password": None,
                "enable-rcon": True,
            },
        )

    @classmethod
    def load(cls, path: Path) -> Self:
        path.touch(exist_ok=True)

        if path.stat().st_size > 0:
            props, layout = parse_properties(path)
            config = cls.model_validate(props, by_alias=True)
            config._layout = layout
        else:
            config = cls.default()
            config.dump(path)

        return config

    def dump(self, path: Path) -> None:
        values = {
            key: (
                ""
                if value is None
                else str(value).lower()
                if isinstance(value, bool)
                else str(value)
            )
            for key, value in self.model_dump(by_alias=True).items()
        }

        seen: set[str] = set()

        with path.open("w", encoding="utf-8", newline="\n") as f:
            for line in self._layout:
                if line.key is None:
                    f.write(line.value + "\n")
                    continue

                value = values[line.key]
                f.write(f"{line.key}={value}\n")
                seen.add(line.key)

            # Append newly-added properties.
            for key, value in values.items():
                if key not in seen:
                    f.write(f"{key}={value}\n")