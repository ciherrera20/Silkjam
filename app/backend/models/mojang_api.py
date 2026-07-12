import uuid

from pydantic import BaseModel


class UserProfile(BaseModel):
    name: str
    id: uuid.UUID