import jproperties
from pathlib import Path
from pydantic import BaseModel, Field

class ServerProperties(BaseModel):
    motd: str
    max_players: int = Field(alias="max-players")
    server_port: int | None = Field(alias="server-port")
    rcon_port: int | None = Field(alias="rcon.port")
    rcon_password: str | None = Field(alias="rcon.password")
    enable_rcon: bool = Field(alias="enable-rcon")

    @classmethod
    def default(cls):
        return cls(**{
            "motd": "A Minecraft Server",
            "max-players": 20,
            "server-port": None,
            "rcon.port": None,
            "rcon.password": None,
            "enable-rcon": True
        })

    @classmethod
    def load(cls, path: Path):
        path.touch(exist_ok=True)
        if path.stat().st_size > 0:
            props = jproperties.Properties()
            props.load(path.read_text())
            config = cls.model_validate({k: v.data for k, v in props.items()}, by_alias=True)
        else:
            config = cls.default()
            config.dump(path)
        return config

    def dump(self, path: Path):
        props = jproperties.Properties()
        if path.stat().st_size > 0:
            props.load(path.read_text())
        for k, v in self.model_dump(by_alias=True).items():
            if isinstance(v, bool):
                props[k] = str(v).lower()
            elif v is None:
                props[k] = ""
            else:
                props[k] = str(v)
        with path.open("wb") as f:
            props.store(f)