import json
from pathlib import Path
from pydantic import BaseModel, Field

class ProxyListing(BaseModel):
    name: str
    port: int
    enabled: bool

class SleepProperties(BaseModel):
    timeout: int | None = None
    online_players: int = Field(default=0, alias="online-players")
    max_players: int | None = Field(default=None, alias="max-players")
    motd: str | None = None
    waking_kick_msg: str = Field(default="§eServer is waking up, try again in 30s", alias="waking-kick-msg")

class Version(BaseModel):
    name: str
    protocol: int

class ServerListing(BaseModel):
    name: str
    subdomain: str
    version: Version
    proxy: str
    sleep_properties: SleepProperties = SleepProperties()
    enabled: bool

class Config(BaseModel):
    proxy_listing: list[ProxyListing]
    server_listing: list[ServerListing]

    @classmethod
    def default(cls):
        return cls(
            proxy_listing=[ProxyListing(
                name="proxy1",
                port=25565,
                enabled=True
            )],
            server_listing=[]
        )

    @classmethod
    def load(cls, path: Path):
        path.touch(exist_ok=True)
        if path.stat().st_size > 0:
            config = cls(**json.loads(path.read_text()))
        else:
            config = cls.default()
            config.dump(path)
        return config

    def dump(self, path: Path):
        path.write_text(self.model_dump_json(indent=4, by_alias=True))