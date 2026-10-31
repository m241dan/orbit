# Docstrings and comments

- A docstring says in short plain English what the function returns or does. It opens "The …"
  for a value, "Whether …" for a bool, and with an imperative for an action. Parameters appear in
  ``double backticks``.
- A class docstring documents the constructor: it names every `__init__` parameter in ``double
  backticks``, and `__init__` has no docstring of its own. A dataclass's docstring names its
  fields the same way.
- Every module opens with a one-line docstring naming its subject, tests included.
- A constant or a type alias has a docstring on the line below it.
- A function names what it raises as "Raises :class:`X` when …".
- Docstrings are prose wrapped at 100 columns. A one-paragraph docstring closes its quotes on the
  text line; a longer one closes them on a line of their own.
- Single backticks appear only inside Sphinx roles such as :class:`X`.
- Comments are rare and say why: a catch-all, a cleanup, an assert that narrows a type.
