import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, HttpUrl, StringConstraints, UrlConstraints

EndpointName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=255,
    ),
]

EndpointUrl = Annotated[
    HttpUrl,
    UrlConstraints(max_length=2048),
]


class WebhookEndpointCreate(BaseModel):
    name: EndpointName
    target_url: EndpointUrl


class WebhookEndpointResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    target_url: EndpointUrl
    is_active: bool
    created_at: datetime
    updated_at: datetime
