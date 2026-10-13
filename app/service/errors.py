class BusinessError(Exception):
    def __init__(self, status, code, detail, location=None):
        self.status = status
        self.payload = {"code": code, "detail": detail, "issues": []}
        if location:
            self.payload["issues"].append({"location": location, "reason": detail})
        super().__init__(detail)


def invalid(detail, location, code="INVALID_REQUEST"):
    raise BusinessError(400, code, detail, location)
