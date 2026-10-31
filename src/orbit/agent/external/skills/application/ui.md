# User interfaces

- **Presentation rules** are plain functions (Domain): what to show, in what order, with what
  wording.
- **Controllers** hold UI state and drive widgets through Protocols (Application): a passive view.
- **Widgets** only lay out what they are given (External). The framework's event loop is someone
  else's Execution, and user input arrives as External events.
- **Testing**: test presentation rules and controllers with fake widgets, synchronously and
  without the framework; test widgets with the framework's test pilot.
- **Keep UI state in step with the backend**:
  - undo an optimistic change the backend refuses, and clear waiting states on refusal;
  - keep selections by identity rather than position;
  - show one modal at a time;
  - rebuild from the backend's snapshot on a switch or a reconnect.
- **Wording**: messages and help text a user can follow word for word, kept in Domain with the
  rest of the presentation rules.
