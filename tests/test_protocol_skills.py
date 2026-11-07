"""The rule of which skills an agent is given, which the server and its frontends share: its
role's, then its task's, then the mapped ones, each once, where first named."""

from orbit.protocol import given_skills


def test_an_agent_is_given_its_roles_then_its_tasks_then_the_mapped_skills_each_once() -> None:
    given = given_skills(
        ["code-style", "review-bugs"],
        ["python", "application"],
        ["application", "python", "layers", "orbit"],
    )
    assert given == ("code-style", "review-bugs", "python", "application", "layers", "orbit")


def test_a_roles_skill_the_task_or_mapping_also_names_is_given_once_in_the_roles_place() -> None:
    assert given_skills(["python", "security"], ["security"], ["python", "cpp"]) == (
        "python",
        "security",
        "cpp",
    )


def test_without_task_skills_an_agent_is_given_its_roles_then_the_mapped_ones() -> None:
    assert given_skills(["code-style"], [], ["python", "code-style"]) == ("code-style", "python")


def test_without_mapped_skills_an_agent_is_given_its_roles_then_its_tasks() -> None:
    assert given_skills(["code-style"], ["cpp"], []) == ("code-style", "cpp")


def test_nothing_named_gives_nothing() -> None:
    assert given_skills([], [], []) == ()
