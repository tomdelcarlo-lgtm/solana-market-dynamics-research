"""Historical exception type separated to keep dry-run core dependency-free."""
GUARD_DATA_ERROR_THRESHOLD_BPS = 30_000

class EntryPremiumAbort(Exception):
    """Raised when the live curve price exceeds the signal price cap before send."""
    def __init__(self, premium_bps: int, current_price: float, signal_price: float) -> None:
        self.premium_bps = premium_bps
        self.current_price = current_price
        self.signal_price = signal_price
        self.is_data_error = premium_bps > GUARD_DATA_ERROR_THRESHOLD_BPS
        if self.is_data_error:
            super().__init__(
                f"entry_premium_guard_data_error: posible unit mismatch, "
                f"premium={premium_bps}bps current={current_price:.10f} "
                f"signal={signal_price:.10f}"
            )
        else:
            super().__init__(
                f"entry_premium_guard aborted: premium={premium_bps}bps "
                f"current={current_price:.10f} signal={signal_price:.10f}"
            )
