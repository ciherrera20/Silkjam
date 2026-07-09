import enum
from pydantic import BaseModel, Field, PrivateAttr, PositiveInt, NonNegativeInt

class SleepProperties(BaseModel):
    timeout: PositiveInt | None = None
    motd: str | None = None
    waking_kick_msg: str = Field(default="§eServer is waking up, try again soon...", alias="waking-kick-msg")

class BackupStrategy(str, enum.Enum):
    EXPONENTIAL = "exponential"
    FIXED = "fixed"

    def __str__(self) -> str:
        return self.value

class BackupProperties(BaseModel):
    interval: PositiveInt | None
    max_backups: NonNegativeInt
    strategy: BackupStrategy = BackupStrategy.EXPONENTIAL
    enabled: bool

class Version(BaseModel):
    name: str
    protocol: int

UNKNOWN_VERSION = Version(name="unknown", protocol=0)

class ServerListing(BaseModel):
    version: Version = UNKNOWN_VERSION
    sleep_properties: SleepProperties = SleepProperties()
    backup_properties: BackupProperties = BackupProperties(
        interval=60,
        max_backups=1,
        strategy=BackupStrategy.EXPONENTIAL,
        enabled=False
    )
    enabled: bool

    # Annotate listing as valid or not
    _errors: list[str] = PrivateAttr(default_factory=list)

    @property
    def valid(self) -> bool:
        return len(self._errors) == 0

    @property
    def errors(self) -> list[str]:
        return self._errors