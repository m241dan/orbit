"""The protocol's names: its version, and the lanes, triggers, roles, task origins and
pseudo-tasks both sides refer to."""

PROTOCOL_VERSION = 2
"""The protocol's major version, which a ``Hello`` must name to be welcomed."""


WORK_LANE = "work"
PLANNER_LANE = "planner"
"""The lanes' names: task runs in the work lane, the planner's turns in its own."""


SUPER_REVIEW = "super-review"
CONFIGURE_CHECKS = "configure-checks"
"""The built-in triggers' names: a super review now, and a configurator task to set up checks."""


PLANNER = "planner"
CODER = "coder"
REVIEWER = "reviewer"
SUPER_REVIEWER = "super-reviewer"
CONFIGURATOR = "configurator"
"""The built-in roles' names."""

BUILT_IN_ROLE_NAMES = (PLANNER, CODER, REVIEWER, SUPER_REVIEWER, CONFIGURATOR)
"""The built-in roles' names, in the order the TUI's strip shows their tabs."""

REVIEW_ROLES = frozenset({REVIEWER, SUPER_REVIEWER})
"""The roles that review work rather than do it."""


BusyLane = str
"""Where work runs, by name: task runs in ``work``; the planner's turns in ``planner``."""


TaskOrigin = str
"""Who queued a task: ``manual`` (the user), ``planner``, or a review's suggestions,
``review`` or ``super_review``."""

MANUAL_ORIGIN = "manual"
PLANNER_ORIGIN = "planner"
REVIEW_ORIGIN = "review"
SUPER_REVIEW_ORIGIN = "super_review"
"""The task origins' names: the user, the planner, a review and a super review."""


STARTUP_TASK = "startup"
"""The task the startup check's ``AgentStarted`` names; no task on the list has this id."""

PLAN_TASK = "plan"
"""The task the planner's ``AgentStarted`` names; it works on no task of the list."""


CONTROL_PLANE_STREAM = "control_plane"
"""The optional stream of the agents running across the workspaces and what turns have cost,
which ``Watch`` turns on."""

STREAMS = (CONTROL_PLANE_STREAM,)
"""The optional streams' names, which ``Watch`` chooses among."""
