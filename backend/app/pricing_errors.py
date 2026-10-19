"""Machine-readable failures shared by calculation, repair, audit and presentation."""


class PricingError(ValueError):
    def __init__(self, code, message, paths=(), *, cause="plan_repair", action="repair_estimate"):
        super().__init__(message)
        self.diagnostic = {"code": code, "message": message, "paths": list(paths),
                           "cause": cause, "action": action}
