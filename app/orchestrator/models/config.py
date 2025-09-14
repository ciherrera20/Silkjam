import re
import enum
import json
from pathlib import Path
from pydantic import BaseModel, Field, PrivateAttr, model_validator, computed_field

SUBDOMAIN_REGEX: re.Pattern = re.compile(r"(?:(?!-)[a-zA-Z0-9\-]+\.)*?(?!-)[a-zA-Z0-9\-]+")

class ProxyListing(BaseModel):
    name: str
    port: int
    enabled: bool
    subdomains: dict[str, str]

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

class BackupStrategy(str, enum.Enum):
    EXPONENTIAL = "exponential"
    FIXED = "fixed"

    def __str__(self):
        return self.value

class BackupProperties(BaseModel):
    interval: int
    max_backups: int
    strategy: BackupStrategy = BackupStrategy.EXPONENTIAL

class Version(BaseModel):
    name: str
    protocol: int

UNKNOWN_VERSION = Version(name="unknown", protocol=0)

class ServerListing(BaseModel):
    name: str
    version: Version = UNKNOWN_VERSION
    sleep_properties: SleepProperties = SleepProperties()
    backup_properties: BackupProperties | None = BackupProperties(interval=60, max_backups=1, strategy=BackupStrategy.EXPONENTIAL)
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
        for listing in self.server_listing:
            if listing.name in server_names:
                listing.errors.append(f"Server name \"{listing.name}\" is already taken")
            if listing.valid:
                server_names.add(listing.name)
        for listing in self.proxy_listing:
            if listing.name in proxy_names:
                listing.errors.append(f"Proxy name \"{listing.name}\" is already taken")
            if listing.port in proxy_ports:
                listing.errors.append(f"Proxy port \"{listing.port}\" is already taken")
            for subdomain, server_name in listing.subdomains.items():
                if subdomain != "" and not SUBDOMAIN_REGEX.fullmatch(subdomain):
                    listing.errors.append(f"Proxy subdomain \"{subdomain}\" is invalid")
                if server_name not in server_names:
                    listing.errors.append(f"Proxy subdomain \"{subdomain}\" routes to non-existent server \"{server_name}\"")
            if listing.valid:
                proxy_names.add(listing.name)
                proxy_ports.add(listing.port)
        return self

    @classmethod
    def default(cls):
        return cls(
            proxy_listing=[ProxyListing(
                name="proxy1",
                port=25565,
                enabled=True,
                subdomains={}
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