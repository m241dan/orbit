"""The protocol's names: its version, and the lanes, triggers, roles, task origins, pause
reasons and pseudo-tasks both sides refer to."""

PROTOCOL_VERSION = 2
"""The protocol's major version, which a ``Hello`` must name to be welcomed."""


WORK_LANE = "work"
PLANNER_LANE = "planner"
"""The lanes' names: task runs in the work lane, the planner's turns in its own."""


SUPER_REVIEW = "super-review"
CONFIGURE_CHECKS = "configure-checks"
"""The built-in triggers' names: a super review now, and a configurator task to set up checks.
The super review's tasks, and those it suggests, take its name as their origin."""


PLANNER = "planner"
CODER = "coder"
REVIEWER = "reviewer"
SUPER_REVIEWER = "super-reviewer"
CONFIGURATOR = "configurator"
EXPERIMENTER = "experimenter"
"""The built-in roles' names."""

BUILT_IN_ROLE_NAMES = (PLANNER, EXPERIMENTER, CODER, REVIEWER, SUPER_REVIEWER, CONFIGURATOR)
"""The built-in roles' names, in the order the TUI's strip shows their tabs."""

REVIEW_ROLES = frozenset({REVIEWER, SUPER_REVIEWER})
"""The roles that review work rather than do it."""


BusyLane = str
"""Where work runs, by name: the planner's turns in ``planner``, and tasks in their roles' lanes,
such as ``work``, where task runs start on the user's asking, and ``review``."""


TaskOrigin = str
"""Who queued a task: ``manual`` (the user), ``planner``, or the trigger that queued it, by
name, such as ``review`` or ``super-review``, as the tasks a review suggests take its trigger's
name too."""

MANUAL_ORIGIN = "manual"
PLANNER_ORIGIN = "planner"
REVIEW_ORIGIN = "review"
"""The task origins' names: the user, the planner and the review trigger; the super review's is
its trigger's, :data:`SUPER_REVIEW`."""

REVIEW_ORIGINS = frozenset({REVIEW_ORIGIN, SUPER_REVIEW})
"""The origins of what orbit's reviews queue: their reviews and the tasks they suggest."""


PauseReason = str
"""Why a session's run waits to start again by itself: ``usage_limit``, Claude's usage limit
stopped it, or ``api_unavailable``, Claude's API not answering did; a peer may send others."""

PAUSED_FOR_USAGE_LIMIT = "usage_limit"
PAUSED_FOR_API_UNAVAILABLE = "api_unavailable"
"""The pause reasons' names: Claude's usage limit and Claude's API not answering."""


STARTUP_TASK = "startup"
"""The task the startup check's ``AgentStarted`` names; no task on the list has this id."""

PLAN_TASK = "plan"
"""The task the planner's ``AgentStarted`` names; it works on no task of the list."""


CONTROL_PLANE_STREAM = "control_plane"
"""The optional stream of the agents running across the workspaces and what turns have cost,
which ``Watch`` turns on."""

STREAMS = (CONTROL_PLANE_STREAM,)
"""The optional streams' names, which ``Watch`` chooses among."""
