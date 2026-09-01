"""The wire contract between the analyst and the chart, mirrored from the frontend.

The authority for this vocabulary is ``frontend/src/lib/charts/types.ts``. The two
must stay in step; a shape added on one side and not the other is dropped in
silence, because the frontend ignores stream frames whose ``type`` it does not
recognise and ignores command ops it cannot match.

Facts this module is built around:

  - Times are UTC seconds, matching the chart engine's single internal time model.
    Never milliseconds, never bar indices. OpenAlgo hands back daily bars
    timezone-naive and intraday bars tz-aware Asia/Kolkata, so normalisation
    happens once, on the way in, and everything downstream is UTC seconds.

  - Shapes carry a semantic ``tone``, not a colour. The chart resolves tone
    against the active theme. Drawing style stores literal colour strings, so a
    shape that carried its own hex would be stranded on the old palette after a
    theme swap.

  - Anchors are computed here, in Python, and narrated by the model. A model
    eyeballing 200 bars of OHLCV and inventing a swing high is the one failure
    mode this whole design exists to avoid.

  - Shapes travel on their own stream event, not in a tool result. Tool results
    pass through a 12,000 character cap; an envelope with thirty vertices plus a
    caption would be truncated into invalid JSON somewhere in the middle.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Tone = Literal["bullish", "bearish", "neutral"]

#: Marks a drawing as the analyst's rather than the user's. The chart's hit-test
#: parser splits an external id on "#", so a group id must never contain one.
AI_PREFIX = "ai"


class Anchor(BaseModel):
    """One anchor in data space.

    Attributes:
        time: UTC seconds. May sit between bars, or past the last one.
        price: Price on the pane's scale.
    """

    time: float
    price: float


class Envelope(BaseModel):
    """A closed band through real swing points.

    The primary markup shape. Highs run left to right, lows are reversed and
    appended, and the path is filled. This is not a parallel channel: the
    boundaries step and bend, tracking the pivots the detector actually found.
    """

    kind: Literal["envelope"] = "envelope"
    highs: list[Anchor]
    lows: list[Anchor]
    label: str | None = None
    tone: Tone | None = None


class Trendline(BaseModel):
    """A straight line between two anchors, optionally projected right."""

    kind: Literal["trendline"] = "trendline"
    from_: Anchor = Field(alias="from")
    to: Anchor
    extend_right: bool = Field(default=False, alias="extendRight")
    label: str | None = None
    tone: Tone | None = None

    model_config = {"populate_by_name": True}


class Level(BaseModel):
    """A horizontal support or resistance level.

    Attributes:
        ray: Start at ``time`` instead of spanning the whole pane.
    """

    kind: Literal["level"] = "level"
    price: float
    time: float
    ray: bool = False
    label: str | None = None
    tone: Tone | None = None


class Zone(BaseModel):
    """A rectangular region, for supply and demand areas."""

    kind: Literal["zone"] = "zone"
    from_: Anchor = Field(alias="from")
    to: Anchor
    label: str | None = None
    tone: Tone | None = None

    model_config = {"populate_by_name": True}


class Channel(BaseModel):
    """A straight parallel channel, for when the user explicitly asks for one.

    Attributes:
        offset: Signed price distance from the base line to the second rail.
            Negative puts the second rail below the first.
    """

    kind: Literal["channel"] = "channel"
    from_: Anchor = Field(alias="from")
    to: Anchor
    offset: float
    label: str | None = None
    tone: Tone | None = None

    model_config = {"populate_by_name": True}


class Fib(BaseModel):
    """A Fibonacci retracement across one leg."""

    kind: Literal["fib"] = "fib"
    from_: Anchor = Field(alias="from")
    to: Anchor
    levels: list[float] | None = None
    label: str | None = None
    tone: Tone | None = None

    model_config = {"populate_by_name": True}


class Callout(BaseModel):
    """A labelled bubble seated clear of price, with a leader line to ``at``."""

    kind: Literal["callout"] = "callout"
    at: Anchor
    seat: Anchor
    text: str
    tone: Tone | None = None


class Marker(BaseModel):
    """A dot plus a price label, for naming a single pivot."""

    kind: Literal["marker"] = "marker"
    at: Anchor
    text: str
    tone: Tone | None = None


AnnotationShape = (
    Envelope | Trendline | Level | Zone | Channel | Fib | Callout | Marker
)


class ChartIndicator(BaseModel):
    """One indicator currently on the chart."""

    instance_id: str = Field(alias="instanceId")
    indicator_id: str = Field(alias="indicatorId")
    name: str
    pane_index: int = Field(default=0, alias="paneIndex")
    settings: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class ChartContext(BaseModel):
    """What the analyst is told about the chart, every turn.

    The reference interaction supplies no symbol, exchange, interval or date, so
    all four are resolved from here. If the model has to ask which symbol, the
    feature has already failed.

    ``visible_from`` and ``visible_to`` matter more than they look: "draw the
    visible highs and lows" is clipped to the viewport, so it has to travel with
    the request rather than being inferred from the loaded range.
    """

    symbol: str = ""
    exchange: str = ""
    interval: str = ""
    chart_type: str = Field(default="candlestick", alias="chartType")
    bar_count: int = Field(default=0, alias="barCount")
    first_time: float | None = Field(default=None, alias="firstTime")
    last_time: float | None = Field(default=None, alias="lastTime")
    visible_from: float | None = Field(default=None, alias="visibleFrom")
    visible_to: float | None = Field(default=None, alias="visibleTo")
    last_price: float | None = Field(default=None, alias="lastPrice")
    indicators: list[ChartIndicator] = Field(default_factory=list)
    theme: Literal["dark", "light"] = "dark"

    model_config = {"populate_by_name": True}

    def describe(self) -> str:
        """Render the context as one line for the model's system message.

        Returns:
            A plain sentence naming the instrument, timeframe and loaded range,
            or a note that no chart is open.
        """
        if not self.symbol:
            return "No chart is currently open."
        parts = [f"The user is looking at {self.symbol} on {self.exchange}, {self.interval}."]
        if self.bar_count:
            parts.append(f"{self.bar_count} bars loaded.")
        if self.last_price is not None:
            parts.append(f"Last price {self.last_price}.")
        if self.indicators:
            names = ", ".join(i.name for i in self.indicators)
            parts.append(f"Indicators on the chart: {names}.")
        return " ".join(parts)


def draw_command(group: str, shapes: list[Any]) -> dict[str, Any]:
    """Build a draw command.

    Replaces the named group and leaves every other group, and every hand-placed
    drawing, untouched.

    Args:
        group: Group id. Must not contain "#".
        shapes: Annotation shapes, already validated.

    Returns:
        A command dict ready to put on the stream.
    """
    return {
        "op": "draw",
        "group": group,
        "shapes": [
            s.model_dump(by_alias=True, exclude_none=True) if isinstance(s, BaseModel) else s
            for s in shapes
        ],
    }


def clear_command(group: str | None = None) -> dict[str, Any]:
    """Build a clear command.

    Args:
        group: The group to remove, or None for every analyst group. Never
            touches a drawing the user placed by hand.

    Returns:
        A command dict ready to put on the stream.
    """
    return {"op": "clear"} if group is None else {"op": "clear", "group": group}
