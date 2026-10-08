"""交易系统健康监控。"""


class HealthMonitor:
    """把连接、账户、持仓和行情健康统一成 fail-closed 判断。"""

    def __init__(self, max_market_delay_seconds: float = 10.0) -> None:
        self.max_market_delay_seconds = max_market_delay_seconds

    def evaluate(
        self,
        *,
        connected: bool,
        account_ready: bool,
        position_ready: bool,
        quotes_ready: bool,
        max_quote_age: float,
    ) -> str:
        """返回健康异常原因；空字符串表示通过。"""
        if not connected:
            return "broker connection is not healthy"
        if not account_ready:
            return "account snapshot is not ready"
        if not position_ready:
            return "position snapshot is not ready"
        if not quotes_ready:
            return "required market quotes are missing"
        if max_quote_age > self.max_market_delay_seconds:
            return "market quote is stale"
        return ""
