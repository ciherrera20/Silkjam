import json
from pathlib import Path
from pydantic import BaseModel, Field, PrivateAttr, model_validator, computed_field

class ProxyListing(BaseModel):
    name: str
    port: int
    enabled: bool

    # Annotate listing as valid or not
    _errors: list[str] = PrivateAttr(default_factory=list)

    @property
    def valid(self):
        return len(self._errors) == 0

    @property
    def errors(self):
        return self._errors

class SleepProperties(BaseModel):
    timeout: int | None = None
    motd: str | None = None
    waking_kick_msg: str = Field(default="§eServer is waking up, try again soon...", alias="waking-kick-msg")

class Version(BaseModel):
    name: str
    protocol: int

UNKNOWN_VERSION = Version(name="unknown", protocol=0)

class ServerListing(BaseModel):
    name: str
    subdomain: str
    version: Version = UNKNOWN_VERSION
    proxy: str | None
    sleep_properties: SleepProperties = SleepProperties()
    enabled: bool

    # Annotate listing as valid or not
    _errors: list[str] = PrivateAttr(default_factory=list)

    @property
    def valid(self):
        return len(self._errors) == 0

    @property
    def errors(self):
        return self._errors

class Config(BaseModel):
    proxy_listing: list[ProxyListing]
    server_listing: list[ServerListing]

    @model_validator(mode="after")
    def validate_semantics(self):
        proxy_names = set()
        proxy_ports = set()
        server_names = set()
        server_subdomains = set()
        for listing in self.proxy_listing:
            if listing.name in proxy_names:
                listing.errors.append(f"Proxy name \"{listing.name}\" is already taken")
            if listing.port in proxy_ports:
                listing.errors.append(f"Proxy port \"{listing.port}\" is already taken")
            if listing.valid:
                proxy_names.add(listing.name)
                proxy_ports.add(listing.port)
        for listing in self.server_listing:
            if listing.name in server_names:
                listing.errors.append(f"Server name \"{listing.name}\" is already taken")
            if listing.subdomain in server_names:
                listing.errors.append(f"Server subdomain \"{listing.subdomain}\" is already taken")
            if listing.proxy not in proxy_names:
                listing.errors.append(f"Server proxy \"{listing.proxy}\" does not exist")
            if listing.valid:
                server_names.add(listing.name)
                server_subdomains.add(listing.subdomain)
        return self

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