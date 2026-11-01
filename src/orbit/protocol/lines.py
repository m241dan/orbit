"""How text splits into lines, and how much of a failed step's output both sides quote."""

OUTPUT_TAIL_LINES = 10
"""How many of a failed step's output's last lines the verification panel shows and a notice
quotes."""


def text_lines(text: str) -> list[str]:
    """``text``'s lines, split only at newlines, so a form feed or the like stays in its line;
    none for empty text, and without a carriage return before a newline or an empty line
    after the last newline."""
    if not text:
        return []
    return [line.removesuffix("\r") for line in text.removesuffix("\n").split("\n")]


def output_tail(output: str) -> list[str]:
    """The last lines of a failed step's ``output``, as the verification panel shows them under
    its steps and a notice quotes them."""
    return text_lines(output)[-OUTPUT_TAIL_LINES:]
