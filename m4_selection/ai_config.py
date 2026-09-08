"""Validated, per-run model configuration; no implicit remote endpoint."""
from pathlib import Path
import hashlib
import json
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from m4_optimizer.contracts import StrictModel
from m4_selection.__main__ import _unique_pairs, _reject_constant

DEFAULT_CONFIG_PATH=Path(__file__).resolve().parents[1]/'m4/ai_selection_config.json'


class GenerationOptions(StrictModel):
    temperature: float = Field(default=0.0, ge=0, le=2, allow_inf_nan=False)
    seed: int = Field(default=42, ge=0, le=2147483647)
    num_ctx: int = Field(default=4096, ge=512, le=1048576)
    num_predict: int = Field(default=192, ge=16, le=8192)


class ModelConfig(StrictModel):
    enabled: bool = False
    provider: Literal['ollama'] = 'ollama'
    base_url: str | None = None
    model: str | None = None
    think: bool | Literal['low','medium','high','max'] | None = False
    request_timeout_seconds: float = Field(default=120.0, gt=0, le=900, allow_inf_nan=False)
    stream_budget_seconds: float = Field(default=180.0, gt=0, le=1800, allow_inf_nan=False)
    generation: GenerationOptions = Field(default_factory=GenerationOptions)

    @model_validator(mode='after')
    def validate_endpoint(self):
        if self.base_url is not None:
            parts=urlsplit(self.base_url)
            if (parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password
                    or parts.path not in ('','/') or parts.query or parts.fragment
                    or any(char.isspace() for char in self.base_url)):
                raise ValueError('model endpoint must be an HTTP origin without credentials or path')
            try:parts.port
            except ValueError:raise ValueError('model endpoint port is invalid') from None
            self.base_url=self.base_url.rstrip('/')
        if self.model is not None and (not self.model.strip() or self.model!=self.model.strip() or len(self.model)>200):
            raise ValueError('model name must be nonblank without surrounding whitespace')
        if self.enabled and (self.base_url is None or self.model is None):
            raise ValueError('enabled model requires base_url and model')
        return self

    def digest(self):
        return hashlib.sha256(json.dumps(self.model_dump(mode='json'),sort_keys=True,
            separators=(',',':'),allow_nan=False).encode()).hexdigest()


def load_model_config(path=DEFAULT_CONFIG_PATH):
    try:
        data=json.loads(Path(path).read_text(encoding='utf-8'),object_pairs_hook=_unique_pairs,
                        parse_constant=_reject_constant)
        return ModelConfig.model_validate(data)
    except (OSError,ValueError) as error:
        # Validation errors can contain rejected plaintext, including URL credentials.
        raise ValueError('AI configuration is missing or invalid; no model fallback was applied') from None


def resolve_model_config(path=DEFAULT_CONFIG_PATH,*,base_url=None,model=None,disable=False):
    data=load_model_config(path).model_dump()
    if base_url is not None:data.update(base_url=base_url,enabled=True)
    if model is not None:data.update(model=model,enabled=True)
    if disable:data['enabled']=False
    return ModelConfig.model_validate(data)
