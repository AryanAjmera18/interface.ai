"""Parse fixture HTML for assertions; forbid live-service imports."""

from html.parser import HTMLParser

from pydantic import BaseModel, Field


class Control(BaseModel):
    tag: str
    role: str
    name: str
    field: str = ""
    value: str = ""
    attributes: dict[str, str] = Field(default_factory=dict)


class Surface(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.controls: list[Control] = []
        self.tokens: list[str] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "input" and values.get("type") == "hidden":
            if values.get("name") == "token":
                self.tokens.append(values["value"])
            return
        if tag in {"input", "select", "a", "button"} or "onclick" in values:
            self.controls.append(
                Control(
                    tag=tag,
                    role=values.get(
                        "role",
                        {
                            "input": "textbox",
                            "select": "combobox",
                            "a": "link",
                            "button": "button",
                        }.get(tag, ""),
                    ),
                    name=values.get("aria-label", ""),
                    field=values.get("name", ""),
                    value=values.get("value", ""),
                    attributes=values,
                )
            )
