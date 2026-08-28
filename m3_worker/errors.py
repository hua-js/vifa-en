class M3Error(RuntimeError):
    def __init__(
        self, code: str, message: str, details: dict[str, object] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def public_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "details": self.details}
