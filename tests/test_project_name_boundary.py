"""#338: every `--project-name` in the CLI tree refuses a blank name.

The defect class is "a blank project name accepted at a CLI boundary".
The project name is an identity: it keys the journal audits, the
decision register and, under `--single-pr`, the branch, and "" is the
one value at which the convergence accounting counted an audit with no
project as BOTH this project's and unattributed.

Round 1 of review found the class half-fixed. `ks decompose` and
`ks factory` had the callback; `ks queue add` did not, because Click
runs a callback over an option's own default and that option's default
is the "" sentinel `serve._derive_project_name` turns into
`queue-<id>`. Gating the refusal on `ctx.get_parameter_source` serves
all three: the untyped default is left alone, an explicit blank is
refused.

The census here is the guard for instance N+1. It walks the Click tree
and counts every parameter it OBTAINS that spells the `--project-name`
flag or binds the `project_name` destination, so a fourth option
appears as an unexplained delta rather than as a site outside a list
someone maintained by hand. Keyed on the flag as well as the
destination because round 2 of review planted
`@click.option("--project-name", "project")`, an idiom `cli.py` uses
eight times, and a census keyed on the destination alone cleared it.
It CLEARS each site, so it clears only on
`callback is _reject_blank_project_name`, an object identity it can
prove; and it pins both the count and the command paths, so a walk
that has gone blind fails red instead of agreeing with an empty
result.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import click
import pytest
from click.core import ParameterSource
from click.testing import CliRunner, Result

from kstrl.cli import _reject_blank_project_name, cli
from kstrl.workqueue import Queue, QueueConfig

#: Every command path in the Click tree that takes a project name.
#: Pinned rather than counted alone so a walk that stops recursing
#: fails on the missing path instead of on a number nobody reads.
#: `cli queue add` is nested one group deep, which is what makes its
#: presence proof that the walk descends.
EXPECTED_PROJECT_NAME_SITES = {
    "cli decompose",
    "cli factory",
    "cli queue add",
}


def _every_parameter(
    command: click.Command,
    path: tuple[str, ...] = (),
) -> Iterator[tuple[str, click.Parameter]]:
    """Yield (command path, parameter) for the whole tree under `command`.

    Enumerates what it obtains - a command, then its `params`, then its
    subcommands - and never decides that some parameter shape is
    uninteresting, so the only way for a site to escape is for the
    command itself to be unreachable from `cli`.

    `list_commands`/`get_command` rather than the `commands` dict:
    those are the methods a lazily loaded group would override, and a
    group that lists a name it cannot resolve raises here rather than
    being skipped.
    """
    here = (*path, command.name or "<anonymous>")
    where = " ".join(here)
    for param in command.params:
        yield where, param
    if isinstance(command, click.Group):
        ctx = click.Context(command)
        for name in command.list_commands(ctx):
            sub = command.get_command(ctx, name)
            assert sub is not None, f"{where} lists {name!r} but cannot resolve it"
            yield from _every_parameter(sub, here)


def _takes_a_project_name(param: click.Parameter) -> bool:
    """The flag an operator types, or the destination the body reads.

    Either alone is a list of the options that happen to use one
    spelling: `"--project-name"` with a renamed destination is what
    round 2 planted, and `project_name` bound from some other flag is
    the reverse. Widening the key can only add sites, and the count is
    a set equality, so over-matching fails red rather than clearing.
    """
    return param.name == "project_name" or "--project-name" in param.opts


def _project_name_sites(root: click.Command = cli) -> dict[str, click.Parameter]:
    sites: dict[str, click.Parameter] = {}
    for where, param in _every_parameter(root):
        if _takes_a_project_name(param):
            assert where not in sites, f"two project-name parameters on {where}"
            sites[where] = param
    return sites


class TestProjectNameCensus:
    def test_the_tree_has_exactly_the_three_known_project_name_options(self) -> None:
        assert set(_project_name_sites()) == EXPECTED_PROJECT_NAME_SITES

    def test_every_one_of_them_carries_the_rejecting_callback(self) -> None:
        sites = _project_name_sites()
        # Stand-alone: an empty walk must not clear this layer either.
        assert sites
        uncovered = {
            where
            for where, param in sites.items()
            if param.callback is not _reject_blank_project_name
        }
        assert uncovered == set()

    def test_the_census_sees_the_flag_under_a_renamed_destination(self) -> None:
        """Round 2 of review planted this shape and the census cleared it.

        `cli.py` binds `"--all"` to `show_all` and `"--state"` to
        `states`; a `--project-name` written the same way has
        `param.name == "project"` and was invisible to a census keyed
        on the destination. Planted on a throwaway group rather than
        on `cli`, so the pinned census above is unaffected.
        """

        @click.group()
        def root() -> None:
            pass

        @root.command()
        @click.option("--project-name", "project")
        def renamed(project: str | None) -> None:
            pass

        @root.command()
        @click.option("--project-name")
        def default_destination(project_name: str | None) -> None:
            pass

        @root.command()
        @click.option("--name", "project_name")
        def other_flag(project_name: str | None) -> None:
            pass

        @root.command()
        @click.option("--project")
        def unrelated(project: str | None) -> None:
            pass

        sites = _project_name_sites(root)
        assert set(sites) == {
            "root renamed",
            "root default-destination",
            "root other-flag",
        }
        assert all(param.callback is None for param in sites.values())


class TestTheCallbackFailsClosed:
    """The two branches no CLI path reaches today, pinned directly.

    No option here declares `envvar=` or `prompt=` and Click never
    hands a declared option a `None` name, so neither branch changes
    what an operator sees. They are pinned because `_use_cli_value`
    sits eight lines above and clears on `ParameterSource.COMMANDLINE`;
    harmonising the two would widen this callback's clearing set from
    one source to four.
    """

    def test_a_blank_from_the_environment_is_refused(self) -> None:
        param = click.Option(["--project-name"])
        ctx = click.Context(click.Command("probe", params=[param]))
        ctx.set_parameter_source("project_name", ParameterSource.ENVIRONMENT)

        with pytest.raises(click.BadParameter):
            _reject_blank_project_name(ctx, param, "   ")

    def test_a_blank_the_default_supplied_is_returned(self) -> None:
        param = click.Option(["--project-name"], default="")
        ctx = click.Context(click.Command("probe", params=[param]))
        ctx.set_parameter_source("project_name", ParameterSource.DEFAULT)

        assert _reject_blank_project_name(ctx, param, "") == ""

    def test_an_unresolvable_parameter_name_is_refused(self) -> None:
        param = click.Option(["--project-name"])
        ctx = click.Context(click.Command("probe", params=[param]))
        param.name = None

        with pytest.raises(click.BadParameter):
            _reject_blank_project_name(ctx, param, "")


def _spec_at(root: Path) -> Path:
    spec = root / "feature.md"
    spec.write_text("# Feature\n\nDo the thing.\n", encoding="utf-8")
    return spec


def _queue_add(root: Path, *extra: str) -> Result:
    return CliRunner().invoke(
        cli,
        ["queue", "add", str(_spec_at(root)), *extra, "--root", str(root), "--no-color"],
    )


class TestQueueAddBlankProjectName:
    """The third boundary: what `serve` later spawns a child factory with."""

    @pytest.mark.parametrize("project_name", ["", "   ", "\t"])
    def test_an_explicit_blank_name_exits_two_and_queues_nothing(
        self, tmp_path: Path, project_name: str
    ) -> None:
        result = _queue_add(tmp_path, "--project-name", project_name)

        assert result.exit_code == 2
        assert "--project-name" in result.output
        # Refused at parse time, so the item never reached the queue and
        # `serve` never spawns a child factory that has to refuse it.
        assert Queue(tmp_path, QueueConfig()).items() == []

    def test_the_absent_flag_still_stores_the_empty_sentinel(self, tmp_path: Path) -> None:
        """The "" default is load-bearing and the source gate protects it.

        `serve` reads `item.project_name or _derive_project_name(item)`,
        so "" is how an item says "name me `queue-<id>`". Click runs the
        callback over that default, which is why the refusal keys on the
        parameter source instead of on the value alone.
        """
        result = _queue_add(tmp_path)

        assert result.exit_code == 0
        items = Queue(tmp_path, QueueConfig()).items()
        assert len(items) == 1
        assert items[0].project_name == ""

    def test_a_name_with_surrounding_space_is_stored_verbatim(self, tmp_path: Path) -> None:
        result = _queue_add(tmp_path, "--project-name", " x ")

        assert result.exit_code == 0
        assert Queue(tmp_path, QueueConfig()).items()[0].project_name == " x "
