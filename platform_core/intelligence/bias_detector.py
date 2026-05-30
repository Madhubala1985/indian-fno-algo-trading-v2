from platform_core.data.bar_builder import (
    LiveBar,
)


class BiasDetector:

    def detect_bias(
        self,
        *,
        bar: LiveBar | None,
    ) -> str:

        if bar is None:
            return "NEUTRAL"

        if (
            bar.close_price
            > bar.open_price
        ):
            return "BULLISH"

        if (
            bar.close_price
            < bar.open_price
        ):
            return "BEARISH"

        return "NEUTRAL"

    def detect_multi_timeframe_bias(
        self,
        *,
        h4_bar: LiveBar | None,
        h1_bar: LiveBar | None,
        m15_bar: LiveBar | None,
    ) -> dict[str, str]:

        return {
            "h4_bias": (
                self.detect_bias(
                    bar=h4_bar
                )
            ),
            "h1_bias": (
                self.detect_bias(
                    bar=h1_bar
                )
            ),
            "m15_bias": (
                self.detect_bias(
                    bar=m15_bar
                )
            ),
        }
