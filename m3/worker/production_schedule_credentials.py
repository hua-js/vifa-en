"""Server-side production schedule credential, embedded at user request."""

from pydantic import SecretStr

DEFAULT_PRODUCTION_SCHEDULE_API_KEY = SecretStr("vf_ps_9oidIoATCJkbrFywSGHihI5bYOuUG2t_EFlEylKIBCf3gDYgfivb7S48ThYZA7W9")
